# -*- coding: utf-8 -*-
"""Статус 12 WW2-джоб. Реальный прогресс берём из log (conversation turns), не из progress_percent."""
import json, sqlite3, glob, os, re
SRV = "/root/rewrite-server"
res = json.load(open(f"{SRV}/_ww2_launch_result.json", encoding="utf-8"))
conn = sqlite3.connect(f"{SRV}/job_queue.db")

def out_words(jid):
    tot = 0
    for p in glob.glob(f"{SRV}/outputs/292d249f2de0/**/*.txt", recursive=True):
        if f"[{jid[:8]}]" in p and os.path.isfile(p):
            try: tot += len(open(p, encoding="utf-8", errors="ignore").read().split())
            except Exception: pass
    return tot

def last_stage(log):
    """Последняя содержательная строка лога: turn/recovery/done/ошибка."""
    lines = [l for l in (log or "").strip().split("\n") if l.strip()]
    for l in reversed(lines):
        if re.search(r"Turn \d+/\d+|recovery|✅|❌|ERROR|Ошибка|no output|TaskGroup|band|done", l):
            return l.strip()[:72]
    return (lines[-1].strip()[:72] if lines else "")

counts = {}
print(f"{'CHANNEL':<20} {'STATUS':<10} {'OUTwords':<9} LAST LOG STAGE")
for r in res:
    jid = r.get("job_id")
    if not jid:
        print(f"{r['name']:<20} NOT-LAUNCHED"); continue
    row = conn.execute("SELECT status, log FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        print(f"{r['name']:<20} MISSING"); continue
    st = row[0]; counts[st] = counts.get(st, 0) + 1
    ow = out_words(jid)
    print(f"{r['name']:<20} {st:<10} {ow:<9} {last_stage(row[1])}")
print("\nИТОГО:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
