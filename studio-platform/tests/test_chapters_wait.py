#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Готовый ролик не ждёт таймкоды бесконечно (04.10.2026).

Что случилось. Таймкоды — необязательный этап: если их задача упала, ролик уходит без
них. А пока задача стоит в очереди, движок ждал её без срока. 04.10 восемь готовых
роликов ждали таймкоды с 09:25 до ночи за промптами новых проектов и опоздали к слотам.

Решение владельца (тест решений, А4): ждать не дольше 6 часов, а если до слота меньше
3 часов — лить без таймкодов сразу.

Проверяется: свежие таймкоды ждём; 6 часов прошло — уходим без них и пишем об этом в
журнал; до слота меньше 3 часов — уходим без них, даже если задача только поставлена;
готовые таймкоды — как раньше; обязательные этапы (рендер) это правило не трогает.

    .venv/bin/python tests/test_chapters_wait.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

import pub_services                                               # noqa: E402

NOW = 1_800_000_000.0
HOUR = 3600.0
FAILS = []
PDIR = tempfile.mkdtemp(prefix="chapters-wait-")
os.makedirs(os.path.join(PDIR, "googler"), exist_ok=True)
open(os.path.join(PDIR, "googler", "001.jpg"), "wb").write(b"x")


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh(chapters_status="queued", chapters_age=HOUR, render_status="done"):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE users(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT,
                         key_opts TEXT, keys TEXT);
      CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, settings TEXT);
      CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
                               project_id TEXT, kind TEXT, status TEXT, error TEXT,
                               created REAL);
      CREATE TABLE job_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER,
                            ts REAL, line TEXT);
      CREATE TABLE pub_release_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,
        release_id TEXT, service TEXT, kind TEXT, remote_job_id TEXT, status TEXT,
        error TEXT, created REAL, updated REAL, UNIQUE(release_id,service,kind));
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        ts REAL, level TEXT, step TEXT, message TEXT,
        repeats INTEGER NOT NULL DEFAULT 1, last_ts REAL);
    """)
    c.execute("INSERT INTO users VALUES(1,'dev','dev','{}','{}')")
    rows = [(10, "tts", "done", NOW - 9 * HOUR), (11, "images", "done", NOW - 8 * HOUR),
            (12, "render", render_status, NOW - 7 * HOUR),
            (13, "chapters", chapters_status, NOW - chapters_age)]
    for jid, kind, st, created in rows:
        c.execute("INSERT INTO queue_tasks VALUES(?,?,?,?,?,NULL,?)",
                  (jid, "dev", "proj-1", kind, st, created))
        c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
                  "error,created,updated) VALUES('rel-1','vsgen',?,?,?,NULL,?,?)",
                  (kind, str(jid), st, created, created))
    c.commit()
    return c


def rel(slot_in=2 * 24 * HOUR):
    return {"release_id": "rel-1", "tenant_id": "dev", "project_id": "proj-1", "lang": "pl",
            "preset_cover": "", "donor_url": "https://youtu.be/aaa", "updated": NOW,
            "slot_at": NOW + slot_in}


def services(placed):
    s = pub_services.PubServices({
        "create_project": lambda *a, **k: "proj-1",
        "enqueue": lambda uid, tid, pid, kind, params: (placed.append(kind), 100 + len(placed))[1],
        "project_dir": lambda *a, **k: PDIR})
    s._clock = lambda: NOW
    return s


def last_msg(c):
    r = c.execute("SELECT message FROM pub_release_log ORDER BY id DESC LIMIT 1").fetchone()
    return r[0] if r else ""


def main() -> int:
    print("Таймкоды поставлены час назад, до слота двое суток — ждём")
    c = fresh(chapters_age=HOUR)
    check("ждём", services([]).do_media_ready(c, rel()), {"wait": True})
    check("в журнале ничего про «без таймкодов»", "без них" in last_msg(c), False)

    print("\nЖдали 6 часов — ролик уходит без таймкодов")
    c = fresh(chapters_age=6 * HOUR + 60)
    check("этап пройден", services([]).do_media_ready(c, rel()), {})
    msg = last_msg(c)
    check("в журнале — почему", "ждали 6 ч" in msg and "без них" in msg, True)

    print("\nДо слота меньше 3 часов — без таймкодов, даже если их только поставили")
    c = fresh(chapters_age=60)
    check("этап пройден", services([]).do_media_ready(c, rel(slot_in=2 * HOUR)), {})
    check("в журнале — до слота", "до слота" in last_msg(c), True)

    print("\nДо слота ровно 3 часа и чуть больше — ещё ждём")
    c = fresh(chapters_age=60)
    check("ждём", services([]).do_media_ready(c, rel(slot_in=3 * HOUR + 120)), {"wait": True})

    print("\nТаймкоды готовы — как раньше")
    c = fresh(chapters_status="done")
    check("этап пройден", services([]).do_media_ready(c, rel()), {})
    check("про «без таймкодов» не пишем", "без них" in last_msg(c), False)

    print("\nОбязательный этап правило не трогает: рендер идёт — ждём при любом слоте")
    c = fresh(render_status="running", chapters_age=10 * HOUR)
    check("ждём рендер", services([]).do_media_ready(c, rel(slot_in=HOUR)), {"wait": True})

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
