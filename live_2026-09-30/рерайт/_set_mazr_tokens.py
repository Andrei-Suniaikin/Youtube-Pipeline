# -*- coding: utf-8 -*-
"""Ставит mazr (4d40de53262a) OAuth-токены Claude = как у админа (292d249f2de0).
Бэкапит текущие токены mazr, пишет напрямую в user_keys (WAL, без кэша). Значения не печатает."""
import sqlite3, json, hashlib, time, sys
DB = "/root/rewrite-server/sessions.db"
ADMIN = "292d249f2de0"; MAZR = "4d40de53262a"

def fp(toks): return [hashlib.sha256(t.encode()).hexdigest()[:8] for t in toks]

c = sqlite3.connect(DB, timeout=10)
c.execute("PRAGMA busy_timeout=10000")

def get_tokens(uid):
    r = c.execute("SELECT oauth_tokens FROM user_keys WHERE user_id=?", (uid,)).fetchone()
    if not r: return None  # нет строки
    try: return json.loads(r[0]) if r[0] else []
    except Exception: return []

admin = get_tokens(ADMIN) or []
before = get_tokens(MAZR)
row_exists = before is not None
before = before or []

print("admin tokens:", len(admin), "| fp:", fp(admin))
print("mazr before :", len(before), "| fp:", fp(before), "| row_exists:", row_exists)

if not admin:
    print("FATAL: у админа нет токенов — отмена"); sys.exit(1)

# бэкап текущих токенов mazr
json.dump({"user_id": MAZR, "oauth_tokens": before, "row_exists": row_exists, "ts": time.time()},
          open("/root/rewrite-server/_mazr_oauth_backup.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("backup -> _mazr_oauth_backup.json")

new = json.dumps(admin, ensure_ascii=False)
now = time.time()
if row_exists:
    c.execute("UPDATE user_keys SET oauth_tokens=?, updated_at=? WHERE user_id=?", (new, now, MAZR))
else:
    c.execute("INSERT INTO user_keys (user_id, oauth_tokens, updated_at) VALUES (?,?,?)", (MAZR, new, now))
c.commit()

after = get_tokens(MAZR) or []
print("mazr after  :", len(after), "| fp:", fp(after))
print("MATCH admin :", fp(after) == fp(admin))
