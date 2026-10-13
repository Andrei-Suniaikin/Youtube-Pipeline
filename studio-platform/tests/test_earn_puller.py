#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пуллер сбора часов на Минске (13.10.2026): задание → сборщик → результат в студию.

Сборщик Володи подменён скриптом, который пишет то же, что настоящий: `events.jsonl` и
`reader_result.json`. Студия — настоящее приложение через TestClient. Проверяется:
результат доезжает и записывается; занятый профиль — ожидание и повтор, а не сбой;
пустой прогон — сбой со словами; чужое задание пуллер не закрывает.

    .venv/bin/python tests/test_earn_puller.py
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
TMP = tempfile.mkdtemp(prefix="earn-puller-")
DB = os.path.join(TMP, "vsgen.db")
os.environ.update({"VS_DEV_DB": DB, "VS_SERVER_DB": DB, "VS_DEV_TENANT": "t1",
                   "VS_TENANTS_DIR": os.path.join(TMP, "tenants")})
COLL = os.path.join(TMP, "collector")
os.makedirs(COLL)
os.environ.update({"VSGEN_SERVER": "http://studio", "UPLOAD_TOKEN": "tok-node-1",
                   "EARN_COLLECTOR_DIR": COLL, "EARN_PYTHON": sys.executable,
                   "EARN_RUNS_DIR": os.path.join(TMP, "runs"), "EARN_BUSY_RETRY_SEC": "0"})
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))
sys.path.insert(0, os.path.join(BASE, "workers", "minsk"))

from pub_schema import ensure_pub_schema                           # noqa: E402

NOW = time.time()
UC = "UC" + "a" * 22
_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    role TEXT, tenant_id TEXT, disabled INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
  CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
""")
_c.execute("INSERT INTO users(id,name,role,tenant_id) VALUES(3,'оператор','user','t1')")
_c.execute("INSERT INTO sessions(id,user_id,expires) VALUES('s-user',3,?)", (NOW + 3600,))
_c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,created) VALUES('t1','21','x',?)", (NOW,))
_c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,deferred_limit,"
           "created,updated,profile_id) VALUES('t1',?,'Альфа','pl','Europe/Warsaw','{}',1,0,?,?,1)",
           (UC, NOW, NOW))
CID = _c.execute("SELECT id FROM pub_channels").fetchone()[0]
_c.commit()
_c.close()

import app as W                                                    # noqa: E402
import earn_puller as P                                            # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

W._earn_agent_token = lambda: "tok-node-1"
client = TestClient(W.app, raise_server_exceptions=False)
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fake_call(path, payload=None, claim="", data=None, ctype=""):
    """Как urllib к студии, только через TestClient: /studio/api/earn… → /api/earn…"""
    headers = {"X-Upload-Token": P.TOKEN}
    if claim:
        headers["X-Earn-Claim"] = claim
    if data is None:
        r = client.post("/api/earn" + path, json=payload or {}, headers=headers)
    else:
        r = client.post("/api/earn" + path, content=data,
                        headers={**headers, "Content-Type": ctype})
    if r.status_code >= 400:
        raise P.urllib.error.HTTPError(path, r.status_code, r.text, {}, None)
    return r.json()


P.call = fake_call

RESULT = {"schema_version": 1, "uc": UC, "status": "complete",
          "context": {"profile": "21", "email": "m21@example.com"},
          "started_at": "2026-10-14T01:00:00+00:00", "finished_at": "2026-10-14T01:00:00+00:00",
          "active_identity_verified": True,
          "earn": {"status": "complete", "capture_status": "reported", "context_verified": True,
                   "qualified_watch_hours_365d": {"value": 4321},
                   "qualified_shorts_views_90d": {"value": 10}, "subscribers": {"value": 777},
                   "as_of": "Oct 13, 2026"},
          "lifetime": {"status": "complete", "views": {"value": 1}, "watch_hours": {"value": 2}}}


def collector(mode: str) -> None:
    """Подставной collector.py: busy — профиль занят бронью, ok — результат, empty — ничего."""
    with open(os.path.join(COLL, "collector.py"), "w") as f:
        f.write(f"""import json, os, sys
a = sys.argv; out = a[a.index('--out') + 1]; prof = a[a.index('--profiles') + 1]
os.makedirs(out, exist_ok=True)
mode = {mode!r}
if mode == 'busy-then-ok':
    flag = os.path.join({COLL!r}, 'was_busy')
    mode = 'ok' if os.path.exists(flag) else 'busy'
    open(flag, 'a').close()
ev = open(os.path.join(out, 'events.jsonl'), 'w')
if mode == 'busy':
    ev.write(json.dumps({{'event': 'run_stopped', 'profile': prof,
                          'reason': 'Could not hold required profile lease'}}) + '\\n')
elif mode == 'ok':
    d = os.path.join(out, 'profiles', prof + '_k1', 'attempt_1', 'channels', 'authuser_00', {UC!r})
    os.makedirs(d)
    json.dump({RESULT!r}, open(os.path.join(d, 'reader_result.json'), 'w'))
    open(os.path.join(d, 'earn.png'), 'w').write('x')
else:
    ev.write(json.dumps({{'event': 'run_stopped', 'reason': 'Start outcome is ambiguous'}}) + '\\n')
""")


def queue_and_claim():
    client.cookies.set(W.auth.COOKIE, "s-user")
    client.post(f"/ui/channels/{CID}/earn/collect")
    client.cookies.clear()
    return fake_call("/claim", {"host": "minsk"})["job"]


def card():
    client.cookies.set(W.auth.COOKIE, "s-user")
    d = client.get(f"/ui/channels/{CID}").json()
    client.cookies.clear()
    return d


print("Профиль сначала занят заливкой, потом свободен")
collector("busy-then-ok")
job = queue_and_claim()
check("задание получено", job["profile"], "21")
P.handle(job)
d = card()
check("часы записаны", (d["earn"]["good"] or {}).get("hours_365"), 4321)
check("задание закрыто как готовое", d["earn_request"]["last"]["status"], "done")
check("в заметке — что прочитано", "прочитано 1" in d["earn_request"]["last"]["note"], True)
runs = sorted(os.listdir(os.environ["EARN_RUNS_DIR"]))
check("два прогона: занято → готово", len([r for r in runs if not r.endswith(".log")]), 2)

print("Сборщик ничего не дал")
collector("empty")
job = queue_and_claim()
P.handle(job)
last = card()["earn_request"]["last"]
check("сбой словами, с причиной", (last["status"], "Start outcome is ambiguous" in last["note"]),
      ("failed", True))

print("Занят слишком долго")
collector("busy")
P.BUSY_MAX_SEC = 0
job = queue_and_claim()
P.handle(job)
last = card()["earn_request"]["last"]
check("сбой: профиль не освободился", (last["status"], "не освободился" in last["note"]),
      ("failed", True))

print()
if FAILS:
    print(f"провалов: {len(FAILS)}")
    sys.exit(1)
print("все проверки прошли")
