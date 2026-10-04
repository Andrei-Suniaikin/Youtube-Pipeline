#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Задача рерайта снова в работе — экран выпуска перестаёт показывать «ошибка» (04.10.2026).

Владелец, со снимком выпуска «William the Conqueror…»: «почему не могу запустить». Длинный
рерайт трижды прервали выкатки, выпуск ушёл в разбор, его вернули в работу, и задача уже
стояла у сервиса текста («pending»). А зеркало этапа держало старое «error» с прежней
причиной: экран показывал «ошибка» и значок «повторить», сервис на каждое нажатие отвечал
«retry available only for failed/cancelled/partial/interrupted jobs» — восемь строк
столбиком.

Что проверяется:
  • сосед говорит «pending»/«running», а зеркало «error» — зеркало «running», причина
    снята, в журнале выпуска сказано, что задача снова в работе;
  • зеркало уже «running» — второй строки в журнал не пишем;
  • прерванная задача по-прежнему возобновляется (поведение не тронуто);
  • пульт переводит отказ сервиса «retry available only…» на русский;
  • строка отказа у кнопки одна, а не по строке на нажатие.

    .venv/bin/python tests/test_rewrite_mirror_sync.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_calls                                                  # noqa: E402
import pub_services                                               # noqa: E402

NOW = 1_800_000_000.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Client:
    state = "pending"
    retried = []

    def __init__(self, *_a, **_k):
        pass

    def job(self, job_id):
        return {"status": Client.state}

    def retry(self, job_id):
        Client.retried.append(job_id)
        return {"status": "pending"}

    def resolve_model(self, preset):
        return "terra"


def fresh(mirror_status, error):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE pub_release_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,
        release_id TEXT, service TEXT, kind TEXT, remote_job_id TEXT, status TEXT,
        payload TEXT, error TEXT, created REAL, updated REAL,
        UNIQUE(release_id,service,kind));
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        ts REAL, level TEXT, step TEXT, message TEXT,
        repeats INTEGER NOT NULL DEFAULT 1, last_ts REAL);
      CREATE TABLE pub_releases(id INTEGER PRIMARY KEY, release_id TEXT, tenant_id TEXT,
        project_id TEXT, lang TEXT, preset_rewrite TEXT, updated REAL);
      CREATE TABLE users(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT,
        key_opts TEXT, keys TEXT);
      CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, settings TEXT);
    """)
    pub_calls.ensure_calls_schema(c)
    c.execute("INSERT INTO admin_state VALUES('pub_key_rewrite','k',0)")
    c.execute("INSERT INTO users VALUES(1,'dev','dev','{}','{}')")
    c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
              "payload,error,created,updated) VALUES('rel-1','rewrite','job','3b4f43234f27',?,"
              "'{\"donor_chars\": 111375}',?,?,?)", (mirror_status, error, NOW, NOW))
    c.commit()
    return c


REL = {"release_id": "rel-1", "tenant_id": "dev", "project_id": "proj-1", "lang": "cs",
       "preset_rewrite": "Базовый", "ch_name": "Noční Portréty",
       "donor_url": "https://youtu.be/aaa", "updated": NOW}


def svc():
    return pub_services.PubServices({"create_project": lambda *a, **k: "",
                                     "enqueue": lambda *a, **k: None,
                                     "project_dir": lambda *a, **k: ""})


def mirror(c):
    return dict(c.execute("SELECT status, error FROM pub_release_jobs").fetchone())


def log(c):
    return [r[0] for r in c.execute("SELECT message FROM pub_release_log")]


def main() -> int:
    saved = pub_services.RewriteClient
    pub_services.RewriteClient = Client
    try:
        print("Задача снова у сервиса текста, зеркало — старое «error»")
        old = "задача рерайта прервалась — рерайт перезапускали …; нужен разбор"
        for state in ("pending", "running"):
            Client.state = state
            c = fresh("error", old)
            out = svc().do_source_ready(c, REL)
            check(f"«{state}»: ждём", out, {"wait": True})
            check(f"«{state}»: зеркало в работе, причина снята", mirror(c),
                  {"status": "running", "error": None})
            check(f"«{state}»: в журнале — снова в работе",
                  any("снова в работе" in m for m in log(c)), True)
            check(f"«{state}»: повторно не дёргали", Client.retried, [])

        print("\nЗеркало уже «running» — журнал не засоряем")
        Client.state = "running"
        c = fresh("running", None)
        svc().do_source_ready(c, REL)
        check("строки нет", log(c), [])

        print("\nПрерванная задача — возобновляется, как раньше")
        Client.state = "interrupted"
        c = fresh("running", None)
        out = svc().do_source_ready(c, REL)
        check("возобновили ту же задачу", Client.retried, ["3b4f43234f27"])
    finally:
        pub_services.RewriteClient = saved

    print("\nПульт: отказ сервиса — по-русски, одной строкой у кнопки")
    app_src = open(os.path.join(BASE, "web", "app.py"), encoding="utf-8").read()
    check("«retry available only…» переводится",
          'if "retry available only for" in (detail or ""):' in app_src
          and "повторить нельзя: задача уже не упавшая" in app_src, True)
    idx = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
    check("строка отказа у кнопки одна", "':scope > .note.bad.act-err'" in idx, True)
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
