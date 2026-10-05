#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Таймкоды, упавшие на перегрузке Codex, ставятся заново (05.10.2026).

03–05.10 десять задач таймкодов упали на «Selected model is at capacity» и 429, и девять
роликов ушли без таймкодов, хотя Codex освободился через минуты. Теперь такая задача
ставится заново через 15 минут, не больше двух раз и только пока таймкоды можно ждать:
срок 6 часов считается от ПЕРВОЙ задачи, повтор его не сбрасывает.

    .venv/bin/python tests/test_chapters_capacity_retry.py
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


def fresh(chapters_status="queued", chapters_age=HOUR, render_status="done",
          chapters_error=None, mark=None):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE users(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT,
                         key_opts TEXT, keys TEXT);
      CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, settings TEXT);
      CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
                               project_id TEXT, kind TEXT, status TEXT, error TEXT,
                               created REAL, retry_after REAL);
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
        c.execute("INSERT INTO queue_tasks VALUES(?,?,?,?,?,?,?,NULL)",
                  (jid, "dev", "proj-1", kind, st,
                   chapters_error if kind == "chapters" else None, created))
        c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
                  "error,created,updated) VALUES('rel-1','vsgen',?,?,?,?,?,?)",
                  (kind, str(jid), st, mark if kind == "chapters" else None, created, created))
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


def mirror(c):
    return dict(c.execute("SELECT remote_job_id, status, error FROM pub_release_jobs "
                          "WHERE release_id='rel-1' AND kind='chapters'").fetchone())


def main() -> int:
    cap = "CODEX_ERROR: Selected model is at capacity. Please try a different model."

    print("Перегрузка Codex, первый раз — ставим заново")
    placed = []
    c = fresh(chapters_status="error", chapters_age=HOUR, chapters_error=cap)
    check("ждём повтор", services(placed).do_media_ready(c, rel()), {"wait": True})
    check("поставлены таймкоды", placed, ["chapters"])
    m = mirror(c)
    check("зеркало смотрит на новую задачу", (m["remote_job_id"], m["status"], m["error"]),
          ("101", "queued", "chapters-retry:1"))
    check("в журнале — почему и счёт", "перегрузке Codex" in last_msg(c) and "1 из 2" in last_msg(c), True)

    print("\nПауза перед повтором записана в очередь")
    c2 = fresh(chapters_status="error", chapters_age=HOUR, chapters_error=cap)

    def _enqueue(uid, tid, pid, kind, params):        # как настоящая постановка: строка в очереди
        c2.execute("INSERT INTO queue_tasks VALUES(101,?,?,?,'queued',NULL,?,NULL)",
                   (tid, pid, kind, NOW))
        return 101
    s2 = services([])
    s2.enqueue = _enqueue
    s2.do_media_ready(c2, rel())
    ra = c2.execute("SELECT retry_after FROM queue_tasks WHERE job_id=101").fetchone()[0]
    check("retry_after = сейчас + 15 мин", ra, NOW + 900)

    print("\nДва повтора было — третьего нет, ролик уходит без таймкодов")
    placed = []
    c = fresh(chapters_status="error", chapters_age=HOUR, chapters_error=cap, mark="chapters-retry:2")
    check("этап пройден", services(placed).do_media_ready(c, rel()), {})
    check("ничего не ставили", placed, [])

    print("\nНе перегрузка — как раньше, без повтора")
    placed = []
    c = fresh(chapters_status="error", chapters_age=HOUR, chapters_error="мало глав: 2 из 5")
    check("этап пройден", services(placed).do_media_ready(c, rel()), {})
    check("ничего не ставили", placed, [])

    print("\nДо слота меньше 3 часов — не повторяем")
    placed = []
    c = fresh(chapters_status="error", chapters_age=60, chapters_error=cap)
    check("этап пройден", services(placed).do_media_ready(c, rel(slot_in=2 * HOUR)), {})
    check("ничего не ставили", placed, [])

    print("\nСрок 6 часов идёт от первой задачи, повтор его не сбрасывает")
    placed = []
    c = fresh(chapters_status="queued", chapters_age=60, mark="chapters-retry:1")
    c.execute("UPDATE pub_release_jobs SET created=? WHERE kind='chapters'", (NOW - 6 * HOUR - 60,))
    check("ждали 6 ч от первой — уходим без них", services(placed).do_media_ready(c, rel()), {})
    check("в журнале — ждали 6 ч", "ждали 6 ч" in last_msg(c), True)

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
