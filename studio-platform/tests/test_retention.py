#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Уборка диска: что считается, что можно трогать и чего нельзя.

Работает на настоящем дереве файлов во временном каталоге — потому что проверять надо
именно поведение с диском: симлинк наружу, пустой каталог, отказ удалять чужое.

    .venv/bin/python tests/test_retention.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_retention as R                                         # noqa: E402

NOW = 1_800_000_000.0
DAY = 86400.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def put(path, size=1024):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)


def make_project(root, tid, pid, frames=10, clips=0, audio=1, final=True):
    d = os.path.join(root, tid, "projects", pid)
    for i in range(frames):
        put(os.path.join(d, "googler", f"{i:03d}.jpg"), 4096)
    for i in range(clips):
        put(os.path.join(d, "googler video", f"{i:03d}.mp4"), 8192)
    for i in range(audio):
        put(os.path.join(d, f"voice_{i}.mp3"), 2048)
    if final:
        put(os.path.join(d, "render", "out.mp4"), 16384)
        put(os.path.join(d, "subtitles.srt"), 512)
        put(os.path.join(d, "chapters", "chapters.txt"), 256)
    return d


def fresh_db(projects, phase="published", published_at=NOW - 30 * DAY, busy=0, calls=0):
    c = sqlite3.connect(":memory:")
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT);
      CREATE TABLE pub_releases(id INTEGER PRIMARY KEY, project_id TEXT, release_id TEXT,
                                phase TEXT, status TEXT, video_id TEXT, updated REAL);
      CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, project_id TEXT, status TEXT);
      CREATE TABLE provider_calls(id INTEGER PRIMARY KEY, project_id TEXT, kind TEXT,
                                  status TEXT, cost REAL);
    """)
    for i, (tid, pid) in enumerate(projects):
        c.execute("INSERT INTO projects VALUES(?,?,?)", (pid, tid, "Проект " + pid))
        c.execute("INSERT INTO pub_releases VALUES(?,?,?,?,?,?,?)",
                  (i + 1, pid, f"rel-{pid}", phase, "done", "yt1", published_at))
        for j in range(busy):
            c.execute("INSERT INTO queue_tasks VALUES(?,?,'running')",
                      (i * 100 + j + 1, pid))
        for j in range(calls):
            c.execute("INSERT INTO provider_calls VALUES(?,?,'images','delivered',4.0)",
                      (i * 1000 + j + 1, pid))
    c.commit()
    return c


def main() -> int:
    root = tempfile.mkdtemp(prefix="retention-test-")
    try:
        print("Замер каталога проекта")
        d = make_project(root, "dev", "p1", frames=10, clips=2, audio=1)
        sizes, newest, rest = R.measure(d)
        check("кадры посчитаны", sizes["frames"], 10 * 4096)
        check("клипы посчитаны", sizes["clips"], 2 * 8192)
        check("готовый ролик отдельно от клипов", sizes["final"], 16384)
        check("субтитры и тайм-коды видны", (sizes["subs"], sizes["chapters"]), (512, 256))
        check("постороннего нет", rest, 0)
        check("свежесть файла прочитана", newest > 0, True)

        print("\n  посторонний файл попадает в «прочее», а не теряется")
        put(os.path.join(d, "заметка.txt"), 100)
        sizes2, _n, rest2 = R.measure(d)
        check("прочее учтено", rest2, 100)
        check("и в свою категорию", sizes2["other"], 100)

        print("\n  симлинк наружу не считается своим")
        outside = os.path.join(root, "чужое.jpg")
        put(outside, 999999)
        os.symlink(outside, os.path.join(d, "googler", "999.jpg"))
        sizes3, _n, _r = R.measure(d)
        check("размер кадров не вырос", sizes3["frames"], 10 * 4096)

        print("\nПравила владельца нет — не делаем ничего")
        c = fresh_db([("dev", "p1")])
        projects = R.scan(c, root)
        check("проект найден", len(projects), 1)
        check("опубликован", projects[0].phase, "published")
        acts = R.plan(projects, R.policy(c), now=NOW)
        check("оставить", acts[0][1], "keep")
        check("и сказать почему", "срок хранения владельцем не задан" in acts[0][2], True)

        print("\nПравило есть — промежуточное убирается, артефакты остаются")
        c.execute("INSERT INTO admin_state VALUES('retention',?,0)",
                  (json.dumps({"strip_after_days": 7}),))
        acts = R.plan(R.scan(c, root), R.policy(c), now=NOW)
        check("очистить", acts[0][1], "strip")
        p = acts[0][0]
        check("освободится только промежуточное", acts[0][3], p.strippable)
        before_final = p.sizes["final"]
        R.apply(c, acts, dry_run=True, log=lambda *_a: None)
        check("показ ничего не удалил", os.path.exists(os.path.join(d, "googler")), True)
        R.apply(c, acts, dry_run=False, log=lambda *_a: None)
        check("кадры удалены",
              len([f for f in os.listdir(os.path.join(d, "googler"))
                   if not os.path.islink(os.path.join(d, "googler", f))]), 0)
        check("клипы удалены", os.path.isdir(os.path.join(d, "googler video")), False)
        # Симлинк наружу мы не удаляем даже когда убираем всё вокруг: os.remove сняла бы
        # сам линк, но правило «не ходить за пределы каталога проекта» дороже опрятности.
        check("симлинк наружу не тронут",
              os.path.islink(os.path.join(d, "googler", "999.jpg")), True)
        check("и файл, на который он смотрит, цел", os.path.exists(outside), True)
        check("готовый ролик на месте",
              os.path.getsize(os.path.join(d, "render", "out.mp4")), before_final)
        check("субтитры на месте", os.path.exists(os.path.join(d, "subtitles.srt")), True)
        check("тайм-коды на месте",
              os.path.exists(os.path.join(d, "chapters", "chapters.txt")), True)
        check("проект остался в базе",
              c.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)

        print("\nНеопубликованное не трогаем никогда")
        d2 = make_project(root, "dev", "p2")
        c = fresh_db([("dev", "p2")], phase="scheduled")
        c.execute("INSERT INTO admin_state VALUES('retention',?,0)",
                  (json.dumps({"strip_after_days": 0, "drop_after_days": 0}),))
        acts = R.plan(R.scan(c, root), R.policy(c), now=NOW)
        check("оставить", acts[0][1], "keep")
        check("причина названа прямо", "не опубликован" in acts[0][2], True)
        R.apply(c, acts, dry_run=False, log=lambda *_a: None)
        check("кадры на месте", os.path.isdir(os.path.join(d2, "googler")), True)

        print("\nЗанятый работой проект не трогаем")
        c = fresh_db([("dev", "p2")], busy=1)
        c.execute("INSERT INTO admin_state VALUES('retention',?,0)",
                  (json.dumps({"strip_after_days": 0}),))
        acts = R.plan(R.scan(c, root), R.policy(c), now=NOW)
        check("оставить", acts[0][1], "keep")
        check("причина — идёт работа", "идёт работа" in acts[0][2], True)

        print("\nСрок ещё не вышел")
        c = fresh_db([("dev", "p2")], published_at=NOW - 2 * DAY)
        c.execute("INSERT INTO admin_state VALUES('retention',?,0)",
                  (json.dumps({"strip_after_days": 7}),))
        acts = R.plan(R.scan(c, root), R.policy(c), now=NOW)
        check("оставить", acts[0][1], "keep")
        check("сказано, сколько прошло", "2 дн. назад — рано" in acts[0][2], True)

        print("\nЦена решения видна до нажатия")
        c = fresh_db([("dev", "p2")], calls=155)
        p = R.scan(c, root)[0]
        price = R.price_of(p, "strip")
        check("оплаченные вызовы посчитаны", price["calls"], 155)
        check("и их сумма", price["cost"], 155 * 4.0)

        print("\nУдаление проекта целиком")
        c = fresh_db([("dev", "p2")])
        c.execute("INSERT INTO admin_state VALUES('retention',?,0)",
                  (json.dumps({"drop_after_days": 14}),))
        acts = R.plan(R.scan(c, root), R.policy(c), now=NOW)
        check("удалить", acts[0][1], "drop")
        R.apply(c, acts, dry_run=False, log=lambda *_a: None)
        check("каталога нет", os.path.exists(d2), False)
        check("строки в базе тоже нет",
              c.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)

        print("\nЧужой путь удалять отказываемся")
        strange = os.path.join(root, "не-проект")
        os.makedirs(strange, exist_ok=True)
        put(os.path.join(strange, "файл.bin"), 10)
        c = fresh_db([("dev", "p3")])
        fake = R.Project(pid="p3", tid="dev", name="p3", path=strange, exists=True,
                         sizes={}, total=10, newest=0, phase="published",
                         release_id="", video_id="", published_at=NOW - 99 * DAY,
                         busy=0, paid={})
        out = R.apply(c, [(fake, "drop", "тест", 10)], dry_run=False,
                      log=lambda *_a: None)
        check("отказ записан", len(out["errors"]), 1)
        check("каталог цел", os.path.exists(strange), True)

        print("\nПропавший каталог не ломает разбор")
        c = fresh_db([("dev", "нет-такого")])
        acts = R.plan(R.scan(c, root), {"drop_after_days": 0}, now=NOW)
        check("оставить", acts[0][1], "keep")
        check("сказано почему", "каталога нет" in acts[0][2], True)
    finally:
        shutil.rmtree(root, ignore_errors=True)

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
