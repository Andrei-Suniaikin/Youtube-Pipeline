# -*- coding: utf-8 -*-
"""Статус 12 seed2-джоб. Реальный прогресс из log (conversation turns)."""
import json, sqlite3, glob, os, re
SRV = "/root/rewrite-server"
res = json.load(open(f"{SRV}/_ww2_launch_result_seed2.json", encoding="utf-8"))
conn = sqlite3.connect(f"{SRV}/job_queue.db")

def out_words(jid):
    tot = 0
    for p in glob.glob(f"{SRV}/outputs/292d249f2de0/**/*.txt", recursive=True):
        if f"[{jid[:8]}]" in p and os.path.isfile(p):
            try: tot += len(open(p, encoding="utf-8", errors="ignore").read().split())
            except Exception: pass
    return tot

def last_stage(log):
    lines = [l for l in (log or "").strip().split("\n") if l.strip()]
    for l in reversed(lines):
        if re.search(r"Turn \d+/\d+|recovery|✅|❌|ERROR|Ошибка|no output|TaskGroup|band|done|воронка|чанк", l):
            return l.strip()[:70]
    return (lines[-1].strip()[:70] if lines else "")

counts = {}
print(f"{'CHANNEL':<20} {'STATUS':<10} {'OUTw':<7} {'PROG':<5} LAST STAGE")
for r in res:
    jid = r.get("job_id")
    if not jid:
        print(f"{r['name']:<20} NOT-LAUNCHED"); continue
    row = conn.execute("SELECT status, data, log FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        print(f"{r['name']:<20} MISSING"); continue
    st = row[0]; counts[st] = counts.get(st, 0) + 1
    try: prog = json.loads(row[1]).get("progress_percent", "")
    except Exception: prog = ""
    print(f"{r['name']:<20} {st:<10} {out_words(jid):<7} {str(prog):<5} {last_stage(row[2])}")
print("\nИТОГО:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
