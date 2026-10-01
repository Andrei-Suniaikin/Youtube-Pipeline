"""Сколько места освободит штатная уборка кадров при разном сроке."""
import os, subprocess, time

BASE = "/root/manga_rewrite/projects_web/projects"


def size(p):
    try:
        return int(subprocess.run(["du", "-sb", p], capture_output=True, text=True,
                                  timeout=120).stdout.split()[0])
    except Exception:
        return 0


rows = []
now = time.time()
for pid in os.listdir(BASE):
    d = os.path.join(BASE, pid)
    video = os.path.join(d, "out", "video.mp4")
    if not os.path.exists(video):
        continue
    age_days = (now - os.path.getmtime(video)) / 86400
    heavy = sum(size(os.path.join(d, n)) for n in ("frames", "clips")
                if os.path.isdir(os.path.join(d, n)))
    rows.append((age_days, heavy))

print("готовых роликов: %d" % len(rows))
print()
print("%-14s %-12s %s" % ("срок", "роликов", "освободится"))
for days in (3, 7, 14, 30):
    hit = [h for a, h in rows if a >= days]
    print("%-14s %-12d %.0f ГБ" % ("%d суток" % days, len(hit), sum(hit) / 2**30))
