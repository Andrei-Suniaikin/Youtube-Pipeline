#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обложка делается рядом с производством, а не после него.

Проверяется то, ради чего правка и сделана: заказ уходит в момент постановки озвучки
и картинок, а не после рендера. Плюс три запрета, без которых параллельная ветка
начала бы ронять основную.

    .venv/bin/python tests/test_parallel_cover.py
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


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class FakeCover:
    made = 0
    boom = False

    def __init__(self, key):
        pass

    def create_job(self, preset, lang, donor_url):
        if FakeCover.boom:
            raise RuntimeError("обложки недоступны")
        FakeCover.made += 1
        return f"cover-{FakeCover.made}"


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE users(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT,
                         key_opts TEXT, keys TEXT);
      CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, settings TEXT);
      CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
                               project_id TEXT, kind TEXT, status TEXT);
      CREATE TABLE pub_release_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,
        release_id TEXT, service TEXT, kind TEXT, remote_job_id TEXT, status TEXT,
        error TEXT, created REAL, updated REAL, UNIQUE(release_id,service,kind));
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        ts REAL, level TEXT, step TEXT, message TEXT,
        repeats INTEGER NOT NULL DEFAULT 1, last_ts REAL);
      CREATE TABLE pub_releases(id INTEGER PRIMARY KEY, release_id TEXT, tenant_id TEXT,
        project_id TEXT, lang TEXT, preset_cover TEXT, updated REAL);
    """)
    c.execute("INSERT INTO users VALUES(1,'dev','dev','{}','{}')")
    c.execute("INSERT INTO admin_state VALUES('pub_key_cover','k',0)")
    c.commit()
    return c


REL = {"release_id": "rel-1", "tenant_id": "dev", "project_id": "proj-1", "lang": "pl",
       "preset_cover": "Базовая", "donor_url": "https://youtu.be/aaa", "updated": NOW}


def services(placed):
    s = pub_services.PubServices({
        "create_project": lambda *a, **k: "proj-1",
        "enqueue": lambda uid, tid, pid, kind, params: (placed.append(kind),
                                                        len(placed))[1],
        "project_dir": lambda *a, **k: ""})
    return s


def cover_of(c):
    r = c.execute("SELECT remote_job_id FROM pub_release_jobs WHERE service='cover'"
                  ).fetchone()
    return r["remote_job_id"] if r else None


def main() -> int:
    saved = pub_services.CoverClient
    pub_services.CoverClient = FakeCover
    try:
        print("Обложка заказывается вместе с производством")
        c = fresh()
        FakeCover.made = 0
        placed = []
        svc = services(placed)
        svc.do_media_ready(c, REL)
        check("производство началось", placed, ["tts"])
        check("и обложка заказана тем же тиком", cover_of(c), "cover-1")

        print("\n  повторный тик не заказывает вторую")
        # Задача озвучки должна существовать в очереди, иначе движок справедливо
        # считает этап потерянным — это его отдельная защита, не наша тема.
        c.execute("INSERT INTO queue_tasks VALUES(1,'dev','proj-1','tts','running')")
        c.commit()
        svc.do_media_ready(c, REL)
        check("заказ по-прежнему один", FakeCover.made, 1)
        check("тот же самый", cover_of(c), "cover-1")

        print("\nБез пресета обложки производство не ломается")
        c = fresh()
        FakeCover.made = 0
        placed = []
        services(placed).do_media_ready(c, dict(REL, preset_cover=""))
        check("производство началось", placed, ["tts"])
        check("обложка не заказана", cover_of(c), None)
        check("платного вызова не было", FakeCover.made, 0)

        print("\nОбложки недоступны — производство всё равно идёт")
        c = fresh()
        FakeCover.made = 0
        FakeCover.boom = True
        placed = []
        try:
            services(placed).do_media_ready(c, REL)
        finally:
            FakeCover.boom = False
        check("производство началось", placed, ["tts"])
        check("выпуск не упал", True, True)
        check("сказано в журнал",
              any("вернёмся к ней на своей фазе" in r[0] for r in
                  c.execute("SELECT message FROM pub_release_log")), True)

        print("\nБез ключа к обложкам — молча ждём свою фазу")
        c = fresh()
        c.execute("DELETE FROM admin_state WHERE k='pub_key_cover'")
        c.commit()
        FakeCover.made = 0
        placed = []
        services(placed).do_media_ready(c, REL)
        check("производство началось", placed, ["tts"])
        check("обложка не заказана", FakeCover.made, 0)
    finally:
        pub_services.CoverClient = saved

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
