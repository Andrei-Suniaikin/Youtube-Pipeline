# -*- coding: utf-8 -*-
"""Серверные подписки для текста: Codex (ChatGPT) и Claude — без SSH и перезапуска.

Зачем (22.09.2026). До сих пор сменить подписку, которой рерайт пишет текст, можно было
только руками на сервере: `codex login` в каталог, правка `/etc/rewrite.env`
(`REWRITE_CODEX_HOMES` / `REWRITE_CLAUDE_OAUTH_TOKENS`) и `systemctl restart`. Заказчику
это недоступно, а разлогиненная подписка молча роняла каждую задачу текста.

Теперь администратор добавляет подписку на экране «Ключи». Здесь она и живёт:

    <каталог>/index.json              что есть и в каком состоянии — БЕЗ секретов
    <каталог>/codex/<id>/auth.json    вход Codex, 0600 (каталог 0700)
    <каталог>/claude/<id>.token       токен `claude setup-token`, 0600

Каталог — рядом с `sessions.db` рерайта (`.text-subscriptions`), переопределяется
только серверным ENV `REWRITE_SUBSCRIPTIONS_DIR`. Пул раздачи читает список при каждой
выдаче (`rewrite_core.get_server_codex_homes` / `get_server_claude_oauth_tokens`), так что
добавленная подписка начинает работать со следующей задачи, без перезапуска.

Значение секрета наружу не отдаётся никогда: ни в ответ, ни в лог, ни в тревогу. Видно
только имя, хвост и состояние. Подписки из ENV остаются как были — рядом, только чтение.

Состояние каждой подписки пишет сам рерайт по факту вызова (`note`):
  ok      — последний вызов прошёл;
  limit   — окно подписки исчерпано: у Claude спит до `until` (раздача пропускает её
            сама), Codex время сброса не называет — держится до первого успеха;
  dead    — вход протух: два отказа авторизации подряд. Такая подписка из раздачи
            ВЫНИМАЕТСЯ — иначе каждая следующая задача текста падала бы на ней же —
            и остаётся вынутой, пока её не заменят или не вернут вручную.
Состояние лежит в файле, а не в памяти: его читают и пульт, и отдельный процесс тревог.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import stat
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

try:
    import fcntl
except ImportError:                                   # pragma: no cover — не POSIX
    fcntl = None

PROVIDERS = ("codex", "claude")
# Отказ авторизации бывает и сбоем провайдера. Одиночный 401 — не повод вынимать
# подписку из раздачи: вынутая сама не вернётся.
DEAD_AFTER = 2
# «Последний успех» освежаем не на каждом вызове — иначе каждая задача текста писала бы
# файл. Смена состояния пишется сразу.
_OK_REFRESH_SEC = 600
_ID_RE = re.compile(r"^[a-z0-9]{8,32}$")
_LOCK = threading.RLock()


class SubscriptionError(ValueError):
    """Отказ, который можно показать человеку как есть: без секрета внутри."""


# ─────────────────────────────── где лежит ───────────────────────────────

def root() -> str:
    configured = (os.environ.get("REWRITE_SUBSCRIPTIONS_DIR") or "").strip()
    if configured:
        return configured
    db = os.environ.get("SESSION_DB") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "sessions.db")
    return os.path.join(os.path.dirname(os.path.abspath(db)), ".text-subscriptions")


def _private_dir(path: str) -> None:
    """Каталог наш, настоящий (не симлинк) и закрытый: 0700."""
    try:
        os.makedirs(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"каталог подписок подменён: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError(f"каталог подписок чужой: {path}")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)


def _write_private(path: str, text: str) -> None:
    """Атомарно и сразу 0600: секрет ни мгновения не лежит открытым."""
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def _locked():
    """Один писатель на каталог — и между потоками, и между процессами."""
    with _LOCK:
        base = root()
        _private_dir(base)
        fh = open(os.path.join(base, ".lock"), "a")
        try:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_EX)
            yield base
        finally:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


def _load(base: Optional[str] = None) -> List[Dict[str, Any]]:
    path = os.path.join(base or root(), "index.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return [i for i in (items or []) if isinstance(i, dict)
            and i.get("provider") in PROVIDERS and _ID_RE.match(str(i.get("id") or ""))]


def _save(base: str, items: List[Dict[str, Any]]) -> None:
    _write_private(os.path.join(base, "index.json"),
                   json.dumps({"items": items}, ensure_ascii=False, indent=1))


def _codex_home(base: str, sid: str) -> str:
    return os.path.join(base, "codex", sid)


def _claude_file(base: str, sid: str) -> str:
    return os.path.join(base, "claude", f"{sid}.token")


def _read_claude(base: str, sid: str) -> str:
    try:
        with open(_claude_file(base, sid), encoding="utf-8") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return ""


# ─────────────────────────────── проверка входа ───────────────────────────────

def validate_codex_auth(payload: Any) -> None:
    """Та же проверка, что `rewrite_core.validate_codex_auth_document` (тест держит их
    в паре). Своя копия — чтобы процессу тревог не тянуть 10 000 строк рерайта."""
    if not isinstance(payload, dict):
        raise SubscriptionError("auth.json должен быть JSON-объектом")
    if payload.get("auth_mode") != "chatgpt":
        raise SubscriptionError(
            "это не вход по подписке ChatGPT: в auth.json нет auth_mode=chatgpt. "
            "Войдите командой `codex login` (без ключа API) и вставьте файл заново")
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        raise SubscriptionError("в auth.json нет блока tokens")
    for field in ("access_token", "refresh_token"):
        if not isinstance(tokens.get(field), str) or not tokens[field].strip():
            raise SubscriptionError(f"в auth.json нет tokens.{field}")


def _parse_codex(text: str) -> Dict[str, Any]:
    try:
        payload = json.loads(text or "")
    except ValueError:
        raise SubscriptionError(
            "это не JSON. Нужно содержимое файла ~/.codex/auth.json целиком")
    validate_codex_auth(payload)
    return payload


def _jwt_email(token: str) -> str:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part.encode()))
    except Exception:                                  # noqa: BLE001
        return ""
    email = claims.get("email") if isinstance(claims, dict) else ""
    return email if isinstance(email, str) else ""


def _mask_email(email: str) -> str:
    if "@" not in email:
        return ""
    name, host = email.split("@", 1)
    return f"{name[:2]}…@{host}"


def _codex_account(payload: Dict[str, Any]) -> str:
    tokens = payload.get("tokens") or {}
    return str(tokens.get("account_id") or "")


def _codex_tail(payload: Dict[str, Any]) -> str:
    tokens = payload.get("tokens") or {}
    email = _mask_email(_jwt_email(str(tokens.get("id_token") or "")))
    if email:
        return email
    account = _codex_account(payload)
    return f"…{account[-4:]}" if account else "вход ChatGPT"


def normalize_claude_token(text: str) -> str:
    token = "".join(str(text or "").split())
    if token.lower().startswith("bearer"):
        token = token[6:]
    if not token:
        raise SubscriptionError("токен пустой")
    if token.startswith("sk-ant-api"):
        raise SubscriptionError(
            "это ключ API Anthropic, а не подписка: он платит за каждое слово. "
            "Нужен токен подписки из `claude setup-token` (начинается с sk-ant-oat)")
    if not token.startswith("sk-ant-oat") or len(token) < 40:
        raise SubscriptionError(
            "не похоже на токен подписки Claude. Его печатает команда "
            "`claude setup-token`, он начинается с sk-ant-oat")
    return token


# ─────────────────────────────── чтение для раздачи ───────────────────────────────

def _in_rotation(item: Dict[str, Any]) -> bool:
    return item.get("state") != "dead" and not item.get("paused")


def codex_homes() -> List[str]:
    """Каталоги CODEX_HOME подписок, которые можно раздавать. Путь — не секрет."""
    base = root()
    out = []
    for item in _load(base):
        if item["provider"] == "codex" and _in_rotation(item):
            home = _codex_home(base, item["id"])
            if os.path.isfile(os.path.join(home, "auth.json")):
                out.append(home)
    return out


def claude_tokens() -> List[str]:
    base = root()
    out = []
    for item in _load(base):
        if item["provider"] == "claude" and _in_rotation(item):
            token = _read_claude(base, item["id"])
            if token:
                out.append(token)
    return out


# ─────────────────────────────── изменение ───────────────────────────────

def _new_id() -> str:
    return secrets.token_hex(6)


def _find_by_value(base, items, provider, value) -> Optional[Dict[str, Any]]:
    for item in items:
        if item["provider"] != provider:
            continue
        if provider == "codex":
            if os.path.abspath(_codex_home(base, item["id"])) == os.path.abspath(value):
                return item
        elif _read_claude(base, item["id"]) == value:
            return item
    return None


def _duplicate(base, items, provider, secret_key, skip_id="") -> Optional[Dict[str, Any]]:
    """Та же подписка уже добавлена? Для Codex — тот же аккаунт ChatGPT, для Claude —
    тот же токен. Две записи одной подписки делили бы одно окно и врали про ёмкость."""
    for item in items:
        if item["provider"] != provider or item["id"] == skip_id:
            continue
        if provider == "claude":
            if _read_claude(base, item["id"]) == secret_key:
                return item
        else:
            try:
                with open(os.path.join(_codex_home(base, item["id"]), "auth.json"),
                          encoding="utf-8") as fh:
                    other = json.load(fh)
            except (OSError, ValueError):
                continue
            if secret_key and _codex_account(other) == secret_key:
                return item
    return None


def _label(label: str, provider: str, items) -> str:
    label = " ".join(str(label or "").split())[:60]
    if label:
        return label
    n = 1 + sum(1 for i in items if i["provider"] == provider)
    return f"{'Codex' if provider == 'codex' else 'Claude'} {n}"


def add(provider: str, secret: str, label: str = "", who: str = "") -> Dict[str, Any]:
    provider = (provider or "").strip().lower()
    if provider not in PROVIDERS:
        raise SubscriptionError("подписка бывает Codex или Claude")
    if provider == "codex":
        payload = _parse_codex(secret)
        key = _codex_account(payload)
    else:
        token = normalize_claude_token(secret)
        key = token
    with _locked() as base:
        items = _load(base)
        dup = _duplicate(base, items, provider, key)
        if dup:
            raise SubscriptionError(
                f"эта подписка уже добавлена как «{dup.get('label')}» — "
                "чтобы обновить вход, нажмите у неё «Заменить»")
        sid = _new_id()
        _store_secret(base, provider, sid, payload if provider == "codex" else token)
        now = time.time()
        item = {"id": sid, "provider": provider, "label": _label(label, provider, items),
                "tail": _codex_tail(payload) if provider == "codex" else f"…{token[-4:]}",
                "added": now, "added_by": who, "state": "new", "since": now}
        items.append(item)
        _save(base, items)
        return public(item)


def _store_secret(base, provider, sid, value) -> None:
    if provider == "codex":
        _private_dir(os.path.join(base, "codex"))
        home = _codex_home(base, sid)
        _private_dir(home)
        _write_private(os.path.join(home, "auth.json"), json.dumps(value, indent=2))
    else:
        _private_dir(os.path.join(base, "claude"))
        _write_private(_claude_file(base, sid), value)


def replace(sid: str, secret: str, who: str = "") -> Dict[str, Any]:
    """Новый вход в ту же запись: имя остаётся, состояние начинается заново.

    Для Codex перезаписывается auth.json в ТОМ ЖЕ каталоге — путь в пуле не меняется,
    и задача, которая прямо сейчас держит эту подписку, допишет в него уже новый вход.
    """
    with _locked() as base:
        items = _load(base)
        item = next((i for i in items if i["id"] == sid), None)
        if item is None:
            raise KeyError(sid)
        if item["provider"] == "codex":
            payload = _parse_codex(secret)
            dup = _duplicate(base, items, "codex", _codex_account(payload), skip_id=sid)
            value, tail = payload, _codex_tail(payload)
        else:
            token = normalize_claude_token(secret)
            dup = _duplicate(base, items, "claude", token, skip_id=sid)
            value, tail = token, f"…{token[-4:]}"
        if dup:
            raise SubscriptionError(
                f"эта подписка уже добавлена как «{dup.get('label')}»")
        _store_secret(base, item["provider"], sid, value)
        now = time.time()
        for k in ("until", "fails", "note", "last_ok", "paused"):
            item.pop(k, None)
        item.update({"tail": tail, "state": "new", "since": now,
                     "replaced": now, "replaced_by": who})
        _save(base, items)
        return public(item)


def rename(sid: str, label: str) -> Dict[str, Any]:
    with _locked() as base:
        items = _load(base)
        item = next((i for i in items if i["id"] == sid), None)
        if item is None:
            raise KeyError(sid)
        item["label"] = _label(label, item["provider"],
                               [i for i in items if i["id"] != sid])
        _save(base, items)
        return public(item)


def set_paused(sid: str, paused: bool) -> Dict[str, Any]:
    """Вынуть из раздачи и вернуть. Возврат снимает и «вход протух»: человек
    говорит «проверил, работает» — следующий вызов покажет, прав ли он."""
    with _locked() as base:
        items = _load(base)
        item = next((i for i in items if i["id"] == sid), None)
        if item is None:
            raise KeyError(sid)
        if paused:
            item["paused"] = True
        else:
            item.pop("paused", None)
            if item.get("state") == "dead":
                item.update({"state": "new", "since": time.time()})
                item.pop("fails", None)
        _save(base, items)
        return public(item)


def remove(sid: str) -> None:
    with _locked() as base:
        items = _load(base)
        item = next((i for i in items if i["id"] == sid), None)
        if item is None:
            raise KeyError(sid)
        items = [i for i in items if i["id"] != sid]
        _save(base, items)
        if item["provider"] == "codex":
            shutil.rmtree(_codex_home(base, sid), ignore_errors=True)
        else:
            try:
                os.unlink(_claude_file(base, sid))
            except FileNotFoundError:
                pass


# ─────────────────────────────── состояние по факту вызова ───────────────────────────────

def note(provider: str, value: str, outcome: str, until: float = 0.0,
         detail: str = "") -> None:
    """Рерайт сообщает, чем кончился вызов этой подпиской.

    `value` — то, что выдал пул (каталог Codex или токен Claude). Не наша подписка
    (из ENV или своя у пользователя) — тихо ничего. Сбой записи не должен ронять
    задачу текста, поэтому любые ошибки здесь глотаются.
    """
    if provider not in PROVIDERS or not value or outcome not in ("ok", "limit", "auth"):
        return
    try:
        base = root()
        if not os.path.isdir(base):
            return
        if _find_by_value(base, _load(base), provider, value) is None:
            return
        with _locked() as base:
            items = _load(base)
            item = _find_by_value(base, items, provider, value)
            if item is None:
                return
            now = time.time()
            changed = False
            if outcome == "ok":
                if item.get("state") != "ok":
                    item.update({"state": "ok", "since": now})
                    changed = True
                for stale in ("fails", "until", "note"):
                    if item.pop(stale, None):
                        changed = True
                if now - float(item.get("last_ok") or 0) > _OK_REFRESH_SEC:
                    changed = True
                item["last_ok"] = now
            elif outcome == "limit":
                # Codex время сброса не называет — тогда `until` пустой, и «упёрлась
                # в лимит» держится до первого успешного вызова, а не до выдуманного часа.
                until = float(until or 0) or None
                if item.get("state") != "limit" or item.get("until") != until:
                    if item.get("state") != "limit":
                        item["since"] = now
                    item.update({"state": "limit", "until": until})
                    changed = True
            else:
                fails = int(item.get("fails") or 0) + 1
                item["fails"] = fails
                item["note"] = _clean_detail(detail)
                if fails >= DEAD_AFTER and item.get("state") != "dead":
                    item.update({"state": "dead", "since": now})
                changed = True
            if changed:
                _save(base, items)
    except Exception:                                  # noqa: BLE001
        return


def _clean_detail(detail: str) -> str:
    """Причина отказа для человека. Без секретов: хвосты похожего на токен режем."""
    text = " ".join(str(detail or "").split())[:200]
    return re.sub(r"(sk-ant-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]+", r"\1…", text)


# ─────────────────────────────── наружу ───────────────────────────────

def state_of(item: Dict[str, Any], now: Optional[float] = None) -> str:
    """new | ok | limit | dead | paused — `limit` с прошедшим сроком снова рабочая."""
    now = now or time.time()
    if item.get("paused"):
        return "paused"
    state = item.get("state") or "new"
    if state == "limit" and item.get("until") and float(item["until"]) <= now:
        return "ok" if item.get("last_ok") else "new"
    return state


def public(item: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """Всё, что можно показать: ни значения, ни пути к нему."""
    return {
        "id": item["id"], "provider": item["provider"], "label": item.get("label") or "",
        "tail": item.get("tail") or "", "state": state_of(item, now),
        "since": item.get("since"), "until": item.get("until"),
        "last_ok": item.get("last_ok"), "added": item.get("added"),
        "fails": int(item.get("fails") or 0), "note": item.get("note") or "",
    }


def listing(now: Optional[float] = None) -> List[Dict[str, Any]]:
    return [public(i, now) for i in _load()]
