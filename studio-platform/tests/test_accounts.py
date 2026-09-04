#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Учётные записи: завести, заблокировать, сменить роль и пароль.

Главное, что проверяется, — запреты. Систему нельзя оставить без администратора,
себя нельзя заблокировать, а блокировка обязана гасить живые сессии: иначе человек
продолжает работать в открытой вкладке.

    .venv/bin/python tests/test_accounts.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import accounts as A                                              # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fails(label, fn, expect: str):
    try:
        fn()
        check(label, "прошло без отказа", f"отказ: {expect}")
    except A.AccountError as exc:
        check(label, expect in str(exc), True)


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
        token TEXT, role TEXT, created TEXT, password_hash TEXT, daily_quota INTEGER,
        tenant_id TEXT, disabled INTEGER DEFAULT 0);
      CREATE TABLE tenants(id TEXT PRIMARY KEY, name TEXT, created TEXT,
        is_active INTEGER, daily_quota INTEGER);
      CREATE TABLE sessions(id TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
    """)
    c.commit()
    return c


def hasher(pw):
    return "pbkdf2$1$aa$" + pw[::-1]      # тот же вид, что у кабинета; сам алгоритм не наш


def main() -> int:
    print("Заведение")
    c = fresh()
    boss = A.create_user(c, "boss", "very-secret", hasher, role="admin")
    check("администратор заведён", boss["login"], "boss")
    check("тенант равен идентификатору", boss["tenant_id"], str(boss["id"]))
    check("запись о тенанте появилась",
          c.execute("SELECT COUNT(*) FROM tenants WHERE id=?",
                    (boss["tenant_id"],)).fetchone()[0], 1)

    fails("короткий пароль", lambda: A.create_user(c, "x1", "12345", hasher),
          "короче 6")
    fails("логин с пробелом", lambda: A.create_user(c, "два слова", "нормальный", hasher),
          "латиница")
    fails("занятый логин", lambda: A.create_user(c, "boss", "нормальный", hasher),
          "уже занят")
    fails("выдуманная роль",
          lambda: A.create_user(c, "x2", "нормальный", hasher, role="король"),
          "роль бывает")
    fails("квота словами",
          lambda: A.create_user(c, "x3", "нормальный", hasher, quota="много"),
          "целое число")

    print("\nПароль наружу не отдаётся")
    rows = A.list_users(c)
    leaked = [k for k in rows[0] if "pass" in k or "hash" in k or "token" in k]
    check("в списке нет ни хэша, ни токена", leaked, ["has_password"])
    check("но видно, задан ли пароль", rows[0]["has_password"], True)

    print("\nСистему нельзя оставить без администратора")
    fails("снять роль с последнего", lambda: A.set_role(c, boss["id"], "user"),
          "последний администратор")
    fails("заблокировать последнего",
          lambda: A.set_disabled(c, boss["id"], True), "последний администратор")

    print("\n  а когда админов двое — можно")
    second = A.create_user(c, "second", "very-secret", hasher, role="admin")
    check("роль снята", A.set_role(c, second["id"], "user")["role"], "user")
    check("админ снова один", A.admin_count(c), 1)

    print("\nСебя заблокировать нельзя")
    partner = A.create_user(c, "vasya", "very-secret", hasher)
    fails("сам себя",
          lambda: A.set_disabled(c, boss["id"], True, actor_id=boss["id"]),
          "самого себя")

    print("\nБлокировка гасит живые сессии")
    c.execute("INSERT INTO sessions VALUES('s1',?,0)", (partner["id"],))
    c.execute("INSERT INTO sessions VALUES('s2',?,0)", (boss["id"],))
    c.commit()
    A.set_disabled(c, partner["id"], True, actor_id=boss["id"])
    check("его сессии стёрты",
          c.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?",
                    (partner["id"],)).fetchone()[0], 0)
    check("чужие не тронуты",
          c.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?",
                    (boss["id"],)).fetchone()[0], 1)
    check("состояние записано",
          [u["disabled"] for u in A.list_users(c) if u["id"] == partner["id"]], [True])
    check("вернуть можно", A.set_disabled(c, partner["id"], False)["disabled"], False)

    print("\nСмена пароля закрывает открытые вкладки")
    c.execute("INSERT INTO sessions VALUES('s3',?,0)", (partner["id"],))
    c.commit()
    A.set_password(c, partner["id"], "new-secret", hasher)
    check("сессии стёрты",
          c.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?",
                    (partner["id"],)).fetchone()[0], 0)
    check("хэш обновлён",
          c.execute("SELECT password_hash FROM users WHERE id=?",
                    (partner["id"],)).fetchone()[0], hasher("new-secret"))
    fails("короткий пароль не примут",
          lambda: A.set_password(c, partner["id"], "12345", hasher), "короче 6")

    print("\nКвота меняется и у человека, и у тенанта")
    A.set_quota(c, partner["id"], "42")
    check("у человека",
          c.execute("SELECT daily_quota FROM users WHERE id=?",
                    (partner["id"],)).fetchone()[0], 42)
    check("у тенанта",
          c.execute("SELECT daily_quota FROM tenants WHERE id=?",
                    (str(partner["id"]),)).fetchone()[0], 42)
    check("пусто значит без лимита",
          A.set_quota(c, partner["id"], "")["quota"], None)

    print("\nНесуществующая учётка")
    fails("блокировка", lambda: A.set_disabled(c, 9999, True), "нет такой")
    fails("пароль", lambda: A.set_password(c, 9999, "very-secret", hasher), "нет такой")

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
