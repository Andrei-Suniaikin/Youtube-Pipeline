# -*- coding: utf-8 -*-
"""Подарочные сбросы лимита Codex: применяются сами, когда окно кончилось.

Что это. В кабинете Codex лежат «Full reset» — подарки, каждый обнуляет недельное окно
подписки. У них есть срок годности: 20.09.2026 один такой сгорал в ту же ночь, а окно
было занято на 90% и пачка роликов стояла. Применили руками — окно стало 0%.

Правила (сознательно простые, потому что подарков мало и они невозвратные):
1. применяем, ТОЛЬКО когда окно фактически кончилось — занято не меньше MIN_UTIL
   (по умолчанию 99%). Подарки невозвратные, и решение «потратить пораньше» принимает
   владелец, а не студия;
2. спасение сгорающего подарка по умолчанию ВЫКЛЮЧЕНО (MANGA_CODEX_RESET_EXPIRY_HOURS=0).
   Включённым оно тратит подарок, который сгорит в ближайшие N часов, если окно занято
   хотя бы наполовину;
3. не чаще одного применения на учётку в COOLDOWN секунд — сбой сети не должен сжечь
   все подарки подряд;
4. каждое применение пишется в журнал службы: что за подарок, у какой учётки, сколько
   осталось.

Живые данные провайдера (проверено 20.09.2026):
  GET  /wham/rate-limit-reset-credits          → {"credits": [{id, status, expires_at, …}]}
  POST /wham/rate-limit-reset-credits/consume  → тело {"redeem_request_id": "<uuid>"},
       ответ {"code": "reset", "credit": {..., "status": "redeemed"}}.
Какой именно подарок тратится, выбирает сервер — тела с идентификатором он не принимает.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("codex_resets")

BASE = (os.environ.get("MANGA_CODEX_RESETS_URL")
        or "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits")


def _float_env(name: str, default: float, low: float, high: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name}: нужно число, а не {raw!r}") from None
    if not low <= value <= high:
        raise ValueError(f"{name}: от {low} до {high}")
    return value


def enabled() -> bool:
    return (os.environ.get("MANGA_CODEX_AUTO_RESET", "on").strip().lower()
            not in {"0", "off", "false", "no", "нет"})


MIN_UTIL = _float_env("MANGA_CODEX_RESET_MIN_UTIL", 0.99, 0.5, 1.0)
EXPIRY_HOURS = _float_env("MANGA_CODEX_RESET_EXPIRY_HOURS", 0.0, 0.0, 720.0)
COOLDOWN = _float_env("MANGA_CODEX_RESET_COOLDOWN", 900.0, 60.0, 86400.0)
_float_env("MANGA_CODEX_RESET_COOLDOWN", 900.0, 60.0, 86400.0)   # битая настройка — ошибка при запуске


def _http(method: str, url: str, headers: dict, body: Optional[bytes], timeout: float):
    """(код, тело). Единственная точка выхода в сеть — подменяется в тестах."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw, code = (e.read() or b""), e.code
    except (urllib.error.URLError, OSError) as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}
    try:
        return code, (json.loads(raw.decode("utf-8", "replace")) if raw else {})
    except ValueError:
        return code, {}


def _stamp(value: str) -> float:
    """«2026-09-20T23:52:54.168109Z» → время в секундах; при беде — 0 (срока не знаем)."""
    try:
        text = str(value).replace("Z", "+00:00")
        from datetime import datetime
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def available(headers: dict, timeout: float = 30.0) -> List[dict]:
    code, body = _http("GET", BASE, headers, None, timeout)
    if code != 200 or not isinstance(body, dict):
        return []
    return [c for c in (body.get("credits") or [])
            if isinstance(c, dict) and str(c.get("status") or "").lower() == "available"]


def worth_using(credits: List[dict], util: Optional[float], now: float) -> Tuple[bool, str]:
    """(применять ли, причина словами для журнала)."""
    if not credits:
        return False, "подарочных сбросов нет"
    if util is None:
        return False, "запас окна неизвестен"
    if util >= MIN_UTIL:
        return True, f"окно занято на {round(100 * util)}%"
    if EXPIRY_HOURS <= 0:
        return False, f"окно занято на {round(100 * util)}% — сброс придержан"
    soonest = min((_stamp(c.get("expires_at")) for c in credits if _stamp(c.get("expires_at"))), default=0.0)
    hours = (soonest - now) / 3600 if soonest else None
    if hours is not None and 0 < hours <= EXPIRY_HOURS and util >= 0.5:
        return True, f"сброс сгорает через {hours:.0f} ч, а окно занято на {round(100 * util)}%"
    return False, f"окно занято на {round(100 * util)}% — сброс придержан"


def consume(headers: dict, timeout: float = 40.0) -> Tuple[bool, str]:
    """Применить один подарок. (успех, что сказал провайдер)."""
    body = json.dumps({"redeem_request_id": str(uuid.uuid4())}).encode("utf-8")
    head = dict(headers)
    head["Content-Type"] = "application/json"
    code, data = _http("POST", BASE + "/consume", head, body, timeout)
    if code != 200 or not isinstance(data, dict):
        return False, f"HTTP {code}: {json.dumps(data, ensure_ascii=False)[:200]}"
    credit = data.get("credit") if isinstance(data.get("credit"), dict) else {}
    ok = str(data.get("code") or "").lower() == "reset" or str(credit.get("status") or "").lower() == "redeemed"
    return ok, str(data.get("code") or credit.get("status") or "ответ без кода")[:80]


class Keeper:
    """Помнит, когда у какой учётки применяли сброс: подряд не жжём."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._last: Dict[str, float] = {}

    def may_try(self, name: str) -> bool:
        last = self._last.get(name)
        return last is None or self.clock() - last >= COOLDOWN

    def note(self, name: str) -> None:
        self._last[name] = self.clock()

    def try_reset(self, name: str, headers: dict, util: Optional[float]) -> bool:
        """Применить сброс учётке, если он того стоит. True — окно обнулено."""
        if not enabled() or not self.may_try(name):
            return False
        credits = available(headers)
        ok, why = worth_using(credits, util, self.clock())
        if not ok:
            return False
        self.note(name)
        done, said = consume(headers)
        if done:
            log.warning("Codex, %s: применён подарочный сброс (%s); подарков было %d, осталось %d",
                        name, why, len(credits), max(0, len(credits) - 1))
            return True
        log.warning("Codex, %s: подарочный сброс применить не вышло (%s)", name, said)
        return False


_KEEPER: Optional[Keeper] = None


def keeper() -> Keeper:
    global _KEEPER
    if _KEEPER is None:
        _KEEPER = Keeper()
    return _KEEPER
