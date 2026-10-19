#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Уборка копий mp4, оставшихся от заданий заливки на минской машине (19.10.2026).

Зачем. Пуллер скачивает готовый ролик с VPS в свою папку `~/upload_jobs_<место>/<выпуск>/`
и заливает его оттуда в YouTube. После подтверждённой заливки копия не нужна — ролик у
Google, а исходник остался на VPS. Но пуллер её не удаляет: та же папка нужна для
возобновления прерванной передачи, и решать «уже можно» по одной машине нельзя.

Поэтому решение приносится снаружи: список выпусков, у которых задание в базе студии
стоит `done`, с номером ролика и давно законченное. Такую копию удалять безопасно —
возобновлять нечего.

Кроме списка проверяется сама папка: если внутри что-то менялось в последние полчаса,
она пропускается, как бы ни выглядел список. Задание могло начаться заново.

19.10 это было не уборкой «на будущее»: пуллеры остановили заливку сами — «на диске
24 ГБ, меньше порога 25, задания не беру», и конвейер стоял два с половиной часа.

Список готовится на VPS:

    sqlite3 -readonly /srv/studio/data/vsgen.db "SELECT release_id FROM pub_upload_jobs
      WHERE status='done' AND video_id IS NOT NULL AND video_id<>''
        AND finished < strftime('%s','now')-21600;" > /tmp/done_releases.txt

    python3 ~/upload_jobs_cleanup.py /tmp/done_releases.txt            показать
    python3 ~/upload_jobs_cleanup.py /tmp/done_releases.txt --apply    убрать

Отдельно убираются осиротевшие папки: те, чьего задания в студии нет вообще — выпуск
отменили, пересоздали или задание вычистили. Возобновлять там нечего по определению,
но судить об этом можно только по полному списку заданий, иначе первая же недогруженная
выгрузка снесёт работу. Поэтому список обязателен и проверяется на правдоподобие:

    sqlite3 -readonly … "SELECT release_id FROM pub_upload_jobs;" > /tmp/all_jobs.txt
    python3 ~/upload_jobs_cleanup.py /tmp/done_releases.txt --known /tmp/all_jobs.txt
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time

HOME = os.path.expanduser("~")
JOB_DIRS = os.path.join(HOME, "upload_jobs_")
REL_RE = re.compile(r"^r_[0-9a-f]{10}$")
BUSY_SEC = 1800          # что-то менялось меньше получаса назад — не трогать


def dir_mb(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                continue
    return total // 1024 // 1024


def newest(path: str) -> float:
    last = 0.0
    for dirpath, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for fn in files + [""]:
            try:
                last = max(last, os.path.getmtime(os.path.join(dirpath, fn) if fn
                                                  else dirpath))
            except OSError:
                continue
    return last


def free_gb() -> float:
    st = os.statvfs(HOME)
    return st.f_bavail * st.f_frsize / 1024 ** 3


def places() -> list:
    out = []
    for name in sorted(os.listdir(HOME)):
        full = os.path.join(HOME, name)
        if name.startswith("upload_jobs_") and os.path.isdir(full):
            out.append(full)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("list_file", help="файл со номерами выпусков, по одному в строке")
    ap.add_argument("--known", metavar="ФАЙЛ",
                    help="все задания студии: папки, которых в списке нет, осиротели")
    ap.add_argument("--apply", action="store_true", help="удалить")
    args = ap.parse_args()

    def read_ids(path: str) -> set:
        out = set()
        with open(path, encoding="utf-8") as f:
            for line in f:
                for word in line.split():
                    if REL_RE.match(word):
                        out.add(word)
        return out

    done = read_ids(args.list_file)
    if not done:
        raise SystemExit(f"{args.list_file}: ни одного номера выпуска вида r_… — остановился")
    known = set()
    if args.known:
        known = read_ids(args.known)
        # Неполный список превращает уборку осиротевших в уборку работающих.
        if len(known) < len(done):
            raise SystemExit(f"{args.known}: заданий {len(known)}, а подтверждённых "
                             f"заливок {len(done)} — список явно неполный, остановился")
    print(f"залито и подтверждено по списку: {len(done)} выпусков")
    if known:
        print(f"всего заданий в студии: {len(known)}")
    print(f"свободно сейчас: {free_gb():.0f} ГБ\n")

    now = time.time()
    ready, orphans, busy, keep, alien = [], [], [], 0, 0
    for place in places():
        for name in sorted(os.listdir(place)):
            path = os.path.join(place, name)
            if not os.path.isdir(path):
                continue
            # В тех же папках работает заливка манхвы — её задания зовутся m_… и живут
            # в своей базе, которой у нас нет. Для нашего списка они всегда «осиротевшие»,
            # хотя в логах пуллера видно, как они возобновляются. Чужое не трогаем.
            if not REL_RE.match(name):
                alien += 1
                continue
            uploaded = name in done
            orphan = bool(known) and name not in known
            if not (uploaded or orphan):
                keep += 1
                continue
            if now - newest(path) < BUSY_SEC:
                busy.append(path)
                continue
            (ready if uploaded else orphans).append((path, dir_mb(path)))

    total = sum(mb for _p, mb in ready + orphans)
    print(f"{'папка':<46}{'МБ':>8}  что это")
    for path, mb in sorted(ready, key=lambda x: -x[1])[:10]:
        print(f"{path.replace(HOME + '/', ''):<46}{mb:>8}  залито")
    if len(ready) > 10:
        print(f"… и ещё {len(ready) - 10} залитых")
    for path, mb in sorted(orphans, key=lambda x: -x[1])[:10]:
        print(f"{path.replace(HOME + '/', ''):<46}{mb:>8}  задания в студии нет")
    if len(orphans) > 10:
        print(f"… и ещё {len(orphans) - 10} без задания")
    print(f"\nк уборке {len(ready) + len(orphans)} папок, {total / 1024:.1f} ГБ "
          f"(залито {len(ready)}, осиротело {len(orphans)})")
    print(f"оставляю: {keep} (заливка не подтверждена — нужны для возобновления)"
          f"{f', {len(busy)} в работе прямо сейчас' if busy else ''}"
          f"{f', {alien} от заливки манхвы — чужой поток' if alien else ''}")
    ready = ready + orphans

    if not args.apply:
        print("\nничего не удалено. Для уборки — --apply")
        return 0

    freed = 0
    for path, mb in ready:
        try:
            shutil.rmtree(path)
            freed += mb
        except OSError as exc:
            print(f"  ОШИБКА {path}: {exc}")
    print(f"\nосвобождено {freed / 1024:.1f} ГБ, свободно теперь {free_gb():.0f} ГБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
