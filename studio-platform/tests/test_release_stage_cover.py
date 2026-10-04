#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экран выпуска: обложка видна картинкой, публикация — без сырого JSON (04.10.2026).

Владелец: «не показывается обложка на странице проекта, где стоит галочка возле
готового видео, хотя картинки и всё остальное видно». У этапа «Обложка» не было своей
группы файлов: строка писала «готово · задача …» без самой картинки. Там же строка
«Публикация» печатала шаг машины JSON-строкой ({"phase": …}) и уезжала за край.

    .venv/bin/python tests/test_release_stage_cover.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="stage-cover-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

from pub_schema import ensure_pub_schema                           # noqa: E402

_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    role TEXT, tenant_id TEXT, disabled INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
  CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
    kind TEXT, status TEXT, created REAL, finished REAL, project_id TEXT, error TEXT,
    progress REAL, stage TEXT, provider TEXT);
  CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT);
""")
_c.commit()
_c.close()

import app as W                                                    # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# Кабинет в тесте — заглушка: группы файлов проекта отдаёт он (`_dl_items`).
FILES = {"covers": ["cover.jpg"], "renders": ["render/512.mp4"], "images": ["googler/001.jpg"]}
fake = types.ModuleType("main")
fake._project_dir = lambda tenant, pid: os.path.join(TMP, "p", pid)
fake._dl_items = lambda pdir, code: [(os.path.join(pdir, n), n) for n in FILES.get(code, [])]
fake._project_status = lambda tenant, pid: {}
sys.modules["main"] = fake


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def main() -> int:
    c = db()
    c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
              "created,updated) VALUES('r1','cover','job','cj1','done',1,1)")
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,"
              "channel_url,title,video_path,slot_at,status,phase,host,verdict,video_id,"
              "created,updated) VALUES('r1','t1',1,'UC1',42,'u','t','/v.mp4',1,'done',?,"
              "'minsk','scheduled','rWN0KfvPg0Y',1,1)",
              (json.dumps({"phase": "video_id_known", "upload_phase_schema_version": 1,
                           "video_id": "rWN0KfvPg0Y"}),))
    c.commit()
    stages = {s["key"]: s for s in W._stages(c, {"release_id": "r1", "project_id": "p1"})}
    c.close()

    print("Обложка — картинкой")
    cov = stages["cover"]
    check("у этапа есть файл обложки",
          [(f["code"], f["kind"], f["names"]) for f in cov["files"]],
          [("covers", "image", ["cover.jpg"])])
    check("адрес файлов — пульта", cov["file_base"], "/ui/projects/p1/file/")
    check("этап готов", cov["state"], "done")
    check("у этапа обложки нет своих «повторить/отменить» — они шли мимо движка",
          cov["job"], None)
    c = db()
    blocked = {s_["key"]: s_ for s_ in W._stages(c, {"release_id": "r1", "project_id": "p1",
                                                     "status": "blocked"})}["cover"]
    c.close()
    check("у выпуска в разборе — куда жать", "«Взять эту обложку»" in blocked["note"], True)
    check("обложка лежит в проекте и задача сделана — второй раз не показываем",
          cov.get("review"), False)
    c = db()
    c.execute("UPDATE pub_release_jobs SET status='error'")
    c.commit()
    err = {s_["key"]: s_ for s_ in W._stages(c, {"release_id": "r1", "project_id": "p1",
                                                 "status": "blocked"})}["cover"]
    c.close()
    check("обложка не принята — показываем её в строке этапа", err.get("review"), True)
    check("кадры обложку к себе не забрали",
          [f["code"] for f in stages["images"]["files"]], ["images"])

    print("\nПубликация — словами и ссылкой, без сырого JSON")
    pub = stages["upload"]
    facts = " · ".join(pub["facts"])
    check("нет JSON", "{" in facts, False)
    check("шаг словами", "шаг ролик создан" in facts, True)
    check("вердикт словами", "вердикт в расписании" in facts, True)
    check("ролик — ссылкой", pub.get("link"),
          {"href": "https://youtu.be/rWN0KfvPg0Y", "text": "ролик rWN0KfvPg0Y"})

    print("\nЭкран рисует обложку крупнее кадров и ссылку на ролик")
    src = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
    check("у обложки своя ветка в stageFiles", "if (g.code === 'covers')" in src, True)
    check("ссылка ролика выводится", "s.link ? `${s.facts.length ? ' · ' : ''}<a href=" in src, True)
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
