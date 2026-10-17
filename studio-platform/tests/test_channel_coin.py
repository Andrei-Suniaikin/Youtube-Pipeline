#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Монетный канал и почта канала — сервер пульта (владелец 17.10.2026).

«Главное чтобы в студии могли видеть монетные каналы, которые и будем помечать
монеткой, плюс добавим фильтр что канал на монете».

Что здесь проверяется и почему именно это:

  • СТАРАЯ БАЗА ПОЛУЧАЕТ КОЛОНКИ. Боевая база заведена до монетки: `ensure_pub_schema`
    обязана добавить `coin`/`coin_note` и не упасть на повторе. Проверяется живым
    SELECT, а не молчанием `except`.
  • МОНЕТКА РУКАМИ. Ставится и снимается одной ручкой, с заметкой; чужой/несуществующий
    канал — 404. Из писем монетка не ставится: письмо — повод, не факт.
  • ОТБОР СЧИТАЕТ ОДИНАКОВО. `?coin=1` и `?coin=0` на сервере дают те же множества,
    что и отбор на экране.
  • ПИСЬМА ПРИЕЗЖАЮТ НА ЭКРАН КАНАЛА. Из базы читалки, только чтение; базы нет —
    пустой список, а не ошибка на весь экран. На карточке — счёт писем.

    VS_ALERTS=0 .venv/bin/python tests/test_channel_coin.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="channel-coin-")
DB = os.path.join(TMP, "vsgen.db")
MAIL_DB = os.path.join(TMP, "mail_monitor.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["MAIL_MONITOR_DB"] = MAIL_DB
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

from pub_schema import ensure_pub_schema                           # noqa: E402

# «Старая» база: таблица каналов без колонок монетки, как в бою до этой правки.
_c = sqlite3.connect(DB)
_c.executescript("""
  CREATE TABLE pub_channels(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
    uc TEXT UNIQUE, name TEXT NOT NULL, handle TEXT, lang TEXT, niche TEXT,
    profile_id INTEGER, host_id INTEGER, tz TEXT NOT NULL DEFAULT 'Europe/Warsaw',
    schedule TEXT NOT NULL DEFAULT '{}', horizon_days INTEGER NOT NULL DEFAULT 3,
    preset_rewrite TEXT, preset_vsgen_id INTEGER, preset_cover TEXT,
    collaborator TEXT, video_mode TEXT NOT NULL DEFAULT 'slideshow',
    t2v_first_n INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
    note TEXT, created REAL NOT NULL, updated REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
    token TEXT, role TEXT, created TEXT, password_hash TEXT, daily_quota INTEGER,
    tenant_id TEXT, disabled INTEGER DEFAULT 0, keys TEXT);
  CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
  CREATE TABLE IF NOT EXISTS tenants(id TEXT PRIMARY KEY, name TEXT, created TEXT,
    is_active INTEGER, daily_quota INTEGER);
  CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT);
""")
_c.commit()
ensure_pub_schema(_c)
ensure_pub_schema(_c)          # повтор — идемпотентно
_c.commit()
_c.close()

import app as W                                                    # noqa: E402
import pub_mail                                                    # noqa: E402
from fastapi import HTTPException                                  # noqa: E402

FAILS = []
TENANT = "t1"
UC1 = "UC" + "m" * 22
UC2 = "UC" + "n" * 22


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def sql(q, args=()):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()


def run(q, args=()):
    c = sqlite3.connect(DB)
    try:
        c.execute(q, args)
        c.commit()
    finally:
        c.close()


def channel(name, uc, tenant=TENANT):
    run("INSERT INTO pub_channels(tenant_id,name,uc,lang,status,tz,schedule,"
        "horizon_days,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (tenant, name, uc, "pl", "active", "Europe/Warsaw", "{}", 14, time.time(), time.time()))
    return sql("SELECT id FROM pub_channels WHERE uc=?", (uc,))[0]["id"]


def listed(**kw):
    return {c["name"]: c for c in W.ui_channels(**kw)["channels"]}


def main() -> int:
    print("— миграция —")
    cols = {r["name"]: r for r in sql("PRAGMA table_info(pub_channels)")}
    check("колонка coin появилась", "coin" in cols, True)
    check("колонка coin_note появилась", "coin_note" in cols, True)
    a = channel("Монетный", UC1)
    b = channel("Обычный", UC2)
    channel("Чужой", "UC" + "z" * 22, tenant="t2")
    check("умолчание coin=0", sql("SELECT coin FROM pub_channels WHERE id=?", (a,))[0]["coin"], 0)
    check("вид pub_channels_live отдаёт coin",
          sql("SELECT coin, coin_note FROM pub_channels_live WHERE id=?", (a,))[0],
          {"coin": 0, "coin_note": None})

    print("— список —")
    ch = listed()
    check("в списке оба своих, чужого нет", sorted(ch), ["Монетный", "Обычный"])
    check("coin в списке — false, заметка пустая",
          (ch["Монетный"]["coin"], ch["Монетный"]["coin_note"]), (False, ""))
    check("писем пока 0 (базы почты нет — не ошибка)", ch["Монетный"]["mail_n"], 0)

    print("— монетка —")
    out = W.ui_channel_coin(a, W.ChannelCoinIn(coin=True, note="  одобрен   12.10 "))
    check("ответ: coin=true, заметка ужата", (out["coin"], out["coin_note"]), (True, "одобрен 12.10"))
    row = sql("SELECT coin, coin_note FROM pub_channels WHERE id=?", (a,))[0]
    check("в базе 1 и заметка", (row["coin"], row["coin_note"]), (1, "одобрен 12.10"))
    ch = listed()
    check("список: монетный помечен, обычный нет",
          (ch["Монетный"]["coin"], ch["Обычный"]["coin"]), (True, False))
    check("?coin=1 — только монетные", sorted(listed(coin=1)), ["Монетный"])
    check("?coin=0 — только без монетки", sorted(listed(coin=0)), ["Обычный"])
    try:
        W.ui_channel_coin(999, W.ChannelCoinIn(coin=True))
        check("несуществующий канал — 404", "прошло", "404")
    except HTTPException as exc:
        check("несуществующий канал — 404", exc.status_code, 404)
    chuzhoy = sql("SELECT id FROM pub_channels WHERE tenant_id='t2'")[0]["id"]
    try:
        W.ui_channel_coin(chuzhoy, W.ChannelCoinIn(coin=True))
        check("чужой канал — 404", "прошло", "404")
    except HTTPException as exc:
        check("чужой канал — 404", exc.status_code, 404)
    check("чужой не тронут", sql("SELECT coin FROM pub_channels WHERE id=?", (chuzhoy,))[0]["coin"], 0)
    long = "х" * 300
    out = W.ui_channel_coin(a, W.ChannelCoinIn(coin=True, note=long))
    check("заметка режется до 200", len(out["coin_note"]), 200)
    W.ui_channel_coin(a, W.ChannelCoinIn(coin=False))
    row = sql("SELECT coin, coin_note FROM pub_channels WHERE id=?", (a,))[0]
    check("снята: 0 и заметка пустая", (row["coin"], row["coin_note"]), (0, None))
    W.ui_channel_coin(a, W.ChannelCoinIn(coin=True, note="снова"))

    print("— экран канала —")
    d = asyncio.run(W.ui_channel(a))
    check("карточка несёт coin и заметку",
          (d["channel"]["coin"], d["channel"]["coin_note"]), (True, "снова"))
    check("писем нет — пустой список, не ошибка", d["mail_events"], [])

    # Положим письма через приём с машины: своё письмо UC1 и письмо уровня аккаунта
    # почты, за которой числятся оба канала.
    vs = sqlite3.connect(DB)
    vs.row_factory = sqlite3.Row
    mail = pub_mail.open_mail_db(MAIL_DB)
    pub_mail.ingest(mail, vs, {"host": "minsk", "profile": "20", "login_masked": "abc@gmail.com",
                               "login_ucs": [UC1, UC2], "rows": [
        {"sender": "no-reply@youtube.com", "subject": "Your channel is eligible for YPP",
         "snippet": f"channel {UC1}", "date": "Mon, Oct 12, 2026, 1:00 PM", "unread": True},
        {"sender": "no-reply@accounts.google.com", "subject": "Security alert",
         "snippet": "new sign-in abc@gmail.com", "date": "Sun, Oct 11, 2026, 9:00 AM"},
    ]})
    mail.commit()
    mail.close()
    vs.close()
    d = asyncio.run(W.ui_channel(a))
    ev = d["mail_events"]
    check("у монетного — своё письмо и письмо аккаунта",
          [(e["type"], e["account_level"]) for e in ev],
          [("monetization_eligible", False), ("account_security", True)])
    check("названия по-русски", ev[0]["title"], "можно подавать на монетизацию")
    check("почта в маске, не полная", ev[1]["login"], "abc…@gmail.com")
    check("непрочитанное помечено", ev[0]["unread"], True)
    d2 = asyncio.run(W.ui_channel(b))
    check("у обычного — только письмо аккаунта",
          [(e["type"], e["account_level"]) for e in d2["mail_events"]], [("account_security", True)])
    ch = listed()
    check("счёт писем на карточках: 2 и 1", (ch["Монетный"]["mail_n"], ch["Обычный"]["mail_n"]), (2, 1))

    print()
    if FAILS:
        print(f"ПРОВАЛОВ: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
