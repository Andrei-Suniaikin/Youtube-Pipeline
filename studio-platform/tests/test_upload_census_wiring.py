#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Перепись срывов включена во всех местах, где заливка срывается (01.10.2026).

`tests/test_upload_census.py` проверяет РАЗБОР срыва на классы. Здесь — то, что разбор
вообще вызывается, и каждым из четырёх путей, которыми заливка умирает:

| Путь | Где | Класс |
|---|---|---|
| нода замолчала дольше аренды | `sweep_stuck` | `silent` |
| нода ответила неуспехом до передачи файла | `POST /result` | `safe` |
| нода ответила «нужен разбор», ролик на канале есть | `POST /result` | `sent` |
| ролик пересобран между допуском и выдачей | `claim` | `stale` |
| время выхода прошло, пока задание стояло | `claim` | `stale` |

Почему это отдельный файл, а не пара строк в том же. Числа переписи — единственное
основание решать про автоповтор заливки (решение владельца, п. 7), а пропущенный вызов
занижает их МОЛЧА: таблица остаётся непустой, отчёт складывается, и видно только то,
что попало. Поэтому каждый путь проверяется своим прогоном через настоящую ручку, а не
вызовом `note` напрямую.

Отдельно закреплено: успешный вердикт строки НЕ пишет. Перепись считает срывы, и
подмешанный успех превратил бы её в бессмысленный счётчик заливок.

Сеть не трогается: AdsPower не спрашивается, генератор названий подменён.

    .venv/bin/python tests/test_upload_census_wiring.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="upload-census-")
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
    project_id TEXT, kind TEXT, status TEXT, output_rel TEXT, render_sha256 TEXT,
    render_size INTEGER, created REAL, finished REAL);
""")
_c.execute("INSERT INTO users(name,role,tenant_id) VALUES('хозяин','admin','t1')")
_c.commit()
_c.close()

import pub_adspower                                               # noqa: E402
import pub_services as S                                          # noqa: E402
import pub_upload_census as census                                # noqa: E402
from pub_upload_api import register_upload_routes, sweep_stuck    # noqa: E402

from fastapi import FastAPI                                        # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

FAILS = []
TENANT = "t1"
TOKEN = "census_token"
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


def run(q, args=()):
    c = conn()
    try:
        c.execute(q, args)
        c.commit()
    finally:
        c.close()


def rows(q, args=()):
    c = conn()
    try:
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()


def перепись(rid):
    got = rows("SELECT kind, reason FROM pub_upload_failures WHERE release_id=? "
               "ORDER BY id", (rid,))
    return [(r["kind"], r["reason"]) for r in got]


def boom(*_a, **_kw):
    raise AssertionError("перепись полезла в AdsPower")


pub_adspower.active_profiles = boom
S.make_title = lambda *a, **k: "Название ролика"
S.chapters_enabled = lambda *a, **k: False

app = FastAPI()
register_upload_routes(app, {"db": conn, "_now": lambda: NOW["t"],
                             "upload_token": lambda: TOKEN})
client = TestClient(app, raise_server_exceptions=False)

N = {"i": 0}


def job(status="running", slot_in=3 * 86400, phase=None, video_id=None,
        lease=None, passport=None):
    """Канал, профиль, выпуск и задание заливки в заданном состоянии."""
    N["i"] += 1
    n = N["i"]
    rid, uc = f"rel-{n}", "UC" + str(n) * 22
    c = conn()
    c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,status,created) "
              "VALUES(?,?,?,'active',?)", (TENANT, str(n), f"Профиль {n}", NOW["t"]))
    prof = c.execute("SELECT id FROM pub_profiles WHERE serial=?", (str(n),)).fetchone()[0]
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,profile_id,tz,schedule,"
              "deferred,deferred_limit,created,updated) "
              "VALUES(?,?,?,'ru',?,'Europe/Warsaw','{}',1,0,?,?)",
              (TENANT, uc, f"Канал {n}", prof, NOW["t"], NOW["t"]))
    ch = c.execute("SELECT id FROM pub_channels WHERE uc=?", (uc,)).fetchone()[0]
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
              "plan_item_version,channel_id,uc,slot_at,project_id,status,phase,"
              "created,updated) VALUES(?,?,?,1,?,?,?,?,'running','upload_pending',?,?)",
              (rid, TENANT, n, ch, uc, NOW["t"] + slot_in, f"p{n}", NOW["t"], NOW["t"]))
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,"
              "channel_url,title,video_path,video_passport,slot_at,status,phase,video_id,"
              "lease_until,schedule_iso,claim_token,created,updated) "
              "VALUES(?,?,?,?,?,'http://c','Ролик',?,?,?,?,?,?,?,?,?,?,?)",
              (rid, TENANT, ch, uc, prof, os.path.join(TMP, f"{n}.mp4"), passport,
               NOW["t"] + slot_in, status, phase, video_id, lease,
               "2026-10-05T18:00:00", "tok-%d" % n, NOW["t"], NOW["t"]))
    c.commit()
    c.close()
    return rid


def результат(rid, body):
    # Клейм задания — пропуск ноды к ручке результата: без него ответ чужой.
    клейм = rows("SELECT claim_token FROM pub_upload_jobs WHERE release_id=?",
                 (rid,))[0]["claim_token"]
    return client.post(f"/api/upload/{rid}/result", json=body,
                       headers={"X-Upload-Token": TOKEN, "X-Upload-Claim": клейм})


def main() -> int:
    print("Нода замолчала дольше аренды — silent")
    rid = job(status="running", lease=NOW["t"] - 60)
    # sweep_stuck пишет в переданное соединение; фиксируем, как это делает движок.
    c = conn()
    sweep_stuck(c, NOW["t"])
    c.commit()
    c.close()
    got = перепись(rid)
    check("строка переписи появилась", len(got) >= 1, True)
    if got:
        check("и класс — silent", got[0][0], "silent")
        check("с причиной словами", "молч" in got[0][1], True)

    print("\nНода ответила неуспехом, файла в YouTube не было — safe")
    rid = job(status="running")
    r = результат(rid, {"result": {"success": False, "error": "браузер умер на входе"}})
    check("ручка приняла ответ", r.status_code, 200)
    got = перепись(rid)
    check("одна строка", len(got), 1)
    if got:
        check("класс safe", got[0][0], "safe")
        check("в причине слова ноды", "браузер умер" in got[0][1], True)

    print("\nНода ответила «нужен разбор», ролик на канале есть — sent")
    rid = job(status="running")
    r = результат(rid, {"result": {
        "success": True, "video_id": "ytAbCdEf123",
        "studio_row_evidence": {"studio_state": "DRAFT"}}})
    check("ручка приняла ответ", r.status_code, 200)
    got = перепись(rid)
    check("одна строка", len(got), 1)
    if got:
        # Класс взят уликой (video_id в задании), а не разбором слова вердикта.
        check("класс sent", got[0][0], "sent")

    print("\nУспешный вердикт переписи не касается")
    # Перепись считает СРЫВЫ. Подмешанный успех превратил бы её в счётчик заливок, и
    # числа, по которым решают про автоповтор, перестали бы значить что-либо.
    rid = job(status="running")
    строка = rows("SELECT uc, schedule_iso FROM pub_upload_jobs WHERE release_id=?",
                  (rid,))[0]
    import datetime as _dt
    момент = int(_dt.datetime.fromisoformat(строка["schedule_iso"]).timestamp())
    r = результат(rid, {"result": {
        "success": True, "video_id": "ytOkOkOk999",
        "exact_studio_evidence": {"studio_state": "SCHEDULED",
                                  "scheduled_time_seconds": момент,
                                  "channel_id": строка["uc"]}}})
    check("вердикт принят как успех", (r.json() or {}).get("verdict"), "scheduled")
    check("строк переписи нет", перепись(rid), [])

    print("\nВремя выхода прошло, пока задание стояло в очереди — stale")
    rid = job(status="queued", slot_in=-3600)
    r = client.post("/api/upload/claim", json={"host": "minsk", "worker": "1"},
                    headers={"X-Upload-Token": TOKEN})
    check("нода задания не получила", (r.json() or {}).get("task"), None)
    got = перепись(rid)
    check("одна строка", len(got), 1)
    if got:
        check("класс stale", got[0][0], "stale")
        check("и названо время", "врем" in got[0][1], True)

    print("\nРолик пересобран между допуском и выдачей — stale")
    # Паспорт в задании есть, а файла по пути нет вовсе: принятой версии не существует.
    rid = job(status="queued", passport='{"build": {"job_id": 1}, '
                                        '"video": {"size": 999, "head": "ЫЫ"}}')
    r = client.post("/api/upload/claim", json={"host": "minsk", "worker": "1"},
                    headers={"X-Upload-Token": TOKEN})
    check("нода задания не получила", (r.json() or {}).get("task"), None)
    got = перепись(rid)
    check("одна строка", len(got), 1)
    if got:
        check("класс stale", got[0][0], "stale")

    print("\nЧисла за сутки складываются из всех путей")
    c = conn()
    итог = census.since(c, NOW["t"] - 86400, NOW["t"] + 86400)
    c.close()
    check("молчание посчитано", итог["silent"] >= 1, True)
    check("безопасные посчитаны", итог["safe"], 1)
    check("ушедшие посчитаны", итог["sent"], 1)
    check("нечего повторять посчитано", итог["stale"], 2)

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
