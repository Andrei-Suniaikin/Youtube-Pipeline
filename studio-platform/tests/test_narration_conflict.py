#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Две озвучки в проекте — рендер останавливается и объясняет, а не склеивает обе.

Рендер-нода собирает ВСЕ `_FINAL_AUDIO_*` рекурсивно и склеивает их в одну дорожку —
об этом прямо сказано у `_ENGINE_AUDIO_DIR` в кабинете. Штатно лишние папки сносит
`_clear_other_audio_dirs` перед НОВОЙ озвучкой, но до рендера проект доезжает и в обход:
движок сменили после озвучки, задача упала на полпути, материалы пришли из старого
проекта. Тогда в ролик уходит задвоенный звук, а внешне всё «готово».

Кабинет здесь настоящий, подменяется только каталог проектов — на временный.

    .venv/bin/python tests/test_narration_conflict.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("VS_SERVER_DISABLE_BACKGROUND", "1")
os.environ.setdefault("VS_SERVER_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
os.environ.setdefault("VS_TENANTS_DIR", os.path.join(BASE, "dev", "data", "tenants"))
os.environ.setdefault("HOME", os.path.join(BASE, "dev", "home"))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

import main as M                                                  # noqa: E402

FAILS = []
TID, PID = "dev", "proj-test-narration"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def project(root, dirs, images=3):
    """Папка проекта: картинки плюс перечисленные папки озвучки."""
    proj = os.path.join(root, TID, "projects", PID)
    shutil.rmtree(proj, ignore_errors=True)
    os.makedirs(os.path.join(proj, "googler"), exist_ok=True)
    for i in range(images):
        open(os.path.join(proj, "googler", f"{i + 1:03d}.jpg"), "wb").write(b"x" * 10)
    for name in dirs:
        os.makedirs(os.path.join(proj, name), exist_ok=True)
        open(os.path.join(proj, name, "001.mp3"), "wb").write(b"a" * 200000)
    return proj


def main() -> int:
    root = tempfile.mkdtemp(prefix="narration-")
    saved_dir, saved_get, saved_ratio, saved_gap, saved_active = (
        M.TENANTS_DIR, M._get_project, M._images_ratio_ok, M._images_gap_ok,
        M._project_has_active_work)
    settings = {}
    M.TENANTS_DIR = root
    M._get_project = lambda tid, pid: {"settings": "{}"}
    M._psettings = lambda p: settings
    M._images_ratio_ok = lambda tid, pid: True
    M._images_gap_ok = lambda tid, pid, st=None: True
    M._project_has_active_work = lambda tid, pid: False
    try:
        print("Одна озвучка — как было")

        project(root, ["_FINAL_AUDIO_ELEVENLABS"])
        check("папка озвучки видна", M._narration_dirs(M._project_dir(TID, PID)),
              ["_FINAL_AUDIO_ELEVENLABS"])
        check("возражений нет", M._narration_conflict(TID, PID, {}), "")
        check("рендер разрешён", M._render_inputs_ready(TID, PID), True)

        print("\nПустая вторая папка — это не вторая озвучка")

        proj = project(root, ["_FINAL_AUDIO_ELEVENLABS"])
        os.makedirs(os.path.join(proj, "_FINAL_AUDIO_LUMEAN"), exist_ok=True)
        check("пустая папка не считается", M._narration_dirs(proj),
              ["_FINAL_AUDIO_ELEVENLABS"])
        check("рендер по-прежнему разрешён", M._render_inputs_ready(TID, PID), True)

        print("\nДве настоящие озвучки — стоп с объяснением")

        project(root, ["_FINAL_AUDIO_ELEVENLABS", "_FINAL_AUDIO_LUMEAN"])
        why = M._narration_conflict(TID, PID, {"engine": "lumean"})
        check("причина названа словами", bool(why), True)
        check("перечислены обе папки",
              all(name in why for name in ("_FINAL_AUDIO_ELEVENLABS", "_FINAL_AUDIO_LUMEAN")),
              True)
        check("сказано, какая папка у выбранного движка",
              "_FINAL_AUDIO_LUMEAN" in why and "lumean" in why, True)
        settings.clear(); settings.update({"engine": "lumean"})
        check("рендер остановлен", M._render_inputs_ready(TID, PID), False)

        # Материалы при этом ГОТОВЫ — то есть остановка именно из-за озвучек, а не
        # из-за нехватки картинок или звука. Без этой проверки тест доказывал бы не то.
        ready, _n, _a = M._materials_ready(M._project_dir(TID, PID))
        check("материалы на месте, дело не в них", ready, True)

        print("\nДвижок не выбран — тем более стоп")

        why2 = M._narration_conflict(TID, PID, {})
        check("сказано, что движок не выбран", "не выбран" in why2, True)
        settings.clear()
        check("рендер остановлен и тут", M._render_inputs_ready(TID, PID), False)

        print("\nОстанется одна папка — рендер идёт дальше")

        shutil.rmtree(os.path.join(M._project_dir(TID, PID), "_FINAL_AUDIO_ELEVENLABS"))
        settings.clear(); settings.update({"engine": "lumean"})
        check("возражений больше нет", M._narration_conflict(TID, PID, settings), "")
        check("рендер разрешён", M._render_inputs_ready(TID, PID), True)
    finally:
        M.TENANTS_DIR, M._get_project = saved_dir, saved_get
        M._images_ratio_ok, M._images_gap_ok = saved_ratio, saved_gap
        M._project_has_active_work = saved_active
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
