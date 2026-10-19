#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Светофор монетизации на карточке канала (владелец 19.10.2026).

Четыре цвета, и четвёртый — главный. Владелец просил три: красный «нет монеты», жёлтый
«подались и ждём», зелёный «зарабатывает». Но чтение страницы Earn на Минске падает у
половины каналов (19.10: у 42 каналов из 68, где попытка была), и закрасить их красным
значит соврать: «не знаем» — это не «монеты нет». Поэтому серый.

Что проверяется:
  • зелёный даёт и Studio («уже в партнёрке»), и ручная монетка владельца;
  • жёлтый — только когда пройдены ОБА порога YPP: 4000 часов и 1000 подписчиков;
  • красный называет, чего именно не хватает, и на какое число посчитано;
  • серый приходит, когда принятого чтения нет, и объясняет причину словами;
  • `lights()` считает то же, что `light_of()`, одним проходом по базе.

    .venv/bin/python tests/test_money_light.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_earn as E                                              # noqa: E402

FAILS = []
NOW = 1_800_000_000.0


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def reading(state="ok", hours=None, subs=None, as_of="", reason=""):
    return {"state": state, "hours_365": hours, "subs": subs, "as_of": as_of,
            "reason": reason, "state_title": E.STATE_TITLE.get(state, state)}


def test_green():
    print("Зелёный — Studio сказала «в партнёрке» или владелец поставил монетку")
    m = E.light_of(reading("partner"), reading("partner"))
    check("партнёрка → зелёный", m["light"], "green")
    check("словами", m["text"], "зарабатывает")
    check("сказано, откуда знаем", "партнёрской" in m["why"], True)

    m = E.light_of(None, reading("error"), coin=True)
    check("ручная монетка → зелёный даже без чтения", m["light"], "green")
    check("сказано, что руками", "руками" in m["why"], True)

    m = E.light_of(reading("ok", hours=12, subs=3), None, coin=True)
    check("ручная монетка сильнее слабых чисел", m["light"], "green")


def test_yellow():
    print("\nЖёлтый — пройдены ОБА порога, а партнёрки ещё нет")
    m = E.light_of(reading("ok", hours=7191, subs=1441, as_of="Oct 10, 2026"), None)
    check("порог пройден → жёлтый", m["light"], "yellow")
    check("словами", m["text"], "ждёт решения YouTube")
    check("названы оба числа", "7191 ч из 4000" in m["why"] and "1441 подписчиков" in m["why"], True)
    check("сказано, что заявку надо подать или ждать",
          "пора подать" in m["why"] or "подана" in m["why"], True)

    print("  часов хватает, подписчиков нет — это ещё не жёлтый")
    m = E.light_of(reading("ok", hours=7191, subs=441), None)
    check("только часы → красный", m["light"], "red")
    check("сказано, чего не хватает", "441 подписчиков из 1000" in m["why"], True)

    print("  подписчиков хватает, часов нет")
    m = E.light_of(reading("ok", hours=120, subs=1500), None)
    check("только подписчики → красный", m["light"], "red")
    check("названы часы", "120 ч из 4000" in m["why"], True)


def test_red():
    print("\nКрасный — числа есть, до порога далеко")
    m = E.light_of(reading("ok", hours=93, subs=2, as_of="Oct 10, 2026"), None)
    check("красный", m["light"], "red")
    check("словами", m["text"], "монеты нет")
    check("названы часы и подписчики",
          "93 ч из 4000" in m["why"] and "2 подписчиков из 1000" in m["why"], True)
    check("названа дата числа", "Oct 10, 2026" in m["why"], True)


def test_gray():
    print("\nСерый — принятого чтения нет; это НЕ «монеты нет»")
    m = E.light_of(None, reading("error", reason="metric_missing_or_conflicting"))
    check("сбой чтения → серый", m["light"], "gray")
    check("словами", m["text"], "не знаем")
    check("причина словами", m["why"], E.STATE_TITLE["error"])

    m = E.light_of(None, reading("unverified"))
    check("не подтверждено → серый", m["light"], "gray")
    check("объяснено", "не принято" in m["why"], True)

    m = E.light_of(None, None)
    check("ни одного чтения → серый", m["light"], "gray")
    check("так и сказано", m["why"], "часы ни разу не собирались")

    print("  чтение прочиталось, но числа не отдало")
    m = E.light_of(reading("ok", hours=None, subs=None), reading("ok"))
    check("чисел нет → серый, а не красный", m["light"], "gray")


def test_lights_over_db():
    print("\nlights(): один проход по базе даёт то же, что light_of()")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE pub_channels(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT,
                                coin INTEGER NOT NULL DEFAULT 0, coin_note TEXT);
    """)
    E.ensure_earn_schema(c)
    for cid, name, coin in ((1, "в партнёрке", 0), (2, "порог пройден", 0),
                            (3, "далеко", 0), (4, "сбой чтения", 0),
                            (5, "монетка руками", 1), (6, "ни разу не собирали", 0)):
        c.execute("INSERT INTO pub_channels(id,tenant_id,name,coin) VALUES(?,?,?,?)",
                  (cid, "t", name, coin))
    c.execute("INSERT INTO pub_channels(id,tenant_id,name,coin) VALUES(9,'чужой','чужой',0)")

    def put(cid, read_at, state, hours=None, subs=None):
        c.execute("INSERT INTO pub_channel_earn(channel_id,read_at,uc,state,hours_365,subs,"
                  "loaded) VALUES(?,?,?,?,?,?,?)",
                  (cid, read_at, "UC" + "x" * 22, state, hours, subs, NOW))

    put(1, NOW, "partner")
    put(2, NOW, "ok", 5174, 1102)
    put(3, NOW, "ok", 229, 31)
    put(4, NOW - 100, "ok", 150, 14)          # вчера прочиталось
    put(4, NOW, "error")                      # сегодня сбой — числа не теряем
    put(5, NOW, "error")
    c.commit()

    out = E.lights(c, "t")
    check("каналы свои, чужой не попал", sorted(out), [1, 2, 3, 4, 5, 6])
    check("партнёрка", out[1]["light"], "green")
    check("порог пройден", out[2]["light"], "yellow")
    check("далеко до порога", out[3]["light"], "red")
    check("свежий сбой не стирает вчерашние числа", out[4]["light"], "red")
    check("и числа именно вчерашние", "150 ч из 4000" in out[4]["why"], True)
    check("ручная монетка", out[5]["light"], "green")
    check("ни разу не собирали", out[6]["light"], "gray")


def main():
    test_green()
    test_yellow()
    test_red()
    test_gray()
    test_lights_over_db()
    print()
    if FAILS:
        print(f"провалов: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("все проверки прошли")


if __name__ == "__main__":
    main()
