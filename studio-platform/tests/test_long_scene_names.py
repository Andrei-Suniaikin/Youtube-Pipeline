#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ролик длиннее 999 сцен: кадры 1000.jpg и дальше (05.10.2026).

Что случилось. Кадры называются по формату :03d — 001.jpg … 999.jpg, а с тысячной сцены
четыре цифры: 1000.jpg. И сервер, и рендерер искали номер сцены ровно из трёх цифр.
Четыре готовых длинных ролика (1184–1351 сцен) сутки стояли в очереди рендера: проверка
дыр видела сцены 1000+ пропавшими («дыра» в 210 кадров) и задачу ноде не выдавала. Пустить
их как есть было бы хуже: рендерер сортирует кадры строкой (1000.jpg встаёт между 100.jpg
и 101.jpg) и не привязывает сцены 1000+ к озвучке — последние 20 минут ролика шли бы
одной застывшей картинкой.

Проверяется: сервер видит полный набор 1..1209 без дыр и настоящую дыру по-прежнему
ловит; рендерер привязывает 1000.jpg к сцене 999 и ставит кадры по номеру сцены; у
проектов до 999 сцен порядок ровно прежний, строковый.

    .venv/bin/python tests/test_long_scene_names.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import random
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "shared"))
sys.path.insert(0, os.path.join(BASE, "server"))

FAILS = []
TMP = tempfile.mkdtemp(prefix="long-scenes-")
LOCKS = os.path.join(TMP, "locks")
os.makedirs(LOCKS, mode=0o700, exist_ok=True)
os.chmod(LOCKS, 0o700)
os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"] = LOCKS
KEYS_PATH = os.path.join(TMP, "video_studio_api_keys.json")
json.dump({}, open(KEYS_PATH, "w"))
DEV_DB = os.path.join(BASE, "dev", "vsgen.dev.db")
DB_COPY = os.path.join(TMP, "vsgen.copy.db")
if os.path.exists(DEV_DB):
    shutil.copy(DEV_DB, DB_COPY)
os.environ["HOME"] = os.path.join(BASE, "dev", "home")
os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
os.environ["VS_SERVER_KEYS_PATH"] = KEYS_PATH
os.environ["VS_SERVER_DB"] = DB_COPY
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["VS_KEY_BUDGET_MODE"] = "off"

import main                                                         # noqa: E402

spec = importlib.util.spec_from_file_location(
    "render_worker_long", os.path.join(BASE, "render", "render_worker.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
        return
    if isinstance(got, list) and isinstance(want, list):
        at = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))
        got, want = got[at:at + 3], want[at:at + 3]
        label += f" (расходится с позиции {at})"
    print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
    FAILS.append(label)


def make_project(scenes, missing=()):
    proj = tempfile.mkdtemp(prefix="proj-", dir=TMP)
    g = os.path.join(proj, "googler")
    os.makedirs(g)
    json.dump([{"text_segment": f"s{i}"} for i in range(scenes)],
              open(os.path.join(g, "scenes.json"), "w"))
    for i in range(1, scenes + 1):
        if i not in missing:
            open(os.path.join(g, f"{i:03d}.jpg"), "wb").write(b"x")
    return proj


print("\nСервер: проверка дыр перед выдачей рендера")
_orig_dir = main._project_dir
try:
    full = make_project(1209)
    main._project_dir = lambda tid, pid: full
    ok, longest, missing, total = main._images_gap_report("3", "p")
    check("1209 сцен без пропусков — рендер выдаётся", ok, True)
    check("1209 сцен без пропусков — дыр нет", (longest, missing), (0, []))
    check("1209 сцен — всего сцен", total, 1209)

    holed = make_project(1209, missing=set(range(1100, 1110)))
    main._project_dir = lambda tid, pid: holed
    ok, longest, missing, _total = main._images_gap_report("3", "p")
    check("настоящая дыра в 10 кадров после тысячной — держим", ok, False)
    check("настоящая дыра — названа точно", (longest, missing[0], missing[-1]), (10, 1100, 1109))

    tail = make_project(1209, missing=set(range(1205, 1210)))
    main._project_dir = lambda tid, pid: tail
    check("хвост в 5 кадров — в пределах допуска", main._images_gap_report("3", "p")[0], True)

    short = make_project(287, missing={100, 101})
    main._project_dir = lambda tid, pid: short
    check("короткий ролик — как раньше", main._images_gap_report("3", "p")[:3], (True, 2, [100, 101]))
finally:
    main._project_dir = _orig_dir

check("имя 1000.jpg — сцена 1000", main._numbered_googler_scene_id("/x/googler/1000.jpg"), 1000)
check("имя 047_variant.png — сцена 47", main._numbered_googler_scene_id("047_variant.png"), 47)
check("«1945 Berlin.jpg» — не сцена", main._numbered_googler_scene_id("1945 Berlin.jpg"), None)

print("\nРендерер: привязка к сцене")
check("1000.jpg → сцена 999 (0-based)", R._scene_index_from_filename("/p/googler/1000.jpg"), 999)
check("1209_video.mp4 → сцена 1208", R._scene_index_from_filename("1209_video.mp4"), 1208)
check("047.jpg → сцена 46, как раньше", R._scene_index_from_filename("047.jpg"), 46)
check("«1945 Berlin.jpg» — не сцена, как раньше", R._scene_index_from_filename("1945 Berlin.jpg"), None)
check("«12.jpg» — не сцена, как раньше", R._scene_index_from_filename("12.jpg"), None)

print("\nРендерер: порядок кадров")
names = [f"/p/googler/{i:03d}.jpg" for i in range(1, 1210)]
shuffled = list(names)
random.Random(5).shuffle(shuffled)
check("строковая сортировка действительно ломает порядок (исходный дефект)",
      sorted(shuffled) == names, False)
check("1..1209 встают по номеру сцены", R._sorted_media(shuffled), names)
check("по алфавиту (по имени файла) — тоже по номеру",
      R._sorted_media(shuffled, key=lambda x: os.path.basename(x).lower()), names)

mixed = ([f"/p/googler/{i:03d}.jpg" for i in range(1, 300)]
         + ["/p/googler/1945 Berlin.jpg", "/p/googler/0.jpg", "/p/googler/00_x.jpg",
            "/p/googler video/001_video.mp4", "/p/intro.mp4", "/p/googler/12.jpg",
            "/p/googler/001_variant.png", "/p/googler/0001.jpg", "/p/googler/0999.jpg"])
random.Random(7).shuffle(mixed)
check("без сцен 1000+ — ровно прежняя строковая сортировка", R._sorted_media(mixed), sorted(mixed))
alpha = lambda x: os.path.basename(x).lower()                       # noqa: E731
check("без сцен 1000+ — «по алфавиту» как раньше",
      R._sorted_media(mixed, key=alpha), sorted(mixed, key=alpha))

print("\nРендерер: сортировка встроена в оба места")
src = open(os.path.join(BASE, "render", "render_worker.py"), encoding="utf-8").read()
check("сбор медиа проекта идёт через _sorted_media", src.count("all_media = _sorted_media(all_media)"), 2)
check("голой all_media.sort() не осталось", "all_media.sort()" in src, False)
check("режим «по алфавиту» идёт через _sorted_media",
      "ordered = _sorted_media(videos_ordered + images_ordered" in src, True)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nпровалов: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
