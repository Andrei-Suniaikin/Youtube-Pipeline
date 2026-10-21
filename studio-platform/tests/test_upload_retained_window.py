#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Удержанное окно заливки: машина находит своё задание и доносит номер видео (17.10.2026).

Восемь профилей стояли с открытым мастером под «Verify it's you» без номера видео; задание
в `repair` не мог взять никто. Проверяется:
  • GET /api/upload/retained отдаёт задание только по точной паре профиль+канал, только в
    `repair` без номера, только у остановленного выпуска, не подтверждённого руками;
  • POST …/retained_progress записывает номер видео и фазу без клейма, но только для
    того задания, что нашла первая ручка; кривой номер и чужое задание — отказ;
  • записанный номер второй раз не перетирается (задание уже с номером — не удержанное);
  • POST …/retained_lost (18.10): пуллер сам закрыл окно, довести которое не смог, —
    выпуск возвращается заново как «окно потеряно»; чужой выпуск и канал с двумя
    удержанными заданиями — отказ, ничего не меняется.

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
import pub_engine                                                  # noqa: E402

TOKEN = "upl_test"
NOW = time.time()
FAILS = []
UC_A, UC_B = "UC" + "a" * 22, "UC" + "b" * 22
UC_C, UC_D = "UC" + "c" * 22, "UC" + "d" * 22


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
            ("rel-vid", UC_B, "repair", "VID00000001", "blocked", 0),  # номер уже есть
            ("rel-gate", UC_C, "error", "", "blocked", 0),      # вердикт failed с AUTH_GATE — окно тоже держит
            ("rel-err", UC_D, "error", "", "blocked", 0))       # другой отказ — окна нет
    for n, (rid, uc, st, vid, rst, hand) in enumerate(rows, start=1):
        c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
                  "channel_id,uc,slot_at,status,phase,created,updated,hand_confirmed) "
                  "VALUES(?,?,?,1,?,?,?,?,'upload_running',?,?,?)",
                  (rid, "t1", n, n, uc, NOW + 3600, rst, NOW, NOW, NOW if hand else None))
        c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,"
                  "host_id,channel_url,title,description,schedule_iso,visibility,video_path,"
                  "slot_at,status,phase,video_id,error,created,updated) "
                  "VALUES(?,?,?,?,?,1,?,?,'','','public','/v.mp4',?,?,?,?,"
                  "?,?,?)",
                  (rid, "t1", n, uc, pid, f"https://studio.youtube.com/channel/{uc}/",
                   f"ролик {rid}", NOW + 3600, st,
                   # машина успела сообщить «файл пошёл»: загрузка начиналась — как в бою
                   json.dumps({"upload_phase_schema_version": 1, "phase": "upload_started",
                               "video_id": vid or ""}),
                   vid or None,
                   {"rel-gate": "AUTH_GATE: native confirmation stopped; existing upload preserved",
                    "rel-err": "нода: HTTP Error 502"}.get(rid, "UPLOAD_IN_PROGRESS_PROFILE_RETAINED: …"),
                   NOW - (100 if rid == "rel-old" else 0), NOW - (100 if rid == "rel-old" else 0)))
        # История фаз до «идёт заливка»: загрузка начиналась, выпуск необратим — как в бою.
        for seq, ph in enumerate(pub_engine.PHASES[:pub_engine.PHASE_INDEX["upload_running"] + 1], start=1):
            c.execute("INSERT INTO pub_release_events(release_id,seq,phase,created,repeats,last_seen) "
                      "VALUES(?,?,?,?,1,?)", (rid, seq, ph, NOW - 3600, NOW - 3600))
    c.commit(); c.close()


def job(rid):
    c = conn()
    try:
        return dict(c.execute("SELECT * FROM pub_upload_jobs WHERE release_id=?", (rid,)).fetchone())
    finally:
        c.close()


def rel(rid):
    c = conn()
    try:
        return dict(c.execute("SELECT * FROM pub_releases WHERE release_id=?", (rid,)).fetchone())
    finally:
        c.close()


def log_of(rid):
    c = conn()
    try:
        return [m[0] for m in c.execute("SELECT message FROM pub_release_log WHERE release_id=?", (rid,))]
    finally:
        c.close()


def main():
    seed()
    print("Найти своё удержанное задание")
    r = client.get("/api/upload/retained", params={"profile": "28", "uc": UC_A}, headers=H)
    check("нашли самое свежее задание этого профиля и канала", r.json().get("release_id"), "rel-stuck")
    check("отдан заголовок — по нему машина сверит окно", r.json().get("title"), "ролик rel-stuck")
    check("сказано, что таких заданий у канала два — машина окно не атрибутирует", r.json().get("candidates"), 2)
    r = client.get("/api/upload/retained", params={"profile": "29", "uc": UC_A}, headers=H)
    check("чужой профиль — ничего", r.json().get("release_id"), None)
    r = client.get("/api/upload/retained", params={"profile": "28", "uc": UC_B}, headers=H)
    check("подтверждённое руками и с номером — не удержанные", r.json().get("release_id"), None)
    r = client.get("/api/upload/retained", params={"profile": "28", "uc": UC_C}, headers=H)
    check("вердикт failed с AUTH_GATE — окно тоже удержанное", r.json().get("release_id"), "rel-gate")
    r = client.get("/api/upload/retained", params={"profile": "28", "uc": UC_D}, headers=H)
    check("другой отказ без окна — ничего", r.json().get("release_id"), None)
    r = client.get("/api/upload/retained", params={"profile": "28", "uc": "UCbad"}, headers=H)
    check("кривой UC — отказ", r.status_code, 400)
    r = client.get("/api/upload/retained", params={"profile": "28", "uc": UC_A})
    check("без секрета ноды — не пущу", r.status_code in (401, 403), True)

    print("\nПуллер сам закрыл удержанное окно, которое довести не смог")
    r = client.post("/api/upload/rel-stuck/retained_lost",
                    json={"profile": "28", "uc": UC_C, "why": "Verify не пройден, окно закрыто"}, headers=H)
    check("чужой release_id (у профиля и канала удержано другое) — отказ", r.status_code, 409)
    check("чужой выпуск не тронут", (rel("rel-stuck")["status"], job("rel-stuck")["status"]), ("blocked", "repair"))
    check("и своё, rel-gate, тоже не тронуто", rel("rel-gate")["status"], "blocked")
    r = client.post("/api/upload/rel-stuck/retained_lost",
                    json={"profile": "28", "uc": UC_A, "why": "окно закрыто"}, headers=H)
    check("у канала два удержанных задания — отказ, вернуть кнопкой", r.status_code, 409)
    check("в отказе сказано про кнопку в пульте", "кнопкой в пульте" in r.json().get("detail", ""), True)
    check("оба выпуска канала не тронуты",
          (rel("rel-stuck")["status"], rel("rel-old")["status"]), ("blocked", "blocked"))
    check("ни одного задания не убрано", job("rel-stuck")["status"] and job("rel-old")["status"], "repair")
    check("в журнале ничего не появилось", any("закрыто пуллером" in m for m in log_of("rel-stuck")), False)
    r = client.post("/api/upload/rel-gate/retained_lost", json={"uc": UC_C, "why": "x"}, headers=H)
    check("без profile — отказ 400", r.status_code, 400)
    r = client.post("/api/upload/rel-gate/retained_lost", content=b"not json", headers=H)
    check("не JSON — отказ 400", r.status_code, 400)
    r = client.post("/api/upload/rel-gate/retained_lost", json={"profile": "28", "uc": UC_C, "why": "x"})
    check("без секрета ноды — не пущу", r.status_code in (401, 403), True)
    check("после отказов rel-gate всё ещё остановлен", rel("rel-gate")["status"], "blocked")
    r = client.post("/api/upload/rel-gate/retained_lost",
                    json={"profile": "28", "uc": UC_C, "why": "Verify it's you не пройден, окно закрыто пуллером"},
                    headers=H)
    check("наше удержанное задание — принято", (r.status_code, r.json().get("revived")), (200, True))
    check("в ответе номер выпуска", r.json().get("release_id"), "rel-gate")
    g = rel("rel-gate")
    check("выпуск снова в работе", g["status"], "pending")
    check("с фазы «обложка готова» — комплект соберётся заново", g["phase"], "cover_ready")
    c = conn()
    n_jobs = c.execute("SELECT COUNT(*) FROM pub_upload_jobs WHERE release_id='rel-gate'").fetchone()[0]
    irrev = pub_engine.reached_irreversible(c, "rel-gate")
    c.close()
    check("старое задание убрано", n_jobs, 0)
    check("после отметки выпуск больше не необратим", irrev, False)
    msgs = log_of("rel-gate")
    check("в журнале сказано, что окно закрыто пуллером", any("удержанное окно закрыто пуллером" in m for m in msgs), True)
    check("и что выпуск возвращён заново", any("окно потеряно" in m for m in msgs), True)
    r = client.post("/api/upload/rel-gate/retained_lost",
                    json={"profile": "28", "uc": UC_C, "why": "повтор"}, headers=H)
    check("второй раз — уже не удержанное, отказ", r.status_code, 409)

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
                    json={"profile": "28", "uc": UC_A, "phase": "", "stdout": "черновик сохранён; профиль закрыт"},
                    headers=H)
    check("итог от той же машины после номера — принимается", r.status_code, 200)
    check("номер при этом не тронут", job("rel-stuck")["video_id"], "Abc-123_xyz")
    r = client.post("/api/upload/rel-stuck/retained_progress",
                    json={"profile": "29", "uc": UC_A, "phase": phase}, headers=H)
    check("после номера чужой профиль — отказ", r.status_code, 409)
    r = client.post("/api/upload/rel-stuck/retained_progress",
                    json={"profile": "28", "uc": UC_A, "phase": json.dumps(
                        {"upload_phase_schema_version": 1, "phase": "video_id_known", "video_id": "Other-1234_"})},
                    headers=H)
    check("другой номер поверх записанного — отказ", r.status_code, 409)
    check("записанный номер не перетёрт", job("rel-stuck")["video_id"], "Abc-123_xyz")
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

    print("\nЗадание в error с AUTH_GATE получило номер — само становится доведением по номеру (21.10.2026)")
    c = conn()
    pid = c.execute("SELECT id FROM pub_profiles").fetchone()[0]
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,status,phase,created,updated,hand_confirmed) "
              "VALUES('rel-auth','t1',99,1,3,?,?,'blocked','upload_running',?,?,NULL)",
              (UC_C, NOW + 3600, NOW, NOW))
    c.execute("UPDATE pub_releases SET blocked_reason='AUTH_GATE: previous confirmation is held' "
              "WHERE release_id='rel-auth'")
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,"
              "host_id,channel_url,title,description,schedule_iso,visibility,video_path,"
              "slot_at,status,phase,video_id,error,cover_path,created,updated) "
              "VALUES('rel-auth','t1',3,?,?,1,?,'ролик rel-auth','','','public','/v.mp4',?,"
              "'error',?,NULL,'AUTH_GATE: previous confirmation is held; existing upload preserved',"
              "'/c.jpg',?,?)",
              (UC_C, pid, f"https://studio.youtube.com/channel/{UC_C}/", NOW + 3600,
               json.dumps({"upload_phase_schema_version": 1, "phase": "upload_started", "video_id": ""}),
               NOW, NOW))
    c.commit(); c.close()
    r = client.get("/api/upload/retained", params={"profile": "28", "uc": UC_C}, headers=H)
    check("окно rel-auth удержанное", r.json().get("release_id"), "rel-auth")
    r = client.post("/api/upload/rel-auth/retained_progress",
                    json={"profile": "28", "uc": UC_C, "phase": json.dumps(
                        {"upload_phase_schema_version": 1, "phase": "video_id_known", "video_id": "Ziua0000001"})},
                    headers=H)
    check("номер принят", (r.status_code, r.json().get("video_id")), (200, "Ziua0000001"))
    j = job("rel-auth")
    check("задание переведено в repair с номером", (j["status"], j["video_id"]), ("repair", "Ziua0000001"))
    check_true = lambda label, cond: check(label, bool(cond), True)           # noqa: E731
    check_true("причина задания говорит про доведение", "доводится по номеру" in (j["error"] or ""))
    check_true("клейм снят", j["claim_token"] is None)
    check_true("выпуск остался в разборе, причина — про черновик",
               rel("rel-auth")["status"] == "blocked" and "Ziua0000001" in (rel("rel-auth")["blocked_reason"] or ""))
    r = client.post("/api/upload/rel-auth/retained_progress",
                    json={"profile": "28", "uc": UC_C, "phase": "", "stdout": "черновик сохранён; профиль закрыт"},
                    headers=H)
    check("итоговая строка после номера принимается (было 409)", r.status_code, 200)
    c = conn()
    msgs = [m[0] for m in c.execute("SELECT message FROM pub_release_log WHERE release_id='rel-auth'")]
    c.close()
    check_true("в журнале сказано, что файл второй раз не уходит", any("второй раз не уходит" in m for m in msgs))

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}"); [print("  -", f) for f in FAILS]; return 1
    print("все проверки прошли"); return 0


if __name__ == "__main__":
    sys.exit(main())
