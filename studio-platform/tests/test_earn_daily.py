#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневный сбор часов монетизации (14.10.2026).

Владелец: «стата почему-то должна ждать, когда я тыкаю заказать стату… хотя бы раз в
день фетчить всю стату». Раз в сутки, в назначенный час, студия ставит сбор по всем
каналам с профилем — тем же путём, что кнопка, с пометкой «ночной». Проверяется без
сети: до часа — ничего; в час — все профили; повтор в те же сутки — ничего; назавтра —
снова, но без профилей, по которым сбор ещё стоит; выключатель и час — в admin_state,
умолчание — из окружения; экран и ручки — только админу; таймер тревог его зовёт.

    .venv/bin/python tests/test_earn_daily.py
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="earn-daily-")
DB = os.path.join(TMP, "vsgen.db")
os.environ.update({"VS_DEV_DB": DB, "VS_SERVER_DB": DB, "VS_DEV_TENANT": "t1",
                   "VS_TENANTS_DIR": os.path.join(TMP, "tenants")})
for k in ("VS_EARN_AUTO", "VS_EARN_HOUR", "VS_EARN_TZ"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

from pub_schema import ensure_pub_schema                           # noqa: E402

NOW = time.time()
_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    role TEXT, tenant_id TEXT, disabled INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
  CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
""")
_c.execute("INSERT INTO users(id,name,role,tenant_id) VALUES(1,'хозяин','admin','t1')")
_c.execute("INSERT INTO users(id,name,role,tenant_id) VALUES(3,'оператор','user','t1')")
for sid, uid in (("s-admin", 1), ("s-user", 3)):
    _c.execute("INSERT INTO sessions(id,user_id,expires) VALUES(?,?,?)", (sid, uid, NOW + 3600))
for i, serial in enumerate(("21", "22", "5"), start=1):
    _c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,created) VALUES('t1',?,'x',?)",
               (serial, NOW))
for k, name, prof in (("a", "Альфа", 1), ("b", "Бета", 1), ("c", "Гамма", 2), ("d", "Дельта", 3),
                      ("e", "Без профиля", None)):
    _c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
               "deferred_limit,created,updated,profile_id) VALUES('t1',?,?,'pl','Europe/Warsaw',"
               "'{}',1,0,?,?,?)", ("UC" + k * 22, name, NOW, NOW, prof))
# Чужой тенант: его профили в обход t1 попасть не должны.
_c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,created) VALUES('t2','99','x',?)", (NOW,))
_c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,deferred_limit,"
           "created,updated,profile_id) VALUES('t2',?,'Чужой','pl','Europe/Warsaw','{}',1,0,?,?,4)",
           ("UC" + "z" * 22, NOW, NOW))
_c.commit()
_c.close()

import pub_earn as E                                               # noqa: E402

FAILS = []
WAW = E._zone("Europe/Warsaw")


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def at(day, hour, minute=0):
    """Момент по Варшаве → epoch."""
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=WAW).timestamp()


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def queued(c):
    E.ensure_request_schema(c)
    return [(r[0], r[1], r[2]) for r in c.execute(
        "SELECT profile, auto, created_by FROM pub_earn_requests WHERE status='queued' "
        "ORDER BY CAST(profile AS INTEGER)")]


print("Умолчание: включено, 04:00 по Варшаве")
c = db()
s = E.auto_settings(c)
check("включено и 4 утра", (s["on"], s["hour"], s["tz"]), (True, 4, "Europe/Warsaw"))
os.environ["VS_EARN_AUTO"] = "0"
os.environ["VS_EARN_HOUR"] = "6"
s = E.auto_settings(c)
check("окружение меняет умолчание", (s["on"], s["hour"]), (False, 6))
os.environ.pop("VS_EARN_AUTO")
os.environ.pop("VS_EARN_HOUR")

print("До назначенного часа — ничего")
out = E.daily_tick(c, "t1", now=at(15, 3, 59))
check("ещё рано", (out["queued"], out["why"]), ([], "ещё рано: сбор в 04:00"))
check("заданий нет", queued(c), [])

print("В назначенный час — все профили тенанта, одно задание на профиль")
out = E.daily_tick(c, "t1", now=at(15, 4, 0))
check("поставлены профили t1 по порядку", out["queued"], ["5", "21", "22"])
check("канал без профиля назван", out["no_profile"], ["Без профиля"])
check("задания ночные, с подписью", queued(c),
      [("5", 1, E.AUTO_BY), ("21", 1, E.AUTO_BY), ("22", 1, E.AUTO_BY)])
check("чужой тенант не тронут",
      c.execute("SELECT COUNT(*) FROM pub_earn_requests WHERE tenant_id='t2'").fetchone()[0], 0)

print("Повтор в те же сутки — ничего (таймер зовёт каждую минуту)")
out = E.daily_tick(c, "t1", now=at(15, 23, 59))
check("уже ставился", (out["queued"], out["why"]), ([], "за эти сутки обход уже ставился"))
check("заданий столько же", len(queued(c)), 3)

print("Назавтра: профиль, по которому сбор ещё стоит, второй раз не ставится")
c.execute("UPDATE pub_earn_requests SET status='done', finished=? WHERE profile IN ('5','21')",
          (at(15, 6),))
c.commit()
out = E.daily_tick(c, "t1", now=at(16, 4, 30))
check("поставлены только закончившиеся", (out["queued"], out["already"]), (["5", "21"], ["22"]))

print("Кнопка, нажатая до обхода, не дублируется обходом")
c.execute("UPDATE pub_earn_requests SET status='done' WHERE status='queued'")
E.request_collect(c, "t1", None, by="оператор", now=at(17, 3))
c.commit()
out = E.daily_tick(c, "t1", now=at(17, 4))
check("всё уже стоит по кнопке — ничего нового", (out["queued"], out["already"]),
      ([], ["5", "21", "22"]))
c.execute("UPDATE pub_earn_requests SET status='done' WHERE status='queued'")
c.commit()

print("Выключатель и час")
E.set_auto_settings(c, on=False)
c.commit()
out = E.daily_tick(c, "t1", now=at(18, 12))
check("выключено — ничего", (out["queued"], out["why"]), ([], "ежедневный сбор выключен настройкой"))
E.set_auto_settings(c, on=True, hour=7)
c.commit()
check("час 7: в 6:30 рано", E.daily_tick(c, "t1", now=at(18, 6, 30))["queued"], [])
check("в 7:00 — пора", E.daily_tick(c, "t1", now=at(18, 7))["queued"], ["5", "21", "22"])
for bad in (24, -1, "x", True):
    try:
        E.set_auto_settings(c, hour=bad)
        check(f"час {bad!r} отклонён", "принят", "отказ")
    except ValueError as exc:
        check(f"час {bad!r} отклонён словами", str(exc), "час сбора — целое число от 0 до 23")
check("без тенанта — ничего и почему", E.daily_tick(c, None, now=at(19, 12))["why"],
      "не задан тенант — ставить сбор не для кого")
c.close()

print("Итог последнего обхода для экрана")
c = db()
E.ensure_request_schema(c)
rows = c.execute("SELECT id, profile FROM pub_earn_requests WHERE status='queued' AND auto=1 "
                 "ORDER BY id").fetchall()
c.execute("UPDATE pub_earn_requests SET status='skipped', note='пропущено: профиль занят "
          "заливкой на Минске — соберётся в следующий проход' WHERE id=?", (rows[0][0],))
c.execute("UPDATE pub_earn_requests SET status='failed', note='сборщик не дал результата' "
          "WHERE id=?", (rows[1][0],))
c.commit()
st = E.auto_status(c, "t1")
check("счёт по состояниям", st["last"]["counts"], {"skipped": 1, "failed": 1, "queued": 1})
check("пропущенные профили названы", st["last"]["skipped_profiles"], [rows[0][1]])
check("причины сбоев", st["last"]["failed_reasons"], [{"note": "сборщик не дал результата", "n": 1}])
check("день обхода", st["last"]["day"], "2026-10-18")
c.close()

print("Экран: смотреть может каждый, менять — только админ")
import app as W                                                    # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

client = TestClient(W.app, raise_server_exceptions=False)
client.cookies.set(W.auth.COOKIE, "s-user")
r = client.get("/ui/analytics/earn/auto")
check("оператор видит настройку", (r.status_code, r.json()["settings"]["hour"]), (200, 7))
r = client.post("/ui/analytics/earn/auto", json={"on": False})
check("оператору менять нельзя", r.status_code, 403)
client.cookies.clear()
client.cookies.set(W.auth.COOKIE, "s-admin")
r = client.post("/ui/analytics/earn/auto", json={"on": False, "hour": 5})
check("админ выключил и сменил час", (r.status_code, r.json()["on"], r.json()["hour"]),
      (200, False, 5))
r = client.post("/ui/analytics/earn/auto", json={"hour": 30})
check("плохой час — 400 словами", (r.status_code, "0 до 23" in r.text), (400, True))
c = db()
check("настройка в admin_state", E.auto_settings(c)["on"], False)
c.close()
client.cookies.clear()

print("Таймер тревог зовёт ежедневный сбор, и его сбой тревоги не роняет")
src = open(os.path.join(BASE, "dev", "alerts.py"), encoding="utf-8").read()
check("dev/alerts.py зовёт E.daily_tick", "E.daily_tick(c, TENANT" in src, True)
tick_at = src.index("E.daily_tick(")
check("вызов — после отправки тревог", src.index("A.tick(c, tg.send") < tick_at, True)
check("вызов обёрнут в try", "try:" in src[tick_at - 200:tick_at], True)

print()
if FAILS:
    print(f"провалов: {len(FAILS)}")
    sys.exit(1)
print("все проверки прошли")
