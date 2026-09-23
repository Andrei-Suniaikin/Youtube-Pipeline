#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Завести администратора в свежей базе студии — или выдать права существующему.

Зачем: пустая база при первом старте заводит `admin` и `demo` БЕЗ пароля, только с
токенами, и печатает их в журнал запуска. Для живого человека это не годится: ему
нужен вход логином и паролем.

Алгоритм хэширования берётся у кабинета (`server/main.hash_password`) — второго в
системе быть не должно. Пароль в аргументах командной строки не передаётся: он
спрашивается вводом, иначе останется в истории оболочки и в списке процессов.

    /srv/studio/.venv/bin/python deploy/make_admin.py --login boss
    /srv/studio/.venv/bin/python deploy/make_admin.py --login boss --promote

`--promote` — сделать администратором уже заведённого человека и заодно сменить пароль.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import secrets
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import accounts as A                                              # noqa: E402


def hash_password(pw: str) -> str:
    """Дословно как в `server/main.py:hash_password` — формат хранения общий."""
    salt = secrets.token_bytes(16)
    it = 200000
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, it)
    return f"pbkdf2${it}${salt.hex()}${dk.hex()}"


def main() -> int:
    ap = argparse.ArgumentParser(description="администратор студии")
    ap.add_argument("--login", required=True, help="логин латиницей")
    ap.add_argument("--db", default=os.environ.get("VS_SERVER_DB", ""),
                    help="путь к базе (по умолчанию из VS_SERVER_DB)")
    ap.add_argument("--tenants-dir", default=os.environ.get("VS_TENANTS_DIR", ""),
                    help="каталог тенантов (по умолчанию из VS_TENANTS_DIR)")
    ap.add_argument("--promote", action="store_true",
                    help="уже заведённому: дать права администратора и сменить пароль")
    args = ap.parse_args()

    if not args.db:
        print("не задан путь к базе: --db или VS_SERVER_DB", file=sys.stderr)
        return 2
    if not os.path.exists(args.db):
        print(f"базы нет: {args.db}. Сначала запустите студию — она создаст схему.",
              file=sys.stderr)
        return 2

    pw = getpass.getpass("пароль: ")
    if pw != getpass.getpass("ещё раз: "):
        print("пароли не совпали", file=sys.stderr)
        return 2

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row
    try:
        row = c.execute("SELECT id, role FROM users WHERE name=?", (args.login,)).fetchone()
        if row and not args.promote:
            print(f"такой человек уже есть (роль {row['role']!r}). "
                  f"Чтобы сменить пароль и роль, добавьте --promote", file=sys.stderr)
            return 1
        if row:
            c.execute("UPDATE users SET role='admin', password_hash=?, disabled=0 WHERE id=?",
                      (hash_password(pw), row["id"]))
            c.commit()
            print(f"готово: «{args.login}» теперь администратор, пароль задан")
            return 0
        user = A.create_user(c, args.login, pw, hash_password, role="admin",
                             tenants_dir=args.tenants_dir or "")
        print(f"готово: администратор «{user['login']}», тенант {user['tenant_id']}")
        print("этот тенант надо указать в REWRITE_ADMIN_USER_ID и COVER_ADMIN_USER_ID")
        return 0
    except A.AccountError as exc:
        print(f"не вышло: {exc}", file=sys.stderr)
        return 1
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
