"""Общий бюджет FastGen на ключ: единицы часа и потоки делятся между всеми роликами процесса.

Зачем. При нескольких роликах сразу (MANGA_PARALLEL_VIDEOS > 1) каждый покупал бы кадры сам по себе,
и час ключа кончался бы посреди пачки: раньше такой кадр сразу падал «квота часа исчерпана».
Теперь перед покупкой кадр резервирует единицы в бюджете текущего часа ключа. Нет места —
ждёт начала следующего часа UTC (квота FastGen обнуляется на границе часа), а не падает.

Откуда бюджет. Ориентир — остаток ключа по GET /api/v6/usage (бесплатный запрос): он видит и чужие
траты на том же ключе. Между опросами к последнему ответу добавляются свои покупки, которых в том
ответе ещё не было. Возврат (refunded у неудачной генерации, отказ POST) освобождает резерв.
Никаких догадок по отказам: 429 «квота» ведёт к ожиданию, только если /usage подтверждает, что час
исчерпан; иначе кадр падает с ошибкой, как раньше.

Настройки (по умолчанию выключено: один ключ, как раньше, без опросов и ожидания):
  MANGA_FASTGEN_BUDGET        on / off; без значения — включено, если задан MANGA_FASTGEN_KEYS
                              или MANGA_PARALLEL_VIDEOS > 1
  MANGA_FASTGEN_KEYS          ключи через запятую/пробел или JSON: ["k1", {"key": "k2", "limit": 6000,
                              "threads": 25, "name": "запасной"}]; без значения — один ключ из keys.json
  MANGA_FASTGEN_KEY_THREADS   потоков на ключ, по умолчанию 25 (выше 25 нельзя: предел FastGen)
  MANGA_FASTGEN_RESERVE       сколько единиц часа не трогать на каждом ключе (запас для чужих трат), 0
  MANGA_FASTGEN_USAGE_SECONDS как часто перечитывать остаток ключа, по умолчанию 60
Ключи в журнал и на страницу не попадают: только имя («ключ 1») и отпечаток (12 знаков sha256).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("manga.fastgen")
HOUR = 3600
MAX_KEY_THREADS = 25          # img_generation_threads_allowed у ключа FastGen (замер /usage 16.09.2026)
DEFAULT_LIMIT = 6000          # img_gen_per_hour_limit; только пока /usage ни разу не прочитан
UNKNOWN_UNITS = 8             # самая дорогая картинка каталога (nano-banana-pro 2x): резерв для неизвестной модели
CURRENT = None                # пул процесса, если включён (images._pool); читает страница через wait_note()


class QuotaWait(RuntimeError):
    """Час ключа исчерпан по /usage: кадр ждёт следующего часа, попытка не тратится."""


class Cancelled(RuntimeError):
    """Ожидание места прервано (остановка службы или ролик забрал другой исполнитель) до записи «отправляю»:
    покупки не было, квитанция чистая."""


# Чей это кадр: ролик и признак отмены. Ставит исполнитель очереди в потоке ролика (bind),
# generate_many переносит в свои потоки. Без привязки — как раньше: общий ролик «без имени», без отмены.
_CTX = threading.local()


def bind(tag=None, cancelled=None):
    _CTX.tag, _CTX.cancelled = tag, cancelled


def context():
    return getattr(_CTX, "tag", None), getattr(_CTX, "cancelled", None)


def _who(tag):
    return f"ролик {str(tag)[:8]}, " if tag else ""


def key_id(api_key):
    return hashlib.sha256(str(api_key).encode()).hexdigest()[:12]


def units_for(body):
    """Единицы часа за одну картинку по каталогу /models (10.09.2026); как Receipt.accepted, но с запасом
    для неизвестного: резерв не может быть «0», иначе бюджет пропустит любую покупку."""
    model = str(body.get("model") or "")
    if model.startswith("nano-banana") and not body.get("generation_config"):
        return 4
    if model == "openai-image":
        return {"fast": 4, "thinking": 6}.get(body.get("quality", "fast"), UNKNOWN_UNITS)
    return UNKNOWN_UNITS


def _positive(env, name, default, low=0):
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}: нужно целое число, получено {raw!r}") from None
    if value < low:
        raise ValueError(f"{name}: нужно число не меньше {low}, получено {raw!r}")
    return value


def parse_keys(raw):
    """[{key, name, limit, threads}] из MANGA_FASTGEN_KEYS. Ошибка — ValueError без текста ключа."""
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            items = json.loads(raw)
        except ValueError:
            raise ValueError("MANGA_FASTGEN_KEYS: JSON не читается") from None
    else:
        items = [x for x in re.split(r"[\s,;]+", raw) if x]
    out, seen = [], set()
    for i, item in enumerate(items):
        spec = {"key": item} if isinstance(item, str) else item
        if not isinstance(spec, dict) or not isinstance(spec.get("key"), str) or not spec["key"].strip():
            raise ValueError(f"MANGA_FASTGEN_KEYS: элемент {i + 1} — не ключ")
        k = spec["key"].strip()
        if k in seen:
            continue
        seen.add(k)
        for field in ("limit", "threads"):
            if spec.get(field) is not None and (type(spec[field]) is not int or spec[field] < 1):
                raise ValueError(f"MANGA_FASTGEN_KEYS: у ключа {i + 1} поле {field} — целое число от 1")
        out.append({"key": k, "name": str(spec.get("name") or f"ключ {len(out) + 1}"),
                    "limit": spec.get("limit"), "threads": spec.get("threads")})
    if not out:
        raise ValueError("MANGA_FASTGEN_KEYS: ни одного ключа")
    return out


def settings(env=None):
    """None — бюджет выключен (поведение как раньше). Иначе параметры пула; ключи — отдельно (parse_keys)."""
    env = os.environ if env is None else env
    mode = str(env.get("MANGA_FASTGEN_BUDGET", "") or "").strip().lower()
    if mode not in ("", "on", "off"):
        raise ValueError("MANGA_FASTGEN_BUDGET: on или off")
    keys = parse_keys(env.get("MANGA_FASTGEN_KEYS", ""))
    parallel = str(env.get("MANGA_PARALLEL_VIDEOS", "") or "").strip()
    auto = bool(keys) or (parallel.isdigit() and int(parallel) > 1)
    if mode == "off" or (mode == "" and not auto):
        return None
    threads = _positive(env, "MANGA_FASTGEN_KEY_THREADS", MAX_KEY_THREADS, low=1)
    return {"keys": keys, "threads": min(MAX_KEY_THREADS, threads),
            "reserve": _positive(env, "MANGA_FASTGEN_RESERVE", 0),
            "usage_every": _positive(env, "MANGA_FASTGEN_USAGE_SECONDS", 60, low=1)}


def _utc(ts):
    return time.strftime("%H:%M", time.gmtime(ts))


class _Key:
    def __init__(self, spec, threads):
        self.key = spec["key"]
        self.id = key_id(self.key)
        self.name = spec.get("name") or "ключ"
        self.limit_cfg = spec.get("limit")
        self.threads = min(MAX_KEY_THREADS, spec.get("threads") or threads)
        self.active = 0            # потоков этого ключа сейчас занято (POST и ожидание результата)
        self.usage = None          # последний прочитанный остаток: used, limit, threads, window, read_start, read_at
        self.failed_at = None      # когда остаток не прочитался в последний раз
        self.error_told = False
        self.refreshing = False
        self.res = []              # свои резервы (текущий и прошлый час)


class _Reservation:
    """Резерв единиц одной покупки. Пока покупка в пути (не принята и место не закрыто), она считается
    в текущем часе, каким бы он ни стал к ответу; принятая — в часе приёма, как её посчитает FastGen."""
    __slots__ = ("units", "window", "accepted_at", "refunded", "settled")

    def __init__(self, units, window):
        self.units, self.window, self.accepted_at, self.refunded, self.settled = units, window, None, False, False


class Lease:
    """Место на ключе: поток занят до close(); единицы резерва списаны, пока не refund()."""

    def __init__(self, pool, key, res):
        self._pool, self._key, self._res, self._open = pool, key, res, True

    @property
    def key(self):
        return self._key.key

    @property
    def key_id(self):
        return self._key.id

    @property
    def units(self):
        return self._res.units if self._res else 0

    def accepted(self):
        with self._pool.cond:
            if self._res:
                now = self._pool.clock()
                self._res.accepted_at, self._res.window, self._res.settled = now, self._pool.window(now), True

    def refund(self):
        """Провайдер не взял единицы (отказ POST, refunded у неудачной генерации): резерв освобождается."""
        with self._pool.cond:
            if self._res and not self._res.refunded:
                self._res.refunded = True
                self._pool.cond.notify_all()

    def close(self):
        with self._pool.cond:
            if self._open:
                self._open = False
                self._key.active -= 1
                if self._res and not self._res.settled:
                    # Ответ POST не получен (нет связи, 5xx): провайдер мог взять единицы — считаем их в этом часе.
                    self._res.window, self._res.settled = self._pool.window(self._pool.clock()), True
                self._pool.cond.notify_all()


class Pool:
    def __init__(self, keys, fetch, threads=MAX_KEY_THREADS, reserve=0, usage_every=60, grace=5, clock=time.time):
        """keys — [{key, name, limit, threads}]; fetch(api_key) -> (http_status, json) для GET /usage."""
        if not keys:
            raise ValueError("пул FastGen без ключей")
        self.keys = [_Key(k, threads) for k in keys]
        self.fetch = fetch
        self.reserve = reserve
        self.usage_every = usage_every
        self.grace = grace
        self.clock = clock
        self.cond = threading.Condition()
        self.waiting = {}          # поток -> (до какого времени ждёт час, ролик) — для страницы
        self.told = {}             # ролик -> час, об ожидании которого уже написано в журнал (одна строка на ролик)
        self.purchases = {k.id: 0 for k in self.keys}   # сколько резервов выдано ключу (для проверки распределения)

    # ---------- остаток ключа ----------
    @staticmethod
    def window(ts):
        return ts // HOUR * HOUR

    def _read(self, k):
        """GET /usage без замка пула. Возвращает остаток или None (причина — в журнале)."""
        started = self.clock()
        try:
            status, body = self.fetch(k.key)
        except Exception as exc:  # noqa: BLE001 — причину покажем в журнале
            status, body = 0, {"error": f"{type(exc).__name__}: {exc}"}
        now = self.clock()
        try:
            limits = body["account_limits"]
            limit = limits["img_gen_per_hour_limit"]
            threads = limits.get("img_generation_threads_allowed")
            if status != 200 or type(limit) is not int or limit <= 0:
                raise ValueError
            stats = ((body.get("current_usage") or {}).get("hourly_usage") or {}).get("image_generation") or {}
            used = stats.get("current_usage", 0) if isinstance(stats, dict) else 0
            start = stats.get("window_start") if isinstance(stats, dict) else None
            used = used if type(used) is int and used >= 0 else 0
        except (KeyError, TypeError, ValueError, AttributeError):
            why = f"HTTP {status}" + (f": {str(body.get('error'))[:120]}" if isinstance(body, dict) and body.get("error") else "")
            if not k.error_told:
                log.warning("FastGen, %s (%s): остаток часа не прочитан (%s); считаю по своим покупкам "
                            "и пределу %s, перечитаю через %s с", k.name, k.id, why,
                            k.limit_cfg or (k.usage or {}).get("limit") or DEFAULT_LIMIT, self.usage_every)
            k.error_told = True
            return None
        window = self.window(started)
        # Трата прошлого часа к этому часу не относится: окно FastGen обнуляется на границе часа.
        if isinstance(start, (int, float)) and start < window:
            used = 0
        return {"used": used, "limit": limit, "threads": threads if type(threads) is int and threads > 0 else None,
                "window": window, "read_start": started, "read_at": now}

    def _refresh(self, k):
        usage = self._read(k)
        with self.cond:
            k.refreshing = False
            if usage is None:
                k.failed_at = self.clock()
            else:
                if k.error_told:
                    log.warning("FastGen, %s (%s): остаток снова читается: израсходовано %s из %s",
                                k.name, k.id, usage["used"], usage["limit"])
                k.usage, k.failed_at, k.error_told = usage, None, False
            self.cond.notify_all()
        return usage

    def _stale(self, k, now, window):
        """Пора перечитать остаток: его нет, он из прошлого часа или старше usage_every.
        После неудачного чтения — не чаще раза в usage_every в пределах часа."""
        if k.refreshing:
            return False
        if k.usage is not None and k.usage["window"] == window and now - k.usage["read_at"] < self.usage_every:
            return False
        return not (k.failed_at is not None and k.failed_at >= window and now - k.failed_at < self.usage_every)

    def _limit(self, k):
        known = [x for x in ((k.usage or {}).get("limit"), k.limit_cfg) if x]
        return min(known) if known else DEFAULT_LIMIT

    def _threads(self, k):
        provider = (k.usage or {}).get("threads")
        return min(k.threads, provider) if provider else k.threads

    def _used(self, k, window):
        """Израсходовано в часе window: остаток по /usage плюс свои покупки, которых в том ответе ещё нет."""
        u = k.usage if k.usage is not None and k.usage["window"] == window else None
        base, cut = (u["used"], u["read_start"]) if u else (0, None)
        own = sum(r.units for r in k.res if not r.refunded and (r.window == window or not r.settled)
                  and not (cut is not None and r.accepted_at is not None and r.accepted_at < cut))
        return base + own

    def free(self, k, window=None):
        window = self.window(self.clock()) if window is None else window
        return self._limit(k) - self.reserve - self._used(k, window)

    # ---------- выдача места ----------
    def acquire(self, units, key=None):
        """Место на ключе с units свободными единицами часа (0 — только поток, для догрузки уже купленного).
        key — отпечаток ключа (кадр куплен им раньше); None — ключ с наибольшим запасом.
        Час исчерпан на всех ключах — ждём начала следующего часа UTC, с проверкой /usage."""
        me = threading.get_ident()
        tag, cancelled = context()
        try:
            while True:
                todo = []
                with self.cond:
                    if cancelled is not None and cancelled():
                        raise Cancelled("ожидание места FastGen прервано: служба останавливается или ролик "
                                        "забрал другой исполнитель; покупки не было")
                    now = self.clock()
                    window = self.window(now)
                    cands = [k for k in self.keys if key is None or k.id == key]
                    if not cands:
                        raise KeyError(key)
                    for k in cands:
                        k.res = [r for r in k.res if r.window >= window or not r.settled]
                        if units and self._stale(k, now, window):
                            k.refreshing = True
                            todo.append(k)
                    if not todo:
                        best, room = None, False
                        for k in cands:
                            free = self.free(k, window)
                            if units and (k.refreshing or free < units):
                                continue
                            room = True
                            if k.active < self._threads(k) and (best is None or free > best[1]):
                                best = (k, free)
                        if best:
                            k = best[0]
                            k.active += 1
                            res = None
                            if units:
                                res = _Reservation(units, window)
                                k.res.append(res)
                                self.purchases[k.id] += 1
                            if units and self.told.pop(tag, None) is not None:
                                log.warning("%sкартинки: квота FastGen освободилась (%s, свободно %s); продолжаем",
                                            _who(tag), k.name, best[1])
                            return Lease(self, k, res)
                        if room or any(k.refreshing for k in cands):
                            self.cond.wait(1.0)     # есть единицы, но заняты потоки ключа: ждём освобождения
                            continue
                        wake = window + HOUR + self.grace
                        if self.told.get(tag) != window:
                            self.told[tag] = window
                            left = ", ".join(f"{k.name}: свободно {max(0, self.free(k, window))} из {self._limit(k)}"
                                             for k in cands)
                            log.warning("%sкартинки: квота FastGen этого часа исчерпана (%s); новые кадры ждут "
                                        "до %s UTC, остаток проверяется раз в %s с", _who(tag), left,
                                        _utc(wake - self.grace), self.usage_every)
                        self.waiting[me] = (wake - self.grace, tag)
                        # С признаком отмены спим короткими отрезками: остановка службы не ждёт конца часа.
                        cap = self.usage_every if cancelled is None else min(self.usage_every, 1.0)
                        self._pause(max(0.0, min(wake - now, cap)))
                        continue
                for k in todo:
                    self._refresh(k)
        finally:
            with self.cond:
                self.waiting.pop(me, None)

    def _pause(self, seconds):
        """Под замком пула; подменяется в проверках вместе с clock."""
        self.cond.wait(seconds)

    def confirm_exhausted(self, lease):
        """429 «квота» на покупку. Ждать следующий час — только если /usage подтверждает: единиц не хватает.
        Остаток не прочитан или показывает свободно — False: кадр падает, как раньше."""
        k = lease._key
        units = lease.units or UNKNOWN_UNITS
        with self.cond:
            if k.refreshing:
                while k.refreshing:
                    self.cond.wait(1.0)
            k.refreshing = True
        usage = self._refresh(k)
        if usage is None:
            return False
        with self.cond:
            return self.free(k) < units

    def summary(self):
        """Остаток по всем ключам для журнала шага (images.quota): читает /usage каждого ключа."""
        for k in self.keys:
            with self.cond:
                while k.refreshing:
                    self.cond.wait(1.0)
                k.refreshing = True
            self._refresh(k)
        with self.cond:
            read = [k for k in self.keys if k.usage is not None]
            window = self.window(self.clock())
            return {"limit": sum(self._limit(k) for k in read), "threads": sum(self._threads(k) for k in read),
                    "used": sum(self._used(k, window) for k in read), "keys": len(self.keys), "read": len(read)}

    def wait_note(self, tag=None):
        """Ожидание часа: tag — только кадры этого ролика; None — любые."""
        with self.cond:
            mine = [until for until, who in self.waiting.values() if tag is None or who == tag]
            if not mine:
                return None
            until = min(mine)
        return f"ждём квоту FastGen до {_utc(until)} UTC: единицы этого часа на всех ключах израсходованы"


def wait_note(tag=None):
    pool = CURRENT
    return pool.wait_note(tag) if pool is not None else None
