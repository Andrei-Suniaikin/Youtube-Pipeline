# -*- coding: utf-8 -*-
"""Грузчик: увозит старые готовые ролики в Contabo и оставляет вместо них указатель.

Правило простое и возрастное, а не «залит ли на YouTube»: из 332 ГБ mp4 на диске залито
на YouTube лишь 44 ГБ, остальные 286 ГБ — просто лежат. Зато ролик старше месяца никто
уже не открывает: на 25.09.2026 таких 1605 штук на 211 ГБ.

Порядок на каждый ролик строго такой и другим быть не может:
  1. посчитать размер и контрольную сумму на диске;
  2. загрузить в хранилище;
  3. убедиться, что в хранилище лежит объект ровно того же размера;
  4. записать указатель рядом (атомарно, с fsync);
  5. и только теперь удалить локальный файл.
Любой сбой на шагах 2-4 оставляет ролик на месте — потерять его нельзя.

    python3 archive_worker.py --days 30                 посчитать, что уедет
    python3 archive_worker.py --days 30 --apply --limit 3   увезти три (проверка)
    python3 archive_worker.py --days 30 --apply        увезти всё
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_archive as RA

КОРЕНЬ = os.environ.get("VS_TENANTS", "/data/tenants")
ГБ = 1024 ** 3


def ключ(path: str) -> str:
    """render/17654.mp4 тенанта 3, проекта p_ab12 → 3/p_ab12/render/17654.mp4."""
    rel = os.path.relpath(path, КОРЕНЬ)
    части = rel.split(os.sep)
    if len(части) >= 3 and части[1] == "projects":
        return "/".join([части[0]] + части[2:])
    return rel.replace(os.sep, "/")


def кандидаты(days: float):
    порог = time.time() - days * 86400
    вышло = []
    for f in glob.glob(os.path.join(КОРЕНЬ, "**", "*.mp4"), recursive=True):
        if not os.path.isfile(f) or os.path.islink(f):
            continue
        if os.path.exists(RA.pointer_path(f)):
            continue
        try:
            st = os.stat(f)
        except OSError:
            continue
        if st.st_size <= 0 or st.st_mtime > порог:
            continue
        вышло.append((f, st.st_size, st.st_mtime))
    вышло.sort(key=lambda x: x[2])          # самые старые первыми
    return вышло


def в_хранилище(remote_key: str):
    """Размер объекта в хранилище или None. Это и есть проверка «доехал ли»."""
    r = subprocess.run([RA.RCLONE, "lsjson", f"{RA.REMOTE}/{remote_key}",
                        "--contimeout", "30s", "--timeout", "2m"],
                       capture_output=True, timeout=300)
    if r.returncode != 0:
        return None
    try:
        import json
        d = json.loads(r.stdout or b"[]")
        return int(d[0]["Size"]) if d else None
    except (ValueError, KeyError, IndexError):
        return None


def увезти(path: str, размер: int, bwlimit: str) -> str:
    k = ключ(path)
    if в_хранилище(k) == размер:            # уже лежит (повтор после сбоя) — не гоняем заново
        сумма = RA.sha256_файла(path)
    else:
        сумма = RA.sha256_файла(path)
        cmd = [RA.RCLONE, "copyto", path, f"{RA.REMOTE}/{k}",
               "--contimeout", "30s", "--timeout", "30m", "--retries", "3", "-q"]
        if bwlimit:
            cmd += ["--bwlimit", bwlimit]
        r = subprocess.run(cmd, capture_output=True, timeout=3600)
        if r.returncode != 0:
            return "не загрузился: " + r.stderr.decode("utf-8", "replace")[:160]
    факт = в_хранилище(k)
    if факт != размер:
        return f"в хранилище {факт} байт вместо {размер} — локальный файл не трогаю"
    RA.write_pointer(path, {"remote_key": k, "size": размер, "sha256": сумма,
                            "archived_at": time.time(), "orig_mtime": os.path.getmtime(path)})
    if RA.read_pointer(path) is None:       # перечитали с диска: указатель на месте и читается
        return "указатель не перечитался — локальный файл не трогаю"
    os.unlink(path)
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--bwlimit", default=os.environ.get("VS_ARCHIVE_BWLIMIT", "40M"))
    ap.add_argument("--min-free-gib", type=float, default=0,
                    help="остановиться, когда свободного места станет больше этого")
    a = ap.parse_args()

    спис = кандидаты(a.days)
    всего = sum(s for _f, s, _m in спис)
    свободно = os.statvfs("/").f_bavail * os.statvfs("/").f_frsize
    print(f"роликов старше {a.days:g} дней: {len(спис)} на {всего/ГБ:.1f} ГБ")
    print(f"свободно сейчас: {свободно/ГБ:.1f} ГБ → станет ~{(свободно+всего)/ГБ:.1f} ГБ")
    if not a.apply:
        for f, s, m in спис[:5]:
            print(f"   {s/2**20:8.0f} МБ  {time.strftime('%d.%m.%Y', time.localtime(m))}  {ключ(f)}")
        print("\nсухой прогон. увезти: добавь --apply")
        return 0

    ушло = байт = 0
    сбои = []
    t0 = time.time()
    for f, s, _m in спис:
        if a.limit and ушло >= a.limit:
            break
        if a.min_free_gib:
            своб = os.statvfs("/").f_bavail * os.statvfs("/").f_frsize
            if своб / ГБ >= a.min_free_gib:
                print(f"свободного места уже {своб/ГБ:.1f} ГБ — останавливаюсь")
                break
        беда = увезти(f, s, a.bwlimit)
        if беда:
            сбои.append((f, беда))
            print(f"  !! {ключ(f)}: {беда}")
            if len(сбои) >= 5:
                print("пять сбоев подряд по разным роликам — останавливаюсь, нужен разбор")
                break
            continue
        ушло += 1
        байт += s
        if ушло % 10 == 0 or ушло <= 3:
            скор = байт / max(1e-9, time.time() - t0) / 2 ** 20
            print(f"  увезено {ушло}/{len(спис)} · {байт/ГБ:.1f} ГБ · {скор:.0f} МБ/с")
    своб = os.statvfs("/").f_bavail * os.statvfs("/").f_frsize
    print(f"\nувезено роликов: {ушло}, освобождено {байт/ГБ:.1f} ГБ, сбоев {len(сбои)}")
    print(f"свободно стало: {своб/ГБ:.1f} ГБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
