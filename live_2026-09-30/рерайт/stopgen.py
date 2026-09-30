# -*- coding: utf-8 -*-
"""СРОЧНЫЙ СТОП всех WW2-ген-джоб. Имя файла без ww2-префикса, чтобы pkill не самоматчился."""
import json, sqlite3, sys, os, urllib.request, urllib.error
SRV = "/root/rewrite-server"; BASE = "http://127.0.0.1:8899"; ADMIN = "292d249f2de0"
sys.path.insert(0, SRV)

# 1) немедленно убить claude-субпроцессы (стоп расхода токенов)
os.system("pkill -9 -x claude")
# 2) погасить поллер мониторинга
os.system("pkill -9 -f pollww.sh 2>/dev/null; pkill -9 -f _ww2_poll 2>/dev/null")

# 3) отменить джобы через API (мягко, чтобы воркер отпустил)
from auth import LicenseManager
sid = LicenseManager(f"{SRV}/sessions.db")._create_session(ADMIN, "Админ", "127.0.0.1")
HDR = {"X-Session-Id": sid, "Content-Type": "application/json"}
res = json.load(open(f"{SRV}/_ww2_launch_result.json", encoding="utf-8"))
ids = [r["job_id"] for r in res if r.get("job_id")]
for jid in ids:
    try:
        req = urllib.request.Request(BASE + f"/jobs/{jid}/cancel", data=b"{}", headers=HDR, method="POST")
        code = urllib.request.urlopen(req, timeout=20).getcode()
        print("cancel", jid, "->", code)
    except urllib.error.HTTPError as e:
        print("cancel", jid, "HTTP", e.code, e.read().decode()[:80])
    except Exception as e:
        print("cancel", jid, "ERR", type(e).__name__, str(e)[:60])

# 4) belt-and-suspenders: пометить в БД всё ещё активные
c = sqlite3.connect(f"{SRV}/job_queue.db")
ph = ",".join("?" * len(ids))
active = ("running", "pending", "processing", "in_progress")
pa = ",".join("?" * len(active))
c.execute(f"UPDATE jobs SET status='cancelled' WHERE id IN ({ph}) AND status IN ({pa})", ids + list(active))
c.commit()
print("db_cancelled_rows:", c.total_changes)

# 5) добить любые респавны claude
os.system("sleep 1; pkill -9 -x claude")
os.system("sleep 1")
left = os.popen("pgrep -x -c claude").read().strip()
print("claude_left:", left)
# статусы
from collections import Counter
cnt = Counter()
for r in res:
    jid = r.get("job_id")
    if not jid: continue
    row = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
    st = row[0] if row else "MISSING"; cnt[st] += 1
print("STATUSES:", dict(cnt))
