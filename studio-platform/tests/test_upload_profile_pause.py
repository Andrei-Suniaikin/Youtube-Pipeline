#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пауза профиля или канала после отказа, который чинит только человек (21.10.2026).

Живой случай: профиль 62 разлогинен, студия за 50 минут выдала в него десять выпусков
Baśnie Leszego, каждый пробовал войти сам и сжёг суточный лимит Google. Теперь:

  • «account recovery needs the owner» / «RecoveryBlocked» — профиль без входа: стоит ВЕСЬ
    профиль, задания в нём ждут с причиной «на паузе», в журнале сказано один раз;
  • «not logged in / no access» / «активный аккаунт не управляет каналом» — стоит только
    канал, соседний канал того же профиля льётся;
  • паузу снимает «Вернуть в работу» выпуска, успешная заливка или срок (одна попытка, при
    том же отказе пауза ставится снова);
  • вердикт с номером ролика паузу не ставит (ролик на канале есть, это не вход).

    VS_TEST_NO_NET=1 .venv/bin/python tests/test_upload_profile_pause.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="upload-pause-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["VS_PROFILE_PAUSE_SEC"] = "3600"
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

import requests                                                    # noqa: E402

from pub_schema import ensure_pub_schema                           # noqa: E402

_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    role TEXT, tenant_id TEXT, disabled INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
  CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
    kind TEXT, status TEXT, created REAL, finished REAL, project_id TEXT, error TEXT);
  CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT);
""")
from pub_calls import ensure_calls_schema                          # noqa: E402

ensure_calls_schema(_c)
_c.commit()
_c.close()

from fastapi import FastAPI                                        # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

import pub_adspower as A                                           # noqa: E402
import pub_engine                                                  # noqa: E402
from pub_upload_api import register_upload_routes                  # noqa: E402

FAILS = []
TENANT = "t1"
TOKEN = "upl_test_token"
ADS_URL = "http://127.0.0.1:50325"
CLOCK = {"t": time.time()}


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def check_true(label, cond):
    check(label, bool(cond), True)


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def sql(q, args=()):
    c = conn()
    try:
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()


def run(q, args=()):
    c = conn()
    try:
        c.execute(q, args)
        c.commit()
    finally:
        c.close()


def job(release_id):
    return sql("SELECT * FROM pub_upload_jobs WHERE release_id=?", (release_id,))[0]


def journal(release_id):
    return " | ".join(r["message"] for r in sql(
        "SELECT message FROM pub_release_log WHERE release_id=? ORDER BY id", (release_id,)))


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _ads_get(url, params=None, timeout=None):
    return FakeResponse({"code": 0, "msg": "success", "data": {"list": []}})


A.requests = type("R", (), {"get": staticmethod(_ads_get),
                            "RequestException": requests.RequestException})

app = FastAPI()
register_upload_routes(app, {"db": conn, "_now": lambda: CLOCK["t"],
                             "upload_token": lambda: TOKEN})
client = TestClient(app, raise_server_exceptions=False)


def claim(worker="1"):
    A._reset_active_cache_for_tests()
    A._reset_pace_state_for_tests()
    r = client.post("/api/upload/claim", json={"host": "minsk", "worker": worker},
                    headers={"X-Upload-Token": TOKEN})
    return r.json().get("task")


def result(release_id, claim_token, error="", success=False, video_id=""):
    body = {"claim": claim_token, "result": {"success": success, "error": error,
                                             "video_id": video_id, "do_not_reupload": False}}
    if success:
        body["result"]["exact_studio_evidence"] = {
            "studio_state": "SCHEDULED", "verified_channel_uc": job(release_id)["uc"]}
    r = client.post(f"/api/upload/{release_id}/result", json=body,
                    headers={"X-Upload-Token": TOKEN, "X-Upload-Claim": claim_token})
    return r.json()


UCS = {"a": "UC" + "a" * 22, "b": "UC" + "b" * 22, "c": "UC" + "c" * 22}


def seed():
    c = conn()
    now = CLOCK["t"]
    c.execute("INSERT INTO pub_hosts(id,tenant_id,code,title,adspower_url,status,created) "
              "VALUES(1,?,'minsk','Минская машина',?,'active',?)", (TENANT, ADS_URL, now))
    for serial, uid in (("62", "u62"), ("27", "u27")):
        c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,default_host_id,status,"
                  "created,adspower_user_id,active_now,last_seen) "
                  "VALUES(?,?,?,1,'active',?,?,0,?)",
                  (TENANT, serial, f"Профиль {serial}", now, uid, now))
    prof = {r[1]: r[0] for r in c.execute("SELECT id, serial FROM pub_profiles")}
    # Каналы: a и b в профиле 62, c в профиле 27.
    for n, (key, serial) in enumerate((("a", "62"), ("b", "62"), ("c", "27")), start=1):
        c.execute("INSERT INTO pub_channels(id,tenant_id,uc,name,profile_id,status,created,updated) "
                  "VALUES(?,?,?,?,?,'active',?,?)",
                  (n, TENANT, UCS[key], f"Канал {key}", prof[serial], now, now))
    rows = (("rel-a1", "a", 1), ("rel-a2", "a", 1), ("rel-b1", "b", 2), ("rel-c1", "c", 3))
    for n, (rid, key, cid) in enumerate(rows, start=1):
        serial = {"a": "62", "b": "62", "c": "27"}[key]
        c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
                  "plan_item_version,channel_id,uc,slot_at,status,phase,created,updated) "
                  "VALUES(?,?,?,1,?,?,?,'running','upload_pending',?,?)",
                  (rid, TENANT, n, cid, UCS[key], now + 86400, now, now))
        c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,"
                  "profile_id,host_id,channel_url,title,description,schedule_iso,"
                  "visibility,video_path,slot_at,status,created,updated) "
                  "VALUES(?,?,?,?,?,1,?,?,'','','public',?,?,'queued',?,?)",
                  (rid, TENANT, cid, UCS[key], prof[serial],
                   f"https://studio.youtube.com/channel/{UCS[key]}/", f"ролик {rid}",
                   os.path.join(TMP, "v.mp4"), now + 86400, now, now))
    c.commit()
    c.close()
    return prof


def main() -> int:
    prof = seed()
    open(os.path.join(TMP, "v.mp4"), "wb").write(b"\0" * 10)

    print("Разлогиненный профиль: первый отказ ставит паузу на весь профиль")
    t1 = claim()
    check("первое задание выдано", (t1 or {}).get("release_id"), "rel-a1")
    result("rel-a1", t1["claim_token"], error="account recovery needs the owner: too many recovery "
                                        "attempts for this account today")
    pause = sql("SELECT v FROM admin_state WHERE k=?", (f"pub_pause:profile:{prof['62']}",))
    check_true("пауза профиля записана", pause)
    check_true("в журнале выпуска сказано про паузу", "ставлю на паузу" in journal("rel-a1"))

    print("Следующие задания того же профиля не выдаются, соседний профиль льётся")
    t2 = claim(worker="2")
    check("выдано задание другого профиля (27)", (t2 or {}).get("release_id"), "rel-c1")
    a2 = job("rel-a2")
    check("задание канала a ждёт", a2["status"], "queued")
    check_true("с причиной «на паузе»", "на паузе" in (a2["blocked_why"] or ""))
    check_true("причина в журнале выпуска", "на паузе" in journal("rel-a2"))
    b1 = job("rel-b1")
    check_true("задание канала b того же профиля тоже ждёт", "на паузе" in (b1["blocked_why"] or ""))
    check("третий опрос ничего не выдаёт", claim(worker="3"), None)
    said = journal("rel-a2").count("на паузе")
    claim(worker="3")
    check("повторный опрос журнал не засоряет", journal("rel-a2").count("на паузе"), said)

    print("«Вернуть в работу» выпуска снимает паузу профиля")
    run("UPDATE pub_releases SET status='blocked', blocked_reason='x' WHERE release_id='rel-a1'")
    c = conn()
    out = pub_engine.revive(c, "rel-a1", CLOCK["t"])
    c.commit()
    c.close()
    check("возврат прошёл", out.get("ok"), True)
    check("пауза профиля снята", sql("SELECT v FROM admin_state WHERE k=?",
                                     (f"pub_pause:profile:{prof['62']}",)), [])
    check_true("в журнале сказано, что пауза снята", "пауза профиля снята" in journal("rel-a1"))
    t3 = claim(worker="4")
    check("задание канала a выдано", (t3 or {}).get("release_id"), "rel-a2")
    check("пометка ожидания снята", (job("rel-a2")["blocked_since"], job("rel-a2")["blocked_why"]),
          (None, None))

    print("Нет прав на канал: стоит только канал, сосед по профилю льётся")
    result("rel-a2", t3["claim_token"], error="активный аккаунт не управляет каналом: Studio показала "
                                        "«нет доступа», канал бренда в переключателе не выбран")
    check_true("пауза канала a записана",
               sql("SELECT v FROM admin_state WHERE k=?", (f"pub_pause:channel:{UCS['a']}",)))
    check("паузы профиля нет", sql("SELECT v FROM admin_state WHERE k=?",
                                   (f"pub_pause:profile:{prof['62']}",)), [])
    t4 = claim(worker="5")
    check("канал b того же профиля выдан", (t4 or {}).get("release_id"), "rel-b1")

    print("Успешная заливка снимает паузу канала и профиля")
    run("INSERT INTO admin_state(k,v,updated) VALUES(?,?,?)",
        (f"pub_pause:profile:{prof['62']}", '{"since":0,"until":9e12,"why":"x","release_id":"r"}', 0))
    out = result("rel-b1", t4["claim_token"], success=True, video_id="abcdefghijk")
    check("вердикт принят как успех", out.get("verdict"), "scheduled")
    check("пауза профиля снята успехом", sql("SELECT v FROM admin_state WHERE k=?",
                                            (f"pub_pause:profile:{prof['62']}",)), [])
    check_true("в журнале сказано", "пауза профиля снята" in journal("rel-b1"))

    print("Срок паузы вышел — одна попытка разрешена")
    check_true("пауза канала a ещё стоит",
               sql("SELECT v FROM admin_state WHERE k=?", (f"pub_pause:channel:{UCS['a']}",)))
    run("UPDATE pub_upload_jobs SET status='queued', host=NULL, worker=NULL, lease_until=NULL, "
        "claim_token=NULL, blocked_since=NULL, blocked_why=NULL WHERE release_id='rel-a2'")
    run("UPDATE pub_releases SET status='running' WHERE release_id='rel-a2'")
    check("до срока — не выдаётся", claim(worker="6"), None)
    CLOCK["t"] += 3601
    t5 = claim(worker="6")
    check("после срока — выдано", (t5 or {}).get("release_id"), "rel-a2")
    check("просроченная пауза удалена", sql("SELECT v FROM admin_state WHERE k=?",
                                            (f"pub_pause:channel:{UCS['a']}",)), [])

    print("Вердикт с номером ролика паузу не ставит")
    result("rel-a2", t5["claim_token"], error="not logged in / no access: x", video_id="zzzzzzzzzzz")
    check("паузы канала нет", sql("SELECT v FROM admin_state WHERE k=?",
                                  (f"pub_pause:channel:{UCS['a']}",)), [])

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
