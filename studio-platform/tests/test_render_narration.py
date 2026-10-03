#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Набор файлов основной озвучки выбирает ОДИН модуль — общий у кабинета и рендер-ноды.

Было так: какие именно файлы попадут в звуковую дорожку, решала нода. Она обходит
каталог проекта рекурсивно и склеивает всё найденное аудио, а сервер проверял только
факт «есть хоть один mp3». Отсюда дефект, ради которого всё и затевалось: две
сохранившиеся папки `_FINAL_AUDIO_*` → в ролике звучат обе озвучки подряд.

Лечение — выбрать набор на сервере при выдаче задачи и закрепить его. Чтобы выбор
сервера и проверка ноды не разъехались, код выбора не копируется, а берётся один:
`render/render_narration.py` побайтово равен и боевому кабинету, и файлу на ноде
(sha `dfcbe0a056ee…a1bf9`, выкладка 14.09). Первая проверка ниже — сторож этого
равенства: разъезд должен ловиться здесь, а не в готовом ролике.

Тело модуля поэтому НЕ комментируется по-русски: любая правка внутри ломает равенство.
«Почему» живёт здесь и на месте вызова в `server/main.py`.

    .venv/bin/python tests/test_render_narration.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
REPO = os.path.abspath(os.path.join(BASE, ".."))
sys.path.insert(0, os.path.join(BASE, "render"))
sys.path.insert(0, os.path.join(BASE, "server"))

FAILS = []
# Боевой файл: и в кабинете, и на ноде — один и тот же (проверено diff'ом снимка 18.09).
LIVE = os.path.join(REPO, "live_2026-09-18", "код", "кабинет", "render_narration.py")
NODE = os.path.join(REPO, "live_2026-09-18", "код", "рендер", "нода_минск", "render_narration.py")
OURS = os.path.join(BASE, "render", "render_narration.py")
LIVE_SHA = "dfcbe0a056eeb1b87b14e4276bbedd82ee54a9d2f526bd4ef1a7a52d840a1bf9"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def raises(label, fn):
    """Ошибка выбора обязана быть ValueError с текстом по-русски: этот текст уходит
    человеку в карточку задачи, а не в лог для разработчика."""
    try:
        got = fn()
    except ValueError as exc:
        text = str(exc)
        ok = bool(text) and any("а" <= ch.lower() <= "я" for ch in text)
        check(label + " — отказ словами", ok, True)
        return text
    check(label + " — отказ", f"вернулось {got!r}", "ValueError")
    return ""


def sha_file(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def audio(root, rel, size=2048):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"a" * size)
    return path


def fresh(root, name):
    proj = os.path.join(root, name)
    shutil.rmtree(proj, ignore_errors=True)
    os.makedirs(proj, exist_ok=True)
    return proj


def main() -> int:
    print("Модуль у нас и в бою — один и тот же файл")

    check("боевой кабинет на месте", os.path.isfile(LIVE), True)
    # 03.10.2026: побайтовое равенство снято осознанно — ТЗ «озвучка FastGen», п. 1.4:
    # в ENGINE_DIRS добавлена пара 'fastgen' → '_FINAL_AUDIO_FASTGEN' (та же правка в gen).
    # Ноду обновлять для работы не нужно: закреплённый narration_files уезжает в cfg, и
    # select_narration отдаёт его раньше, чем смотрит в ENGINE_DIRS. Сторож теперь точный:
    # отличие от боя и от ноды — ровно одна строка, и в ней ровно одна новая пара. Любое
    # другое расхождение — провал, как и раньше: выбор файлов обязан совпадать.
    check("боевой кабинет и нода — одно и то же (их мы не трогали)",
          sha_file(LIVE), sha_file(NODE))
    check("и это тот самый sha из квитанции выкладки 14.09", sha_file(LIVE), LIVE_SHA)
    import difflib
    отличия = [l for l in difflib.unified_diff(
        open(NODE, encoding="utf-8").read().splitlines(),
        open(OURS, encoding="utf-8").read().splitlines(), lineterm="", n=0)
        if (l.startswith("+") or l.startswith("-")) and not l.startswith(("+++", "---"))]
    check("от ноды отличаемся ровно одной заменённой строкой", len(отличия), 2)
    check("и это строка ENGINE_DIRS",
          all(l[1:].startswith("ENGINE_DIRS=") for l in отличия), True)
    было_ = next((l[1:] for l in отличия if l.startswith("-")), "")
    стало_ = next((l[1:] for l in отличия if l.startswith("+")), "")
    check("добавлена ровно пара fastgen, остальное слово в слово",
          стало_ == было_.replace("}", ",'fastgen':'_FINAL_AUDIO_FASTGEN'}"), True)

    from render_narration import ENGINE_DIRS, select_narration

    # Вторую таблицу «движок → папка» не заводим: она уже есть в кабинете и обязана
    # совпадать. Разойдутся — кабинет будет чистить одну папку, а рендер брать другую.
    os.environ.setdefault("VS_SERVER_DISABLE_BACKGROUND", "1")
    os.environ.setdefault("VS_SERVER_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
    os.environ.setdefault("HOME", os.path.join(BASE, "dev", "home"))
    sys.path.insert(0, os.path.join(BASE, "workers"))
    import main as M                                             # noqa: E402
    check("таблица движков кабинета совпадает с модулем", M._ENGINE_AUDIO_DIR, ENGINE_DIRS)

    root = tempfile.mkdtemp(prefix="narration-contract-")
    try:
        print("\nОдна папка озвучки — она и едет")

        proj = fresh(root, "one")
        audio(proj, "_FINAL_AUDIO_EDGETTS/001.mp3")
        audio(proj, "_FINAL_AUDIO_EDGETTS/002.mp3")
        check("выбрана вся папка", select_narration(proj, {}),
              ["_FINAL_AUDIO_EDGETTS/001.mp3", "_FINAL_AUDIO_EDGETTS/002.mp3"])
        check("пути относительные — корень проекта на ноде другой",
              all(not os.path.isabs(p) for p in select_narration(proj, {})), True)

        print("\nДве папки: движок известен — берётся его папка, неизвестен — отказ")

        proj = fresh(root, "two")
        audio(proj, "_FINAL_AUDIO_EDGETTS/001.mp3")
        audio(proj, "_FINAL_AUDIO_LUMEAN/001.mp3")
        check("движок lumean → только его файлы", select_narration(proj, {"engine": "lumean"}),
              ["_FINAL_AUDIO_LUMEAN/001.mp3"])
        check("legacy-значение tts читается как ElevenLabs", ENGINE_DIRS["tts"],
              "_FINAL_AUDIO_ELEVENLABS")
        why = raises("движок не выбран", lambda: select_narration(proj, {}))
        check("сказано, что вариантов несколько", "есколько вариантов" in why, True)
        why = raises("движок выбран, а его папки нет",
                     lambda: select_narration(proj, {"engine": "voicer"}))
        check("сказано, что озвучки этого движка нет", "выбранного движка" in why, True)
        check("tts_engine — тот же ключ, что engine",
              select_narration(proj, {"tts_engine": "edge"}), ["_FINAL_AUDIO_EDGETTS/001.mp3"])

        print("\nПустая вторая папка — это не второй вариант")

        proj = fresh(root, "empty-second")
        audio(proj, "_FINAL_AUDIO_EDGETTS/001.mp3")
        os.makedirs(os.path.join(proj, "_FINAL_AUDIO_LUMEAN"), exist_ok=True)
        audio(proj, "_FINAL_AUDIO_VOICER/001.mp3", size=0)
        check("пустая папка и файл нулевого размера не считаются",
              select_narration(proj, {}), ["_FINAL_AUDIO_EDGETTS/001.mp3"])

        print("\nПапок нет — фолбэк по корню, со всеми исключениями")

        proj = fresh(root, "fallback")
        audio(proj, "001.mp3")
        audio(proj, "_ASSETS/beep.mp3")
        audio(proj, "_RENDER_MEDIA/stock/music.mp3")
        audio(proj, "chapters/voice.mp3")
        audio(proj, "render/old.mp3")
        audio(proj, ".remote_jobs/x.mp3")
        audio(proj, "temp_files/002.mp3")
        audio(proj, "final_video_preview.mp3")
        audio(proj, "bg.mp3")
        audio(proj, "outro.mp3")
        got = select_narration(proj, {"background_audio_file": "bg.mp3",
                                      "end_track_file": "outro.mp3"})
        check("служебные папки, temp и final_video исключены; фон и финальный трек тоже",
              got, ["001.mp3"])

        print("\nПорядок — по числу в имени, а не по строке")

        proj = fresh(root, "order")
        for name in ("2.mp3", "10.mp3", "1.mp3"):
            audio(proj, "_FINAL_AUDIO_EDGETTS/" + name)
        check("10 идёт после 2", select_narration(proj, {}),
              ["_FINAL_AUDIO_EDGETTS/1.mp3", "_FINAL_AUDIO_EDGETTS/2.mp3",
               "_FINAL_AUDIO_EDGETTS/10.mp3"])

        print("\nЗакреплённый список проверяется, а не принимается на веру")

        proj = fresh(root, "pinned")
        audio(proj, "_FINAL_AUDIO_EDGETTS/001.mp3")
        audio(proj, "_FINAL_AUDIO_EDGETTS/002.mp3")
        audio(proj, "_FINAL_AUDIO_EDGETTS/empty.mp3", size=0)
        outside = os.path.join(root, "outside.mp3")
        open(outside, "wb").write(b"a" * 2048)
        check("нормальный список возвращается как есть",
              select_narration(proj, {"narration_files": ["_FINAL_AUDIO_EDGETTS/002.mp3"]}),
              ["_FINAL_AUDIO_EDGETTS/002.mp3"])
        check("порядок закреплённого списка не пересортировывается: его выбрал сервер",
              select_narration(proj, {"narration_files": ["_FINAL_AUDIO_EDGETTS/002.mp3",
                                                          "_FINAL_AUDIO_EDGETTS/001.mp3"]}),
              ["_FINAL_AUDIO_EDGETTS/002.mp3", "_FINAL_AUDIO_EDGETTS/001.mp3"])
        for label, value in (
                ("пустой список", []),
                ("не список", "_FINAL_AUDIO_EDGETTS/001.mp3"),
                ("абсолютный путь", [outside]),
                ("выход из проекта через ..", ["../outside.mp3"]),
                ("дубли", ["_FINAL_AUDIO_EDGETTS/001.mp3", "_FINAL_AUDIO_EDGETTS/001.mp3"]),
                ("файла нет", ["_FINAL_AUDIO_EDGETTS/003.mp3"]),
                ("файл пустой", ["_FINAL_AUDIO_EDGETTS/empty.mp3"]),
                ("не аудио", ["_FINAL_AUDIO_EDGETTS/001.txt"]),
        ):
            raises("закреплённый список: " + label,
                   lambda v=value: select_narration(proj, {"narration_files": v}))
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
