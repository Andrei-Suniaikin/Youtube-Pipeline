#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Нода получает серийный номер AdsPower, а не номер строки студии (04.10.2026).

Ни одно из 10 заданий студии не залилось. Заливщик на минской машине писал:

    Канал 'Tichá Obloha' (UC UCAvlAWXAk7yitpFLLIwBlgg) не найден в переключателе.
    Доступно: Apinya Layoo | Cartierul General
    ABORT (роутинг): account recovery needs the owner: no exact account binding …

Нода открывает AdsPower по полю `profile` как по серийному номеру (цифры → serial_number,
`upload_worker._ads_param`). В старом кабинете таблицы профилей нет, номер у канала и есть
серийный. В студии номер канала — строка `pub_profiles.id`, а серийный лежит рядом, и они
разошлись: у «Tichá Obloha» профиль 43 = AdsPower №24, а нода открыла №43 — чужой
аккаунт. По всем 30 каналам видно, что выбирали верно: польские каналы на «рыба польша»,
чешские на «рыба чех»; не так читала нода.

Что проверяется:
  • в задании — серийный номер профиля, числом, как у старого кабинета;
  • повторная выдача того же задания — тоже серийный;
  • у профиля нет серийного номера — задание НЕ выдаётся (нода открыла бы чужой),
    причина сказана; задание другого канала при этом идёт;
  • строки профиля нет (номер вписан руками по-старому) — номер отдаётся как есть;
  • в журнале выпуска виден и наш номер, и номер AdsPower.

    VS_TEST_NO_NET=1 .venv/bin/python tests/test_upload_profile_serial.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="upload-serial-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))

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
import pub_upload_api as U                                         # noqa: E402

FAILS = []
TENANT = "t1"
TOKEN = "upl_test_token"
ADS_URL = "http://127.0.0.1:50325"
NOW = {"t": time.time()}


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


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


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def fake_get(url, params=None, timeout=None):
    # Никто посторонний не открыт: проверяется только номер профиля в задании.
    return FakeResponse({"code": 0, "msg": "success", "data": {"list": []}})


A.requests = type("R", (), {"get": staticmethod(fake_get),
                            "RequestException": requests.RequestException})

app = FastAPI()
U.register_upload_routes(app, {"db": conn, "_now": lambda: NOW["t"],
                               "upload_token": lambda: TOKEN})
client = TestClient(app, raise_server_exceptions=False)


def claim(worker="3", host="minsk"):
    r = client.post("/api/upload/claim", json={"host": host, "worker": worker},
                    headers={"X-Upload-Token": TOKEN})
    return r.json().get("task")


SEQ = [0]


def profile(serial, uid):
    c = conn()
    c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,default_host_id,status,"
              "created,adspower_user_id,active_now,last_seen) VALUES(?,?,?,1,'active',?,?,0,?)",
              (TENANT, serial, f"Профиль {serial}", NOW["t"], uid, NOW["t"]))
    c.commit()
    pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.close()
    return pid


def job(rid, profile_id):
    SEQ[0] += 1
    n = SEQ[0]
    uc = "UC" + chr(96 + n) * 22
    c = conn()
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,status,phase,created,updated) "
              "VALUES(?,?,?,1,?,?,?,'pending','created',?,?)",
              (rid, TENANT, n, n, uc, NOW["t"] + 86400, NOW["t"], NOW["t"]))
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,host_id,"
              "channel_url,title,description,schedule_iso,visibility,video_path,slot_at,status,"
              "created,updated) VALUES(?,?,?,?,?,1,?,?,'','','public',?,?,'queued',?,?)",
              (rid, TENANT, n, uc, profile_id, f"https://studio.youtube.com/channel/{uc}/",
               f"ролик {rid}", os.path.join(TMP, "v.mp4"), NOW["t"] + 86400 + n, NOW["t"],
               NOW["t"]))
    c.commit()
    c.close()


def only_queued(rid):
    run("UPDATE pub_upload_jobs SET status='done' WHERE release_id<>? AND status='queued'", (rid,))


def journal(rid):
    return " | ".join(r["message"] for r in sql(
        "SELECT message FROM pub_release_log WHERE release_id=? ORDER BY id", (rid,)))


def main() -> int:
    c = conn()
    c.execute("INSERT INTO pub_hosts(id,tenant_id,code,title,adspower_url,status,created) "
              "VALUES(1,?,'minsk','Минская машина',?,'active',?)", (TENANT, ADS_URL, NOW["t"]))
    c.commit()
    c.close()
    # Номера строк студии нарочно не совпадают с серийными, как на бою.
    filler = [profile(str(900 + i), f"f{i}") for i in range(5)]
    p24 = profile("24", "k1brhhqe")            # строка 6 = AdsPower №24
    check("номер строки не совпадает с серийным", p24 != 24, True)

    print("Задание уходит ноде с серийным номером AdsPower")
    job("rel-tiha", p24)
    only_queued("rel-tiha")
    t = claim()
    check("задание выдано", bool(t), True)
    check("в задании серийный №24, а не номер строки", (t or {}).get("profile"), 24)
    check("числом, как у старого кабинета", type((t or {}).get("profile")), int)
    log = journal("rel-tiha")
    check("в журнале оба номера", f"профиль {p24} (AdsPower №24)" in log, True)

    print("\n  повторная выдача той же ноде — тот же серийный")
    t2 = claim()
    check("повтор отдаёт то же задание", (t2 or {}).get("release_id"), "rel-tiha")
    check("и тот же номер AdsPower", (t2 or {}).get("profile"), 24)

    print("\nУ профиля нет серийного номера — задание ждёт, а не открывает чужой профиль")
    run("UPDATE pub_upload_jobs SET status='done' WHERE release_id='rel-tiha'")
    nos = profile("", "")
    job("rel-noserial", nos)
    job("rel-ok", profile("31", "k1ok"))
    t = claim()
    check("выдано задание другого канала", (t or {}).get("release_id"), "rel-ok")
    check("с его номером", (t or {}).get("profile"), 31)
    j = sql("SELECT status, blocked_why FROM pub_upload_jobs WHERE release_id='rel-noserial'")[0]
    check("задание без номера осталось в очереди", j["status"], "queued")
    check("причина сказана", "нет номера AdsPower" in (j["blocked_why"] or ""), True)
    check("и в журнале выпуска", "чужой профиль" in journal("rel-noserial"), True)
    run("UPDATE pub_upload_jobs SET status='done' WHERE release_id='rel-ok'")
    check("одно оно не выдаётся никогда", claim(), None)

    print("\nСтроки профиля нет — номер вписан по-старому, это и есть серийный")
    run("UPDATE pub_upload_jobs SET status='done' WHERE release_id='rel-noserial'")
    job("rel-legacy", 4321)
    t = claim()
    check("номер отдан как есть", (t or {}).get("profile"), 4321)

    print("\nВыкатка просит новых не выдавать — нода ждёт, своё взятое получает")
    run("UPDATE pub_upload_jobs SET status='done'")
    drain = os.path.join(TMP, "upload-drain")
    os.environ["VS_UPLOAD_DRAIN_FILE"] = drain
    job("rel-drain", profile("41", "k1drain"))
    open(drain, "w").close()
    check("новое задание не выдано", claim(), None)
    check("оно ждёт в очереди", sql("SELECT status FROM pub_upload_jobs "
                                    "WHERE release_id='rel-drain'")[0]["status"], "queued")
    old = time.time() - 2 * 3600
    os.utime(drain, (old, old))
    t = claim()
    check("старый флаг (упавшая выкатка) не держит заливку", (t or {}).get("release_id"), "rel-drain")
    open(drain, "w").close()
    t2 = claim()
    check("своё уже взятое задание нода получает и во время выкатки",
          (t2 or {}).get("release_id"), "rel-drain")
    os.remove(drain)
    os.environ.pop("VS_UPLOAD_DRAIN_FILE", None)
    run("UPDATE pub_upload_jobs SET status='done' WHERE release_id='rel-drain'")

    print("\nВспомогательная функция")
    check("цифры — числом", U._agent_profile(" 24 "), 24)
    check("пусто — None", U._agent_profile(""), None)
    check("не цифры — строкой", U._agent_profile("k1abc"), "k1abc")
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
