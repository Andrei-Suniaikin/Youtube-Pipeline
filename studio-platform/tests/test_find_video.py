#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""На канал уходит именно наш mp4, а не «самый свежий файл в папке».

Выбор ролика для заливки был устроен так: точное имя `render/<job>.mp4`, а если его нет —
самый свежий mp4 по времени файла. В папке проекта лежат прошлые версии и результаты
перерендеров, и «самый свежий» — это догадка, ценой которой чужой ролик уходит на канал
под нашим названием и в наш слот. Теперь порядок такой: путь, записанный самим
завершившимся рендером (`queue_tasks.output_rel`) → штатное имя → единственный файл в
папке. Несколько кандидатов без точного ответа — отказ с перечислением, а не выбор.

    .venv/bin/python tests/test_find_video.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_publish import VideoAmbiguous, find_video                # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def project(files):
    """Папка проекта с перечисленными mp4; порядок задаёт время изменения."""
    root = tempfile.mkdtemp(prefix="findvideo-")
    os.makedirs(os.path.join(root, "render"), exist_ok=True)
    stamp = time.time() - 3600
    for name in files:
        path = os.path.join(root, "render", name)
        open(path, "wb").write(b"mp4")
        os.utime(path, (stamp, stamp))
        stamp += 600          # каждый следующий файл «свежее» предыдущего
    return root


def main() -> int:
    print("Точный путь от рендера сильнее всего остального")

    root = project(["15.mp4", "77_pererender.mp4"])
    check("берётся записанный рендером файл",
          os.path.basename(find_video(root, "15", "render/15.mp4")), "15.mp4")
    # Самый свежий здесь — 77_pererender.mp4; раньше уехал бы именно он.
    check("а не самый свежий в папке",
          os.path.basename(find_video(root, "15", "render/15.mp4")) != "77_pererender.mp4",
          True)

    print("\nЗаписанного пути нет — выручает штатное имя задачи")

    check("файл по номеру задачи", os.path.basename(find_video(root, "15")), "15.mp4")
    check("несуществующий output_rel не мешает найти по имени",
          os.path.basename(find_video(root, "15", "render/пропал.mp4")), "15.mp4")

    print("\nНи того, ни другого: один файл — берём, несколько — отказ")

    one = project(["4.mp4"])
    check("единственный mp4 однозначен",
          os.path.basename(find_video(one, "999")), "4.mp4")

    try:
        find_video(root, "999")
        check("несколько кандидатов — отказ", "молча выбрал", "VideoAmbiguous")
    except VideoAmbiguous as exc:
        check("несколько кандидатов — отказ", "VideoAmbiguous", "VideoAmbiguous")
        check("в отказе перечислены оба файла",
              all(n in str(exc) for n in ("15.mp4", "77_pererender.mp4")), True)

    print("\nПустые случаи остаются пустыми, а не падают")

    empty = tempfile.mkdtemp(prefix="findvideo-empty-")
    check("папки render нет вовсе", find_video(empty, "1"), "")
    os.makedirs(os.path.join(empty, "render"))
    check("папка есть, файлов нет", find_video(empty, "1"), "")
    check("без номера задачи и без пути тоже пусто", find_video(empty), "")

    for path in (root, one, empty):
        shutil.rmtree(path, ignore_errors=True)

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
