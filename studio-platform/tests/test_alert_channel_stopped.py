#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""§15: канал остановился — сообщение по каналу (15.10.2026).

Задача Володи: «отдельные уведомления об остановках. В сообщении: какой канал
остановился, почему, что уже попробовано и какое действие нужно».

Проверяется:
  * остановленный выпуск → одно сообщение на канал, даже если остановлено несколько;
  * в тексте канал, профиль AdsPower, причина словами, сколько раз возвращали в работу,
    что сделать и кто;
  * только что остановленный (меньше STOPPED_HOLD_SEC) — молчим: студия может вернуть сама;
  * выход давно в прошлом и канал на паузе — не зовём;
  * повтор раз в сутки, «починилось» — когда остановок не осталось.

    .venv/bin/python tests/test_alert_channel_stopped.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="alert-stopped-")
os.environ["VS_DEV_DB"] = os.path.join(TMP, "vsgen.db")
os.environ["VS_SERVER_DB"] = os.environ["VS_DEV_DB"]
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_alerts as A                                            # noqa: E402
import pub_schema                                                 # noqa: E402

FAILS = []
NOW = 1_792_080_000.0
LOGIN = "account recovery needs the owner: signed in but exact channel not confirmed"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def world():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    c.execute("CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL)")
    c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,created) "
              "VALUES('3','55','V - WW2 HU 1',?)", (NOW,))
    pid = c.execute("SELECT id FROM pub_profiles").fetchone()[0]
    for n, (name, status) in enumerate((("Stal i Popiół", "active"),
                                        ("Пауза", "paused"),
                                        ("Здоровый", "active")), 1):
        c.execute("INSERT INTO pub_channels(tenant_id,uc,name,handle,lang,tz,schedule,deferred,"
                  "deferred_limit,profile_id,status,created,updated) "
                  "VALUES('3',?,?,?,'pl','Europe/Warsaw','{}',1,0,?,?,?,?)",
                  (f"UC{n:022d}", name, f"@ch{n}", pid, status, NOW, NOW))
    return c


def release(c, rid, cid, slot, status="blocked", reason=LOGIN, updated=NOW - 3600, item=None):
    item = item or abs(hash(rid)) % 100000
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,status,phase,blocked_reason,attempts,created,updated) "
              "VALUES(?,'3',?,1,?,'UC',?,?,'upload_running',?,2,?,?)",
              (rid, item, cid, slot, status, reason, NOW - 86400, updated))


def main() -> int:
    c = world()
    release(c, "r_a", 1, NOW + 3600)
    release(c, "r_b", 1, NOW + 90000)
    c.execute("INSERT INTO pub_release_log(release_id,ts,level,step,message) "
              "VALUES('r_a',?,'info','движок','возвращён в работу с фазы «cover_ready», "
              "счётчик попыток обнулён')", (NOW - 7200,))
    release(c, "r_p", 2, NOW + 3600)                      # канал на паузе
    release(c, "r_old", 3, NOW - 5 * 86400)               # выход давно прошёл
    release(c, "r_ok", 3, NOW + 3600, status="done", reason=None)

    print("Сообщение по каналу")
    al = A.check_channel_stopped(c, NOW, "3")
    check("одно сообщение — только остановленный активный канал",
          [a.key for a in al], ["stopped:1"])
    text = al[0].text if al else ""
    check("заголовок называет канал", al[0].title if al else "", "Канал остановился: Stal i Popiół")
    for want in ("@ch1", "Профиль AdsPower: 55 «V - WW2 HU 1»", "r_a",
                 "Почему: в аккаунте нет этого канала — нужны права менеджера",
                 "вернуть в работу — 1 раз", "Что сделать:", "Кто:",
                 "Всего остановлено выпусков на канале: 2"):
        check(f"в тексте есть «{want}»", want in text, True)

    print("Только что остановленный — ждём")
    c2 = world()
    release(c2, "r_new", 1, NOW + 3600, updated=NOW - 60)
    check("меньше STOPPED_HOLD_SEC — молчим", A.check_channel_stopped(c2, NOW, "3"), [])

    print("Неизвестная причина — сырым текстом")
    c3 = world()
    release(c3, "r_x", 1, NOW + 3600, reason="что-то новое   с пробелами")
    al3 = A.check_channel_stopped(c3, NOW, "3")
    check("причина показана как есть", "Почему: что-то новое с пробелами." in al3[0].text, True)

    print("Повтор и «починилось»")
    check("повтор раз в сутки", A.repeat_after("stopped:1"), 24 * 3600.0)
    sent = []
    saved = A.CHECKS
    A.CHECKS = (A.check_channel_stopped,)
    try:
        A.tick(c, lambda t: sent.append(t) or True, now=NOW, tenant="3")
        check("первый проход — тревога", [t.split("\n")[0] for t in sent],
              ["ТРЕВОГА · Канал остановился: Stal i Popiół"])
        sent.clear()
        A.tick(c, lambda t: sent.append(t) or True, now=NOW + 3600, tenant="3")
        check("через час — тишина", sent, [])
        c.execute("UPDATE pub_releases SET status='done' WHERE channel_id=1")
        A.tick(c, lambda t: sent.append(t) or True, now=NOW + 7200, tenant="3")
        check("остановок не осталось — «починилось»",
              [t.split("\n")[0] for t in sent], ["ПОЧИНИЛОСЬ · Канал остановился: Stal i Popiół"])
    finally:
        A.CHECKS = saved

    print(f"\nпровалов: {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
