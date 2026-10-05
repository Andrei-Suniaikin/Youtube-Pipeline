#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Убрать звезду Gemini со старых рендеров ВВ2 — на видеокарте минской машины.

Решение владельца (тест решений 04.10, А6 = «убирать звезду фильтром и лить»; 05.10
«го на минском»). Рендеры ВВ2 до 19.09 несут звезду Gemini в правом нижнем углу (x≈1750–1820,
y≈905–975 на 1920×1080) примерно в четверти кадров; кадры проектов стёрты, перерендерить
нельзя. Проба 05.10: ролик на 2 часа — 15,5 минуты на RTX 4070 SUPER (delogo + h264_nvenc),
звезды нет, на её месте едва заметное сглаживание.

Один ролик за раз, ffmpeg с nice: минская машина — ещё и нода рендера новой студии.
Для каждого ролика:
  1. файл → Минск (туннель ssh -p 2222), там delogo + h264_nvenc → обратно во временный;
  2. длительность сверяется с оригиналом (±1 с) — иначе файл не трогаем;
  3. оригинал → render/<job>.mp4.star (сохраняется), очищенный встаёт на render/<job>.mp4
     (движок старого кабинета берёт ровно его: pub_publish.find_video), рядом отметка
     render/<job>.star_removed.json — по ней мост ВВ2 пропускает старый рендер.
Оригинал не теряется никогда: при уже лежащем .star ничего не перезаписывается, оборванная
подмена восстанавливается при следующем запуске.

Берутся только проекты без выпуска (их и ждёт мост): залитое со звездой не трогаем.

    /root/ops/ww2_sheet/venv/bin/python star_clean.py            # показать, что сделает
    /root/ops/ww2_sheet/venv/bin/python star_clean.py --apply --limit 5
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_link   # noqa: E402

DB = os.environ.get("WW2_VSGEN_DB", "/root/VideoStudio/server/vsgen.db")
TENANTS = os.environ.get("PUB_BRIDGE_TENANTS_DIR", "/data/tenants")
CUTOFF = 1789827886                    # 19.09 14:24 UTC — отсечка моста по рендерам со звездой
MINSK = os.environ.get("STAR_CLEAN_HOST", "vova@127.0.0.1")
MINSK_PORT = os.environ.get("STAR_CLEAN_PORT", "2222")
REMOTE_DIR = "star_jobs"
BOX = os.environ.get("STAR_CLEAN_BOX", "x=1745:y=900:w=82:h=82")
LOCK = "/tmp/star_clean.lock"


def log(msg: str) -> None:
    print(time.strftime("[%d.%m %H:%M:%S] ") + msg, flush=True)


def candidates(c) -> list:
    """(pid, tenant, job, путь) старых рендеров без выпуска, у которых файл на месте и не очищен."""
    rows = c.execute(
        "SELECT q.project_id, p.tenant_id, q.job_id, q.output_rel, q.started FROM queue_tasks q "
        "JOIN projects p ON p.id=q.project_id WHERE q.kind='render' AND q.status='done' "
        "AND q.job_id=(SELECT MAX(q2.job_id) FROM queue_tasks q2 WHERE q2.project_id=q.project_id "
        "AND q2.kind='render' AND q2.status='done') ORDER BY q.started DESC").fetchall()
    with_release = {r[0] for r in c.execute("SELECT DISTINCT project_id FROM pub_releases "
                                            "WHERE project_id IS NOT NULL AND project_id!=''")}
    out = []
    for pid, tid, job, rel, started in rows:
        if not started or float(started) >= CUTOFF or not rel or pid in with_release:
            continue
        path = os.path.join(TENANTS, str(tid), "projects", pid, rel)
        if not os.path.isfile(path) or bridge_link.star_cleaned(path):
            continue
        out.append((pid, str(tid), job, path))
    return out


def recover(path: str) -> str:
    """Оборванная подмена: оригинал ушёл в .star, а на месте пусто — вернуть. Строка — что сделано."""
    star, tmp = path + ".star", path + ".clean.tmp"
    did = ""
    if os.path.exists(star) and not os.path.exists(path):
        os.replace(star, path)
        did = "оригинал возвращён на место"
    if os.path.exists(tmp):
        os.remove(tmp)
        did = (did + "; " if did else "") + "временный файл убран"
    return did


def write_mark(path: str, original_size: int, source: str) -> dict:
    st = os.stat(path)
    mark = {"schema_version": 1, "box": BOX, "at": int(time.time()), "source": source,
            "original": os.path.basename(path) + ".star", "original_size": int(original_size),
            "clean_size": st.st_size, "clean_mtime": st.st_mtime}
    tmp = bridge_link.star_mark_path(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mark, f, ensure_ascii=False, indent=1)
    os.replace(tmp, bridge_link.star_mark_path(path))
    return mark


def swap_in_clean(path: str, clean_tmp: str) -> dict:
    """Очищенный файл — на место оригинала, оригинал — в .star, отметка — рядом."""
    star = path + ".star"
    if os.path.exists(star):
        # Подмена уже была (отметка потерялась): на месте лежит очищенный — только отметка.
        if os.path.exists(clean_tmp):
            os.remove(clean_tmp)
        return write_mark(path, os.path.getsize(star), "повтор: оригинал уже в .star")
    original_size = os.path.getsize(path)
    os.replace(path, star)
    try:
        os.replace(clean_tmp, path)
    except BaseException:
        os.replace(star, path)
        raise
    return write_mark(path, original_size, "delogo + h264_nvenc на Минске")


def duration(path: str, runner=subprocess.run) -> float:
    out = runner(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                  "csv=p=0", path], capture_output=True, text=True, timeout=120).stdout.strip()
    return float(out or 0)


def clean_one(pid, job, path, runner=subprocess.run) -> str:
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-p", MINSK_PORT, MINSK]
    rin, rout = f"{REMOTE_DIR}/{job}.in.mp4", f"{REMOTE_DIR}/{job}.out.mp4"
    tmp = path + ".clean.tmp"
    t0 = time.time()
    runner(ssh + [f"mkdir -p {REMOTE_DIR}"], check=True, timeout=60)
    runner(["scp", "-q", "-o", "BatchMode=yes", "-P", MINSK_PORT, path, f"{MINSK}:{rin}"],
           check=True, timeout=3600)
    runner(ssh + [f"nice -n 15 ffmpeg -v error -y -i {rin} -vf delogo={BOX} -c:v h264_nvenc "
                  f"-preset p5 -rc vbr -cq 24 -b:v 0 -c:a copy {rout}"], check=True, timeout=4 * 3600)
    runner(["scp", "-q", "-o", "BatchMode=yes", "-P", MINSK_PORT, f"{MINSK}:{rout}", tmp],
           check=True, timeout=3600)
    runner(ssh + [f"rm -f {rin} {rout}"], timeout=60)
    a, b = duration(path, runner), duration(tmp, runner)
    if not b or abs(a - b) > 1.0:
        os.remove(tmp)
        raise RuntimeError(f"длительность не совпала: оригинал {a:.1f} с, очищенный {b:.1f} с")
    mark = swap_in_clean(path, tmp)
    return f"{int(time.time() - t0)} с, {mark['original_size'] // 1048576}→{mark['clean_size'] // 1048576} МБ"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("уже идёт другой прогон — выхожу")
        return 0
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    todo = candidates(c)
    c.close()
    log(f"роликов со звездой к очистке: {len(todo)}" + ("" if a.apply else " (только показ)"))
    if a.limit:
        todo = todo[:a.limit]
    done = failed = 0
    for pid, _tid, job, path in todo:
        fixed = recover(path)
        if fixed:
            log(f"{pid}: {fixed}")
        if not a.apply:
            log(f"  очищу {pid} рендер {job} ({os.path.getsize(path) // 1048576} МБ)")
            continue
        try:
            log(f"{pid}: рендер {job} — готово ({clean_one(pid, job, path)})")
            done += 1
        except Exception as exc:                                   # noqa: BLE001
            failed += 1
            log(f"{pid}: рендер {job} — НЕ вышло: {str(exc)[:200]}")
            recover(path)
    log(f"итог: очищено {done}, не вышло {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
