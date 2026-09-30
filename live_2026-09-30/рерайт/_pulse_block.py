
# ============== ЖИВАЯ ОЧЕРЕДЬ (виджет) ==============
# Наружу уходят ТОЛЬКО целые числа-агрегаты и собственные числа юзера:
# ни логинов, ни user_id, ни названий задач, ни чужих ошибок.
# Ошибка БД → HTTP 200 с degraded:true и нулями, а не 500: виджет не должен
# ронять кабинет. Путь /api/queue/pulse закрыт auth_middleware (его нет в
# public_paths и он не начинается на /api/v1/).
PULSE_PEOPLE_MODE = "exact"  # exact | bucket — подпись считает сервер, не клиент
_PULSE_CACHE_TTL = 3.0
_PULSE_CACHE: dict = {"ts": 0.0, "rows": []}
_PULSE_CACHE_LOCK = threading.Lock()


def _pulse_people_label(people: int) -> str:
    """Готовая подпись числа людей. Огрубление переключается одной константой."""
    if PULSE_PEOPLE_MODE != "bucket":
        return str(people)
    if people <= 1:
        return str(max(0, people))
    if people <= 3:
        return "2-3"
    if people <= 9:
        return "4-9"
    return "10+"


def _pulse_rows() -> tuple:
    """Глобальный снимок активных строк: один запрос на процесс, TTL 3 c.

    Персональная часть считается из этого же снимка — отдельного запроса
    на юзера не делаем (при 5-секундном опросе вкладок это был бы шум).
    """
    now = time.time()
    with _PULSE_CACHE_LOCK:
        age = now - _PULSE_CACHE["ts"]
        if age < _PULSE_CACHE_TTL:
            return _PULSE_CACHE["rows"], age
        try:
            rows = queue.queue_pulse()
        except Exception:
            # БД недоступна: ts всё равно двигаем, иначе в аварии каждый запрос
            # виджета ломится в неё заново — ровно тогда, когда ей и так плохо.
            _PULSE_CACHE["ts"] = now
            _PULSE_CACHE["rows"] = []
            raise
        _PULSE_CACHE["ts"] = now
        _PULSE_CACHE["rows"] = rows
        return rows, 0.0


@app.get("/api/queue/pulse")
def queue_pulse(request: Request):
    """Живая очередь: глобальные агрегаты (все тенанты) + свои числа."""
    user_id = _get_user_id(request)
    waiting = running = people = 0
    mine_waiting = mine_running = 0
    cache_age = 0.0
    degraded = False
    try:
        rows, cache_age = _pulse_rows()
        actors = set()
        for status, row_user in rows:
            if status == JobStatus.RUNNING.value:
                running += 1
                if row_user == user_id:
                    mine_running += 1
            else:
                # pending + resetting: слот очереди занят, юзер ждёт.
                waiting += 1
                if row_user == user_id:
                    mine_waiting += 1
            if row_user:
                # Пустой user_id (legacy-строки) — не человек, в COUNT DISTINCT не идёт.
                actors.add(row_user)
        people = len(actors)
    except Exception:
        degraded = True
    return {
        "service": "rewrite",
        "ts": time.time(),
        "cache_age": round(cache_age, 2),
        # Механизма админской паузы очереди в rewrite нет → всегда false.
        "paused": False,
        "paused_reason": "",
        "totals": {
            "waiting": waiting,
            "running": running,
            "people": None if PULSE_PEOPLE_MODE == "bucket" else people,
            "people_label": _pulse_people_label(people),
        },
        "mine": {
            "waiting": mine_waiting,
            "running": mine_running,
            # Минимальный вариант: ключи присутствуют, значения всегда пустые
            # (фронт один на три кабинета и не должен знать, где что есть).
            "ahead": None,
            "eta_sec": None,
            "eta_approx": True,
        },
        "lanes": [],
        "health": {"worker": "ok", "note": ""},
        "degraded": degraded,
    }

