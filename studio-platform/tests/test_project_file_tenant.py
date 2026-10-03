#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Файлы проекта отдаёт пульт, по своему тенанту, — а не кабинет по тенанту вошедшего.

03.10.2026, владелец: «зашёл в проект, нажал плей на видосе, но он не запустился».
Пульт показывает всем один тенант (`TENANT`, на бою — 3). Ссылки на файлы вели в кабинет
(`/p/{pid}/file/…`), а кабинет ищет проект в тенанте ВОШЕДШЕГО: `grind` (тенант 5) видел
карточку, но каждый файл получал 404. Воспроизведено на бою входом из тенанта 6: видео
Платона и его кадры — 404.

Проверяется: человек из другого тенанта получает видео (с перемоткой — Range), кадр с
миниатюрой и архив; путь не выходит за папку проекта; чужой проект — 404; экран получает
адреса пульта, а не кабинета.

    .venv/bin/python tests/test_project_file_tenant.py
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import time
import types
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="pfile-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "3"
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
    kind TEXT, status TEXT, created REAL, finished REAL, project_id TEXT, error TEXT);
  CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT,
    dir_path TEXT, status TEXT, created REAL, updated REAL, settings TEXT);
""")
NOW = time.time()
_c.execute("INSERT INTO projects VALUES('p_plato','3','Plato',NULL,'draft',?,?,'{}')", (NOW, NOW))
_c.execute("INSERT INTO projects VALUES('p_other','6','Чужой',NULL,'draft',?,?,'{}')", (NOW, NOW))
_c.commit()
_c.close()

PDIR = os.path.join(TMP, "tenants", "3", "projects", "p_plato")
os.makedirs(os.path.join(PDIR, "render"))
os.makedirs(os.path.join(PDIR, "googler"))
VIDEO = bytes(range(256)) * 4096                     # 1 МБ «ролика»
open(os.path.join(PDIR, "render", "273.mp4"), "wb").write(VIDEO)
open(os.path.join(PDIR, "googler", "1.jpg"), "wb").write(b"\xff\xd8kadr")
open(os.path.join(TMP, "tenants", "3", "secret.txt"), "w").write("не отдавать")

# Кабинет в одном процессе с пультом — его помощники берутся из модуля `main`.
cab = types.ModuleType("main")
cab._project_dir = lambda tid, pid: os.path.join(TMP, "tenants", str(tid), "projects", pid)
cab._thumbnail = lambda pdir, rel, full, w: full if rel.endswith(".jpg") else None
cab._DL_CATS = {"renders": "роликов"}
cab._dl_items = lambda pdir, what: ([(os.path.join(pdir, "render", "273.mp4"), "render/273.mp4")]
                                    if what == "renders" else [])


def _zip_stream(items):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for full, arc in items:
            z.write(full, arc)
    yield buf.getvalue()


cab._zip_stream = _zip_stream
sys.modules["main"] = cab

import app as W                                                    # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

# Вошёл НЕ владелец: тенант 5, как `grind` на бою.
W.auth.who = lambda c, sid: {"id": 5, "name": "grind", "role": "user", "tenant_id": "5",
                             "disabled": 0}
client = TestClient(W.app, raise_server_exceptions=False)
client.cookies.set(W.auth.COOKIE, "sid-grind")

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def main() -> int:
    print("Вошёл человек из другого тенанта — файлы проекта ему отдаются")
    r = client.get("/ui/projects/p_plato/file/render/273.mp4")
    check("видео: 200", r.status_code, 200)
    check("тип — видео", r.headers.get("content-type"), "video/mp4")
    check("целиком", len(r.content), len(VIDEO))
    r = client.get("/ui/projects/p_plato/file/render/273.mp4",
                   headers={"Range": "bytes=1000-1999"})
    check("перемотка: 206 на Range", r.status_code, 206)
    check("ровно запрошенный кусок", r.content, VIDEO[1000:2000])
    check("кэш — личный, не общий", r.headers.get("cache-control", "").startswith("private"), True)
    r = client.get("/ui/projects/p_plato/file/googler/1.jpg?w=120")
    check("кадр-миниатюра: 200", (r.status_code, r.headers.get("content-type")),
          (200, "image/jpeg"))
    r = client.get("/ui/projects/p_plato/download/renders")
    check("архив: 200", r.status_code, 200)
    check("в архиве — ролик", zipfile.ZipFile(io.BytesIO(r.content)).namelist(),
          ["Plato/render/273.mp4"])

    print("\nЧего не отдаём")
    check("путь за пределы проекта — 404",
          client.get("/ui/projects/p_plato/file/..%2F..%2Fsecret.txt").status_code, 404)
    check("файла нет — 404", client.get("/ui/projects/p_plato/file/render/9.mp4").status_code, 404)
    check("проект чужого тенанта — 404",
          client.get("/ui/projects/p_other/file/render/273.mp4").status_code, 404)
    check("пустая категория архива — 404",
          client.get("/ui/projects/p_plato/download/audio").status_code, 404)
    W.auth.who = lambda c, sid: None
    check("без входа — 401", client.get("/ui/projects/p_plato/file/render/273.mp4").status_code, 401)

    print("\nЭкран получает адреса пульта, а не кабинета")
    src = open(os.path.join(BASE, "web", "app.py"), encoding="utf-8").read()
    check("в выдаче пульта не осталось ссылок на кабинетный /p/{pid}/file/",
          'f"/p/{urllib.parse.quote(pid)}/file/' in src, False)
    check("и на кабинетный архив",
          'f"/api/projects/{urllib.parse.quote(pid)}/download/' in src, False)
    page = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
    check("фронт ставит префикс пульта к его адресам (pultUrl) — три места",
          page.count("pultUrl(s.file_base)") + page.count("pultUrl(d.file_base)"), 3)
    check("и к архиву", "pultUrl(d.zip_base)" in page, True)

    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
