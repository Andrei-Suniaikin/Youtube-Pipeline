#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Удержанное окно заливки: машина находит своё задание и доносит номер видео (17.10.2026).

Восемь профилей стояли с открытым мастером под «Verify it's you» без номера видео; задание
в `repair` не мог взять никто. Проверяется:
  • GET /api/upload/retained отдаёт задание только по точной паре профиль+канал, только в
    `repair` без номера, только у остановленного выпуска, не подтверждённого руками;
  • POST …/retained_progress записывает номер видео и фазу без клейма, но только для
    того задания, что нашла первая ручка; кривой номер и чужое задание — отказ;
  • записанный номер второй раз не перетирается (задание уже с номером — не удержанное).

    .venv/bin/python tests/test_upload_retained_window.py
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
TMP = tempfile.mkdtemp(prefix="upload-retained-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_schema import ensure_pub_schema                           # noqa: E402

_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
""")
_c.commit(); _c.close()

from fastapi import FastAPI                                        # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402
from pub_upload_api import register_upload_routes                  # noqa: E402

TOKEN = "upl_test"
NOW = time.time()
FAILS = []
UC_A, UC_B = "UC" + "a" * 22, "UC" + "b" * 22


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    return c


app = FastAPI()
register_upload_routes(app, {"db": conn, "_now": lambda: NOW, "upload_token": lambda: TOKEN})
client = TestClient(app, raise_server_exceptions=False)
H = {"X-Upload-Token": TOKEN}


def seed():
    c = conn()
    c.execute("INSERT INTO pub_hosts(id,tenant_id,code,title,adspower_url,status,created) "
              "VALUES(1,'t1','minsk','Минск','http://127.0.0.1:50325','active',?)", (NOW,))
    c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,default_host_id,status,created,"
              "adspower_user_id) VALUES('t1','28','П28',1,'active',?,'u28')", (NOW,))
    pid = c.execute("SELECT id FROM pub_profiles").fetchone()[0]
    rows = (("rel-stuck", UC_A, "repair", "", "blocked", 0),
            ("rel-old", UC_A, "repair", "", "blocked", 0),      # старее — не берётся первой
            ("rel-hand", UC_B, "repair", "", "blocked", 1),     # подтверждён руками
            ("rel-vid", UC_B, "repair", "VID00000001", "blocked", 0))  # номер уже есть
    for n, (rid, uc, st, vid, rst, hand) in enumerate(rows, start=1):
        c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
                  "channel_id,uc,slot_at,status,phase,created,updated,hand_confirmed) "
                  "VALUES(?,?,?,1,?,?,?,?,'upload_running',?,?,?)",
                  (rid, "t1", n, n, uc, NOW + 3600, rst, NOW, NOW, NOW if hand else None))
        c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,"
                  "host_id,channel_url,title,description,schedule_iso,visibility,video_path,"
                  "slot_at,status,video_id,error,created,updated) "
                  "VALUES(?,?,?,?,?,1,?,?,'','','public','/v.mp4',?,?,?,"
                  "'UPLOAD_IN_PROGRESS_PROFILE_RETAINED: …',?,?)",
                  (rid, "t1", n, uc, pid, f"https://studio.youtube.com/channel/{uc}/",
                   f"ролик {rid}", NOW + 3600, st, vid or None,
                   NOW - (100 if rid == "rel-old" else 0), NOW - (100 if rid == "rel-old" else 0)))
    c.commit(); c.close()


def job(rid):
    c = conn()
    try:
        return dict(c.execute("SELECT * FROM pub_upload_jobs WHERE release_id=?", (rid,)).fetchone())
    finally:
        c.close()


def main():
    seed()
    print("Найти своё удержанное задание")
    r = client.get("/api/upload/retained", params={"profile": "28", "uc": UC_A}, headers=H)
    check("нашли самое свежее задание этого профиля и канала", r.json().get("release_id"), "rel-stuck")
    check("отдан заголовок — по нему машина сверит окно", r.json().get("title"), "ролик rel-stuck")
    r = client.get("/api/upload/retained", params={"profile": "29", "uc": UC_A}, headers=H)
    check("чужой профиль — ничего", r.json().get("release_id"), None)
    r = client.get("/api/upload/retained", params={"profile": "28", "uc": UC_B}, headers=H)
    check("подтверждённое руками и с номером — не удержанные", r.json().get("release_id"), None)
    r = client.get("/api/upload/retained", params={"profile": "28", "uc": "UCbad"}, headers=H)
    check("кривой UC — отказ", r.status_code, 400)
    r = client.get("/api/upload/retained", params={"profile": "28", "uc": UC_A})
    check("без секрета ноды — не пущу", r.status_code in (401, 403), True)

    print("\nМашина доносит номер видео без клейма")
    phase = json.dumps({"upload_phase_schema_version": 1, "phase": "video_id_known",
                        "video_id": "Abc-123_xyz"})
    r = client.post("/api/upload/rel-stuck/retained_progress",
                    json={"profile": "28", "uc": UC_A, "phase": phase, "stdout": "verify пройден"},
                    headers=H)
    check("принято", (r.status_code, r.json().get("video_id")), (200, "Abc-123_xyz"))
    j = job("rel-stuck")
    check("номер записан, статус остался repair", (j["video_id"], j["status"]), ("Abc-123_xyz", "repair"))
    check("фаза записана целиком", "video_id_known" in (j["phase"] or ""), True)
    c = conn()
    msgs = [m[0] for m in c.execute("SELECT message FROM pub_release_log WHERE release_id='rel-stuck'")]
    c.close()
    check("в журнале выпуска сказано про номер", any("Abc-123_xyz" in m for m in msgs), True)

    print("\nЗащиты")
    r = client.post("/api/upload/rel-stuck/retained_progress",
                    json={"profile": "28", "uc": UC_A, "phase": phase}, headers=H)
    check("второй раз то же задание уже не удержанное (номер есть) — отказ", r.status_code, 409)
    r = client.post("/api/upload/rel-old/retained_progress",
                    json={"profile": "28", "uc": UC_A, "phase": json.dumps(
                        {"upload_phase_schema_version": 1, "phase": "video_id_known", "video_id": "bad id!!!!!"})},
                    headers=H)
    check("кривой номер — отказ", r.status_code, 400)
    check("и ничего не записано", job("rel-old")["video_id"], None)
    r = client.post("/api/upload/rel-old/retained_progress",
                    json={"profile": "28", "uc": UC_A, "phase": json.dumps(
                        {"upload_phase_schema_version": 1, "phase": "upload_started", "video_id": ""})},
                    headers=H)
    check("фаза без номера — принимается как фаза", (r.status_code, job("rel-old")["video_id"]), (200, None))
    r = client.post("/api/upload/rel-hand/retained_progress",
                    json={"profile": "28", "uc": UC_B, "phase": phase}, headers=H)
    check("подтверждённое руками — отказ", r.status_code, 409)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}"); [print("  -", f) for f in FAILS]; return 1
    print("все проверки прошли"); return 0


if __name__ == "__main__":
    sys.exit(main())
