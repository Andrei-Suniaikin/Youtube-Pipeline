# -*- coding: utf-8 -*-
"""Claude: счётчик расхода на каждом вызове и пул подписок.

Счётчик. claude CLI запускается с --output-format json и отдаёт usage и
total_cost_usd (проверено 18.09.2026 на CLI 2.1.267 у Мака; у CLI 2.1.40 на VPS
та же схема ответа: result, usage, total_cost_usd, modelUsage). Каждый вызов
дописывает строку в журнал проекта text/llm_usage.jsonl: шаг, модель, токены
входа/кэша/выхода, эквивалент в долларах по прайсу API, секунды, имя подписки.
Токен в журнал не пишется никогда.

Журнал проекта и текущий шаг задаёт обработчик очереди через use_scope();
потоки внутри шага получают их через carry().

Пул. MANGA_CLAUDE_TOKENS — несколько OAuth-токенов подписок. Не задано — пула
нет, CLI берёт CLAUDE_CODE_OAUTH_TOKEN из окружения службы, как раньше.
Перед вызовом берётся подписка с наибольшим запасом по окнам 5 ч и 7 дней.
Запас читается из заголовков anthropic-ratelimit-unified-* ответа API на
запрос в 1 токен haiku — не чаще раза в MANGA_CLAUDE_PROBE_MINUTES минут на
подписку (CLI 2.1.40 заголовков не отдаёт). Подписка, у которой окно
исчерпано, не берётся до сброса этого окна; исчерпаны все — шаг ждёт
ближайшего сброса и пишет об этом строку на страницу ролика.
"""
from __future__ import annotations

import collections
import contextvars
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

log = logging.getLogger("manga.claude")

API_URL = os.environ.get("MANGA_CLAUDE_API", "https://api.anthropic.com").rstrip("/")
PROBE_MODEL = os.environ.get("MANGA_CLAUDE_PROBE_MODEL", "claude-haiku-4-5")


def _number(name, default, lo, hi):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        raise ValueError(f"Неверный {name}: нужно число от {lo} до {hi}")
    if not lo <= v <= hi:
        raise ValueError(f"Неверный {name}: нужно число от {lo} до {hi}")
    return v


PROBE_MINUTES = _number("MANGA_CLAUDE_PROBE_MINUTES", 10, 1, 1440)
# Доля окна, после которой подписка считается исчерпанной (запас для соседнего рерайта).
STOP_AT = _number("MANGA_CLAUDE_STOP_AT", 0.95, 0.1, 1.0)


# ------------------------------------------------------------------ журнал расхода

@dataclass
class Scope:
    journal: Optional[Path]
    step: str = ""
    check: Optional[Callable[[], None]] = None           # бросает, если задача больше не наша
    on_wait: Optional[Callable[[str, str], None]] = None  # (шаг, строка) — показать ожидание
    stop: Optional[Callable[[], None]] = None            # бросает, если служба останавливается (только в ожидании лимита)


_SCOPE: contextvars.ContextVar = contextvars.ContextVar("manga_claude_scope", default=None)
_WRITE = threading.Lock()
_warned_no_scope = False


def use_scope(journal, step="", check=None, on_wait=None, stop=None):
    """Задаёт журнал и шаг для вызовов Claude в этом потоке; вернуть reset_scope(метка)."""
    return _SCOPE.set(Scope(Path(journal) if journal else None, step, check, on_wait, stop))


def set_step(step: str) -> None:
    scope = _SCOPE.get()
    if scope is not None:
        scope.step = step


def reset_scope(mark) -> None:
    _SCOPE.reset(mark)


def current_scope() -> Optional[Scope]:
    return _SCOPE.get()


# MANGA_CLAUDE_CONCURRENCY — общий предел одновременных текстовых вызовов модели на всю
# службу (все ролики сразу): MANGA_TEXT_THREADS действует на один ролик, и при
# MANGA_PARALLEL_VIDEOS=2 без общего предела сразу идёт до 2×(N+2) процессов CLI на одни
# и те же подписки и общую память VPS. Не задано — предела нет, как было.
_cc = os.environ.get("MANGA_CLAUDE_CONCURRENCY", "").strip()
if _cc and not (_cc.isdigit() and 1 <= int(_cc) <= 64):
    raise ValueError(f"MANGA_CLAUDE_CONCURRENCY: целое число от 1 до 64, получено {_cc!r}")
CONCURRENCY: Optional[int] = int(_cc) if _cc else None


class _FairSlots:
    """Места под общим пределом, по очереди прихода. Обычный семафор отдаёт место тому,
    кто успел первым: поток ролика, который только что освободил место и тут же просит
    новое (следующий кусок той же сцены), обгоняет второй ролик, и тот подолгу стоит."""

    def __init__(self, n: int):
        self.n, self.busy = n, 0
        self.line: collections.deque = collections.deque()
        self.cv = threading.Condition()

    def acquire(self, check: Optional[Callable[[], None]] = None,
                stop: Optional[Callable[[], None]] = None) -> float:
        """Ждёт место; возвращает, сколько секунд простоял в очереди. Раз в секунду
        спрашивает check (задача ещё наша) и stop (служба не останавливается)."""
        me = object()
        t0 = time.monotonic()
        last = time.monotonic()
        with self.cv:
            self.line.append(me)
        while True:
            with self.cv:
                if self.line[0] is me and self.busy < self.n:
                    self.line.popleft()
                    self.busy += 1
                    self.cv.notify_all()          # следующий в очереди тоже может пройти
                    return time.monotonic() - t0
                self.cv.wait(1.0)
            if (check is not None or stop is not None) and time.monotonic() - last >= 1.0:
                last = time.monotonic()
                try:
                    for fn in (stop, check):      # служба останавливается / задача не наша — уйти
                        if fn is not None:
                            fn()
                except BaseException:
                    with self.cv:
                        self.line.remove(me)
                        self.cv.notify_all()
                    raise

    def release(self) -> None:
        with self.cv:
            self.busy -= 1
            self.cv.notify_all()


_SLOTS: Optional[_FairSlots] = _FairSlots(CONCURRENCY) if CONCURRENCY else None
# Сколько вызов простоял в очереди общего предела: record() пишет это в строку журнала.
_QUEUE_WAIT: contextvars.ContextVar = contextvars.ContextVar("manga_claude_queue_wait", default=0.0)
del _cc


def set_concurrency(n: Optional[int]) -> None:
    """Для тестов и запуска: задать общий предел (None — без предела)."""
    global CONCURRENCY, _SLOTS
    CONCURRENCY = n
    _SLOTS = _FairSlots(n) if n else None


def limited(fn):
    """Обёртка вызова модели: ждёт свободное место под общим пределом MANGA_CLAUDE_CONCURRENCY,
    места раздаются по очереди прихода. Без предела — прямой вызов. Пока ждёт, раз в секунду
    проверяет, что задача ещё наша и служба не останавливается. Время в очереди попадает
    в строку журнала расхода (queue_wait, секунды) первого вызова модели после ожидания."""
    def run(*a, **k):
        slots = _SLOTS
        if slots is None:
            return fn(*a, **k)
        scope = _SCOPE.get()
        waited = slots.acquire(scope.check if scope is not None else None,
                               scope.stop if scope is not None else None)
        if waited >= 30:
            log.info("Вызов Claude ждал места под общим пределом MANGA_CLAUDE_CONCURRENCY %.0f с", waited)
        mark = _QUEUE_WAIT.set(waited)
        try:
            return fn(*a, **k)
        finally:
            _QUEUE_WAIT.reset(mark)
            slots.release()
    run.__wrapped__ = fn
    return run


def carry(fn):
    """Обёртка для функций пула потоков: поток видит журнал и шаг того, кто его создал."""
    ctx = contextvars.copy_context()

    def run(*a, **k):
        return ctx.copy().run(fn, *a, **k)
    return run


def parse_result(stdout: str) -> Optional[dict]:
    """Ответ CLI с --output-format json. Не JSON — None (вызывающий решает, что делать)."""
    text = (stdout or "").strip()
    if not text:
        return None
    for chunk in (text, *reversed(text.splitlines())):
        chunk = chunk.strip()
        if not chunk.startswith("{"):
            continue
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(data, dict) and ("result" in data or data.get("type") == "result"):
            return data
    return None


def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def usage_row(res: Optional[dict], model: str, seconds: float, subscription: str = "",
              ok: bool = True) -> dict:
    row = {"ts": round(time.time(), 3), "model": f"claude:{model}", "seconds": round(seconds, 2),
           "ok": bool(ok)}
    if subscription:
        row["subscription"] = subscription
    u = (res or {}).get("usage")
    if not isinstance(u, dict):
        row.update(input=None, cache_read=None, cache_write=None, output=None, cost_usd=None)
        return row
    row.update(input=_int(u.get("input_tokens")), cache_read=_int(u.get("cache_read_input_tokens")),
               cache_write=_int(u.get("cache_creation_input_tokens")), output=_int(u.get("output_tokens")))
    cost = res.get("total_cost_usd")
    row["cost_usd"] = round(float(cost), 6) if isinstance(cost, (int, float)) else None
    # modelUsage — расход по моделям, включая побочные вызовы CLI (их нет в usage верхнего
    # уровня, но они есть в total_cost_usd). Если он есть, токены считаем по нему: тогда
    # токены и $ на одной базе.
    models = res.get("modelUsage")
    if isinstance(models, dict) and models:
        per = {}
        for m, mu in models.items():
            if not isinstance(mu, dict):
                continue
            c = mu.get("costUSD")
            per[str(m)] = dict(input=_int(mu.get("inputTokens")), cache_read=_int(mu.get("cacheReadInputTokens")),
                               cache_write=_int(mu.get("cacheCreationInputTokens")), output=_int(mu.get("outputTokens")),
                               cost_usd=round(float(c), 6) if isinstance(c, (int, float)) else None)
        if per:
            row["models"] = per
            if any("inputTokens" in mu or "outputTokens" in mu for mu in models.values() if isinstance(mu, dict)):
                for k in ("input", "cache_read", "cache_write", "output"):
                    row[k] = sum(v[k] for v in per.values())
    return row


def record(row: dict) -> bool:
    """Дописывает строку в журнал проекта одним write с O_APPEND (строки не перемешиваются)."""
    global _warned_no_scope
    scope = _SCOPE.get()
    if scope is None or scope.journal is None:
        if not _warned_no_scope:
            _warned_no_scope = True
            log.warning("Вызов модели вне шага ролика: расход в журнал проекта не записан")
        return False
    row = {**row, "step": scope.step}
    waited = _QUEUE_WAIT.get()
    if waited >= 0.5:
        row["queue_wait"] = round(waited, 1)
        _QUEUE_WAIT.set(0.0)                 # повтор внутри call_json уже не ждал очередь
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    with _WRITE:
        scope.journal.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(scope.journal), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    return True


def summary(journal) -> Optional[dict]:
    """Итог по журналу: токены, эквивалент в $, по шагам в порядке первого появления."""
    try:
        lines = Path(journal).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    # tokens — вход без кэша + выход (то, что модель реально прочла заново и написала);
    # cache — чтение и запись кэша (в основном служебный промпт CLI, ~42 тыс. на вызов).
    total = dict(calls=0, unknown=0, input=0, cache_read=0, cache_write=0, output=0, tokens=0, cache=0,
                 cost_usd=0.0, routes={}, lean=0, cut=0)
    # Вызовы Codex (provider=codex, с 19.09) — отдельным итогом: у них нет $ и кэша записи,
    # а тратятся окна подписки ChatGPT. В итог Claude они не входят.
    codex = dict(calls=0, unknown=0, failed=0, input=0, cache_read=0, output=0, seconds=0.0,
                 accounts={}, steps={}, routes={})
    steps: Dict[str, dict] = {}
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        if r.get("provider") == "codex" or str(r.get("model") or "").startswith("codex:"):
            codex["calls"] += 1
            codex["failed"] += 0 if r.get("ok", True) else 1
            codex["seconds"] += float(r.get("seconds") or 0)
            acc = str(r.get("account") or "?")
            codex["accounts"][acc] = codex["accounts"].get(acc, 0) + 1
            route = str(r.get("route") or "subscription")
            codex["routes"][route] = codex["routes"].get(route, 0) + 1
            st = codex["steps"].setdefault(str(r.get("step") or ""), dict(calls=0, unknown=0, tokens=0))
            st["calls"] += 1
            if r.get("input") is None:
                codex["unknown"] += 1
                st["unknown"] += 1
                continue
            for k in ("input", "cache_read", "output"):
                codex[k] += _int(r.get(k))
            # у Codex input_tokens уже включает кэшированную часть
            st["tokens"] += _int(r.get("input")) - _int(r.get("cache_read")) + _int(r.get("output"))
            continue
        st = steps.setdefault(str(r.get("step") or ""), dict(calls=0, unknown=0, tokens=0, cache=0, cost_usd=0.0))
        for bucket in (total, st):
            bucket["calls"] += 1
        # route: «alt» — альт-ключ (прокси), «subscription» — подписка; строки до 19.09 без поля — подписка.
        route = str(r.get("route") or "subscription")
        total["routes"][route] = total["routes"].get(route, 0) + 1
        total["lean"] += 1 if r.get("lean") else 0
        total["cut"] += 1 if r.get("stop") == "max_tokens" else 0
        if r.get("input") is None:
            total["unknown"] += 1
            st["unknown"] += 1
            continue
        tokens = _int(r.get("input")) + _int(r.get("output"))
        cache = _int(r.get("cache_read")) + _int(r.get("cache_write"))
        for k in ("input", "cache_read", "cache_write", "output"):
            total[k] += _int(r.get(k))
        for bucket in (total, st):
            bucket["tokens"] += tokens
            bucket["cache"] += cache
        cost = r.get("cost_usd")
        if isinstance(cost, (int, float)):
            total["cost_usd"] += cost
            st["cost_usd"] += cost
    if not total["calls"] and not codex["calls"]:
        return None
    codex["seconds"] = round(codex["seconds"], 1)
    total["codex"] = codex
    total["cost_usd"] = round(total["cost_usd"], 4)
    for st in steps.values():
        st["cost_usd"] = round(st["cost_usd"], 4)
    total["steps"] = steps
    return total


# ------------------------------------------------------------------ пул подписок

class JobGone(RuntimeError):
    """Задача ушла от этого исполнителя (scope.check). Не ошибка сцены — шаг надо остановить."""


class TokensRejected(RuntimeError):
    """Ни одна подписка пула не принята API (401/403). Ждать нечего — шаг падает с понятной строкой."""


@dataclass
class Sub:
    name: str
    token: str = field(repr=False)
    windows: Dict[str, dict] = field(default_factory=dict)  # окно → {util, reset, status}
    checked: Optional[float] = None      # когда последний раз читали заголовки
    blocked_until: float = 0.0           # до какого момента не брать
    blocked_why: str = ""
    inflight: int = 0
    rejected: bool = False               # API не принял токен (401/403) — это не лимит

    def headroom(self) -> float:
        utils = [w["util"] for n, w in self.windows.items()
                 if is_limit_window(n) and isinstance(w.get("util"), float)]
        return 1.0 - max(utils) if utils else 0.0


def _looks_like_token(name: str) -> bool:
    name = name.strip()
    return name.lower().startswith("sk-") or (len(name) >= 40 and not re.search(r"\s", name))


def parse_tokens(raw: str) -> List[Sub]:
    """MANGA_CLAUDE_TOKENS: токены через запятую/пробел/перевод строки,
    или JSON {"имя": "токен"}, или JSON [{"name": ..., "token": ...}]. Токен в ошибку не попадает."""
    raw = (raw or "").strip()
    if not raw:
        return []
    pairs = []
    if raw[0] in "{[":
        try:
            data = json.loads(raw)
        except ValueError:
            raise ValueError("Неверный MANGA_CLAUDE_TOKENS: JSON не разобрался")
        if isinstance(data, dict):
            pairs = [(str(k), v) for k, v in data.items()]
        elif isinstance(data, list):
            for i, item in enumerate(data, 1):
                if isinstance(item, dict):
                    pairs.append((str(item.get("name") or f"подписка {i}"), item.get("token")))
                else:
                    pairs.append((f"подписка {i}", item))
        else:
            raise ValueError("Неверный MANGA_CLAUDE_TOKENS: нужен список или словарь")
    else:
        pairs = [(f"подписка {i}", t) for i, t in enumerate(re.split(r"[\s,;]+", raw), 1) if t]
    subs, seen = [], set()
    for i, (name, token) in enumerate(pairs, 1):
        # Имя из настройки в ошибку не пишем: владелец мог поставить токен на место имени.
        if not isinstance(token, str) or not token.strip() or re.search(r"\s", token.strip()):
            raise ValueError(f"Неверный MANGA_CLAUDE_TOKENS: у записи №{i} нет токена")
        if _looks_like_token(name):
            raise ValueError(f"Неверный MANGA_CLAUDE_TOKENS: имя записи №{i} похоже на токен "
                             "(нужно {\"имя\": \"токен\"}, не наоборот)")
        token = token.strip()
        if token in name or name in seen or token in seen:
            raise ValueError("Неверный MANGA_CLAUDE_TOKENS: имена и токены не должны повторяться, "
                             "имя не должно содержать токен")
        seen.update((name, token))
        subs.append(Sub(name[:60], token))
    return subs


_LIMIT_TEXT = re.compile(r"hit your .*limit|usage limit|rate limit|limit reached|out of extra usage", re.I)


def looks_limited(text: str) -> bool:
    return bool(_LIMIT_TEXT.search(text or ""))


def cli_failure(res: Optional[dict], text: str) -> str:
    """Почему CLI не ответил: 'limit' | 'rejected' | ''. Сначала числовой api_error_status
    из JSON (429 — лимит, 401/403 — токен не принят; проверено на CLI 2.1.267), текст —
    запасной путь для CLI без этого поля."""
    code = (res or {}).get("api_error_status")
    if isinstance(code, int) and not isinstance(code, bool):
        if code == 429:
            return "limit"
        if code in (401, 403):
            return "rejected"
    return "limit" if looks_limited(text) else ""


_HDR = re.compile(r"^anthropic-ratelimit-unified-([a-z0-9_]+)-(utilization|reset|status)$")
# Окна, которые ограничивают подписку: 5 ч, 7 дней и их варианты по моделям (7d_opus и т.п.).
# Остальные группы (например overage — докупка сверх подписки) хранятся для справки,
# но подписку не блокируют и в запас не входят.
_LIMIT_WINDOW = re.compile(r"^(5h|7d)(_[a-z0-9_]+)?$")


def is_limit_window(name: str) -> bool:
    return bool(_LIMIT_WINDOW.match(name or ""))


def windows_from_headers(headers: Dict[str, str]) -> Dict[str, dict]:
    """{'5h': {'util': 0.08, 'reset': 1789722600.0, 'status': 'allowed'}, '7d': {...}, ...}"""
    out: Dict[str, dict] = {}
    for k, v in (headers or {}).items():
        m = _HDR.match(str(k).lower())
        if not m:
            continue
        w = out.setdefault(m.group(1), {})
        try:
            if m.group(2) == "utilization":
                w["util"] = float(v)
            elif m.group(2) == "reset":
                w["reset"] = float(v)
            else:
                w["status"] = str(v).strip().lower()
        except ValueError:
            continue
    return out


def _http_probe(token: str, timeout: float = 20.0):
    """1 токен haiku; возвращает (код, заголовки). 429 тоже несёт заголовки окон."""
    body = json.dumps({"model": PROBE_MODEL, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request(API_URL + "/v1/messages", data=body, method="POST", headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}


def _hhmm(ts: float) -> str:
    return time.strftime("%d.%m %H:%M UTC", time.gmtime(ts))


class Pool:
    def __init__(self, subs: List[Sub], probe=_http_probe, clock=time.time, sleep=time.sleep,
                 probe_every: float = PROBE_MINUTES * 60, stop_at: float = STOP_AT):
        if not subs:
            raise ValueError("пул подписок пуст")
        self.subs, self.probe, self.clock, self.sleep = subs, probe, clock, sleep
        self.probe_every, self.stop_at = probe_every, stop_at
        self._lock = threading.Lock()
        self._probing: set = set()

    def secrets(self) -> List[str]:
        return [s.token for s in self.subs]

    # -- заголовки
    def _read(self, sub: Sub) -> None:
        now = self.clock()
        try:
            code, headers = self.probe(sub.token)
        except Exception as e:  # сеть: запас неизвестен, но подписка не сломана
            with self._lock:
                sub.checked = now
            log.warning("Claude, %s: лимиты не прочитаны (%s)", sub.name, scrub(f"{type(e).__name__}: {e}", [sub.token]))
            return
        wins = windows_from_headers(headers)
        with self._lock:
            sub.checked = now
            if code in (401, 403) and not wins:
                sub.rejected = True
                sub.blocked_until, sub.blocked_why = now + self.probe_every, f"токен не принят (HTTP {code})"
                log.warning("Claude, %s: токен не принят (HTTP %s), подписка отложена на %d мин",
                            sub.name, code, self.probe_every // 60)
                return
            sub.rejected = False
            if wins:
                sub.windows = wins
                log.info("Claude, %s: занято %s", sub.name, ", ".join(
                    f"{n} {round(100 * w['util'])}%" for n, w in sorted(wins.items()) if isinstance(w.get("util"), float)))
            until, why = 0.0, ""
            for name, w in wins.items():
                if not is_limit_window(name):
                    continue
                full = w.get("status") == "rejected" or (isinstance(w.get("util"), float) and w["util"] >= self.stop_at)
                if full and w.get("reset", 0) > now and w["reset"] > until:
                    until, why = w["reset"], f"окно {name} занято на {round(100 * w.get('util', 1.0))}%"
            sub.blocked_until, sub.blocked_why = until, why

    def _stale(self, sub: Sub, now: float) -> bool:
        return sub.checked is None or now - sub.checked >= self.probe_every

    # -- выбор
    def acquire(self) -> Sub:
        waited_for = None
        while True:
            now = self.clock()
            with self._lock:
                todo = [s for s in self.subs if s.blocked_until <= now and self._stale(s, now)
                        and s.name not in self._probing]
                self._probing.update(s.name for s in todo)
            try:
                for s in todo:
                    self._read(s)
            finally:
                with self._lock:
                    self._probing.difference_update(s.name for s in todo)
            now = self.clock()
            best, live = None, self.subs
            with self._lock:
                free = [s for s in self.subs if s.blocked_until <= now]
                if free:
                    best = max(free, key=lambda s: (round(s.headroom(), 2) - 0.02 * s.inflight,
                                                    -self.subs.index(s)))
                    best.inflight += 1
                else:
                    live = [s for s in self.subs if not s.rejected]
                    if live:
                        soon = min(live, key=lambda s: s.blocked_until)
                        until = soon.blocked_until
                    else:
                        why = "; ".join(f"«{s.name}»: {s.blocked_why}" for s in self.subs)
            if best is None and not live:
                # Отказ токена — не лимит: ждать сброса нечего, шаг должен упасть видимо, как без пула.
                raise TokensRejected(f"Claude не принимает токен(ы) подписок ({why}); "
                                     "проверьте MANGA_CLAUDE_TOKENS")
            if best is not None:
                if waited_for is not None:
                    self._wait_over(best)
                return best
            msg = (f"Ждёт лимита Claude: все подписки ({len(self.subs)}) исчерпаны; ближайший сброс "
                   f"{_hhmm(until)} у «{soon.name}» ({soon.blocked_why or 'лимит'})")
            if waited_for != until:
                waited_for = until
                log.warning(msg)
                scope = _SCOPE.get()
                if scope is not None and scope.on_wait is not None:
                    scope.on_wait(scope.step, msg)
            scope = _SCOPE.get()
            if scope is not None and scope.check is not None:
                scope.check()
            delay = max(1.0, min(30.0, until - self.clock()))
            if scope is None or scope.stop is None:
                self.sleep(delay)
            else:
                # Очередь: остановка службы или потеря задачи видны за ~1 с, а не после 30-секундного сна.
                while delay > 0:
                    scope.stop()
                    if scope.check is not None:
                        scope.check()
                    self.sleep(min(1.0, delay))
                    delay -= 1.0
            with self._lock:  # после сброса окно перечитываем заново
                for s in self.subs:
                    if s.blocked_until <= self.clock():
                        s.checked = None

    def _wait_over(self, best: Sub) -> None:
        """Ожидание кончилось: заменить на странице «Ждёт лимита Claude…» — шаг снова работает."""
        msg = f"Лимит Claude сброшен, шаг продолжает работу на подписке «{best.name}»"
        log.info(msg)
        scope = _SCOPE.get()
        if scope is None or scope.on_wait is None:
            return
        try:
            scope.on_wait(scope.step, msg)
        except BaseException:
            with self._lock:
                best.inflight = max(0, best.inflight - 1)
            raise

    def release(self, sub: Sub, limited: bool = False, rejected: int = 0) -> None:
        """rejected — код HTTP (401/403), если CLI сказал «токен не принят»: подписка
        откладывается до следующей проверки, как после отказа на пробном запросе."""
        now = self.clock()
        with self._lock:
            sub.inflight = max(0, sub.inflight - 1)
            if rejected:
                sub.rejected = True
                sub.checked = None  # после паузы — снова пробный запрос: вдруг токен заменили
                sub.blocked_until = now + self.probe_every
                sub.blocked_why = f"токен не принят (HTTP {rejected})"
                log.warning("Claude, %s: CLI сообщил, что токен не принят (HTTP %s), подписка отложена на %d мин",
                            sub.name, rejected, self.probe_every // 60)
                return
            if not limited:
                return
            sub.checked = None  # CLI сказал «лимит» — перечитать окна сейчас
        self._read(sub)
        with self._lock:
            if sub.blocked_until <= now:
                # Заголовки лимита не показывают, а CLI отказал: не долбить подписку до следующего чтения.
                sub.blocked_until = now + self.probe_every
                sub.blocked_why = "Claude ответил «лимит», заголовки окон этого не показывают"
                log.warning("Claude, %s: CLI сообщил о лимите, по заголовкам окна свободны; "
                            "подписка отложена на %d мин", sub.name, self.probe_every // 60)


def scrub(text: str, secrets=None) -> str:
    """Убирает токены из строки ошибки или журнала."""
    out = str(text)
    extra = [os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""), os.environ.get("MANGA_ALT_API_KEY", "").strip()]
    for s in list(secrets or []) + extra + (POOL.secrets() if POOL else []):
        if s and len(s) >= 8:
            out = out.replace(s, "***")
    return out


POOL: Optional[Pool] = None
_subs = parse_tokens(os.environ.get("MANGA_CLAUDE_TOKENS", ""))
if _subs:
    POOL = Pool(_subs)
del _subs
