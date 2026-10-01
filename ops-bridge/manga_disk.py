"""Сколько места держат проекты манхвы и что из этого — у уже залитых роликов."""
import json, os, subprocess, glob

BASE = "/root/manga_rewrite/projects_web/projects"
SEEN = "/root/ops_bridge/manga_seen.json"


def size(path):
    try:
        out = subprocess.run(["du", "-sb", path], capture_output=True, text=True, timeout=120).stdout
        return int(out.split()[0])
    except Exception:
        return 0


seen = json.load(open(SEEN)) if os.path.exists(SEEN) else {}
done = {k for k, v in seen.items() if (v or {}).get("status") == "ok"}

total = free_able = 0
rows = []
for pid in os.listdir(BASE):
    d = os.path.join(BASE, pid)
    if not os.path.isdir(d):
        continue
    whole = size(d)
    total += whole
    heavy = {n: size(os.path.join(d, n)) for n in ("frames", "clips", "derived", "sheets")
             if os.path.isdir(os.path.join(d, n))}
    if pid in done:
        can = sum(heavy.values())
        free_able += can
        rows.append((can, pid, heavy))

print("всего проектов манхвы: %.1f ГБ" % (total / 2**30))
print("из них у УЖЕ ЗАЛИТЫХ роликов, в тяжёлых папках: %.1f ГБ (%d проектов)"
      % (free_able / 2**30, len(rows)))
print()
print("что именно (готовый mp4 и обложка НЕ трогаются):")
for can, pid, heavy in sorted(rows, reverse=True)[:6]:
    parts = ", ".join("%s %.1f ГБ" % (n, v / 2**30) for n, v in heavy.items() if v)
    print("  %s  %.1f ГБ  — %s" % (pid[:8], can / 2**30, parts))
