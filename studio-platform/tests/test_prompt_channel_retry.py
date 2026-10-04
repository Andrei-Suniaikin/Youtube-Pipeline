#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Картинки, упавшие из-за отказа канала промптов, ставятся заново — до двух раз (04.10.2026).

13 выпусков стояли в разборе с «этап images завершился со статусом error»: промпты ушли в
запасной канал OpenAI с пустым счётом. Канал выбирается при постановке задачи, пометка
«мёртв» живёт 10 минут и только в памяти службы — после перезапуска очередная задача снова
пробовала пустой счёт, и выпуск вставал навсегда. Отказ денег не стоит: ни промптов, ни
картинок не сделано, — поэтому картинки ставятся заново, канал выбирается снова.

    .venv/bin/python tests/test_prompt_channel_retry.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_services                                               # noqa: E402

NOW = 1_800_000_000.0
FAILS = []
REFUSED = ("[15:27:59.503] ⚠️ ⚠️ Похоже, у канала промптов «openai» исчерпан счёт/лимит: {")


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh(prompt_error=REFUSED, images_error_mark=None):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE users(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT,
                         key_opts TEXT, keys TEXT);
      CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, settings TEXT);
      CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
                               project_id TEXT, kind TEXT, status TEXT, error TEXT);
      CREATE TABLE pub_release_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,
        release_id TEXT, service TEXT, kind TEXT, remote_job_id TEXT, status TEXT,
        error TEXT, created REAL, updated REAL, UNIQUE(release_id,service,kind));
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        ts REAL, level TEXT, step TEXT, message TEXT,
        repeats INTEGER NOT NULL DEFAULT 1, last_ts REAL);
    """)
    c.execute("INSERT INTO users VALUES(1,'dev','dev','{}','{}')")
    c.execute("INSERT INTO queue_tasks VALUES(10,'dev','proj-1','tts','done',NULL)")
    c.execute("INSERT INTO queue_tasks VALUES(11,'dev','proj-1','prompts','error',?)", (prompt_error,))
    c.execute("INSERT INTO queue_tasks VALUES(12,'dev','proj-1','images','error',"
              "'конвейер оборван: последняя задача промптов не завершилась успешно')")
    for kind, jid, st, err in (("tts", "10", "done", None), ("images", "12", "error", images_error_mark)):
        c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
                  "error,created,updated) VALUES('rel-1','vsgen',?,?,?,?,?,?)",
                  (kind, jid, st, err, NOW, NOW))
    c.commit()
    return c


REL = {"release_id": "rel-1", "tenant_id": "dev", "project_id": "proj-1", "lang": "pl",
       "preset_cover": "", "donor_url": "https://youtu.be/aaa", "updated": NOW}


def services(placed):
    return pub_services.PubServices({
        "create_project": lambda *a, **k: "proj-1",
        "enqueue": lambda uid, tid, pid, kind, params: (placed.append(kind), 100 + len(placed))[1],
        "project_dir": lambda *a, **k: "/нет"})


def images_row(c):
    return dict(c.execute("SELECT remote_job_id, status, error FROM pub_release_jobs "
                          "WHERE kind='images'").fetchone())


def main() -> int:
    print("Канал промптов отказал — картинки ставятся заново, выпуск ждёт")
    c = fresh()
    placed = []
    out = services(placed).do_media_ready(c, REL)
    check("не разбор, а ожидание", out, {"wait": True})
    check("картинки поставлены заново", placed, ["images"])
    check("за выпуском — новая задача, счёт повторов 1",
          images_row(c), {"remote_job_id": "101", "status": "queued", "error": "prompt-channel:1"})
    msg = c.execute("SELECT message FROM pub_release_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    check("в журнале — почему", "канал промптов отказал" in msg and "другим каналом" in msg, True)

    print("\n  второй отказ подряд — ещё раз; третий — разбор")
    c = fresh(images_error_mark="prompt-channel:1")
    placed = []
    services(placed).do_media_ready(c, REL)
    check("второй повтор дан", (placed, images_row(c)["error"]), (["images"], "prompt-channel:2"))
    c = fresh(images_error_mark="prompt-channel:2")
    placed = []
    try:
        services(placed).do_media_ready(c, REL)
        check("третий — разбор", "не упало", "ItemFailed")
    except pub_services.ItemFailed as exc:
        check("третий — разбор", "images" in str(exc), True)
    check("и больше не ставили", placed, [])

    print("\nДругая причина падения — не наш случай, как раньше")
    c = fresh(prompt_error="❌ План сцен отклонён: semantic block 2/56: схема не исправлена")
    placed = []
    try:
        services(placed).do_media_ready(c, REL)
        check("схема модели — разбор", "не упало", "ItemFailed")
    except pub_services.ItemFailed:
        check("схема модели — разбор, без повтора", placed, [])

    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
