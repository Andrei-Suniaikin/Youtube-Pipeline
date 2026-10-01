"""Связь «сколько каналов на профиле» → «какая доля заливок доходит».

Считаем по фактам за трое суток: берём профили, где было хотя бы 5 заливок,
и сопоставляем с числом активных каналов на профиле.
"""
import sqlite3

DB = "/root/VideoStudio/server/vsgen.db"
c = sqlite3.connect(DB, timeout=25)
c.row_factory = sqlite3.Row

load = {str(r["profile_id"]): (r["каналов"], r["почт"])
        for r in c.execute(
            "SELECT profile_id, COUNT(*) каналов, 0 почт FROM pub_channels "
            "WHERE status='active' AND COALESCE(profile_id,'')<>'' GROUP BY profile_id")}

rows = c.execute(
    "SELECT profile_id, COUNT(*) n, "
    "SUM(CASE WHEN verdict IN ('scheduled','published') THEN 1 ELSE 0 END) ok, "
    "CAST(AVG(finished-claimed)/60 AS INT) mins "
    "FROM pub_upload_jobs WHERE finished > strftime('%s','now')-259200 "
    "AND claimed IS NOT NULL AND finished > claimed "
    "GROUP BY profile_id HAVING n >= 5").fetchall()

buckets = {}
print("%-8s %-10s %-10s %-9s %s" % ("профиль", "каналов", "заливок", "дошло", "минут"))
for r in rows:
    pid = str(r["profile_id"])
    ch = load.get(pid, (0, 0))[0]
    share = round(100 * r["ok"] / r["n"])
    print("%-8s %-10s %-10s %-9s %s" % (pid, ch, r["n"], "%d%%" % share, r["mins"]))
    key = "1-3 канала" if ch <= 3 else ("4-9 каналов" if ch <= 9 else "10 и больше")
    b = buckets.setdefault(key, [0, 0])
    b[0] += r["n"]; b[1] += r["ok"]

print()
print("%-14s %-10s %s" % ("нагрузка", "заливок", "дошло до расписания"))
for key in ("1-3 канала", "4-9 каналов", "10 и больше"):
    if key in buckets:
        n, ok = buckets[key]
        print("%-14s %-10d %d%%" % (key, n, round(100 * ok / n)))
c.close()
