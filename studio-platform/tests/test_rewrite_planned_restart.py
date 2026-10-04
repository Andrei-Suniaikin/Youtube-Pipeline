#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выкатка не тратит попытки рерайта (04.10.2026).

Что случилось. На старте процесса рерайт возвращает идущие задачи в очередь и считает
это попыткой: `resume_count + 1`, после двух — задача хоронится в `interrupted`, а
движок после трёх продолжений уводит выпуск в разбор. 04.10 я выкатил студию четыре
раза за сорок минут: у всех идущих рерайтов кончился лимит, два выпуска (Tajemství
prvků, Tichá Obloha) ушли в разбор «рерайт перезапускали», хотя сами задачи ни разу
не падали.

Лимит придуман против задачи, которая роняет сервер сама. Выкатку отличаем по флагу
`upload-drain`, который deploy/update.sh ставит рядом с базой ДО перезапуска и снимает,
когда новая версия ответила.

Проверяется: свежий флаг — задача вернулась, счётчик не вырос, даже на пределе; без
флага — как раньше (счёт, предел, `interrupted`); забытый старый флаг выкаткой не
считается; отменённая задача не воскресает ни в каком случае.

    .venv/bin/python tests/test_rewrite_planned_restart.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "rewrite", "migrations"))

import api_v1_001 as M                                             # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def world(rows):
    d = tempfile.mkdtemp(prefix="rw-restart-")
    jobs = os.path.join(d, "jobs.db")
    with sqlite3.connect(jobs) as c:
        c.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, data TEXT, status TEXT, created_at REAL, "
                  "priority INTEGER, not_before REAL, user_id TEXT, log TEXT, provider TEXT, "
                  "run_token TEXT, cancel_requested INTEGER DEFAULT 0, "
                  "resume_count INTEGER NOT NULL DEFAULT 0)")
        for jid, status, resumes, cancel in rows:
            c.execute("INSERT INTO jobs(id,data,status,created_at,log,run_token,cancel_requested,"
                      "resume_count) VALUES(?,?,?,?,?,?,?,?)",
                      (jid, "{}", status, time.time(), "", "tok", cancel, resumes))
    return d, os.path.join(d, "sessions.db"), jobs


def state(jobs):
    with sqlite3.connect(jobs) as c:
        return {r[0]: (r[1], r[2]) for r in c.execute("SELECT id, status, resume_count FROM jobs")}


def run(rows, flag_age=None):
    d, sessions, jobs = world(rows)
    flag = os.path.join(d, "upload-drain")
    os.environ.pop("VS_PLANNED_RESTART_FILE", None)
    os.environ.pop("VS_UPLOAD_DRAIN_FILE", None)
    os.environ["VS_SERVER_DB"] = os.path.join(d, "vsgen.db")
    if flag_age is not None:
        open(flag, "w").close()
        t = time.time() - flag_age
        os.utime(flag, (t, t))
    M.migrate_001_api_v1(sessions, jobs, requeue_running=True)
    return state(jobs)


ROWS = [("fresh", "running", 0, 0), ("edge", "running", 2, 0),
        ("cancelled", "running", 0, 1), ("done", "completed", 0, 0)]


def main() -> int:
    print("Выкатка: свежий флаг upload-drain")
    s = run(ROWS, flag_age=30)
    check("новая задача вернулась, счётчик тот же", s["fresh"], ("pending", 0))
    check("задача на пределе (2) тоже вернулась и не похоронена", s["edge"], ("pending", 2))
    check("отменённая не воскресла", s["cancelled"][0], "interrupted")
    check("законченная не тронута", s["done"], ("completed", 0))

    print("\nПадение: флага нет — как раньше")
    s = run(ROWS)
    check("новая задача вернулась, попытка засчитана", s["fresh"], ("pending", 1))
    check("задача на пределе похоронена", s["edge"][0], "interrupted")
    check("отменённая не воскресла", s["cancelled"][0], "interrupted")

    print("\nЗабытый флаг старше получаса — выкаткой не считается")
    s = run(ROWS, flag_age=M.PLANNED_RESTART_MAX_SEC + 60)
    check("попытка засчитана", s["fresh"], ("pending", 1))
    check("предел действует", s["edge"][0], "interrupted")

    print("\nФлаг из будущего (сбитые часы) — не в счёт")
    s = run(ROWS, flag_age=-120)
    check("попытка засчитана", s["fresh"], ("pending", 1))

    print("\nЯвный путь к флагу (VS_PLANNED_RESTART_FILE)")
    d, sessions, jobs = world([("x", "running", 2, 0)])
    other = os.path.join(d, "elsewhere.flag"); open(other, "w").close()
    os.environ["VS_SERVER_DB"] = os.path.join(d, "vsgen.db")
    os.environ["VS_PLANNED_RESTART_FILE"] = other
    M.migrate_001_api_v1(sessions, jobs, requeue_running=True)
    os.environ.pop("VS_PLANNED_RESTART_FILE", None)
    check("берётся явный путь", state(jobs)["x"], ("pending", 2))

    print("\nБез requeue_running (ручной прогон миграции) живые задачи не трогаются")
    d, sessions, jobs = world([("live", "running", 0, 0)])
    open(os.path.join(d, "upload-drain"), "w").close()
    os.environ["VS_SERVER_DB"] = os.path.join(d, "vsgen.db")
    M.migrate_001_api_v1(sessions, jobs, requeue_running=False)
    check("running остался running", state(jobs)["live"], ("running", 0))

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
