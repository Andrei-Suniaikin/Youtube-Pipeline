#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приём строк Gmail с Минска — `POST /api/mail/inbox` (17.10.2026).

Пересылка на приёмник Google этим аккаунтам не даётся, поэтому письма читаются
списком внутри профиля AdsPower и привозятся сюда. Что здесь проверяется и почему:

  • ЧУЖОЙ НЕ ПИШЕТ. Ручка под тем же токеном ноды, что и заливка: без него — 401, и в
    базе пусто.
  • ПОВТОРНЫЙ ОБХОД НИЧЕГО НЕ УДВАИВАЕТ. Сборщик смотрит одни и те же 7 дней каждую
    ночь; у строки списка нет Message-ID, дедуп — по (профиль, почта, отправитель,
    тема, дата). События — по idem_key.
  • КАТЕГОРИИ ТЕ ЖЕ, ЧТО У ЧИТАЛКИ ПРИЁМНИКА. Один модуль правил на обе дороги.
  • ПРИВЯЗКА ПО §8 ТЗ. UC в письме → канал почты (если один) → канал профиля (если
    один) → «на проверку». Почта с двумя каналами канал НЕ выбирает.
  • ПОЧТА НЕ УТЕКАЕТ. Полный адрес в `login_masked` и в сниппете превращается в маску.
  • ПУЛЬТ ВИДИТ. `channel_events` отдаёт события канала и события уровня аккаунта его
    почты, помеченные отдельно.

    VS_ALERTS=0 .venv/bin/python tests/test_mail_inbox_api.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="mail-inbox-")
DB = os.path.join(TMP, "vsgen.db")
MAIL_DB = os.path.join(TMP, "mail_monitor.db")
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_DB"] = DB
os.environ["MAIL_MONITOR_DB"] = MAIL_DB
sys.path.insert(0, os.path.join(BASE, "server"))

from fastapi import FastAPI                                        # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

from pub_schema import ensure_pub_schema                           # noqa: E402
import pub_mail as M                                               # noqa: E402

FAILS = []
TOKEN = "upl_test-token"
UC1 = "UC" + "a" * 22
UC2 = "UC" + "b" * 22
UC3 = "UC" + "c" * 22


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def mail_sql(q, args=()):
    c = sqlite3.connect(MAIL_DB)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()


def seed():
    c = db()
    ensure_pub_schema(c)
    now = time.time()
    # профиль 20 — один канал; профиль 21 — два канала; профиль 99 — без каналов
    for serial in ("20", "21", "99"):
        c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,created) VALUES('t1',?,?,?)",
                  (serial, "p" + serial, now))
    pid = {r["serial"]: r["id"] for r in c.execute("SELECT id, serial FROM pub_profiles")}
    for uc, name, serial in ((UC1, "Один", "20"), (UC2, "Два", "21"), (UC3, "Три", "21")):
        c.execute("INSERT INTO pub_channels(tenant_id,uc,name,profile_id,created,updated) "
                  "VALUES('t1',?,?,?,?,?)", (uc, name, pid[serial], now, now))
    c.commit()
    c.close()


def main() -> int:
    seed()
    app = FastAPI()
    M.register_mail_routes(app, {"db": db, "upload_token": lambda: TOKEN,
                                 "mail_db_path": lambda: MAIL_DB})
    client = TestClient(app, raise_server_exceptions=False)

    def send(payload, token=TOKEN):
        headers = {} if token is None else {"X-Upload-Token": token}
        return client.post("/api/mail/inbox", json=payload, headers=headers)

    rows20 = [
        {"sender": "no-reply@youtube.com", "subject": "Changes to your YouTube payment details",
         "snippet": "Hello Ostatnia Kryjówka. The AdSense for YouTube account linked to your "
                    "channel was successfully changed.", "date": "Sun, Oct 11, 2026, 10:01 AM",
         "unread": True},
        {"sender": "no-reply@accounts.google.com", "subject": "Security alert",
         "snippet": "A new sign-in on Mac OS. min0628625171@gmail.com. We noticed a new sign-in",
         "date": "Sat, Oct 10, 2026, 10:22 PM", "unread": True},
        {"sender": "no-reply@google.com", "subject": "Welcome to Google on your Mac OS",
         "snippet": "Get started with Google on your new device",
         "date": "Sat, Oct 10, 2026, 10:22 PM", "unread": False},
    ]
    body20 = {"host": "minsk", "profile": "20", "login_masked": "min0628625171@gmail.com",
              "login_ucs": [UC1], "rows": rows20}

    print("— доступ —")
    r = send(body20, token=None)
    check("без токена — 401", r.status_code, 401)
    r = send(body20, token="upl_chuzhoy")
    check("чужой токен — 401", r.status_code, 401)
    check("в базе пусто", os.path.exists(MAIL_DB) and mail_sql("SELECT COUNT(*) n FROM inbox_messages")[0]["n"] or 0, 0)

    print("— первый приём —")
    r = send(body20)
    check("200", r.status_code, 200)
    out = r.json()
    check("принято 3, новых 3, повторов 0", (out["received"], out["new"], out["dup"]), (3, 3, 0))
    check("категории: платежи, безопасность, без категории",
          out["events"], {"adsense_payments": 1, "account_security": 1, "important_unclassified": 1})
    msgs = mail_sql("SELECT * FROM inbox_messages ORDER BY id")
    check("источник minsk_inbox", {m["source"] for m in msgs}, {"minsk_inbox"})
    check("почта в маске", {m["login_tag"] for m in msgs}, {"min…@gmail.com"})
    check("полной почты нет ни в одном поле",
          any("min0628625171" in json.dumps(m, ensure_ascii=False) for m in msgs), False)
    check("сниппет «Security alert» без адреса",
          "min…@gmail.com" in msgs[1]["snippet"] and "@gmail.com. We" in msgs[1]["snippet"], True)
    check("дата Gmail разобрана", msgs[0]["event_time"], "2026-10-11T10:01")
    check("непрочитанное сохранено", [m["unread"] for m in msgs], [1, 1, 0])
    evs = mail_sql("SELECT * FROM events ORDER BY id")
    check("3 события", len(evs), 3)
    check("почта с одним каналом → канал, уверенность medium",
          {(e["uc"], e["binding_confidence"]) for e in evs}, {(UC1, "medium")})
    check("source событий minsk_inbox, профиль записан",
          {(e["source"], e["profile"]) for e in evs}, {("minsk_inbox", "20")})
    check("account — маска почты", {e["account"] for e in evs}, {"min…@gmail.com"})

    print("— повторный обход —")
    r = send(body20)
    out = r.json()
    check("повторов 3, новых 0", (out["dup"], out["new"]), (3, 0))
    check("строк по-прежнему 3", mail_sql("SELECT COUNT(*) n FROM inbox_messages")[0]["n"], 3)
    check("событий по-прежнему 3", mail_sql("SELECT COUNT(*) n FROM events")[0]["n"], 3)
    # Та же тема другой датой — новое письмо, не повтор.
    again = dict(body20, rows=[dict(rows20[1], date="Fri, Oct 16, 2026, 9:00 AM")])
    out = send(again).json()
    check("та же тема, другая дата — новое", (out["new"], out["dup"]), (1, 0))

    print("— привязка —")
    # Явный UC в тексте бьёт всё остальное — даже при почте с двумя каналами.
    body21 = {"host": "minsk", "profile": "21", "login_masked": "dva@gmail.com",
              "login_ucs": [UC2, UC3], "rows": [
                  {"sender": "no-reply@youtube.com", "subject": "Your channel is eligible for the YouTube Partner Program",
                   "snippet": f"channel {UC3} can apply", "date": "Mon, Oct 12, 2026, 1:00 PM"},
                  {"sender": "no-reply@youtube.com", "subject": "Copyright claim on your video",
                   "snippet": "A claim was made", "date": "Mon, Oct 12, 2026, 2:00 PM"},
              ]}
    out = send(body21).json()
    check("категории: готовность и copyright",
          out["events"], {"monetization_eligible": 1, "copyright": 1})
    evs = mail_sql("SELECT type, uc, binding_confidence FROM events WHERE profile='21' ORDER BY id")
    check("UC в сниппете → high на UC3", (evs[0]["uc"], evs[0]["binding_confidence"]), (UC3, "high"))
    check("почта с двумя каналами и профиль с двумя → на проверку",
          (evs[1]["uc"], evs[1]["binding_confidence"]), (None, "review"))
    check("счёт «на проверку» в ответе", out["review"], 1)
    # Почта без привязок, но профиль с одним каналом → low.
    body20b = {"host": "minsk", "profile": "20", "login_masked": "x@y.com", "login_ucs": [],
               "rows": [{"sender": "no-reply@youtube.com", "subject": "Monetization disabled",
                         "snippet": "", "date": "Tue, Oct 13, 2026, 3:00 PM"}]}
    out = send(body20b).json()
    evs = mail_sql("SELECT uc, binding_confidence, type FROM events WHERE profile='20' AND type='monetization_limited_or_off'")
    check("профиль с одним каналом → low", (evs[0]["uc"], evs[0]["binding_confidence"]), (UC1, "low"))
    # Профиль без каналов и без почты → review.
    out = send({"host": "minsk", "profile": "99", "rows": [
        {"sender": "no-reply@youtube.com", "subject": "Decision on your application",
         "snippet": "", "date": "bad date"}]}).json()
    evs = mail_sql("SELECT uc, binding_confidence, event_time FROM events WHERE profile='99'")
    check("ни почты, ни канала → review", (evs[0]["uc"], evs[0]["binding_confidence"]), (None, "review"))
    check("неразобранная дата — не выдумана: event_time = время приёма (UTC ISO)",
          evs[0]["event_time"].endswith("+00:00"), True)

    print("— отказ по телу —")
    check("без профиля — 400", send({"host": "minsk", "rows": []}).status_code, 400)
    check("слишком много строк — 400",
          send({"profile": "20", "rows": [{"subject": "x"}] * (M.ROWS_LIMIT + 1)}).status_code, 400)

    print("— чтение для пульта —")
    ev1 = M.channel_events(UC1, path=MAIL_DB)
    check("у UC1 все события профиля 20 (своих 5: 4 medium/low + повтор другой датой)", len(ev1), 5)
    check("названия по-русски", ev1[0]["title"] in M.R.TYPE_TITLES.values(), True)
    check("свежее — первым", [e["event_time"][:10] for e in ev1][:2], ["2026-10-16", "2026-10-13"])
    ev2 = M.channel_events(UC2, path=MAIL_DB)
    check("у UC2 — только событие уровня аккаунта (copyright на проверку), помечено",
          [(e["type"], e["account_level"]) for e in ev2], [("copyright", True)])
    ev3 = M.channel_events(UC3, path=MAIL_DB)
    check("у UC3 — своё (high) и уровня аккаунта",
          sorted((e["type"], e["account_level"]) for e in ev3),
          [("copyright", True), ("monetization_eligible", False)])
    check("на проверку — 2 (copyright и профиль 99)", len(M.review_events(path=MAIL_DB)), 2)
    counts = M.mail_counts([UC1, UC2, UC3, "UCnone"], path=MAIL_DB)
    check("счёт по каналам", counts, {UC1: 5, UC2: 1, UC3: 2, "UCnone": 0})
    check("базы нет — пусто, не ошибка", M.channel_events(UC1, path=os.path.join(TMP, "нет.db")), [])

    print("— читалка приёмника импортируется с общими правилами —")
    sys.path.insert(0, os.path.join(BASE, "workers", "mail"))
    import mail_monitor                                            # noqa: E402
    check("classify у читалки — из mail_rules", mail_monitor.classify is M.R.classify, True)

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
