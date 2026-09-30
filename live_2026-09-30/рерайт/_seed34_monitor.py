# -*- coding: utf-8 -*-
"""Статус 24 seed3+4-джоб."""
import json, sqlite3, glob, os, re
SRV = "/root/rewrite-server"
res = json.load(open(f"{SRV}/_ww2_launch_result_seed34.json", encoding="utf-8"))
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
        if re.search(r"Turn \d+/\d+|recovery|✅|❌|ERROR|Ошибка|no output|TaskGroup|band|воронка|чанк", l):
            return l.strip()[:60]
    return (lines[-1].strip()[:60] if lines else "")

counts = {}
print(f"{'CHANNEL#seed':<22} {'STATUS':<10} {'OUTw':<7} {'P':<4} LAST")
for r in res:
    jid = r.get("job_id"); label = f"{r['name']} #{r.get('seed','')}" + (" ⛔" if r.get('flagged') else "")
    if not jid:
        print(f"{label:<22} NOT-LAUNCHED"); continue
    row = conn.execute("SELECT status, data, log FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        print(f"{label:<22} MISSING"); continue
    st = row[0]; counts[st] = counts.get(st, 0) + 1
    try: prog = json.loads(row[1]).get("progress_percent", "")
    except Exception: prog = ""
    print(f"{label:<22} {st:<10} {out_words(jid):<7} {str(prog):<4} {last_stage(row[2])}")
print("\nИТОГО:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
