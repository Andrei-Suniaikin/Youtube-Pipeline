#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Часы для монетизации: выхлоп сборщика Минска → карточка канала (13.10.2026).

Файлы `reader_result.json` собраны по коду считывателя Володи (`studio_reader.py`):
`earn` — то, что вернул `parse_earn` + `context_verified`, `lifetime` — разбор
lifetime-итога. Проверяется главное:

  * число без подтверждения «читали именно этот канал» не принимается;
  * незнакомый студии UC не теряется молча, а называется в отчёте;
  * пробный разбор ничего не пишет, запись повторной загрузкой не задваивается;
  * свежий сбой не стирает вчерашние часы, но виден;
  * ручка только для админа.

    .venv/bin/python tests/test_earn_import.py
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="earn-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
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
  CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
    kind TEXT, status TEXT, created REAL, finished REAL, project_id TEXT, error TEXT);
  CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT, tenant_id TEXT,
    settings TEXT);
""")
_c.execute("INSERT INTO users(id,name,role,tenant_id) VALUES(1,'хозяин','admin','t1')")
_c.execute("INSERT INTO users(id,name,role,tenant_id) VALUES(3,'оператор','user','t1')")
for sid, uid in (("s-admin", 1), ("s-user", 3)):
    _c.execute("INSERT INTO sessions(id,user_id,expires) VALUES(?,?,?)", (sid, uid, NOW + 3600))
UC = {k: "UC" + k * 22 for k in "abcde"}
CID = {}
for k, name in (("a", "Альфа"), ("b", "Бета"), ("c", "Гамма"), ("d", "Дельта")):
    _c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
               "deferred_limit,created,updated) VALUES('t1',?,?,'pl','Europe/Warsaw',"
               "'{}',1,0,?,?)", (UC[k], name, NOW, NOW))
    CID[k] = _c.execute("SELECT id FROM pub_channels WHERE uc=?", (UC[k],)).fetchone()[0]
_c.commit()
_c.close()

import app as W                                                    # noqa: E402
import pub_earn as E                                               # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def num(v):
    return {"value": v, "raw_values": [str(v)], "availability": "reported", "reason": None}


def result(uc, *, hours=1234, subs=321, verified=True, ctx=True, status="complete",
           capture="reported", needs_country=False, at="2026-10-13T03:00:00+00:00"):
    """Как пишет `read_channel`: earn — разбор `parse_earn` плюс подтверждение сессии."""
    earn = {"status": status, "capture_complete": status == "complete",
            "numeric_complete": hours is not None, "capture_status": capture,
            "qualified_watch_hours_365d": (num(hours) if hours is not None else
                                           {"value": None, "availability": "unknown",
                                            "reason": "metric_missing_or_conflicting"}),
            "qualified_shorts_views_90d": num(5000),
            "subscribers": num(subs), "as_of": "Oct 12, 2026", "needs_country": needs_country,
            "context_verified": ctx, "evidence": "/tmp/earn.json"}
    life = {"status": "complete", "views": {"value": 98765}, "watch_hours": {"value": 4321.5}}
    return {"schema_version": 1, "uc": uc, "status": "complete",
            "context": {"profile": "13", "authuser": 2, "email": "m07@example.com"},
            "started_at": at, "finished_at": at, "active_identity_verified": verified,
            "earn": earn, "lifetime": life, "features": {"status": "complete"}}


def zipped(docs):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, d in enumerate(docs):
            z.writestr(f"run/channels/authuser_0{i}/{d['uc']}/reader_result.json", json.dumps(d))
        z.writestr("run/events.jsonl", "{}\n")
        z.writestr("run/channels/authuser_00/x/earn.png", b"\x89PNG")
    return buf.getvalue()


print("Разбор одного результата")
r = E.reading(result(UC["a"]))
check("принятое чтение: часы, подписчики, lifetime", (r["state"], r["hours_365"], r["subs"],
      r["life_views"], r["life_hours"], r["email"]), ("ok", 1234, 321, 98765, 4321.5, "m07@example.com"))
r = E.reading(result(UC["a"], verified=False))
check("без подтверждения канала — число не принято", (r["state"], r["hours_365"], r["subs"],
      r["life_views"]), ("unverified", None, None, None))
r = E.reading(result(UC["a"], ctx=False))
check("без подтверждения сессии на Earn — тоже", (r["state"], r["hours_365"]), ("unverified", None))
r = E.reading(result(UC["a"], hours=None, status="needs_country", needs_country=True))
check("страна не выбрана", (r["state"], r["hours_365"]), ("needs_country", None))
r = E.reading(result(UC["a"], hours=None, capture="observed_ypp_member"))
check("уже в партнёрке — счётчика нет, это не сбой", (r["state"], r["hours_365"], r["subs"]),
      ("partner", None, 321))
r = E.reading({"uc": UC["a"], "started_at": "2026-10-13T03:00:00+00:00", "status": "partial",
               "earn": {"status": "error", "reason": "IdentityError: wrong owner"}})
check("сбой страницы — с причиной", (r["state"], r["reason"]), ("error", "IdentityError: wrong owner"))
check("не результат считывателя", E.reading({"uc": "не-UC"}), None)
check("без времени — не чтение", E.reading({"uc": UC["a"]}), None)

print("Файл целиком")
docs = [result(UC["a"]), result(UC["b"], verified=False),
        result(UC["c"], hours=None, status="needs_country", needs_country=True),
        result(UC["e"])]
readings, notes = E.parse_upload("earn.zip", zipped(docs))
check("из архива взяты только reader_result.json", len(readings), 4)
check("замечаний нет", notes, [])
one, _ = E.parse_upload("reader_result.json", json.dumps(result(UC["a"])).encode())
check("отдельный JSON тоже принимается", len(one), 1)
for bad, why in ((b"PK\x03\x04junk", "архив не открывается"),
                 (b"not json", "ждём zip папки сборщика или reader_result.json")):
    try:
        E.parse_upload("x", bad)
        check(f"отказ: {why}", "принято", why)
    except ValueError as exc:
        check(f"отказ: {why}", str(exc), why)

client = TestClient(W.app, raise_server_exceptions=False)


def as_(sid):
    client.cookies.clear()
    client.cookies.set(W.auth.COOKIE, sid)


def upload(data, apply=0, name="earn.zip"):
    return client.post("/ui/analytics/earn/import" + ("?apply=1" if apply else ""),
                       files={"file": (name, data, "application/zip")})


print("Ручка")
as_("s-user")
check("оператору нельзя", upload(zipped(docs)).status_code, 403)
as_("s-admin")
res = upload(zipped(docs))
check("пробный разбор отвечает", res.status_code, 200)
rep = res.json()["report"]
check("пробный: каналов студии 3, прочитан 1", (rep["channels"], rep["states"].get("ok")), (3, 1))
check("незнакомый UC назван", rep["unknown_uc"], [UC["e"]])
c = sqlite3.connect(DB)
check("пробный ничего не записал",
      c.execute("SELECT name FROM sqlite_master WHERE name='pub_channel_earn'").fetchone() is None
      or c.execute("SELECT COUNT(*) FROM pub_channel_earn").fetchone()[0] == 0, True)
res = upload(zipped(docs), apply=1).json()
check("записано 3 чтения", res["written"], 3)
check("повтор того же файла не задваивает", upload(zipped(docs), apply=1).json()["written"], 0)

print("Карточка канала")
d = client.get(f"/ui/channels/{CID['a']}").json()
e = d.get("earn") or {}
check("часы в карточке", ((e.get("good") or {}).get("hours_365"), e.get("goal_hours")), (1234, 4000))
d = client.get(f"/ui/channels/{CID['b']}").json()["earn"]
check("неподтверждённое: принятого числа нет, сбор виден",
      (d["good"], d["last"]["state"]), (None, "unverified"))
check("канал без сбора — пусто", client.get(f"/ui/channels/{CID['d']}").json()["earn"], {})
# Свежий сбой поверх вчерашнего числа.
upload(zipped([result(UC["a"], hours=1500, at="2026-10-14T03:00:00+00:00")]), apply=1)
upload(zipped([result(UC["a"], verified=False, at="2026-10-15T03:00:00+00:00")]), apply=1)
e = client.get(f"/ui/channels/{CID['a']}").json()["earn"]
check("сбой не стёр последнее число", (e["good"]["hours_365"], e["last"]["state"]), (1500, "unverified"))
check("прирост к прошлому сбору считается", e["prev"]["hours_365"], 1234)
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
check("сводка по каналам — последнее принятое", E.overview(c, "t1")[CID["a"]]["hours_365"], 1500)

print("Почты каналов")
r = E.reading(result(UC["a"], verified=False))
check("неподтверждённое чтение почту не несёт", (r["email"], r["profile"]), ("", ""))
_c = sqlite3.connect(DB)
_c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,created) VALUES('t1','21','x',?)", (NOW,))
_pid = _c.execute("SELECT id FROM pub_profiles WHERE serial='21'").fetchone()[0]
_c.execute("UPDATE pub_channels SET profile_id=? WHERE id IN (?,?)", (_pid, CID["a"], CID["c"]))
_c.commit(); _c.close()
other = result(UC["c"], at="2026-10-16T03:00:00+00:00")
other["context"] = {"profile": "13", "email": "m07@example.com"}
upload(zipped([other]), apply=1)
as_("s-user")
check("оператору почты тоже видны", (client.get(f"/ui/channels/{CID['a']}/email").status_code,
      client.get("/ui/analytics/earn/emails").status_code), (200, 200))
client.cookies.clear()
check("без входа — нет", client.get("/ui/analytics/earn/emails").status_code, 401)
as_("s-admin")
e = client.get(f"/ui/channels/{CID['a']}/email").json()
check("почта канала — из подтверждённого чтения", (e["email"], e["profile"], e["other_profile"]),
      ("m07@example.com", "13", True))
g = client.get("/ui/analytics/earn/emails").json()
check("одна почта — два канала вместе", [(x["email"], len(x["channels"])) for x in g["groups"]],
      [("m07@example.com", 2)])
check("канал без подтверждённых чтений — в «без почты»", g["without"], ["Бета", "Дельта"])
check("чужой профиль отмечен", sorted(ch["channel"] for ch in g["groups"][0]["channels"]
                                    if ch["other_profile"]), ["Альфа", "Гамма"])
check("канал другого тенанта — 404", client.get("/ui/channels/99999/email").status_code, 404)

print()
if FAILS:
    print(f"провалов: {len(FAILS)}")
    sys.exit(1)
print("все проверки прошли")
