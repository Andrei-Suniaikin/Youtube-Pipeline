#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Журнал платных вызовов провайдеров — уровнем ниже журнала выпусков.

Зачем отдельная таблица. Существующий журнал (`pub_release_jobs`) хранит строку на ЭТАП:
`UNIQUE(release_id, service, kind)`. Внутри одного этапа «картинки» при медиане 155 кадров
происходит 155 отдельных платных вызовов, про которые этот журнал не знает ничего.

Что защищает их сегодня — пять независимых файловых кэшей внутри движков (по подсчёту
разбора: картинки 54 места, рендер 18, Lumean 7, Voicer 23, VGC 2) плюс правило «повтор
только на явный 429» в озвучке. Файловый кэш закрывает случай «файл уже скачан» и не
закрывает окно между «провайдер принял заказ» и «файл скачан»: рестарт в этот момент
платит второй раз. Симптом виден в живом коде — 03.09 все таймауты картинок подняты ×10
с комментарием, что оплаченные кадры снимались собственным таймером.

Правило №4 ТЗ: платный вызов повторяется ТОЛЬКО когда доказано, что он не состоялся.
Доказательство — строка в этой таблице.

Порядок работы (важен именно такой):

    1. reserve()          — ДО обращения к провайдеру. Строка появляется раньше денег.
    2. mark_ordered()     — как только известен идентификатор заказа.
    3. mark_delivered()   — когда результат лежит на диске и открывается.

Если между 1 и 2 процесс умер, при следующем reserve() вернётся состояние `reserved`:
это НЕ разрешение повторить, а требование сходить к провайдеру и выяснить судьбу заказа.
Единственное состояние, разрешающее новую попытку, — `failed`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import time

# Состояния строки. Порядок отражает жизненный цикл.
NEW = "new"                 # строки не было, вызов можно делать
RESERVED = "reserved"       # заявка записана, судьба заказа неизвестна
ORDERED = "ordered"         # заказ у провайдера создан — деньги, возможно, потрачены
DELIVERED = "delivered"     # результат получен и лежит на диске
FAILED = "failed"           # провайдер отказал ДО создания заказа — повтор безопасен

# Что делать вызывающему, получив существующую строку.
SKIP = "skip"               # ничего не делать, результат уже есть
VERIFY = "verify"           # спросить провайдера про существующий заказ, НЕ создавать новый
RETRY = "retry"             # прошлая попытка не дошла до заказа — можно пробовать снова

_ACTION = {DELIVERED: SKIP, ORDERED: VERIFY, RESERVED: VERIFY, FAILED: RETRY}


def ensure_calls_schema(c: sqlite3.Connection) -> None:
    """Создать таблицу журнала. Идемпотентно."""
    c.execute(
        "CREATE TABLE IF NOT EXISTS provider_calls("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "tenant_id TEXT NOT NULL, "
        # Проект, а не выпуск: выпуск есть только у автоконвейера, проект — всегда.
        "project_id TEXT NOT NULL, "
        "release_id TEXT NOT NULL DEFAULT '', "
        "kind TEXT NOT NULL, "              # images | clip | tts | cover
        "unit TEXT NOT NULL, "              # номер сцены, кусок текста, язык обложки
        "fingerprint TEXT NOT NULL, "       # хэш входа: модель + промпт + параметры
        "provider TEXT NOT NULL DEFAULT '', "
        "model TEXT NOT NULL DEFAULT '', "
        "remote_order_id TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL, "
        "result_path TEXT NOT NULL DEFAULT '', "
        "cost REAL, "
        "attempts INTEGER NOT NULL DEFAULT 0, "
        "error TEXT NOT NULL DEFAULT '', "
        "created REAL NOT NULL, updated REAL NOT NULL, "
        # Ядро защиты: тот же вход в том же проекте нельзя заказать дважды.
        "UNIQUE(project_id, kind, unit, fingerprint))"
    )
    # Экран выпуска и подсчёт стоимости ходят по проекту; журнал — по времени.
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_project "
              "ON provider_calls(project_id, kind, updated)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_release "
              "ON provider_calls(release_id, status)")
    # Поиск незавершённых при восстановлении после рестарта.
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_open "
              "ON provider_calls(status, updated)")
    ensure_quota_schema(c)


def fingerprint(**parts) -> str:
    """Отпечаток входа вызова.

    Всё, что влияет на РЕЗУЛЬТАТ и на ЦЕНУ, должно попасть сюда: модель, промпт,
    seed, соотношение сторон, референсы. Что не влияет — не должно, иначе один и тот же
    кадр будет считаться разным и оплачиваться заново. Пустые значения отбрасываются,
    чтобы добавление необязательного параметра не обесценивало старые строки.
    """
    clean = {k: v for k, v in parts.items() if v not in (None, "", [], {})}
    blob = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def reserve(c: sqlite3.Connection, *, tenant_id: str, project_id: str, kind: str,
            unit: str, fp: str, provider: str = "", model: str = "",
            release_id: str = "", now: float | None = None) -> dict:
    """Заявить вызов ДО обращения к провайдеру.

    Возвращает {"action": …, "status": …, "call_id": …, "row": …}:

      action=new     строки не было — вызов делать можно
      action=skip    результат уже получен — вызов делать НЕЛЬЗЯ, взять result_path
      action=verify  заказ уже создавался (или судьба неизвестна) — спросить провайдера
                     про remote_order_id, НЕ создавать второй
      action=retry   прошлая попытка отвалилась до создания заказа — можно пробовать

    Гонка двух воркеров разрешается уникальным индексом: проигравший получит `verify`,
    а не вторую заявку.
    """
    now = time.time() if now is None else now
    cur = c.execute(
        "INSERT OR IGNORE INTO provider_calls(tenant_id,project_id,release_id,kind,unit,"
        "fingerprint,provider,model,status,attempts,created,updated) "
        "VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
        (tenant_id, project_id, release_id, kind, unit, fp, provider, model,
         RESERVED, now, now))
    row = c.execute(
        "SELECT * FROM provider_calls WHERE project_id=? AND kind=? AND unit=? "
        "AND fingerprint=?", (project_id, kind, unit, fp)).fetchone()
    if cur.rowcount == 1:
        return {"action": "new", "status": RESERVED, "call_id": row["id"], "row": row}

    status = row["status"]
    action = _ACTION.get(status, VERIFY)
    if action == RETRY:
        # Прошлая попытка не дошла до заказа — открываем строку заново, считая попытку.
        c.execute("UPDATE provider_calls SET status=?, error='', attempts=attempts+1, "
                  "updated=? WHERE id=?", (RESERVED, now, row["id"]))
        row = c.execute("SELECT * FROM provider_calls WHERE id=?", (row["id"],)).fetchone()
        return {"action": "new", "status": RESERVED, "call_id": row["id"], "row": row}
    return {"action": action, "status": status, "call_id": row["id"], "row": row}


def mark_ordered(c: sqlite3.Connection, call_id: int, remote_order_id: str,
                 now: float | None = None) -> None:
    """Заказ создан. С этого момента повтор запрещён — только проверка существующего."""
    now = time.time() if now is None else now
    c.execute("UPDATE provider_calls SET status=?, remote_order_id=?, updated=? "
              "WHERE id=?", (ORDERED, str(remote_order_id or ""), now, call_id))


def mark_delivered(c: sqlite3.Connection, call_id: int, result_path: str,
                   cost: float | None = None, now: float | None = None) -> None:
    """Результат получен. `result_path` обязателен: «готово» без файла — не готово."""
    if not result_path:
        raise ValueError("mark_delivered без result_path: «готово» доказывается файлом")
    now = time.time() if now is None else now
    c.execute("UPDATE provider_calls SET status=?, result_path=?, cost=?, error='', "
              "updated=? WHERE id=?", (DELIVERED, result_path, cost, now, call_id))


def mark_failed(c: sqlite3.Connection, call_id: int, error: str,
                ordered: bool = False, now: float | None = None) -> None:
    """Вызов не удался.

    `ordered=False` — провайдер отказал ДО создания заказа (документированный 429,
    отказ валидации): денег нет, повтор безопасен, строка уходит в `failed`.

    `ordered=True` — заказ создан, но результат не получен: обрыв, таймаут, 5xx.
    Это НЕОДНОЗНАЧНО, и строка остаётся в `ordered`, чтобы следующая попытка пошла
    проверять заказ, а не создавать второй. Ровно та разница, из-за которой в озвучке
    повтор разрешён только на явный 429.
    """
    now = time.time() if now is None else now
    if ordered:
        c.execute("UPDATE provider_calls SET error=?, updated=? WHERE id=?",
                  (str(error or "")[:500], now, call_id))
        return
    c.execute("UPDATE provider_calls SET status=?, error=?, updated=? WHERE id=?",
              (FAILED, str(error or "")[:500], now, call_id))


def open_calls(c: sqlite3.Connection, project_id: str) -> list:
    """Вызовы проекта, судьба которых не выяснена. Это и есть список «сходить спросить»."""
    return c.execute(
        "SELECT * FROM provider_calls WHERE project_id=? AND status IN (?,?) "
        "ORDER BY id", (project_id, RESERVED, ORDERED)).fetchall()


def project_summary(c: sqlite3.Connection, project_id: str) -> dict:
    """Сводка по проекту: сколько вызовов в каком состоянии и сколько потрачено.

    Отдаёт то, чего просит ТЗ §12 — «до запуска видно примерную стоимость»: колонка
    `cost` заполняется по факту, и сумма по проекту становится наблюдаемой величиной,
    а не оценкой.
    """
    out = {"by_status": {}, "calls": 0, "cost": 0.0}
    for r in c.execute(
            "SELECT status, COUNT(*) n, COALESCE(SUM(cost),0) c FROM provider_calls "
            "WHERE project_id=? GROUP BY status", (project_id,)):
        out["by_status"][r["status"]] = r["n"]
        out["calls"] += r["n"]
        out["cost"] += float(r["c"] or 0)
    return out


# ──────────────────── квота YouTube Data API как общий ресурс ────────────────────
#
# Деньги — не единственный исчерпаемый ресурс вызова. Ключ YouTube Data API у нас ОДИН
# на всё: поздняя проверка выхода (`pub_verify`) и ночной обход конкурентов таблицы ВВ2
# берут единицы из одного суточного кошелька, и никто из них не знает про другого.
#
# 18.09 в 01:00 это стоило ночи: прогон пула упал на первом же 403 по квоте, весь
# остаток конкурентов получил «не обработан этой ночью» (`ww2_pool.py:739-743`),
# догоняли руками в 10:09. У нас тот же 403 был неотличим от любой другой ошибки —
# то есть выглядел как проблема ролика, хотя ролик тут ни при чём.
#
# Поэтому квота учитывается здесь, рядом с платными вызовами, по тем же правилам:
# строка расхода на потребителя, суточный бюджет и честное ожидание сброса.
#
# Таблица заводится в этом модуле, а не в `pub_schema`, по той же причине, что и
# `provider_calls`: это учёт ОДНОГО вызова, а не выпуска, и живёт он вместе со своей
# логикой. `ensure_calls_schema` зовут при старте (`main.py:2128`), так что таблица
# появляется сама, без отдельной миграции.

QUOTA_YT = "youtube"                 # имя кошелька: один ключ — один кошелёк
QUOTA_WAIT_KEY = "api_quota_wait"    # admin_state: активное ожидание сброса

# Google считает не запросы, а «единицы», и цены очень разные. По коду мы сегодня
# делаем ровно один вид вызова — `videos.list` (`pub_verify.API`, part=status,snippet),
# это 1 единица. Ночной обход ВВ2 тем же ключом делает `channels.list` (1),
# `playlistItems.list` (1) и `videos.list` (1) — поиска там нет.
# `search.list` внесён заранее и стоит 100: один необдуманный поиск съедает сто
# проверок выхода, и увидеть это надо в тот же день, а не в конце суток.
QUOTA_COSTS = {
    "videos.list": 1,
    "channels.list": 1,
    "playlistItems.list": 1,
    "captions.list": 50,
    "search.list": 100,
}

# Суточный бюджет. 10 000 единиц — это то, что Google даёт проекту по умолчанию,
# без заявки на расширение. Настройкой, а не константой, потому что бюджет — свойство
# ЧУЖОГО проекта в Google Cloud: владелец может попросить больше (или получить меньше
# после аудита), и тогда цифру меняют в окружении, а не в релизе.
YT_DAILY_UNITS = int(float(os.environ.get("VS_YT_QUOTA_UNITS") or 10000))

# Сколько ждать после «слишком часто» (rateLimitExceeded). Это НЕ суточная квота:
# короткий предел на частоту, он отпускает сам через минуты. Ждать до полуночи из-за
# него — потерять день зря, поэтому пауза своя и короткая.
YT_RATE_WAIT_SEC = int(float(os.environ.get("VS_YT_RATE_WAIT_SEC") or 300))

# Google сбрасывает суточную квоту в ПОЛНОЧЬ ПО ТИХООКЕАНСКОМУ ВРЕМЕНИ (Pacific Time),
# а не в полночь UTC и не в нашу. Зона переходит на летнее время (PST=UTC−8, PDT=UTC−7),
# поэтому считаем через zoneinfo, а не фиксированным сдвигом: фиксированный сдвиг дважды
# в год ошибался бы на час, и мы бы либо ждали лишний час впустую, либо начинали тратить
# до сброса и снова получали 403.
QUOTA_TZ = "America/Los_Angeles"


def _quota_zone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(QUOTA_TZ)
    except Exception:                                              # noqa: BLE001
        # Без базы зон (урезанный контейнер) берём зимний сдвиг. Хуже, чем zoneinfo:
        # летом сутки съедут на час. Но это лучше, чем считать сброс по UTC.
        return dt.timezone(dt.timedelta(hours=-8))


def ensure_quota_schema(c: sqlite3.Connection) -> None:
    """Расход квоты: строка на «кошелёк + сутки + потребитель». Идемпотентно.

    Потребитель — не проект и не выпуск, а ИМЯ РАБОТЫ («verify», «donor», «ww2»):
    вопрос, на который таблица отвечает, звучит «кто выел общий ключ сегодня».
    Растёт она медленно: несколько строк в сутки.
    """
    c.execute(
        "CREATE TABLE IF NOT EXISTS api_quota_usage("
        "api TEXT NOT NULL, "               # youtube
        "day TEXT NOT NULL, "               # сутки квоты по тихоокеанскому времени
        "consumer TEXT NOT NULL, "          # verify | donor | ww2 | …
        "units INTEGER NOT NULL DEFAULT 0, "
        "calls INTEGER NOT NULL DEFAULT 0, "
        "updated REAL NOT NULL, "
        "PRIMARY KEY(api, day, consumer))")


def quota_day(now: float | None = None) -> str:
    """Какие сейчас «квотные сутки» — дата по тихоокеанскому времени, YYYY-MM-DD."""
    now = time.time() if now is None else now
    return dt.datetime.fromtimestamp(now, _quota_zone()).strftime("%Y-%m-%d")


def quota_reset_at(now: float | None = None) -> float:
    """Момент ближайшего сброса: ближайшая полночь по тихоокеанскому времени."""
    now = time.time() if now is None else now
    zone = _quota_zone()
    local = dt.datetime.fromtimestamp(now, zone)
    nxt = (local + dt.timedelta(days=1)).date()
    return dt.datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone).timestamp()


def human_left(seconds: float) -> str:
    """«6 ч 40 мин» — ожидание словами. Сухие секунды человек не читает."""
    sec = max(0, int(seconds))
    if sec < 60:
        return "меньше минуты"
    h, m = divmod(sec // 60, 60)
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин"


def quota_spend(c: sqlite3.Connection, consumer: str, units: int = 1,
                api: str = QUOTA_YT, now: float | None = None) -> int:
    """Записать расход. Возвращает, сколько всего потрачено за эти сутки.

    Зовётся ПОСЛЕ ответа провайдера: Google списывает единицы за состоявшийся запрос,
    а не за намерение. Локальный отказ (нет ключа, нет video_id) квоты не стоит, и
    приписывать его сюда — врать самим себе о том, сколько осталось.
    """
    now = time.time() if now is None else now
    ensure_quota_schema(c)
    day = quota_day(now)
    c.execute(
        "INSERT INTO api_quota_usage(api,day,consumer,units,calls,updated) "
        "VALUES(?,?,?,?,1,?) ON CONFLICT(api,day,consumer) DO UPDATE SET "
        "units=units+excluded.units, calls=calls+1, updated=excluded.updated",
        (api, day, str(consumer or "?")[:40], int(units), now))
    return quota_used(c, api, now)


def quota_used(c: sqlite3.Connection, api: str = QUOTA_YT,
               now: float | None = None) -> int:
    """Сколько единиц этого ключа мы насчитали за текущие квотные сутки."""
    now = time.time() if now is None else now
    try:
        row = c.execute(
            "SELECT COALESCE(SUM(units),0) FROM api_quota_usage WHERE api=? AND day=?",
            (api, quota_day(now))).fetchone()
    except Exception:                                              # noqa: BLE001
        return 0          # таблицы ещё нет — считать нечего, но и падать не за что
    return int(row[0] or 0)


def quota_usage(c: sqlite3.Connection, api: str = QUOTA_YT,
                now: float | None = None) -> list:
    """Кто сколько потратил за сутки: [{consumer, units, calls}], самый жадный первым."""
    now = time.time() if now is None else now
    try:
        rows = c.execute(
            "SELECT consumer, units, calls FROM api_quota_usage WHERE api=? AND day=? "
            "ORDER BY units DESC, consumer", (api, quota_day(now))).fetchall()
    except Exception:                                              # noqa: BLE001
        return []
    return [{"consumer": r[0], "units": int(r[1] or 0), "calls": int(r[2] or 0)}
            for r in rows]


def quota_wait(c: sqlite3.Connection, api: str = QUOTA_YT,
               now: float | None = None) -> dict:
    """Активное ожидание сброса или {} — единственный вопрос перед вызовом.

    Запись сама протухает: по времени `until` и по смене квотных суток. Второе важнее
    первого — если часы машины уедут, сутки всё равно сменятся.
    """
    now = time.time() if now is None else now
    try:
        data = json.loads(_state_raw(c, QUOTA_WAIT_KEY) or "{}")
        rec = (data or {}).get(api) or {}
    except (ValueError, TypeError, AttributeError):
        return {}
    if not rec:
        return {}
    if now >= float(rec.get("until") or 0) or rec.get("day") != quota_day(now):
        return {}
    rec = dict(rec)
    rec["left_sec"] = max(0.0, float(rec["until"]) - now)
    return rec


def quota_block(c: sqlite3.Connection, reason: str, api: str = QUOTA_YT,
                kind: str = "daily", now: float | None = None,
                until: float | None = None) -> dict:
    """Отметить «единицы кончились, ждём сброса».

    Возвращает запись с признаком `fresh`: True только когда ожидание НАЧАЛОСЬ сейчас.
    По нему вызывающий и решает, писать ли строку в журнал — причина ожидания нужна
    один раз, а не на каждом тридцатисекундном проходе; иначе журнал выпуска за ночь
    состоит из одной и той же фразы и перестаёт отвечать на вопрос «что было до того».
    """
    now = time.time() if now is None else now
    was = quota_wait(c, api, now)
    if until is None:
        until = (now + YT_RATE_WAIT_SEC) if kind == "rate" else quota_reset_at(now)
    if was:
        # Уже ждём — срок не продлеваем и не укорачиваем: сброс назначает Google,
        # а не частота наших попыток.
        return dict(was, fresh=False)
    try:
        data = json.loads(_state_raw(c, QUOTA_WAIT_KEY) or "{}")
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    rec = {"since": now, "until": float(until), "kind": kind,
           "day": quota_day(now), "reason": str(reason or "")[:300],
           "used_here": quota_used(c, api, now), "budget": YT_DAILY_UNITS}
    data[api] = rec
    try:
        c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
                  (QUOTA_WAIT_KEY, json.dumps(data, ensure_ascii=False), now))
    except Exception:                                              # noqa: BLE001
        # Запомнить ожидание не вышло — причину всё равно возвращаем: вызывающий
        # должен остановить проход сейчас. Хуже, чем с записью (следующий проход
        # снова сходит за 403), но лучше, чем исключение из проверки публикаций.
        pass
    return dict(rec, fresh=True, left_sec=max(0.0, float(until) - now))


def quota_allow(c: sqlite3.Connection, consumer: str, units: int = 1,
                api: str = QUOTA_YT, now: float | None = None,
                budget: int | None = None) -> dict:
    """Можно ли сейчас тратить: {"ok", "why", "wait", "used", "left"}.

    Два разных «нельзя», и оба означают ПОДОЖДАТЬ, а не «сломалось»:
      * активное ожидание после 403 — провайдер уже сказал, что единиц нет;
      * наш собственный счётчик упёрся в бюджет — идти за 403 незачем.

    Второй случай не отменяет первого: ключ общий, и чужой расход наш счётчик не видит,
    поэтому 403 может прийти задолго до того, как наша сумма дойдёт до бюджета.
    """
    now = time.time() if now is None else now
    budget = YT_DAILY_UNITS if budget is None else budget
    wait = quota_wait(c, api, now)
    used = quota_used(c, api, now)
    if wait:
        return {"ok": False, "why": quota_why(wait), "wait": wait,
                "used": used, "left": max(0, budget - used)}
    if used + int(units) > budget:
        return {"ok": False, "used": used, "left": max(0, budget - used), "wait": {},
                "why": (f"свой суточный бюджет YouTube Data API исчерпан: "
                        f"{used} из {budget} единиц. Ждём сброса через "
                        f"{human_left(quota_reset_at(now) - now)} "
                        f"(полночь по тихоокеанскому времени)")}
    return {"ok": True, "why": "", "wait": {}, "used": used, "left": budget - used}


def quota_why(wait: dict) -> str:
    """Причина ожидания словами — то, что увидит владелец вместо кода 403."""
    if not wait:
        return ""
    left = human_left(wait.get("left_sec") or 0)
    if wait.get("kind") == "rate":
        return f"YouTube просит сбавить частоту запросов. Ждём {left}"
    used, budget = int(wait.get("used_here") or 0), int(wait.get("budget") or 0)
    tail = ""
    if budget and used < budget:
        # Наш счётчик далеко от бюджета, а единиц уже нет. Два числа — это улика, но не
        # приговор, и называть виноватым «другого потребителя» как факт нельзя: `budget`
        # — ЭТО НАША СОБСТВЕННАЯ НАСТРОЙКА (VS_YT_QUOTA_UNITS, умолчание 10000), а не
        # то, что Google выдал проекту. Если реальная квота проекта ниже умолчания, наш
        # расход честно меньше бюджета и при этом виноваты мы сами. Поэтому владельцу
        # называются обе возможности — обе проверяемые, и обе ведут к делу.
        tail = (f". Наш расход за сутки — {used} из {budget} единиц: либо единицы "
                f"выел другой потребитель того же ключа, либо бюджет "
                f"VS_YT_QUOTA_UNITS не совпадает с реальной квотой проекта")
    return (f"квота YouTube Data API исчерпана: ждём сброса через {left} "
            f"(полночь по тихоокеанскому времени){tail}")


def quota_state(c: sqlite3.Connection, api: str = QUOTA_YT,
                now: float | None = None) -> dict:
    """Всё про кошелёк одним куском — для экрана и для отчёта прохода."""
    now = time.time() if now is None else now
    wait = quota_wait(c, api, now)
    used = quota_used(c, api, now)
    return {"day": quota_day(now), "budget": YT_DAILY_UNITS, "used": used,
            "left": max(0, YT_DAILY_UNITS - used), "by_consumer": quota_usage(c, api, now),
            "reset_at": quota_reset_at(now), "waiting": bool(wait), "wait": wait,
            "why": quota_why(wait)}


# ─────────────────────────── свободное место на нодах ───────────────────────────

# Тревога про диск смотрела только на сервер. Рендер и заливка идут на другой машине,
# и про её место сервер не знал ничего — а «мало места» без указания машины посылает
# человека проверять не тот компьютер.
NODE_DISK_KEY = "node_disk"
# Через сколько молчания сведения о машине считаются протухшими. Ноды опрашивают
# очередь раз в двадцать секунд, так что четверть часа тишины — это уже тишина.
NODE_DISK_STALE = 900


def note_node_disk(c: sqlite3.Connection, host: str, role: str, free_gb, total_gb,
                   now: float | None = None) -> None:
    """Запомнить, сколько места на машине. Молча игнорирует неполные сведения:
    старый пуллер их не шлёт, и это не повод падать."""
    if not host or free_gb is None:
        return
    now = time.time() if now is None else now
    try:
        data = json.loads(_state_raw(c, NODE_DISK_KEY) or "{}")
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    data[str(host)[:40]] = {"role": str(role or "")[:20], "at": now,
                            "free_gb": float(free_gb),
                            "total_gb": float(total_gb or 0)}
    c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
              (NODE_DISK_KEY, json.dumps(data, ensure_ascii=False), now))


def node_disk(c: sqlite3.Connection, now: float | None = None) -> list:
    """Что известно про машины: [{host, role, free_gb, total_gb, age_sec, stale}]."""
    now = time.time() if now is None else now
    try:
        data = json.loads(_state_raw(c, NODE_DISK_KEY) or "{}")
    except ValueError:
        return []
    out = []
    for host, rec in sorted((data or {}).items()):
        if not isinstance(rec, dict):
            continue
        age = now - float(rec.get("at") or 0)
        out.append({"host": host, "role": rec.get("role") or "",
                    "free_gb": rec.get("free_gb"), "total_gb": rec.get("total_gb"),
                    "age_sec": int(age), "stale": age > NODE_DISK_STALE})
    return out


def _state_raw(c: sqlite3.Connection, key: str):
    try:
        row = c.execute("SELECT v FROM admin_state WHERE k=?", (key,)).fetchone()
    except Exception:                                              # noqa: BLE001
        return None
    return row[0] if row else None
