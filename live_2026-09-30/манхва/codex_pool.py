# -*- coding: utf-8 -*-
"""Codex: пул учёток ChatGPT (CODEX_HOME) — по образцу пула подписок Claude (claude_pool).

MANGA_CODEX_HOMES — несколько папок CODEX_HOME, в каждой свой вход в Codex
(auth.json от «codex login»). Не задано — пула нет, вызов идёт в один CODEX_HOME,
как в выпуске 4 (b2fcab9).

Форматы настройки:
  /root/.codex-manga,/root/.codex-manga-2            — имена «codex-manga», «codex-manga-2»
  {"manga": "/root/.codex-manga", "manga2": {"path": "/root/.codex-manga-2", "limit": 6}}
  [{"name": "manga", "path": "/root/.codex-manga", "limit": 4}]
limit — сколько вызовов codex одновременно на эту учётку (общий предел на процесс),
по умолчанию MANGA_CODEX_PER_HOME (4).

Перед вызовом берётся учётка с наибольшим запасом по окнам 5 ч и неделя. Запас
читается из GET https://chatgpt.com/backend-api/wham/usage (бесплатно, тот же
запрос, что у кабинета) не чаще раза в MANGA_CODEX_PROBE_MINUTES минут на учётку.
Окна распознаются по длине (limit_window_seconds), а не по месту: у Pro при
исчерпанной неделе primary_window — это неделя, а secondary_window — null
(живой замер 19.09.2026). Учётка с окном ≥ MANGA_CODEX_STOP_AT не берётся до
сброса; исчерпаны все — шаг ждёт ближайшего сброса. Вход слетел — учётка
откладывается, в журнал и на страницу идёт строка «перелогиньте: команда …».

Токены из auth.json читаются только для запроса wham/usage и в журнал, ошибки
и на страницу не попадают (scrub).
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import logging
import os
import re
import shlex
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import claude_pool as CP

log = logging.getLogger("manga.codex")

USAGE_URL = os.environ.get("MANGA_CODEX_USAGE_URL", "https://chatgpt.com/backend-api/wham/usage")
LOGIN_HOST = os.environ.get("MANGA_CODEX_LOGIN_HOST", "root@13.140.129.66")
PROBE_MINUTES = CP._number("MANGA_CODEX_PROBE_MINUTES", 10, 1, 1440)
STOP_AT = CP._number("MANGA_CODEX_STOP_AT", 0.95, 0.1, 1.0)
PER_HOME = int(CP._number("MANGA_CODEX_PER_HOME", 4, 1, 128))
# Замок обновления входа (auth.json): codex CLI сам обновляет токен, и два процесса,
# обновившие его одновременно, выбивают вход (refresh-токен одноразовый). Замок берётся
# на весь вызов только когда обновление возможно — за MANGA_CODEX_REFRESH_MARGIN_H часов
# до истечения доступа или 8-дневного срока от last_refresh. Запас — не меньше самого долгого
# вызова (таймаут 10 мин), чтобы вызов, начатый без замка, не застал обновление; больше не нужно:
# пока замок висит, вызовы учётки идут по одному. 0 — замок на каждый вызов.
REFRESH_MARGIN_H = CP._number("MANGA_CODEX_REFRESH_MARGIN_H", 1, 0, 240)
REFRESH_EVERY_DAYS = 8          # codex CLI обновляет вход, когда last_refresh старше 8 суток
LOCK_NAME = ".auth.json.lifecycle.lock"   # то же имя, что у рерайта: общий home не обновят вдвоём
# Разовый 401/403 от wham/usage без JSON-ответа про вход (заглушка Cloudflare, сбой) — ещё не «вход
# слетел»: запас считается неизвестным, вход перепроверяется через столько секунд. Слетевшим он
# считается после второго отказа подряд (или 401/403 от самого CLI + отказ wham).
AUTH_RECHECK_S = CP._number("MANGA_CODEX_AUTH_RECHECK_S", 60, 5, 3600)
SLOT_WAIT_SAY_S = 20            # через сколько секунд ожидания свободного вызова показать строку шага
_AUTH_BODY = re.compile(r"auth|token|log ?in|sign ?in|unauthori|expired|revoked|invalid|credential|session", re.I)


class AllBlocked(RuntimeError):
    """Все учётки исчерпаны до сброса окна, а вызывающий попросил не ждать (acquire(wait_reset=False))."""


class LoginsGone(RuntimeError):
    """Ни у одной учётки нет рабочего входа — ждать нечего, шаг падает с понятной строкой."""


def relogin_line(home: "Home") -> str:
    return (f"вход в Codex «{home.name}» слетел — перелогиньте: "
            f"ssh -t {LOGIN_HOST} \"CODEX_HOME={home.path} codex login --device-auth\"")


@dataclass
class Home:
    name: str
    path: str
    limit: int = PER_HOME
    # Учётка может жить на ДРУГОЙ машине: вызовы для неё идут туда по ssh. Так делается,
    # когда процессор VPS кончился, а рядом есть простаивающий компьютер (20.09: Минск).
    ssh: str = ""            # «vova@127.0.0.1» — пусто, значит вызов местный
    port: int = 0
    key: str = ""            # свой ключ ssh, если нужен
    bin: str = "codex"       # путь к codex на той машине
    cores: int = 0           # сколько ядер у той машины (0 — спросим у неё)
    env: Dict[str, str] = field(default_factory=dict)   # переменные для вызова там (напр. прокси)
    windows: Dict[str, dict] = field(default_factory=dict)   # «5h»/«7d» → {util, reset}
    plan: str = ""
    checked: Optional[float] = None
    blocked_until: float = 0.0
    blocked_why: str = ""
    inflight: int = 0
    rejected: bool = False
    auth_fails: int = 0          # отказов входа подряд (wham/usage без ясного «вход не принят», CLI 401/403)
    secrets: List[str] = field(default_factory=list, repr=False)

    def headroom(self) -> float:
        utils = [w["util"] for w in self.windows.values() if isinstance(w.get("util"), float)]
        return 1.0 - max(utils) if utils else 0.0


def _default_name(path: str) -> str:
    return os.path.basename(path.rstrip("/")).lstrip(".") or "codex"


def parse_homes(raw: str, per_home: int = PER_HOME) -> List[Home]:
    raw = (raw or "").strip()
    if not raw:
        return []
    items = []   # (имя, путь, предел)
    if raw[0] in "{[":
        try:
            data = json.loads(raw)
        except ValueError:
            raise ValueError("Неверный MANGA_CODEX_HOMES: JSON не разобрался")
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict):
                    items.append((str(k), v.get("path"), v.get("limit"), v))
                else:
                    items.append((str(k), v, None, {}))
        elif isinstance(data, list):
            for v in data:
                if isinstance(v, dict):
                    items.append((v.get("name"), v.get("path"), v.get("limit"), v))
                else:
                    items.append((None, v, None, {}))
        else:
            raise ValueError("Неверный MANGA_CODEX_HOMES: нужен список или словарь")
    else:
        items = [(None, p, None, {}) for p in re.split(r"[\s,;]+", raw) if p]
    homes, names, paths = [], set(), set()
    for i, (name, path, limit, extra) in enumerate(items, 1):
        if not isinstance(path, str) or not path.strip().startswith("/"):
            raise ValueError(f"Неверный MANGA_CODEX_HOMES: у записи №{i} нет полного пути к папке CODEX_HOME")
        path = path.strip().rstrip("/") or "/"
        name = str(name or _default_name(path)).strip()[:60]
        if limit is None:
            limit = per_home
        # До 128: у учётки на быстрой машине (Минск, 20 потоков) 42 вызова стоят ~3% её процессора.
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 128:
            raise ValueError(f"Неверный MANGA_CODEX_HOMES: предел у «{name}» — целое число 1–128")
        if name in names or path in paths:
            raise ValueError("Неверный MANGA_CODEX_HOMES: имена и папки не должны повторяться")
        names.add(name)
        paths.add(path)
        ssh = str(extra.get("ssh") or "").strip()
        port = extra.get("port") or 0
        cores = extra.get("cores") or 0
        if ssh and not re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9._:-]+", ssh):
            raise ValueError(f"Неверный MANGA_CODEX_HOMES: ssh у «{name}» — вида пользователь@адрес")
        for label, value in (("port", port), ("cores", cores)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Неверный MANGA_CODEX_HOMES: {label} у «{name}» — целое число")
        env = extra.get("env") or {}
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            raise ValueError(f"Неверный MANGA_CODEX_HOMES: env у «{name}» — словарь строк")
        homes.append(Home(name, path, limit, env={str(k): str(v) for k, v in env.items()}, ssh=ssh, port=int(port),
                          key=str(extra.get("key") or "").strip(),
                          bin=str(extra.get("bin") or "codex").strip() or "codex", cores=int(cores)))
    return homes


# ------------------------------------------------------------------ вход и окна

def read_auth_of(home) -> Optional[dict]:
    """Пропуск учётки. У учётки на ДРУГОЙ машине файл лежит там — читаем его по ssh."""
    if not getattr(home, "ssh", ""):
        return read_auth(home.path)
    import subprocess
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new"]
    if getattr(home, "port", 0):
        cmd += ["-p", str(home.port)]
    if getattr(home, "key", ""):
        cmd += ["-i", home.key]
    cmd += [home.ssh, f"cat {shlex.quote(os.path.join(home.path, 'auth.json'))}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return data if isinstance(data, dict) else None


def read_auth(path: str) -> Optional[dict]:
    try:
        with open(os.path.join(path, "auth.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def auth_secrets(auth: Optional[dict]) -> List[str]:
    t = (auth or {}).get("tokens") or {}
    out = [t.get(k) for k in ("access_token", "refresh_token", "id_token", "account_id")]
    out.append((auth or {}).get("OPENAI_API_KEY"))
    return [s for s in out if isinstance(s, str) and len(s) >= 8]


def _jwt_exp(token: str) -> Optional[float]:
    try:
        part = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        exp = data.get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except Exception:  # noqa: BLE001 — не JWT: срок неизвестен
        return None


def _iso(ts) -> Optional[float]:
    if not isinstance(ts, str) or not ts:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", ts.strip()).replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def refresh_possible(path: str, now: Optional[float] = None, margin_h: Optional[float] = None) -> bool:
    """Может ли codex CLI во время вызова обновить вход (переписать auth.json)."""
    margin_h = REFRESH_MARGIN_H if margin_h is None else margin_h
    if margin_h <= 0:
        return True
    now = time.time() if now is None else now
    auth = read_auth(path)
    if auth is None:
        return False            # входа нет — обновлять нечего, CLI сразу откажет
    margin = margin_h * 3600
    exp = _jwt_exp(((auth.get("tokens") or {}).get("access_token")) or "")
    last = _iso(auth.get("last_refresh"))
    if exp is None or last is None:
        return True
    return exp - now < margin or now - last > REFRESH_EVERY_DAYS * 86400 - margin


def _label(seconds) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "окно"
    if seconds <= 6 * 3600:
        return "5h"
    if seconds >= 6 * 86400:
        return "7d"
    return f"{int(seconds // 3600)}h"


def windows_from_usage(body: Optional[dict], now: float) -> Dict[str, dict]:
    """Ответ wham/usage → {'5h': {'util': 0.0, 'reset': ts}, '7d': {...}}.
    Также понимает rate_limits из событий CLI: {primary: {used_percent, window_minutes, resets_at}}."""
    out: Dict[str, dict] = {}
    rl = (body or {}).get("rate_limit") if isinstance(body, dict) else None
    if not isinstance(rl, dict):
        rl = (body or {}).get("rate_limits") if isinstance(body, dict) else None
    if not isinstance(rl, dict):
        return out
    for key in ("primary_window", "secondary_window", "primary", "secondary"):
        w = rl.get(key)
        if not isinstance(w, dict) or not isinstance(w.get("used_percent"), (int, float)):
            continue
        secs = w.get("limit_window_seconds")
        if secs is None and isinstance(w.get("window_minutes"), (int, float)):
            secs = w["window_minutes"] * 60
        reset = w.get("reset_at", w.get("resets_at"))
        if not isinstance(reset, (int, float)):
            after = w.get("reset_after_seconds", w.get("resets_in_seconds"))
            reset = now + after if isinstance(after, (int, float)) else None
        out[_label(secs)] = {"util": float(w["used_percent"]) / 100.0,
                             "reset": float(reset) if reset is not None else None}
    if rl.get("limit_reached") is True or rl.get("allowed") is False:
        for w in out.values():      # сервер сказал «лимит» — окно с ближайшим сбросом считаем полным
            w.setdefault("full", True)
    return out


def auth_headers(home: "Home") -> Optional[dict]:
    """Заголовки учётки или None, если входа нет. Ими ходят и опрос лимитов, и подарочный сброс."""
    auth = read_auth_of(home)
    tokens = (auth or {}).get("tokens") or {}
    access, account = tokens.get("access_token"), tokens.get("account_id")
    if not access:
        return None
    home.secrets = auth_secrets(auth)
    headers = {"Authorization": "Bearer " + access, "User-Agent": "codex_cli_rs", "Accept": "application/json"}
    if account:
        headers["ChatGPT-Account-Id"] = account
    return headers


def _http_usage(home: "Home", timeout: float = 20.0):
    """(код, тело) запроса wham/usage для учётки; ('nologin', None), если входа нет."""
    headers = auth_headers(home)
    if headers is None:
        return "nologin", None
    access = headers["Authorization"].split(" ", 1)[1]
    req = urllib.request.Request(USAGE_URL, headers=headers, method="GET")
    exp = _jwt_exp(access)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read() or b""
    try:
        body = json.loads(raw.decode("utf-8", "replace")) if raw else None
    except ValueError:
        body = None
    if code in (401, 403) and exp is not None and exp <= time.time():
        # Доступ просто истёк: CLI сам обновит его refresh-токеном — это не «вход слетел».
        return "expired", None
    return code, body


def _hhmm(ts: float) -> str:
    return time.strftime("%d.%m %H:%M UTC", time.gmtime(ts))


_WIN_RU = {"5h": "5 ч", "7d": "неделя"}


class Pool:
    def __init__(self, homes: List[Home], probe=_http_usage, clock=time.time, sleep=time.sleep,
                 probe_every: float = PROBE_MINUTES * 60, stop_at: float = STOP_AT):
        if not homes:
            raise ValueError("пул учёток Codex пуст")
        self.homes, self.probe, self.clock, self.sleep = homes, probe, clock, sleep
        self.probe_every, self.stop_at = probe_every, stop_at
        self._lock = threading.Lock()
        self._freed = threading.Condition(self._lock)
        self._probing: set = set()

    def secrets(self) -> List[str]:
        """Токены и account_id всех учёток: из кэша и свежие из auth.json (CLI мог обновить вход)."""
        out = [s for h in self.homes for s in h.secrets]
        for h in self.homes:
            out += auth_secrets(read_auth_of(h))
        return out

    def scrub(self, text: str) -> str:
        return CP.scrub(text, self.secrets())

    # -- окна
    def _apply(self, home: Home, wins: Dict[str, dict], now: float) -> None:
        """Под self._lock: запомнить окна и решить, до какого момента учётку не брать."""
        home.windows = {k: v for k, v in wins.items() if isinstance(v.get("util"), float)}
        until, why = 0.0, ""
        for name, w in wins.items():
            util = w.get("util")
            full = isinstance(util, float) and util >= self.stop_at
            reset = w.get("reset") or 0
            if full and reset > now and reset > until:
                until, why = reset, f"окно {_WIN_RU.get(name, name)} занято на {round(100 * util)}%"
            elif full and reset <= now and now + self.probe_every > until:
                # срок сброса неизвестен (или уже прошёл, а окно всё ещё полное) — не брать до перепроверки
                until = now + self.probe_every
                why = f"окно {_WIN_RU.get(name, name)} занято на {round(100 * util)}%, срок сброса неизвестен"
        if not until and any(w.get("full") for w in wins.values()):
            resets = [w["reset"] for w in wins.values() if (w.get("reset") or 0) > now]
            if resets:
                until, why = min(resets), "Codex ответил «лимит исчерпан»"
            else:
                until, why = now + self.probe_every, "Codex ответил «лимит исчерпан», срок сброса неизвестен"
        home.blocked_until, home.blocked_why = until, why

    def _probe_one(self, home: Home) -> None:
        """_read с отметкой «учётку сейчас читают»: другие потоки не берут непроверенную учётку
        вслепую, а ждут конца чтения (гонка при старте службы)."""
        with self._lock:
            self._probing.add(home.name)
        try:
            self._read(home)
        finally:
            with self._lock:
                self._probing.discard(home.name)
                self._freed.notify_all()

    def _read(self, home: Home) -> None:
        now = self.clock()
        try:
            code, body = self.probe(home)
        except Exception as e:  # сеть: запас неизвестен, но учётка не сломана
            with self._lock:
                home.checked = now
            log.warning("Codex, %s: лимиты не прочитаны (%s)", home.name, self.scrub(f"{type(e).__name__}: {e}"))
            return
        with self._lock:
            home.checked = now
            if code in (401, 403):
                home.auth_fails += 1
                said = isinstance(body, dict) and bool(_AUTH_BODY.search(json.dumps(body, ensure_ascii=False)))
                if not said and home.auth_fails < 2:
                    # Разовый отказ без ясного ответа про вход: запас неизвестен, вход проверим ещё раз.
                    home.checked = now - self.probe_every + min(AUTH_RECHECK_S, self.probe_every)
                    log.warning("Codex, %s: wham/usage ответил HTTP %s без ответа про вход — запас неизвестен, "
                                "вход перепроверю через %d с", home.name, code, int(min(AUTH_RECHECK_S, self.probe_every)))
                    return
            if code in ("nologin", 401, 403):
                home.rejected = True
                home.blocked_until = now + self.probe_every
                home.blocked_why = "входа нет" if code == "nologin" else f"вход не принят (HTTP {code})"
                log.warning("Codex, %s: %s; учётка отложена на %d мин. %s", home.name, home.blocked_why,
                            self.probe_every // 60, relogin_line(home))
                return
            home.rejected = False
            if code == 200 or (isinstance(code, int) and code < 400 and isinstance(body, dict)):
                home.auth_fails = 0
            if code == "expired" or not isinstance(body, dict):
                return                      # запас неизвестен; вход обновит сам CLI
            home.plan = str(body.get("plan_type") or "")[:20]
            wins = windows_from_usage(body, now)
            self._apply(home, wins, now)
            if wins:
                log.info("Codex, %s (%s): занято %s", home.name, home.plan or "план ?", ", ".join(
                    f"{_WIN_RU.get(n, n)} {round(100 * w['util'])}%" for n, w in sorted(wins.items())))
        # Окно кончилось — пробуем подарочный сброс из кабинета. Вне замка: это поход в сеть.
        if home.blocked_until > now and self._try_gift_reset(home, wins):
            self._read(home)                # окно обнулено — перечитываем и снимаем блокировку

    def _try_gift_reset(self, home: Home, wins: Dict[str, dict]) -> bool:
        """Подарочный сброс лимита, если он того стоит (codex_resets). True — окно обнулено.

        Зачем. 20.09.2026 недельное окно кончилось за сутки, а в кабинете лежали три
        неиспользованных подарка «Full reset», причём ближайший сгорал в ту же ночь.
        """
        try:
            import codex_resets as RS
            if not RS.enabled():
                return False
            headers = auth_headers(home)
            if headers is None:
                return False
            util = max((w.get("util") or 0.0) for w in wins.values()) if wins else None
            return RS.keeper().try_reset(home.name, headers, util)
        except Exception as e:      # подарок — не обязанность: любая осечка не ломает пул
            log.warning("Codex, %s: подарочный сброс не проверен (%s)", home.name,
                        self.scrub(f"{type(e).__name__}: {e}"))
            return False

    def note_windows(self, home: Home, wins: Dict[str, dict]) -> None:
        """Окна из ответа самого CLI (если он их отдал) — свежее кэша wham/usage."""
        if not wins:
            return
        now = self.clock()
        with self._lock:
            self._apply(home, wins, now)
            home.checked = now

    def _stale(self, home: Home, now: float) -> bool:
        return home.checked is None or now - home.checked >= self.probe_every

    # -- выбор
    def acquire(self, wait_reset: bool = True) -> Home:
        """Учётка с наибольшим запасом. wait_reset=False — если все исчерпаны до сброса окна,
        сразу AllBlocked вместо ожидания (у вызывающего есть запасной путь, например выбирающий обложки)."""
        waited_for = None
        slot_since, slot_said = None, False
        while True:
            now = self.clock()
            with self._lock:
                todo = [h for h in self.homes if h.blocked_until <= now and self._stale(h, now)
                        and h.name not in self._probing]
                self._probing.update(h.name for h in todo)
            try:
                for h in todo:
                    self._read(h)
            finally:
                with self._lock:
                    self._probing.difference_update(h.name for h in todo)
                    if todo:
                        self._freed.notify_all()
            now = self.clock()
            scope = CP.current_scope()
            with self._lock:
                free = [h for h in self.homes if h.blocked_until <= now]
                # Учётку, чьи лимиты ещё впервые читает другой поток, не брать вслепую — ждать чтения.
                room = [h for h in free if h.inflight < h.limit
                        and not (h.checked is None and h.name in self._probing)]
                busy = f"{sum(h.inflight for h in free)}/{sum(h.limit for h in free)}"
                reading = any(h.checked is None and h.name in self._probing for h in free)
                if room:
                    best = max(room, key=lambda h: (round(h.headroom(), 2) - 0.02 * h.inflight,
                                                    -self.homes.index(h)))
                    best.inflight += 1
                elif free:
                    best = None
                    self._freed.wait(1.0)   # учётки заняты до предела или читаются — ждём освобождения
                else:
                    best = None
                    live = [h for h in self.homes if not h.rejected]
                    if live:
                        soon = min(live, key=lambda h: h.blocked_until)
                        until = soon.blocked_until
                    else:
                        why = "; ".join(relogin_line(h) for h in self.homes)
            if best is not None:
                if waited_for is not None:
                    self._wait_over(best)
                return best
            if free:
                if slot_since is None:
                    slot_since = self.clock()
                elif not slot_said and not reading and self.clock() - slot_since >= SLOT_WAIT_SAY_S:
                    slot_said = True
                    msg = f"Ждёт свободного вызова Codex: учётки заняты ({busy})"
                    log.info(msg)
                    if scope is not None and scope.on_wait is not None:
                        scope.on_wait(scope.step, msg)
                if scope is not None:
                    for fn in (scope.stop, scope.check):
                        if fn is not None:
                            fn()
                continue
            if not live:
                raise LoginsGone(f"Codex: ни у одной учётки нет рабочего входа: {why}")
            if not wait_reset:
                raise AllBlocked(f"Codex: все учётки ({len(self.homes)}) исчерпаны; ближайший сброс "
                                 f"{_hhmm(until)} у «{soon.name}»")
            msg = (f"Ждёт лимита Codex: все учётки ({len(self.homes)}) исчерпаны; ближайший сброс "
                   f"{_hhmm(until)} у «{soon.name}» ({soon.blocked_why or 'лимит'})")
            if waited_for != until:
                waited_for = until
                log.warning(msg)
                if scope is not None and scope.on_wait is not None:
                    scope.on_wait(scope.step, msg)
            if scope is not None and scope.check is not None:
                scope.check()
            delay = max(1.0, min(30.0, until - self.clock()))
            if scope is None or scope.stop is None:
                self.sleep(delay)
            else:
                while delay > 0:
                    scope.stop()
                    if scope.check is not None:
                        scope.check()
                    self.sleep(min(1.0, delay))
                    delay -= 1.0
            with self._lock:  # после сброса окна перечитываем заново
                for h in self.homes:
                    if h.blocked_until <= self.clock():
                        h.checked = None

    def _wait_over(self, best: Home) -> None:
        msg = f"Лимит Codex сброшен, шаг продолжает работу на учётке «{best.name}»"
        log.info(msg)
        scope = CP.current_scope()
        if scope is None or scope.on_wait is None:
            return
        try:
            scope.on_wait(scope.step, msg)
        except BaseException:
            self.release(best)
            raise

    def release(self, home: Home, limited: bool = False, rejected: bool = False,
                suspect: bool = False) -> bool:
        """Вернуть учётку. rejected — CLI прямо сказал «войдите заново» (refresh-токен сгорел и т.п.):
        учётка откладывается сразу. suspect — голый 401/403 от CLI: это бывает и при обрыве потока,
        поэтому вход перепроверяется через wham/usage, и «слетевшей» учётка считается, только если
        wham тоже не принял вход. Возвращает True, если учётка теперь считается без входа."""
        now = self.clock()
        with self._lock:
            home.inflight = max(0, home.inflight - 1)
            self._freed.notify_all()
            if suspect and not rejected:
                home.checked = None
                home.auth_fails += 1     # отказ CLI — первый; отказ wham следом — второй подряд
        if suspect and not rejected:
            self._probe_one(home)
            with self._lock:
                if not home.rejected:
                    log.warning("Codex, %s: CLI ответил 401/403, но wham/usage вход принимает — "
                                "считаю обычным сбоем вызова", home.name)
                return home.rejected
        with self._lock:
            if rejected:
                home.rejected = True
                home.auth_fails = max(home.auth_fails, 2)   # CLI прямо сказал «войдите заново»
                home.checked = None       # после паузы — снова wham/usage: вдруг перелогинились
                home.blocked_until = now + self.probe_every
                home.blocked_why = "вход слетел"
                log.warning("Codex: %s; учётка отложена на %d мин", relogin_line(home), self.probe_every // 60)
                return True
            if not limited:
                return False
            home.checked = None
        self._probe_one(home)
        with self._lock:
            if home.blocked_until <= now:
                home.blocked_until = now + self.probe_every
                home.blocked_why = "Codex ответил «лимит», wham/usage этого не показывает"
                log.warning("Codex, %s: CLI сообщил о лимите, по wham/usage окна свободны; "
                            "учётка отложена на %d мин", home.name, self.probe_every // 60)
        return False


# ------------------------------------------------------------------ разбор вывода codex exec --json

# Однозначно «войдите заново»: CLI сам пробовал обновить вход и не смог.
_AUTH = re.compile(r"not logged in|log ?in again|sign in again|please (re)?login|log out and sign in|"
                   r"could not be refreshed|refresh[_ ]token (was |has been |is )?(already used|expired|invalid|revoked)|"
                   r"token (has been )?(invalidated|revoked)", re.I)
# Голый 401/403: бывает и у слетевшего входа, и у оборванного потока — проверяется через wham/usage.
_AUTH_MAYBE = re.compile(r"\b(401|403)\b|unauthori[sz]ed|forbidden|token[_ ]expired", re.I)
_LIMIT = re.compile(r"\b429\b|usage[_ ]limit|rate[_ ]limit|too many requests|hit your .*limit|limit reached|"
                    r"out of codex messages", re.I)


def parse_events(stdout: str) -> dict:
    """События codex exec --json: usage из turn.completed, тексты ошибок, rate_limits (если есть)."""
    usage, errors, limits = None, [], None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        if kind == "turn.completed" and isinstance(ev.get("usage"), dict):
            usage = ev["usage"]
        elif kind == "error" and isinstance(ev.get("message"), str):
            errors.append(ev["message"])
        elif kind == "turn.failed":
            msg = (ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict) else ev.get("error")
            if isinstance(msg, str):
                errors.append(msg)
        found = ev.get("rate_limits")
        if found is None and isinstance(ev.get("msg"), dict):
            found = ev["msg"].get("rate_limits")
        if isinstance(found, dict):
            limits = {"rate_limits": found}
    return {"usage": usage, "errors": errors, "limits": limits}


# «refresh token already used» — чаще всего гонка: соседний вызов уже обновил вход сам (CLI делает
# это после 401 вне замка), auth.json переписан — вход перепроверяется через wham/usage, а не «слетел».
_REUSED = re.compile(r"refresh[_ ]token (was |has been |is )?already used|refresh_token_reused", re.I)


def failure(texts: List[str]) -> str:
    """Почему codex не ответил: 'rejected' (вход точно слетел) | 'suspect' (401/403 — перепроверить
    через wham/usage) | 'limit' | ''. По последней ошибке — повторы «Reconnecting... 401» перед ней того же рода."""
    joined = "\n".join(texts[-3:])
    if _LIMIT.search(joined):
        return "limit"
    if _REUSED.search(joined):
        return "suspect"
    if _AUTH.search(joined):
        return "rejected"
    if _AUTH_MAYBE.search(joined):
        return "suspect"
    return ""


def usage_row(usage: Optional[dict], model: str, seconds: float, account: str, ok: bool,
              images: int = 0) -> dict:
    row = {"ts": round(time.time(), 3), "provider": "codex", "model": f"codex:{model}",
           "seconds": round(seconds, 2), "ok": bool(ok), "route": "subscription"}
    if account:
        row["account"] = account
    if images:
        row["images"] = images
    if not isinstance(usage, dict):
        row.update(input=None, cache_read=None, output=None)
        return row
    row.update(input=CP._int(usage.get("input_tokens")), cache_read=CP._int(usage.get("cached_input_tokens")),
               output=CP._int(usage.get("output_tokens")))
    if "reasoning_output_tokens" in usage:
        row["reasoning"] = CP._int(usage.get("reasoning_output_tokens"))
    return row


POOL: Optional[Pool] = None
_homes = parse_homes(os.environ.get("MANGA_CODEX_HOMES", ""))
if _homes:
    POOL = Pool(_homes)
del _homes
