#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пуллер сбора часов на Минске (13.10.2026): задание → сборщик → результат в студию.

Сборщик Володи подменён скриптом, который пишет то же, что настоящий: `events.jsonl` и
`reader_result.json`. Студия — настоящее приложение через TestClient. Проверяется:
результат доезжает и записывается; занятый профиль — ожидание и повтор, а не сбой;
пустой прогон — сбой со словами; чужое задание пуллер не закрывает.

14.10.2026: Мак Володи и бронь заливки. AdsPower Мака подменён словарём «отвечает /
молчит», координатор броней — скриптом, который отвечает на `inspect` так же, как
настоящий на VPS. Проверяется: молчащий Мак — задание по кнопке закрыто словами без
запуска сборщика, ночные ждут в очереди; профиль под бронью ночной обход пропускает,
не открывая; кнопка ждёт брони; кнопочное задание идёт раньше ночного.

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
                   "EARN_RUNS_DIR": os.path.join(TMP, "runs"), "EARN_BUSY_RETRY_SEC": "0",
                   "EARN_PEER_TRIES": "1", "EARN_PEER_PAUSE_SEC": "0"})
# Координатор броней: отвечает на `inspect` по файлу leases.json {профиль: держатель};
# каждый вызов пишет свои аргументы — по ним видно, что спрашивали только чтение.
with open(os.path.join(COLL, "coord.py"), "w") as _f:
    _f.write(f"""import json, os, sys
a = sys.argv[1:]
open(os.path.join({COLL!r}, 'coord_calls.txt'), 'a').write(' '.join(a) + '\\n')
try:
    leases = json.load(open(os.path.join({COLL!r}, 'leases.json')))
except Exception:
    leases = {{}}
prof = a[a.index('--profile') + 1]
h = leases.get(prof)
print(json.dumps({{'schema_version': 1, 'active': bool(h),
                   'lease': {{'profile': prof, 'holder': h}} if h else None}}))
""")
with open(os.path.join(COLL, "lease_context.json"), "w") as _f:
    json.dump({"schema_version": 1, "coordinator_command": [sys.executable,
               os.path.join(COLL, "coord.py")], "holder": "minsk", "ttl_seconds": 900,
               "renew_interval_seconds": 120, "command_timeout_seconds": 40}, _f)
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
# Второй профиль — для ночного обхода и очерёдности «кнопка раньше ночи».
UC2 = "UC" + "b" * 22
_c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,created) VALUES('t1','22','y',?)", (NOW,))
_c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,deferred_limit,"
           "created,updated,profile_id) VALUES('t1',?,'Бета','pl','Europe/Warsaw','{}',1,0,?,?,2)",
           (UC2, NOW, NOW))
CID2 = _c.execute("SELECT id FROM pub_channels WHERE uc=?", (UC2,)).fetchone()[0]
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
MAC = {"up": True, "asked": 0}


def fake_peer_get(url, timeout):
    """AdsPower Мака: отвечает списком открытых профилей или молчит."""
    MAC["asked"] += 1
    check("Мака спрашиваем тем же запросом, что сборщик",
          url, P.PEER_API + "/api/v1/browser/local-active") if MAC["asked"] == 1 else None
    if not MAC["up"]:
        raise OSError("timed out")
    return {"code": 0, "data": {"list": []}}


P._peer_get = fake_peer_get


def leases(**held):
    with open(os.path.join(COLL, "leases.json"), "w") as f:
        json.dump(held, f)


def collector_runs() -> int:
    return len([r for r in os.listdir(os.environ["EARN_RUNS_DIR"]) if not r.endswith(".log")])

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
elif mode == 'peer-down':
    ev.write(json.dumps({{'event': 'run_stopped', 'error_type': 'StopRun',
                          'reason': 'Host peer active-list unavailable (ConnectTimeout)'}}) + '\\n')
elif mode == 'gate-peer':
    d = os.path.join(out, 'profiles', prof + '_k1', 'attempt_1')
    os.makedirs(d)
    json.dump({{'status': 'skipped_busy', 'gate': {{'active_on': ['peer'], 'uploaders': []}}}},
              open(os.path.join(d, 'record.json'), 'w'))
    ev.write(json.dumps({{'event': 'checkpoint', 'stage': 'skipped_busy'}}) + '\\n')
    ev.write(json.dumps({{'event': 'run_completed'}}) + '\\n')
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

import pub_earn as E                                               # noqa: E402


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def card_of(cid):
    client.cookies.set(W.auth.COOKIE, "s-user")
    d = client.get(f"/ui/channels/{cid}").json()
    client.cookies.clear()
    return d


print("Кнопка: профиль под бронью заливки — ждём бронь, сборщик не открываем")
P.BUSY_MAX_SEC = 3600
collector("ok")
leases(**{"21": "minsk"})
_orig_holder = P.lease_holder
_seen = []


def holder_then_free(profile):
    r = _orig_holder(profile)
    _seen.append(r)
    leases()                                     # заливка закончилась — бронь снята
    return r


P.lease_holder = holder_then_free
before = collector_runs()
job = queue_and_claim()
check("задание по кнопке — не ночное", job.get("auto"), False)
P.handle(job)
P.lease_holder = _orig_holder
check("бронь увидели, потом свободно", _seen[:2], ["minsk", ""])
check("сборщик запущен один раз — уже без брони", collector_runs() - before, 1)
check("кнопка дождалась и собрала", card()["earn_request"]["last"]["status"], "done")
calls = open(os.path.join(COLL, "coord_calls.txt")).read().split("\n")
check("у координатора только спрашивали: inspect --profile 21", calls[0], "inspect --profile 21")

print("Ежедневный обход: занятый профиль пропускается, а не ждётся")
c = db()
E.set_auto_settings(c, on=True, hour=0)
c.commit()
out = E.daily_tick(c, "t1", now=time.time())
c.close()
check("обход поставил оба профиля", out["queued"], ["21", "22"])
leases(**{"21": "minsk"})
collector("gate-peer")
before = collector_runs()
check("пуллер взял задание", P.once(), True)
last = card()["earn_request"]["last"]
check("21 под бронью: пропущен, не сбой", last["status"], "skipped")
check("в заметке — почему и когда соберётся",
      "профиль занят заливкой на Минске" in last["note"] and "следующий проход" in last["note"], True)
check("сборщик ради занятого профиля не запускался", collector_runs() - before, 0)
leases()
P.once()
last2 = card_of(CID2)["earn_request"]["last"]
check("22 открыт на Маке (сборщик сказал skipped_busy) — пропущен",
      (last2["status"], "открыт на Маке Володи" in last2["note"]), ("skipped", True))
c = db()
st = E.auto_status(c, "t1")
c.close()
check("итог обхода: пропущено 2, профили названы",
      (st["last"]["counts"], st["last"]["skipped_profiles"]), ({"skipped": 2}, ["21", "22"]))

print("Мак Володи молчит: кнопка — ответ словами сразу, ночное — ждёт в очереди")
MAC["up"] = False
P._PEER.update(at=0.0, ok=None)
c = db()
E.request_collect(c, "t1", [CID2], by=E.AUTO_BY, auto=True)
c.commit()
c.close()
client.cookies.set(W.auth.COOKIE, "s-user")
client.post(f"/ui/channels/{CID}/earn/collect")
client.cookies.clear()
before = collector_runs()
check("кнопочное задание взято", P.once(), True)
last = card()["earn_request"]["last"]
check("сбой словами про Мак, без исключения сборщика",
      (last["status"], last["note"]), ("failed", P.PEER_DOWN_NOTE))
check("сборщик не запускался", collector_runs() - before, 0)
check("ночное пуллер не берёт, пока Мак молчит", P.once(), False)
c = db()
row = c.execute("SELECT status FROM pub_earn_requests WHERE profile='22' ORDER BY id DESC").fetchone()
check("ночное ждёт в очереди", row[0], "queued")
c.close()
d2 = card_of(CID2)["earn_request"]
check("карточка знает, что Мак молчит", bool(d2.get("peer_down_since")), True)
c = db()
check("на экране обхода — тоже", E.auto_status(c, "t1")["host"].get("peer_ok"), False)
E.claim_request(c, "minsk", now=time.time() + 13 * 3600, manual_only=True, peer_ok=False)
c.commit()
row = c.execute("SELECT status, note FROM pub_earn_requests WHERE profile='22' "
                "ORDER BY id DESC").fetchone()
check("за полсуток Мак не ответил — так и сказано, а не «пуллер не работает»",
      (row[0], "Мак Володи так и не ответил" in row[1]), ("failed", True))
c.close()

print("Мак ожил: кнопка раньше ночного; Мак пропал посреди сбора — словами")
MAC["up"] = True
P._PEER.update(at=0.0, ok=None)
c = db()
E.request_collect(c, "t1", [CID2], by=E.AUTO_BY, auto=True, now=time.time() - 60)
c.commit()
c.close()
client.cookies.set(W.auth.COOKIE, "s-user")
client.post(f"/ui/channels/{CID}/earn/collect")
client.cookies.clear()
collector("ok")
job = fake_call("/claim", {"host": "minsk", "peer_ok": True})["job"]
check("первым выдано кнопочное, хотя ночное старше", (job["profile"], job["auto"]), ("21", False))
P.handle(job)
check("кнопочное собрано", card()["earn_request"]["last"]["status"], "done")
collector("peer-down")
check("следом — ночное", P.once(), True)
last2 = card_of(CID2)["earn_request"]["last"]
check("Мак пропал во время сбора — понятная причина",
      (last2["status"], last2["note"]), ("failed", P.PEER_DOWN_NOTE))
check("пуллер запомнил, что Мак молчит", P._PEER["ok"], False)

print()
if FAILS:
    print(f"провалов: {len(FAILS)}")
    sys.exit(1)
print("все проверки прошли")
