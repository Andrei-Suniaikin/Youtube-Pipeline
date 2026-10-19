#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Уборка диска минской машины: показать, что лежит мёртвым грузом (18.10.2026).

Зачем. На Минске 879 ГБ, свободно 41. Студия занимает из них 36 ГБ — всё остальное
это сторонние проекты и личные файлы, часть которых не открывали с апреля. Диск на
96% мешает всему: рендер пишет кадры, заливщик пишет копию mp4, браузеры пишут кэш.

Как устроено. Список кандидатов задан здесь явно, по именам папок, с пометкой,
почему папку считаем мёртвой. Ничего не определяется «по возрасту вообще»: в соседней
папке идёт работа, и правило «старше N дней» снесло бы её вместе с хламом. Перед
каждым кандидатом считается, сколько в нём свежих файлов — если работа идёт, папка
переходит в «не трогать», что бы ни стояло в списке.

По умолчанию ничего не удаляется: скрипт печатает таблицу и итог. Удаление — только
с `--apply` и только для папок, названных в `--only`. Полного «снести всё» нет
намеренно: это чужие файлы, и решение по каждой папке принимает владелец.

    ~/disk_cleanup.py                       показать
    ~/disk_cleanup.py --only cache,trash --apply     убрать названное
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
FRESH_DAYS = 7          # файл свежее этого — признак, что работа идёт
FRESH_LIMIT = 50        # столько свежих файлов уже означают «не трогать»

# код, путь, что это, можно ли удалять папку целиком
CANDIDATES = (
    ("cache",     f"{HOME}/.cache",
     "кэш программ — заполняется заново сам", True),
    ("trash",     f"{HOME}/.local/share/Trash",
     "корзина", True),
    ("tg",        f"{HOME}/Рабочий стол/telegram",
     "кэш телеграма, в нём ничего нашего", True),
    ("transcr",   f"{HOME}/Рабочий стол/дляТранскрибации",
     "материалы для распознавания, работа закончена", True),
    ("downloads", f"{HOME}/Загрузки/Архив",
     "архив загрузок", True),
    ("okavango",  f"{HOME}/okavango-fast",
     "проект okavango — закрыт", True),
    ("tinkov",    f"{HOME}/tinkov-speaker-worker",
     "воркер tinkov — закрыт", True),
    ("ww2night",  f"{HOME}/ww2_night_20260914",
     "прогон ВВ2 от 14.09 — закончен", True),
    ("mangaup",   f"{HOME}/manga_upload",
     "заливка манхвы от 24.09 — закончена", True),
    ("tmp",       "/tmp",
     "временные файлы", False),
)

# Папки, которые скрипт не тронет, даже если их впишут в --only: там идёт работа.
PROTECTED = (f"{HOME}/manga_render", f"{HOME}/docbuilder_night",
             f"{HOME}/VideoStudio", f"{HOME}/ww2_upload_stress_20260914")


def du_mb(path: str) -> int:
    try:
        out = subprocess.run(["du", "-sm", path], capture_output=True, text=True,
                             timeout=600).stdout.split()
        return int(out[0]) if out else 0
    except (subprocess.SubprocessError, ValueError, IndexError):
        return 0


def fresh_files(path: str, limit: int = FRESH_LIMIT) -> int:
    """Сколько файлов моложе FRESH_DAYS. Считаем до limit — дальше не важно."""
    edge = time.time() - FRESH_DAYS * 86400
    n = 0
    for dirpath, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for fn in files:
            try:
                if os.path.getmtime(os.path.join(dirpath, fn)) >= edge:
                    n += 1
                    if n >= limit:
                        return n
            except OSError:
                continue
    return n


def free_gb(path: str = HOME) -> float:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1024 ** 3


def clear_inside(path: str) -> int:
    """Опустошить папку, саму папку оставить (для /tmp и кэшей)."""
    freed = 0
    for name in os.listdir(path):
        full = os.path.join(path, name)
        try:
            size = du_mb(full)
            if os.path.islink(full) or os.path.isfile(full):
                os.remove(full)
            else:
                shutil.rmtree(full)
            freed += size
        except OSError:
            continue
    return freed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="коды папок через запятую")
    ap.add_argument("--apply", action="store_true", help="удалить названное")
    args = ap.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    print(f"свободно сейчас: {free_gb():.0f} ГБ\n")
    print(f"{'код':<10} {'ГБ':>6}  {'свежих':>7}  что это")
    total, ready = 0, []
    for code, path, what, whole in CANDIDATES:
        if not os.path.isdir(path):
            continue
        size = du_mb(path)
        fresh = fresh_files(path)
        busy = fresh >= FRESH_LIMIT
        mark = "ИДЁТ РАБОТА — не трогать" if busy else what
        print(f"{code:<10} {size/1024:>6.1f}  {fresh:>7}  {mark}")
        if not busy:
            total += size
            ready.append((code, path, size, whole))
    print(f"\nвсего мёртвым грузом: {total/1024:.0f} ГБ "
          f"→ стало бы свободно {free_gb() + total/1024:.0f} ГБ")

    if not args.apply:
        print("\nничего не удалено. Чтобы убрать: --only <коды> --apply")
        return 0
    if not only:
        print("\n--apply без --only ничего не делает: называйте папки по одной")
        return 2

    freed = 0
    for code, path, size, whole in ready:
        if code not in only:
            continue
        if any(os.path.realpath(path).startswith(os.path.realpath(p)) for p in PROTECTED):
            print(f"  {code}: отказ — в этой папке идёт работа")
            continue
        try:
            if whole:
                shutil.rmtree(path)
                got = size
            else:
                got = clear_inside(path)
            freed += got
            print(f"  {code}: убрано {got/1024:.1f} ГБ")
        except OSError as exc:
            print(f"  {code}: ОШИБКА {exc}")
    print(f"\nосвобождено {freed/1024:.1f} ГБ, свободно теперь {free_gb():.0f} ГБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
