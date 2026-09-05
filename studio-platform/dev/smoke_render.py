#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка последнего участка конвейера: сборка ролика и его выдача.

Ключей не нужно, денег не тратится, наружу не ходим. Кадры и звук делает ffmpeg,
дальше всё настоящее: задача встаёт в очередь рендера, нода забирает её через
/api/render/claim, БОЕВОЙ render_worker собирает mp4 и отдаёт обратно на платформу.

Зачем отдельно от полного прогона: рендер делает ДРУГАЯ машина. Планировщик берёт из
очереди всё, кроме `render` (main.py:10768). Если ноды нет, конвейер доходит до «Сборки»
и молча встаёт — и это единственная поломка, которую нельзя увидеть по ключам и логам
кабинета. Здесь она видна за минуту.

    dev/smoke_render.py            три кадра и шесть секунд звука
    dev/smoke_render.py --frames 10 --seconds 20

Дальше во втором окне: dev/render-node.sh — и ждать строку «отдан».
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_env import load_env                                      # noqa: E402

load_env()

DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
TENANTS = os.environ.get("VS_TENANTS_DIR") or os.path.join(BASE, "dev", "data", "tenants")
COLORS = ("0x2a1b3c", "0x3c2a1b", "0x1b3c2a", "0x241a33", "0x33241a",
          "0x1a3324", "0x2e2140", "0x402e21", "0x21402e", "0x1e1a2b")


def ffmpeg(*args) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--frames", type=int, default=3, help="сколько кадров нарисовать")
    p.add_argument("--seconds", type=int, default=6, help="длина звуковой дорожки")
    p.add_argument("--tenant", default=os.environ.get("VS_DEV_TENANT") or "dev")
    p.add_argument("--project", default="", help="имя проекта (по умолчанию smoke-ЧЧММСС)")
    args = p.parse_args()

    if not shutil.which("ffmpeg"):
        raise SystemExit("нет ffmpeg — рисовать кадры нечем: brew install ffmpeg")
    if not os.path.exists(DB):
        raise SystemExit(f"нет базы {DB} — сначала: python3 dev/make_db.py")

    pid = args.project or time.strftime("smoke-%H%M%S")
    proj = os.path.join(TENANTS, args.tenant, "projects", pid)
    # Раскладка ровно та, что уезжает на ноду (main.py render_assets): кадры в googler/,
    # озвучка в _FINAL_AUDIO_*/. Придумывать свою здесь нельзя — проверка перестанет
    # проверять то, что происходит в бою.
    frames_dir = os.path.join(proj, "googler")
    audio_dir = os.path.join(proj, "_FINAL_AUDIO_EDGE")
    os.makedirs(frames_dir, exist_ok=True)
    os.makedirs(audio_dir, exist_ok=True)
    for i in range(1, max(1, args.frames) + 1):
        ffmpeg("-f", "lavfi", "-i", f"color=c={COLORS[(i - 1) % len(COLORS)]}:s=1280x720:d=1",
               "-frames:v", "1", os.path.join(frames_dir, f"{i:03d}.jpg"))
    ffmpeg("-f", "lavfi", "-i", f"sine=frequency=330:duration={max(1, args.seconds)}",
           "-q:a", "5", os.path.join(audio_dir, "voice.mp3"))

    c = sqlite3.connect(DB, timeout=10)
    row = c.execute("SELECT id FROM users WHERE tenant_id=? OR name=? LIMIT 1",
                    (args.tenant, args.tenant)).fetchone()
    if not row:
        raise SystemExit(f"нет пользователя с тенантом «{args.tenant}»")
    uid, now = row[0], time.time()
    c.execute("INSERT OR REPLACE INTO projects(id,tenant_id,name,dir_path,status,created,"
              "updated,settings) VALUES(?,?,?,?,?,?,?,?)",
              (pid, args.tenant, f"Проверка сборки {pid}", "", "idle", now, now, "{}"))
    # Синхрон по озвучке выключен намеренно: он тянет whisper, которого на маке нет,
    # а проверяем мы дорогу задачи, а не качество попадания картинок в слова.
    params = json.dumps({"quality": "720p", "transitions": "none",
                         "sync_audio": "0", "subtitles": "0"})
    # Номер общий у очереди и у журнала заданий: строка queue_tasks и строка gen_jobs
    # с одним и тем же id — это одна задача с двух сторон, так её и ищет кабинет.
    jid = c.execute("SELECT MAX(n)+1 FROM (SELECT COALESCE(MAX(job_id),900000) n "
                    "FROM queue_tasks UNION ALL SELECT COALESCE(MAX(id),900000) "
                    "FROM gen_jobs)").fetchone()[0]
    c.execute("INSERT INTO gen_jobs(id,user_id,type,status,ts,title) VALUES(?,?,?,?,?,?)",
              (jid, uid, "render", "queued", "", f"Проверка сборки {pid}"))
    c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,created,"
              "params,uid,progress) VALUES(?,?,?,?,?,?,?,?,0)",
              (jid, args.tenant, pid, "render", "queued", now, params, uid))
    c.commit()
    c.close()

    print(f"\nпроект   {pid}  (кадров: {args.frames}, звук: {args.seconds} с)")
    print(f"папка    {proj}")
    print(f"задача   рендер #{jid}, стоит в очереди\n")
    print("Теперь во втором окне:  dev/render-node.sh")
    print("Готово, когда нода скажет «✅ отдан». Ролик появится здесь:")
    print(f"  {os.path.join(proj, 'render')}/{jid}.mp4")
    print(f"и в пульте: Проекты → «Проверка сборки {pid}» → Сборка → скачать.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
