#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневный сторож серверных доступов трёх сервисов на VPS.

Зачем. За четверо суток контур трижды встал из-за МЁРТВОГО доступа, и каждый раз
это находил партнёр, а не мы:

  01.09  папка Codex рерайтов осталась без auth.json  → рерайты лежали 2 суток
  02.09  ключ VeoNonStop истёк                        → видео не собиралось ни у кого
  26.08  оба Claude-токена рерайтов потеряли доступ   → 14 аккаунтов стояли 9 дней

Общая причина одна: доступ считается рабочим, пока строка есть в конфиге. Живость
не проверял никто. Скрипт закрывает именно это — дёргает каждый серверный доступ
дешёвым запросом и говорит, что мертво.

Запуск: ``python3 check_credentials.py`` (на VPS). Код возврата 1, если что-то мертво,
поэтому годится и как cron-задача, и как ручная проверка перед разбором инцидента.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

REWRITE_ENV = "/etc/rewrite.env"
GEN_KEYS = "/root/video_studio_api_keys.json"
HTTP_TIMEOUT = 30
CLI_TIMEOUT = 90

OK, DEAD, WARN, SKIP = "ok", "МЁРТВ", "внимание", "не проверялось"


def _mask(value: str) -> str:
    value = str(value or "")
    return (value[:12] + "…") if len(value) > 12 else (value or "—")


def _read_env_file(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _http(method: str, url: str, headers: Dict[str, str]) -> Tuple[int, str]:
    """Без requests: скрипт обязан работать даже там, где зависимостей нет."""
    import urllib.error
    import urllib.request

    # Cloudflare у части провайдеров отбивает дефолтный Python-urllib (error 1010,
    # HTTP 403) — с обычным UA тот же запрос проходит. Без этого сторож кричал бы
    # «МЁРТВ» на живом ключе, а сторож, который врёт, хуже отсутствующего.
    headers = {"User-Agent": "curl/8.5.0", "Accept": "*/*", **headers}
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, resp.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4000).decode("utf-8", "replace")
    except Exception as exc:                                    # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def _jwt_expiry(token: str) -> Optional[float]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:                                           # noqa: BLE001
        return None


# ── проверки ────────────────────────────────────────────────────────────────

def check_claude_token(label: str, token: str) -> dict:
    env = dict(os.environ)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        run = subprocess.run(
            ["claude", "-p", "ping"], env=env, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=CLI_TIMEOUT,
        )
    except Exception as exc:                                    # noqa: BLE001
        return {"name": label, "state": DEAD, "detail": f"зонд не ответил: {exc}"}
    if run.returncode == 0:
        return {"name": label, "state": OK, "detail": _mask(token)}
    # Причина у claude уходит в stdout, а не в stderr — из-за этого её и не видели.
    reason = " ".join(((run.stdout or "") + " " + (run.stderr or "")).split())[:160]
    return {"name": label, "state": DEAD, "detail": reason or f"exit {run.returncode}"}


def check_codex_home(label: str, home: str) -> dict:
    auth = os.path.join(home, "auth.json")
    if not os.path.isfile(auth):
        aside = [n for n in sorted(os.listdir(home))[:20]
                 if n.startswith("auth.json.")] if os.path.isdir(home) else []
        hint = f"; рядом лежат {', '.join(aside[:3])}" if aside else ""
        return {"name": label, "state": DEAD,
                "detail": f"нет auth.json в {home}{hint}"}
    try:
        with open(auth, encoding="utf-8") as fh:
            payload = json.load(fh)
        access = (payload.get("tokens") or {}).get("access_token") or ""
    except Exception as exc:                                    # noqa: BLE001
        return {"name": label, "state": DEAD, "detail": f"auth.json не читается: {exc}"}
    exp = _jwt_expiry(access)
    left_days = None if exp is None else (exp - time.time()) / 86400.0
    env = dict(os.environ)
    env["CODEX_HOME"] = home
    try:
        run = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "ping"],
            env=env, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=CLI_TIMEOUT,
        )
        alive = run.returncode == 0
        reason = " ".join(((run.stderr or "") + " " + (run.stdout or "")).split())[-160:]
    except Exception as exc:                                    # noqa: BLE001
        alive, reason = False, f"{type(exc).__name__}: {exc}"
    if not alive:
        return {"name": label, "state": DEAD, "detail": reason or "codex exec упал"}
    if left_days is not None and left_days < 2:
        return {"name": label, "state": WARN,
                "detail": f"работает, но access_token истекает через {left_days:.1f} дн"}
    tail = "" if left_days is None else f", access_token ещё {left_days:.1f} дн"
    return {"name": label, "state": OK, "detail": f"{home}{tail}"}


def check_veo(key: str) -> dict:
    code, body = _http("GET", "https://veononstop.org/api/v1/user/balance",
                       {"X-API-Key": key})
    if code == 200 and '"success":true' in body.replace(" ", ""):
        return {"name": "veo (VeoNonStop)", "state": OK, "detail": _mask(key)}
    return {"name": "veo (VeoNonStop)", "state": DEAD, "detail": body.strip()[:160] or f"HTTP {code}"}


def check_googler(label: str, key: str) -> dict:
    # Именно /v6/usage: он отвечает 401 на мусорный ключ. /v6/models и /v2/usage
    # проверены и НЕ годятся — первый отдаёт 200 вообще без ключа, второй 404.
    code, body = _http("GET", "https://googler.fast-gen.ai/api/v6/usage",
                       {"X-API-Key": key})
    if code == 200:
        return {"name": label, "state": OK, "detail": _mask(key)}
    return {"name": label, "state": DEAD, "detail": body.strip()[:160] or f"HTTP {code}"}


def check_voicegencore(key: str) -> dict:
    code, body = _http("GET", "https://api.qw1voicegencore.pro/api/v1/client/me",
                       {"X-API-Key": key})
    if code != 200:
        return {"name": "voicegencore", "state": DEAD,
                "detail": body.strip()[:160] or f"HTTP {code}"}
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {"name": "voicegencore", "state": WARN, "detail": "ответ не JSON"}
    if not data.get("has_active_subscription", True):
        return {"name": "voicegencore", "state": DEAD, "detail": "подписка неактивна"}
    expires = str(data.get("subscription_expires_at") or "")[:10]
    return {"name": "voicegencore", "state": OK,
            "detail": f"план {data.get('current_plan')}, до {expires}"}


def check_lumean(key: str) -> dict:
    code, body = _http("GET", "https://api.lumean.app/api/public/orders?limit=1",
                       {"X-API-KEY": key})
    if code == 200:
        return {"name": "lumean", "state": OK, "detail": _mask(key)}
    return {"name": "lumean", "state": DEAD, "detail": body.strip()[:160] or f"HTTP {code}"}


# ── сбор ────────────────────────────────────────────────────────────────────

def collect() -> List[dict]:
    rows: List[dict] = []
    env = _read_env_file(REWRITE_ENV)
    keys = _read_json(GEN_KEYS)

    tokens = [t.strip() for t in re.split(r"[,\n]", env.get("REWRITE_CLAUDE_OAUTH_TOKENS", ""))
              if t.strip()]
    if tokens:
        for index, token in enumerate(tokens, 1):
            rows.append(check_claude_token(f"рерайты · claude-токен {index}", token))
    else:
        rows.append({"name": "рерайты · claude-пул", "state": WARN,
                     "detail": "пуст — партнёрам канал недоступен"})

    homes = [("рерайты · codex-home", env.get("REWRITE_CODEX_HOME", "").strip()),
             ("кабинет · codex-home", str(keys.get("codex_home") or "").strip())]
    probed: Dict[str, dict] = {}
    for label, home in homes:
        if not home:
            continue
        # Оба сервиса сейчас смотрят в один каталог — проверяем его один раз,
        # но строку показываем на каждый сервис: иначе пропажа у одного не видна.
        if home not in probed:
            probed[home] = check_codex_home(label, home)
        result = dict(probed[home])
        result["name"] = label
        rows.append(result)

    pool = keys.get("claude_oauth_pool") or []
    if isinstance(pool, str):
        pool = [pool]
    for index, token in enumerate(pool, 1):
        rows.append(check_claude_token(f"кабинет · claude-токен {index}", token))
    if not pool:
        rows.append({"name": "кабинет · claude-пул", "state": SKIP,
                     "detail": "пуст (промпты намеренно только на Codex с 02.09)"})

    if keys.get("veo"):
        rows.append(check_veo(str(keys["veo"])))
    googler_keys = [str(keys.get("googler") or "")] + [
        str(k) for k in (keys.get("googler_pool") or []) if k
    ]
    for index, key in enumerate([k for k in googler_keys if k], 1):
        rows.append(check_googler(f"googler/FastGen {index}", key))
    if keys.get("voicegencore"):
        rows.append(check_voicegencore(str(keys["voicegencore"])))
    if keys.get("lumean"):
        rows.append(check_lumean(str(keys["lumean"])))
    return rows


def main() -> int:
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = collect()
    dead = [r for r in rows if r["state"] == DEAD]
    warn = [r for r in rows if r["state"] == WARN]

    width = max((len(r["name"]) for r in rows), default=20)
    lines = [f"Проверка серверных доступов · {started}", ""]
    for row in rows:
        lines.append(f"  {row['name']:<{width}}  {row['state']:<12} {row['detail']}")
    lines.append("")
    if dead:
        lines.append(f"МЁРТВЫХ ДОСТУПОВ: {len(dead)} — " + "; ".join(r["name"] for r in dead))
    elif warn:
        lines.append(f"Всё живо, но есть предупреждения: {len(warn)}")
    else:
        lines.append("Все доступы живы.")
    report = "\n".join(lines)
    print(report)

    out_dir = os.environ.get("CRED_REPORT_DIR", "/root/ops")
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "credentials_report.txt"), "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        with open(os.path.join(out_dir, "credentials_report.json"), "w", encoding="utf-8") as fh:
            json.dump({"checked_at": started, "rows": rows}, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"(отчёт не сохранён: {exc})", file=sys.stderr)

    if dead:
        # В журнал systemd — туда смотрят при разборе инцидента, и там это увидят
        # раньше, чем пожалуется партнёр.
        try:
            subprocess.run(["systemd-cat", "-t", "cred-check", "-p", "err"],
                           input="МЁРТВЫЕ ДОСТУПЫ: " + "; ".join(
                               f"{r['name']} — {r['detail'][:80]}" for r in dead),
                           text=True, timeout=10)
        except Exception:                                       # noqa: BLE001
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
