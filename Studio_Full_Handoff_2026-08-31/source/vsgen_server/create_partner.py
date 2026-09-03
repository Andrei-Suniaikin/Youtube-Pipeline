#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Завести партнёра (A4-min): создаёт tenant + user с паролем + каталог тенанта.
  python3 create_partner.py <login> <password> [daily_quota]
Самодостаточный (не импортирует main.py). DB рядом (vsgen.db)."""
import sys, os, sqlite3, secrets, datetime, hashlib

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vsgen.db")
TENANTS_ROOT = "/data/tenants"


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16); it = 200000
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, it)
    return f"pbkdf2${it}${salt.hex()}${dk.hex()}"


def main():
    if len(sys.argv) < 3:
        print("usage: create_partner.py <login> <password> [daily_quota]"); sys.exit(1)
    login, password = sys.argv[1], sys.argv[2]
    quota = int(sys.argv[3]) if len(sys.argv) > 3 else None
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    if c.execute("SELECT 1 FROM users WHERE name=?", (login,)).fetchone():
        print(f"❌ пользователь '{login}' уже существует"); c.close(); sys.exit(1)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    tok = "usr_" + secrets.token_urlsafe(24)
    ph = hash_password(password)
    cur = c.execute("INSERT INTO users(name,token,role,created,password_hash,daily_quota) VALUES(?,?,?,?,?,?)",
                    (login, tok, "user", now, ph, quota))
    uid = cur.lastrowid
    tid = str(uid)
    c.execute("UPDATE users SET tenant_id=? WHERE id=?", (tid, uid))
    c.execute("INSERT OR REPLACE INTO tenants(id,name,created,is_active,daily_quota) VALUES(?,?,?,1,?)",
              (tid, login, now, quota))
    c.commit(); c.close()
    troot = os.path.join(TENANTS_ROOT, tid)
    try:
        os.makedirs(os.path.join(troot, "projects"), exist_ok=True)
    except Exception as e:
        print(f"⚠️ каталог тенанта не создан ({e}) — создастся при первом проекте")
    print(f"✅ партнёр '{login}' создан")
    print(f"   user_id={uid}  tenant_id={tid}")
    print(f"   каталог: {troot}")
    print(f"   API-токен: {tok}")
    print(f"   вход: https://gen.example.invalid/login  (логин: {login})")


if __name__ == "__main__":
    main()
