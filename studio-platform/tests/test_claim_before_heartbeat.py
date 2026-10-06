#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выданная, но ещё не отпульсовавшая заливка — не зависание; поздний успех снимает разбор.

06.10.2026, три выпуска «шаг не двигался дольше срока три раза подряд». На Минске всю
ночь было одно место заливки, задания стояли в очереди дольше срока фазы (2 часа). Пока
задание в очереди, движок считал работу живой. Но в момент выдачи задание становилось
`running` без единого пульса (первый приходит через минуту) — и за 16–26 секунд движок
убирал в разбор уже взятую машиной заливку.

Второе: один из трёх роликов через 6 минут встал в расписание YouTube, а выпуск остался
в разборе. Поздний успех поднимал выпуск, только если задание было снято по молчанию
ноды; снятие по таймеру движка он не видел.

    .venv/bin/python tests/test_claim_before_heartbeat.py
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="claim-beat-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_schema import ensure_pub_schema                           # noqa: E402

_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    role TEXT, tenant_id TEXT, disabled INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
    project_id TEXT, kind TEXT, status TEXT, created REAL, finished REAL);
""")
_c.commit()
_c.close()

import pub_engine as E                                            # noqa: E402
import pub_services as S                                          # noqa: E402
from pub_upload_api import register_upload_routes                 # noqa: E402

from fastapi import FastAPI                                        # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

FAILS = []
TENANT = "t1"
TOKEN = "beat_token"
NOW = time.time()
ISO = "2026-10-21T09:30:00+02:00"


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


class Svc:
    """Настоящие обработчики шагов заливки, без остального PubServices."""
    _upload_job = S.PubServices._upload_job
    do_upload_running = S.PubServices.do_upload_running
    do_scheduled = S.PubServices.do_scheduled

    def release_resources(self, c, rel):
        pass


N = {"i": 0}


def setup(claimed_ago, heartbeat=None, job_status="running", rel_status="running"):
    """Выпуск в «передан на заливку»: срок фазы вышел, две попытки потрачены."""
    N["i"] += 1
    n = N["i"]
    rid, uc = f"rel-{n}", "UC" + str(n) * 22
    c = conn()
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
              "deferred_limit,created,updated) VALUES(?,?,?,'pl','Europe/Warsaw','{}',1,0,?,?)",
              (TENANT, uc, f"Канал {n}", NOW, NOW))
    ch = c.execute("SELECT id FROM pub_channels WHERE uc=?", (uc,)).fetchone()[0]
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
              "donor_video_id,donor_title,slot_at,status,created,updated) "
              "VALUES(?,?,?,1,'https://youtu.be/x','x','Донор',?,'running',?,?)",
              (n, TENANT, ch, NOW + 86400, NOW, NOW))
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,project_id,status,phase,attempts,deadline,created,"
              "updated) VALUES(?,?,?,1,?,?,?,?,?,'upload_pending',2,?,?,?)",
              (rid, TENANT, n, ch, uc, NOW + 86400, f"p{n}", rel_status,
               NOW - 600, NOW - 3 * 3600, NOW - 60))
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,"
              "channel_url,title,video_path,slot_at,status,schedule_iso,claim_token,"
              "claimed,heartbeat,lease_until,created,updated) "
              "VALUES(?,?,?,?,1,'http://c','Ролик','/x.mp4',?,?,?,?,?,?,?,?,?)",
              (rid, TENANT, ch, uc, NOW + 86400, job_status, ISO, f"tok-{n}",
               NOW - claimed_ago, heartbeat, NOW + 2700, NOW - 3 * 3600, NOW))
    c.commit()
    c.close()
    return rid


def rel_row(rid):
    c = conn()
    try:
        return c.execute("SELECT * FROM pub_releases WHERE release_id=?", (rid,)).fetchone()
    finally:
        c.close()


def step(rid):
    c = conn()
    try:
        out = E.advance(c, rel_row(rid), Svc(), NOW)
        c.commit()
        return out
    finally:
        c.close()


def main() -> int:
    print("Задание выдано 20 с назад, пульса ещё нет")
    rid = setup(claimed_ago=20)
    c = conn()
    check("не зависание", E.is_stuck(c, rel_row(rid), NOW), False)
    c.close()
    check("выпуск шагает в «идёт заливка»", step(rid), "upload_running")

    print("\nЗадание выдано 12 минут назад и так и не прислало пульса")
    rid = setup(claimed_ago=720)
    c = conn()
    check("это уже тишина", E.is_stuck(c, rel_row(rid), NOW), True)
    c.close()

    print("\nПульс был, но давно — пульс важнее момента выдачи")
    rid = setup(claimed_ago=20, heartbeat=NOW - 900)
    c = conn()
    check("тишина", E.is_stuck(c, rel_row(rid), NOW), True)
    c.close()

    print("\nВыпуск в разборе по таймеру, а заливка дошла до SCHEDULED")
    rid = setup(claimed_ago=400, heartbeat=NOW - 30, rel_status="blocked")
    app = FastAPI()
    register_upload_routes(app, {"db": conn, "_now": lambda: NOW,
                                 "upload_token": lambda: TOKEN})
    client = TestClient(app, raise_server_exceptions=False)
    момент = int(_dt.datetime.fromisoformat(ISO).timestamp())
    r = client.post(f"/api/upload/{rid}/result", json={"result": {
        "success": True, "video_id": "ytLateOk123",
        "exact_studio_evidence": {"studio_state": "SCHEDULED",
                                  "scheduled_time_seconds": момент,
                                  "verified_channel_uc": rel_row(rid)["uc"]}}},
        headers={"X-Upload-Token": TOKEN, "X-Upload-Claim": f"tok-{N['i']}"})
    check("вердикт — успех", (r.json() or {}).get("verdict"), "scheduled")
    check("разбор снят", rel_row(rid)["status"], "running")
    # Видео уже есть, но это ЭТО ЖЕ задание: шаг «идёт заливка» новой заливки не начинает.
    check("шаг в «идёт заливка», не «повтор запрещён»", step(rid), "upload_running")
    check("и дальше — в расписании", step(rid), "scheduled")
    check("с номером ролика", rel_row(rid)["video_id"], "ytLateOk123")
    check("выпуск закрыт", step(rid), "done")

    print("\nЗадание отменено, видео было — повтор по-прежнему запрещён")
    rid = setup(claimed_ago=400, job_status="cancelled")
    c = conn()
    c.execute("UPDATE pub_upload_jobs SET video_id='ytOld' WHERE release_id=?", (rid,))
    c.execute("UPDATE pub_releases SET deadline=? WHERE release_id=?", (NOW + 600, rid))
    c.commit()
    c.close()
    check("в разбор", step(rid), "quarantined")

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
