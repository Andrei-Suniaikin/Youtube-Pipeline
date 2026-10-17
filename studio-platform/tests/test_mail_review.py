#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Почта на проверку — сервер пульта (17.10.2026).

Письмо с Минска, у которого канал не определился (почта или профиль с несколькими
каналами, профиль без каналов), ложится «на проверку». Человек говорит, чей это канал,
либо скрывает письмо. Что здесь проверяется и почему:

  • СПИСОК — ТОЛЬКО НЕРАЗОБРАННОЕ. Привязанное и скрытое из него уходит, но из базы
    не удаляется: на странице канала скрытое письмо по-прежнему видно «по почте аккаунта».
  • ПРИВЯЗКА УЧИТ ПОЧТУ. После «привязать» следующее письмо той же почты того же
    профиля идёт на канал само — пока реестр не скажет, что у почты несколько каналов.
  • ЧУЖОЙ КАНАЛ — 404. Канал другого тенанта, несуществующий UC — та же дверь, что у
    монетки. Разобранное второй раз — тоже 404, а не тихая перезапись.
  • СЧЁТ НА КНОПКЕ. `/ui/channels` несёт `mail_review_n` — один COUNT, без второго запроса.

    VS_ALERTS=0 .venv/bin/python tests/test_mail_review.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="mail-review-")
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

_c = sqlite3.connect(DB)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
    token TEXT, role TEXT, created TEXT, password_hash TEXT, daily_quota INTEGER,
    tenant_id TEXT, disabled INTEGER DEFAULT 0, keys TEXT);
  CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
  CREATE TABLE IF NOT EXISTS tenants(id TEXT PRIMARY KEY, name TEXT, created TEXT,
    is_active INTEGER, daily_quota INTEGER);
  CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT);
""")
ensure_pub_schema(_c)
_c.commit()
_c.close()

import app as W                                                    # noqa: E402
import pub_mail                                                    # noqa: E402
from fastapi import HTTPException                                  # noqa: E402

FAILS = []
UC2, UC3, UCZ = "UC" + "b" * 22, "UC" + "c" * 22, "UC" + "z" * 22
REQ = types.SimpleNamespace(state=types.SimpleNamespace(user={"name": "boss", "id": 1}))


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def sql(path, q, args=()):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()


def seed():
    c = sqlite3.connect(DB)
    now = time.time()
    for serial in ("21", "77"):
        c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,created) VALUES('t1',?,?,?)",
                  (serial, "p" + serial, now))
    pid = {r[1]: r[0] for r in c.execute("SELECT id, serial FROM pub_profiles")}
    for uc, name, serial, tenant in ((UC2, "Два", "21", "t1"), (UC3, "Три", "21", "t1"),
                                     (UCZ, "Чужой", None, "t2")):
        c.execute("INSERT INTO pub_channels(tenant_id,uc,name,profile_id,lang,status,tz,schedule,"
                  "horizon_days,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (tenant, uc, name, pid.get(serial), "pl", "active", "Europe/Warsaw", "{}",
                   14, now, now))
    c.commit()
    c.close()
    return {r["uc"]: r["id"] for r in sql(DB, "SELECT id, uc FROM pub_channels")}


def ingest(profile, login, rows, login_ucs=()):
    vs = sqlite3.connect(DB)
    vs.row_factory = sqlite3.Row
    mail = pub_mail.open_mail_db(MAIL_DB)
    try:
        out = pub_mail.ingest(mail, vs, {"host": "minsk", "profile": profile, "login_masked": login,
                                         "login_ucs": list(login_ucs), "rows": rows})
        mail.commit()
        return out
    finally:
        mail.close()
        vs.close()


def row(sender, subject, date, snippet=""):
    return {"sender": sender, "subject": subject, "snippet": snippet, "date": date, "unread": True}


def bind(eid, uc):
    return W.ui_mail_review_bind(eid, W.MailBindIn(uc=uc), REQ)


def status_of(fn, *a):
    try:
        fn(*a)
        return "прошло"
    except HTTPException as exc:
        return exc.status_code


def main() -> int:
    ids = seed()
    print("— что ложится на проверку —")
    out = ingest("21", "two@gmail.com", [
        row("no-reply@accounts.google.com", "Security alert", "Sat, Oct 10, 2026, 10:22 PM"),
        row("no-reply@youtube.com", "Your channel is eligible for YPP", "Mon, Oct 12, 2026, 1:00 PM"),
        row("no-reply@youtube.com", "Update on your channel", "Sun, Oct 11, 2026, 9:00 AM",
            snippet=f"channel {UC2}"),
    ])
    check("профиль с двумя каналами: 2 на проверку, письмо с UC — нет", out["review"], 2)
    ingest("77", "lone@gmail.com", [row("no-reply@google.com", "Welcome", "Fri, Oct 9, 2026, 8:00 AM")])
    d = W.ui_mail_review()
    ev = d["events"]
    check("в списке 3, новые сверху", [e["subject"] for e in ev],
          ["Your channel is eligible for YPP", "Security alert", "Welcome"])
    check("у письма — профиль, почта маской, категория по-русски, дата Gmail",
          (ev[0]["profile"], ev[0]["login"], ev[0]["title"], ev[0]["event_time"]),
          ("21", "two…@gmail.com", "можно подавать на монетизацию", "2026-10-12T13:00"))
    check("каналы для выбора — только свои, с профилем",
          [(c["name"], c["profile"]) for c in d["channels"]], [("Два", "21"), ("Три", "21")])
    check("счёт для кнопки на экране каналов", W.ui_channels()["mail_review_n"], 3)

    print("— привязать —")
    eligible = ev[0]["id"]
    out = bind(eligible, UC3)
    check("ответ: канал, почта запомнена", (out["uc"], out["routed"], out["channel"]["name"]),
          (UC3, True, "Три"))
    e = sql(MAIL_DB, "SELECT uc, binding_confidence, reviewed_by FROM events WHERE id=?", (eligible,))[0]
    check("событие: uc, «руками», кто", (e["uc"], e["binding_confidence"], e["reviewed_by"]),
          (UC3, "manual", "boss"))
    rt = sql(MAIL_DB, "SELECT route_tag, uc, status, source FROM mail_route")
    check("маршрут почты профиля записан", rt,
          [{"route_tag": "minsk:21:two…@gmail.com", "uc": UC3, "status": "manual", "source": "two…@gmail.com"}])
    check("из списка ушло одно", [x["subject"] for x in W.ui_mail_review()["events"]],
          ["Security alert", "Welcome"])
    page = asyncio.run(W.ui_channel(ids[UC3]))["mail_events"]
    check("на странице канала «Три» письмо своё, с пометкой «указан человеком»",
          [(x["subject"], x["account_level"], x["confidence_title"]) for x in page],
          [("Your channel is eligible for YPP", False, "канал указан человеком")])
    check("у «Два» его нет", [x["subject"] for x in asyncio.run(W.ui_channel(ids[UC2]))["mail_events"]],
          ["Update on your channel"])

    print("— следующие письма этой почты идут сами —")
    out = ingest("21", "two@gmail.com", [
        row("no-reply@youtube.com", "Your channel is eligible for YPP", "Mon, Oct 12, 2026, 1:00 PM"),
        row("no-reply@youtube.com", "Monetization review result", "Tue, Oct 13, 2026, 2:00 PM"),
    ])
    check("повтор старой строки — дубль, новая — принята без проверки", (out["dup"], out["new"], out["review"]), (1, 1, 0))
    page = asyncio.run(W.ui_channel(ids[UC3]))["mail_events"]
    check("новое письмо легло на «Три» само", [x["subject"] for x in page][:1], ["Monetization review result"])
    check("и помечено как указанное человеком", page[0]["confidence"], "manual")
    out = ingest("21", "two@gmail.com", [
        row("no-reply@youtube.com", "Important update about your channel", "Wed, Oct 14, 2026, 3:00 PM")],
        login_ucs=[UC2, UC3])
    check("реестр говорит «у почты два канала» — снова на проверку", out["review"], 1)
    out = ingest("21", "other@gmail.com", [
        row("no-reply@youtube.com", "Important update about your channel", "Wed, Oct 14, 2026, 4:00 PM")])
    check("другая почта того же профиля — на проверку", out["review"], 1)

    print("— чужое и повторное —")
    sec = next(x["id"] for x in W.ui_mail_review()["events"] if x["subject"] == "Security alert")
    check("канал другого тенанта — 404", status_of(bind, sec, UCZ), 404)
    check("несуществующий UC — 404", status_of(bind, sec, "UC" + "q" * 22), 404)
    check("пустой UC — 404", status_of(bind, sec, ""), 404)
    check("письмо не тронуто", sql(MAIL_DB, "SELECT uc, reviewed_utc FROM events WHERE id=?", (sec,))[0],
          {"uc": None, "reviewed_utc": None})
    check("уже привязанное — 404", status_of(bind, eligible, UC2), 404)
    check("несуществующее письмо — 404", status_of(bind, 10_000, UC2), 404)
    check("маршрут не перезаписан чужим", sql(MAIL_DB, "SELECT uc FROM mail_route")[0]["uc"], UC3)

    print("— скрыть —")
    welcome = next(x["id"] for x in W.ui_mail_review()["events"] if x["subject"] == "Welcome")
    check("ответ ok", W.ui_mail_review_dismiss(welcome, REQ), {"ok": True, "id": welcome})
    e = sql(MAIL_DB, "SELECT uc, binding_confidence, reviewed_utc, reviewed_by FROM events WHERE id=?",
            (welcome,))[0]
    check("запись осталась: без канала, просмотрена, кем",
          (e["uc"], e["binding_confidence"], bool(e["reviewed_utc"]), e["reviewed_by"]),
          (None, "review", True, "boss"))
    check("из списка ушло", "Welcome" in [x["subject"] for x in W.ui_mail_review()["events"]], False)
    check("второй раз — 404", status_of(W.ui_mail_review_dismiss, welcome, REQ), 404)
    check("счёт на кнопке: 3 осталось", W.ui_channels()["mail_review_n"], 3)
    check("функция pub_mail: событие с uc скрыть нельзя",
          status_of(W.ui_mail_review_dismiss, eligible, REQ), 404)

    print()
    if FAILS:
        print(f"ПРОВАЛОВ: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
