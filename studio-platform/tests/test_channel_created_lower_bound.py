#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Нижняя граница календаря — момент завода канала (пункт 2 владельца, 22.09.2026, фаза 2).

Свежий канал не должен показывать «пропущено» за время, когда его ещё не было. До
фикса `past_slots()` сочиняла слоты назад от «сейчас» на всю глубину истории
(`history`, до 90 суток), не глядя на `pub_channels.created` — канал, заведённый два
дня назад, получал десятки лет фиктивных «пропусков» задним числом.

Проверяется здесь:
  1) `_calendar()`/`past_slots()`: ни одного ряда (тем более `missed`) раньше `created`,
     при любом `history`, включая `history=90` на канале младше трёх суток (AC-4);
  2) `by_day`/`counts` из настоящего ответа `GET /ui/channels/{id}` (вызванного как
     функция, без сети) не содержат дат раньше даты завода (AC-5);
  3) регрессия: `pub_alerts.check_missed_slots` не поднимает `slot_missed`/`slot_empty`
     для свежего канала без единой строки плана — по коду видно, что повод читает
     только `pub_releases`/`pub_plan_items`, `pub_channels` он не трогает вовсе, но
     триаж просит доказать это тестом, а не поверить чтению (AC-6);
  4) канал без расписания (`schedule={}`) — `_calendar` как и раньше отдаёт `[]`,
     граница по created его не задевает (Edge Case #4);
  5) `created` в будущем (рассинхрон часов) — `past_slots` не падает, история просто
     пуста, как и «истории ещё нет» (Edge Case #5).

Сети и стенда не требует: своя временная база на диске (`db()` пульта открывает файл
по пути, а не абстрактное соединение), схема — `ensure_pub_schema`, та же функция, что
и в бою.

    .venv/bin/python tests/test_channel_created_lower_bound.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "server"))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


DAY = 86400.0


def main() -> int:
    work = tempfile.mkdtemp(prefix="channel-created-")
    db_path = os.path.join(work, "vsgen.db")
    os.environ["VS_DEV_DB"] = db_path
    os.environ.setdefault("VS_DEV_TENANT", "dev")
    # Пульт сам вставляет server/ в sys.path при импорте — но схему готовим до него,
    # тем же файлом, который откроет db().
    from pub_schema import ensure_pub_schema                          # noqa: PLC0415
    c0 = sqlite3.connect(db_path)
    c0.row_factory = sqlite3.Row
    ensure_pub_schema(c0)
    c0.commit()
    c0.close()

    import web.app as pult                                            # noqa: PLC0415
    import pub_ui as U                                                 # noqa: PLC0415
    import pub_alerts as A                                             # noqa: PLC0415

    def conn():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    def add_channel(created, schedule, tz="UTC", horizon_days=3):
        c = conn()
        try:
            cid = U.create_channel(c, "dev", "", "Свежий канал", now=created)
            c.execute("UPDATE pub_channels SET schedule=?, tz=?, horizon_days=? WHERE id=?",
                      (json.dumps(schedule), tz, horizon_days, cid))
            c.commit()
            ch = c.execute("SELECT * FROM pub_channels_live WHERE id=?", (cid,)).fetchone()
            return cid, dict(ch)
        finally:
            c.close()

    print("Календарь свежего канала: ни одного ряда раньше created")
    now = time.time()
    created = now - 2 * DAY - 3600           # канал заведён двое суток и час назад
    schedule = {"*": ["09:00", "21:00"]}      # два слота в сутки — за 90 суток набежало бы 178
    cid, ch = add_channel(created, schedule)

    c = conn()
    try:
        cal = pult._calendar(c, ch, 90)         # history=90 — заведомо больше жизни канала
    finally:
        c.close()

    check("календарь не пуст — слоты после created есть", len(cal) > 0, True)
    # Секунда запаса: `_calendar` берёт «сейчас» повторно между вызовами, точный
    # момент завода мог оказаться на границе своего же слота.
    early = [r for r in cal if r["at"] < created - 1]
    check("ни одного ряда раньше created", early, [])
    missed_early = [r for r in cal if r["state"] == "missed" and r["at"] < created]
    check("и тем более ни одного «пропуска» раньше created", missed_early, [])

    print("\n  history=90 на канале младше трёх суток — не 90 суток пропусков")
    past_rows = [r for r in cal if r["past"] and not r["beyond_horizon"]]
    span_days = (now - created) / DAY
    # Мутация-проверка: убери `created=ch["created"]` из вызова `past_slots` внутри
    # `_calendar` (web/app.py) — эта проверка первой поймает разницу: прошлых рядов
    # станет ~178 (90 суток × 2 слота) вместо ~4-5.
    check("прошлых рядов — по возрасту канала, а не по глубине history",
          len(past_rows) <= int(span_days) + 3, True)
    check("и заведомо меньше того, что дал бы полный history=90 без границы",
          len(past_rows) < 90 * 2, True)

    print("\n  тот же контроль через историю в 2 дня — регрессия FR-6 не зависит от history")
    c = conn()
    try:
        cal14 = pult._calendar(c, ch, 2)
    finally:
        c.close()
    check("короткий history и длинный history сходятся — created режет оба одинаково",
          sorted(r["at"] for r in cal14 if r["past"]),
          sorted(r["at"] for r in past_rows))

    print("\nby_day и counts — из настоящего ответа GET /ui/channels/{id} (без сети)")
    d = pult.ui_channel(cid, history=90)
    resp_early = [r for r in d["calendar"] if r["at"] < created - 1]
    check("в ответе ручки тоже нет рядов раньше created", resp_early, [])
    # Пропуски ПОСЛЕ created — законные (слот прошёл, ролика нет), их и должно быть
    # ровно столько, сколько прошлых рядов у канала без единой ссылки в плане; тест
    # проверяет не «пропусков нет», а «их не больше, чем прожил канал».
    check("счётчик 'missed' — по прожитым слотам, а не по всей глубине history",
          d["counts"].get("missed", 0), len(past_rows))

    import datetime as dt
    import zoneinfo
    created_day = dt.datetime.fromtimestamp(created, zoneinfo.ZoneInfo("UTC")) \
                              .strftime("%Y-%m-%d")
    early_days = [day for day in d["by_day"] if day < created_day]
    check("by_day не содержит дат раньше даты завода канала", early_days, [])

    print("\nРегрессия (AC-6): свежий канал без единой строки плана — "
          "check_missed_slots молчит")
    c = conn()
    try:
        alerts = A.check_missed_slots(c, now, tenant="dev")
    finally:
        c.close()
    # Мутация-проверка: если бы `check_missed_slots` когда-нибудь начал читать
    # синтетические слоты `_calendar`/`past_slots` вместо настоящих строк
    # `pub_releases`/`pub_plan_items`, эта проверка перестала бы молчать —
    # у канала выше сейчас есть «пропущенные» слоты в расписании, но ни одной
    # реальной строки плана.
    check("поводов нет — ни slot_missed, ни slot_empty",
          [a.key for a in alerts], [])

    print("\nEdge Case #4: канал без расписания — календарь по-прежнему пуст")
    cid2, ch2 = add_channel(created, {})
    c = conn()
    try:
        cal2 = pult._calendar(c, ch2, 90)
    finally:
        c.close()
    check("schedule={} — календарь [] независимо от границы по created", cal2, [])

    print("\nEdge Case #5: created в будущем (рассинхрон часов) — не падает, история пуста")
    cid3, ch3 = add_channel(now + 10 * DAY, schedule)
    c = conn()
    try:
        cal3 = pult._calendar(c, ch3, 90)
    finally:
        c.close()
    past3 = [r for r in cal3 if r["past"]]
    check("прошлых рядов нет — «истории ещё нет», без исключения", past3, [])

    print("\nГраница внутри суток: слот РОВНО в created включается (>=), "
          "слот на минуту раньше — нет")
    import datetime as dt
    import zoneinfo
    zone = zoneinfo.ZoneInfo("UTC")
    day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    noon = dt.datetime(day.year, day.month, day.day, 12, 0, tzinfo=zone)
    created_exact = noon.timestamp()
    schedule_noon = {"*": ["12:00"]}
    later = noon + dt.timedelta(days=2)
    # Мутация-проверка: замени `point.timestamp() >= created` на `point.timestamp() >
    # created` в `past_slots` (web/app.py) — эта проверка первой поймает разницу:
    # слот в момент завода канала (created ровно в 12:00) должен остаться в истории,
    # а не пропасть, потому что «канал уже существовал в эту секунду».
    slots_at_created = pult.past_slots(schedule_noon, "UTC", 5, now=later,
                                        created=created_exact)
    check("слот ровно в момент created присутствует",
          any(abs(s.timestamp() - created_exact) < 1 for s in slots_at_created), True)
    slots_created_after = pult.past_slots(schedule_noon, "UTC", 5, now=later,
                                           created=created_exact + 60)
    check("тот же слот, если канал заведён минутой позже него, — исключён",
          any(abs(s.timestamp() - created_exact) < 1 for s in slots_created_after), False)

    print("\nРеальные элементы плана не теряются на границе created")
    # У канала может встретиться строка плана со slot_at ДО created (перенос завода,
    # ручная правка даты) — `past_slots` такую точку не сочинит (она младше границы),
    # но сама строка плана — не выдумка, а факт: её теряет только «догоняющий» блок
    # `_calendar` (строки 692-704 в web/app.py), не граница created. Мутация-проверка:
    # если бы кто-то стал резать `by_slot` по created так же, как режутся синтетические
    # моменты, эта проверка первой обнаружила бы пропажу настоящей ссылки из ответа.
    cid4, ch4 = add_channel(created_exact, schedule_noon)
    c = conn()
    try:
        c.execute(
            "INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,slot_at,"
            "created,updated) VALUES(?,?,?,?,?,?)",
            ("dev", cid4, "https://youtu.be/onboundary", created_exact,
             created_exact, created_exact))
        c.execute(
            "INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,slot_at,"
            "created,updated) VALUES(?,?,?,?,?,?)",
            ("dev", cid4, "https://youtu.be/beforecreated", created_exact - 3600,
             created_exact, created_exact))
        c.commit()
        cal4 = pult._calendar(c, ch4, 90)
    finally:
        c.close()
    by_url = {r["donor_url"]: r for r in cal4}
    check("ссылка ровно на created показана и не 'missed'",
          by_url.get("https://youtu.be/onboundary", {}).get("state"), "planned")
    check("ссылка со slot_at ДО created всё равно показана (реальный план не теряется)",
          "https://youtu.be/beforecreated" in by_url, True)
    check("...и тоже не выдана за 'missed' — это настоящий план, а не пропуск",
          by_url.get("https://youtu.be/beforecreated", {}).get("state"), "planned")

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
