#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""О конфликте озвучек существует ровно ОДНО правило, и оно живёт в render_narration.

Было два. Кабинет считал конфликтом «папок с mp3 больше одной» (`_narration_conflict`),
а на выдаче задачи набор файлов выбирал `render/render_narration.select_narration` — тот
же модуль, что стоит на ноде. Отвечали они по-разному в обе стороны:

  * своё правило МОЛЧАЛО там, где модуль отказывается: папка не того движка, вторая
    папка с wav, пустые файлы — такой проект уезжал на ноду и возвращался чужим голосом;
  * своё правило ЗАПРЕЩАЛО там, где модуль справляется: две папки при явно выбранном
    движке. И запрещало молча: стоп стоял в `_render_inputs_ready`, то есть задача даже
    не попадала в кандидаты на выдачу — её не выдавали, но и в ошибку не переводили.
    Человек видел «нет задач», а причину знал только модуль, до которого дело не доходило.

Теперь правило одно: решает модуль, `_narration_conflict` его пересказывает человеку, а
`_render_inputs_ready` про озвучку не знает вовсе — неоднозначность ловит выдача и пишет
причину в задачу (это проверяет tests/test_render_claim_narration.py).

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
sys.path.append(os.path.join(BASE, "render"))

import main as M                                                  # noqa: E402
from render_narration import ENGINE_DIRS, select_narration        # noqa: E402

FAILS = []
TID, PID = "dev", "proj-test-narration"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def project(root, files, images=3):
    """Папка проекта: картинки плюс перечисленные файлы (путь относительно проекта)."""
    proj = os.path.join(root, TID, "projects", PID)
    shutil.rmtree(proj, ignore_errors=True)
    os.makedirs(os.path.join(proj, "googler"), exist_ok=True)
    for i in range(images):
        open(os.path.join(proj, "googler", f"{i + 1:03d}.jpg"), "wb").write(b"x" * 10)
    for rel in files:
        path = os.path.join(proj, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(b"a" * 200000)
    return proj


def module_says(proj, settings):
    """Ответ ЕДИНСТВЕННОГО источника правды: список файлов либо текст отказа."""
    try:
        return select_narration(proj, dict(settings))
    except ValueError as exc:
        return str(exc)


def main() -> int:
    root = tempfile.mkdtemp(prefix="narration-")
    saved = (M.TENANTS_DIR, M._get_project, M._psettings, M._images_ratio_ok,
             M._images_gap_ok, M._project_has_active_work)
    settings = {}
    M.TENANTS_DIR = root
    M._get_project = lambda tid, pid: {"settings": "{}"}
    M._psettings = lambda p: settings
    M._images_ratio_ok = lambda tid, pid: True
    M._images_gap_ok = lambda tid, pid, st=None: True
    M._project_has_active_work = lambda tid, pid: False
    try:
        print("Таблица «движок → папка» у кабинета и у модуля одна и та же (Issue 8)")

        # Не «совпадает», а именно ОДНА: кабинет не переписывает её у себя, а берёт из
        # модуля. Две копии разъезжаются молча, и тогда `_clear_other_audio_dirs`
        # сносит свежую оплаченную озвучку как «папку чужого движка».
        check("кабинет отдаёт содержимое ENGINE_DIRS", M._ENGINE_AUDIO_DIR, dict(ENGINE_DIRS))
        check("и это не случайное совпадение двух списков",
              M._ENGINE_AUDIO_DIR == dict(ENGINE_DIRS) and set(ENGINE_DIRS) == {
                  "edge", "voicer", "voicegencore", "tts", "lumean", "fastgen"}, True)

        print("\nОдна озвучка — как было")

        proj = project(root, ["_FINAL_AUDIO_ELEVENLABS/001.mp3"])
        settings.clear(); settings.update({"engine": "tts"})
        check("папка озвучки видна", M._narration_dirs(proj), ["_FINAL_AUDIO_ELEVENLABS"])
        check("возражений нет", M._narration_conflict(TID, PID, dict(settings)), "")
        check("рендер разрешён", M._render_inputs_ready(TID, PID), True)

        print("\nДве папки при ВЫБРАННОМ движке — это не конфликт (старое правило врало)")

        # Ключевой случай. Модуль выбирает папку движка и только её; закрепление на
        # claim кладёт в tar ровно эти файлы, поэтому задвоиться звуку негде. Старое
        # правило здесь останавливало рендер — и останавливало молча.
        proj = project(root, ["_FINAL_AUDIO_LUMEAN/001.mp3", "_FINAL_AUDIO_EDGETTS/001.mp3"])
        settings.clear(); settings.update({"engine": "lumean"})
        check("модуль выбирает папку движка",
              module_says(proj, settings), ["_FINAL_AUDIO_LUMEAN/001.mp3"])
        check("двух папок кабинет видит две", M._narration_dirs(proj),
              ["_FINAL_AUDIO_EDGETTS", "_FINAL_AUDIO_LUMEAN"])
        check("и всё равно молчит — решает модуль",
              M._narration_conflict(TID, PID, dict(settings)), "")
        check("рендер разрешён", M._render_inputs_ready(TID, PID), True)

        print("\nДве папки БЕЗ выбранного движка — конфликт, и слова те же, что у модуля")

        settings.clear()
        why = M._narration_conflict(TID, PID, {})
        check("модуль отказывает", module_says(proj, {}),
              "Несколько вариантов озвучки; выберите движок перед рендером")
        check("плашка начинается словами модуля", why.startswith(module_says(proj, {})), True)
        check("перечислены обе папки",
              all(name in why for name in ("_FINAL_AUDIO_EDGETTS", "_FINAL_AUDIO_LUMEAN")),
              True)
        check("сказано, что движок не выбран", "не выбран" in why, True)
        # А рендер при этом НЕ останавливается здесь: причину человеку скажет выдача,
        # переведя задачу в ошибку. Останавливать тут — значит прятать задачу навсегда.
        check("гейт готовности про озвучку не знает", M._render_inputs_ready(TID, PID), True)

        print("\nПапка не того движка — старое правило молчало, модуль отказывает")

        proj = project(root, ["_FINAL_AUDIO_EDGETTS/001.mp3"])
        settings.clear(); settings.update({"engine": "voicer"})
        check("одна папка — старому правилу нечего было ловить",
              len(M._narration_dirs(proj)), 1)
        why = M._narration_conflict(TID, PID, dict(settings))
        check("модуль отказывает", module_says(proj, settings),
              "Нет озвучки выбранного движка; завершите её генерацию перед рендером")
        check("и кабинет говорит это же", why.startswith(module_says(proj, settings)), True)
        check("названа папка выбранного движка",
              ENGINE_DIRS["voicer"] in why and "voicer" in why, True)

        print("\nВторая папка с wav: mp3-правило её не видело, модуль видит")

        proj = project(root, ["_FINAL_AUDIO_LUMEAN/001.mp3", "_FINAL_AUDIO_EDGETTS/001.wav"])
        settings.clear()
        check("по mp3 папка ровно одна", M._narration_dirs(proj), ["_FINAL_AUDIO_LUMEAN"])
        check("модуль считает варианты по ЛЮБОМУ аудио", module_says(proj, {}),
              "Несколько вариантов озвучки; выберите движок перед рендером")
        check("и кабинет больше не молчит",
              M._narration_conflict(TID, PID, {}).startswith(module_says(proj, {})), True)

        print("\nЗакрепление прошлого захвата в ответе не участвует")

        # `narration_files` из params прошлого рендера — снимок старого диска. Спросить
        # с ним значит получить «проверь старый список», а не «что выберется сейчас».
        # render_claim выбрасывает этот ключ ровно так же, перед выбором.
        proj = project(root, ["_FINAL_AUDIO_LUMEAN/001.mp3", "_FINAL_AUDIO_EDGETTS/001.mp3"])
        stale = {"narration_files": ["_FINAL_AUDIO_EDGETTS/001.mp3"]}
        check("со старым закреплением ответ тот же, что без него",
              M._narration_conflict(TID, PID, dict(stale)),
              M._narration_conflict(TID, PID, {}))

        print("\nОзвучки нет вовсе — это не конфликт, а работа _materials_ready")

        project(root, [])
        check("плашки нет", M._narration_conflict(TID, PID, {}), "")
        ready, _n, _a = M._materials_ready(M._project_dir(TID, PID))
        check("а «материалы не готовы» сказано там, где и раньше", ready, False)
    finally:
        (M.TENANTS_DIR, M._get_project, M._psettings, M._images_ratio_ok,
         M._images_gap_ok, M._project_has_active_work) = saved
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
