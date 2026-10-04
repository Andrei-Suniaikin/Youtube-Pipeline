#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Задача без владельца в очередь не ставится (04.10.2026).

Что случилось. Тревога «Задача #13 (images) стоит в очереди 2 сут, её выпуск не
заблокирован». 02.10 движок, увидев, что после этапа картинок в проекте нет картинок,
ставил их заново — и в этом месте единственном не проверял, нашёлся ли владелец
проекта. Десять задач картинок легли в очередь с пустым uid. Шедулер берёт только
строки с владельцем (`q.uid IS NOT NULL`), так что они не исполнились бы никогда, а
новые картинки этим проектам не ставились: «проект уже меняет картинки задачей images».
Десять выпусков стояли двое суток, и ни один не был «в разборе».

Теперь: `_enqueue` без владельца отказывает (вызывающий видит «очередь не приняла»), а
путь повторной постановки картинок без владельца — «ресурс недоступен» со словами, как
и остальные пути постановки.

    .venv/bin/python tests/test_ownerless_task.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "shared"))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

FAILS = []
TMP = tempfile.mkdtemp(prefix="ownerless-")
DEV_DB = os.path.join(BASE, "dev", "vsgen.dev.db")
DB_COPY = os.path.join(TMP, "vsgen.copy.db")
if os.path.exists(DEV_DB):
    shutil.copy(DEV_DB, DB_COPY)
os.environ["HOME"] = os.path.join(BASE, "dev", "home")
os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
os.environ["VS_SERVER_DB"] = DB_COPY
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"] = os.path.join(TMP, "locks")
os.makedirs(os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"], mode=0o700, exist_ok=True)

import pub_services                                               # noqa: E402

NOW = 1_800_000_000.0


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def engine_world():
    """Этап картинок «сделан», а картинок в проекте нет; владельца у кабинета не найти."""
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
    for jid, kind in ((10, "tts"), (11, "images")):
        c.execute("INSERT INTO queue_tasks VALUES(?,?,?,?,'done',NULL,?)",
                  (jid, "dev", "proj-1", kind, NOW))
        c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
                  "error,created,updated) VALUES('rel-1','vsgen',?,?,'done',NULL,?,?)",
                  (kind, str(jid), NOW, NOW))
    c.commit()
    return c


def main() -> int:
    print("Движок: картинок нет, владельца нет — не ставим задачу-сироту")
    empty = tempfile.mkdtemp(prefix="no-images-")
    placed = []
    svc = pub_services.PubServices({
        "create_project": lambda *a, **k: "proj-1",
        "enqueue": lambda uid, *a, **k: (placed.append(uid), 100)[1],
        "project_dir": lambda *a, **k: empty})
    svc._clock = lambda: NOW
    rel = {"release_id": "rel-1", "tenant_id": "dev", "project_id": "proj-1", "lang": "pl",
           "preset_cover": "", "donor_url": "https://youtu.be/aaa", "updated": NOW,
           "slot_at": NOW + 86400}
    try:
        svc.do_media_ready(engine_world(), rel)
        got = "прошло"
    except pub_services.ResourceDown as exc:
        got = "ресурс: " + str(exc)
    except Exception as exc:                                       # noqa: BLE001
        got = type(exc).__name__ + ": " + str(exc)
    check("ждём владельца со словами, а не ставим задачу", "владелец" in got, True)
    check("в очередь ничего не ушло", placed, [])

    print("\nКабинет: _enqueue без владельца отказывает и строки не пишет")
    if not os.path.exists(DB_COPY):
        print("  ПРОПУЩЕНО: нет базы стенда dev/vsgen.dev.db")
    else:
        import main                                               # noqa: E402
        c = main.db()
        try:
            before = c.execute("SELECT COUNT(*) FROM queue_tasks").fetchone()[0]
            u = c.execute("SELECT id, tenant_id FROM users ORDER BY id").fetchone()
        finally:
            c.close()
        for uid in (None, ""):
            check(f"uid={uid!r} — отказ", main._enqueue(uid, u["tenant_id"] or str(u["id"]),
                                                    "p-ownerless", "images", {}), None)
        c = main.db()
        try:
            after = c.execute("SELECT COUNT(*) FROM queue_tasks").fetchone()[0]
        finally:
            c.close()
        check("строк в очереди не прибавилось", after, before)

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
