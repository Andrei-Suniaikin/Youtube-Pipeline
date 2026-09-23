#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Студия настраивает себя сама: человек не заводит внутренних учёток и ключей.

Решение владельца 23.09.2026: «нет понятия rewrite и cover, есть понятие студия, нет
такого что ключи отдельно заводятся». До этого на свежей машине человек должен был
выполнить отдельную команду, и без неё студия отвечала «account is disabled».

Проверяется: доступы выдаются на старте, повторный запуск ничего не портит, пароль
человека не трогается никогда, сбой одной части не мешает другой, а на пустой базе
(администратора ещё нет) это не ошибка, а «сделаем на следующем запуске».

    .venv/bin/python tests/test_self_setup.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import self_setup                                                 # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class FakeManager:
    """Учётные записи модуля: их нет, пока кто-нибудь не заведёт первую."""

    def __init__(self, broken=False):
        self.users = []
        self.broken = broken
        self.passwords = []

    def has_users(self):
        return bool(self.users)

    def setup_admin(self, login, password, ip):
        if self.broken:
            return (False, "первичная настройка уже выполнена", "")
        self.users.append(login)
        self.passwords.append(password)
        return (True, "сессия", login)


class FakeModule:
    """Модуль текста или обложек — ровно та часть, которой пользуется самонастройка."""

    def __init__(self, kind, broken_account=False, broken_key=False):
        self.kind = kind
        self.license_mgr = FakeManager(broken_account)
        self.broken_key = broken_key
        self.made = []
        self.BASE_DIR = "/не/важно"

    def create_api_key(self, *args):
        if self.broken_key:
            raise RuntimeError("провайдер ключей молчит")
        self.made.append(args)
        return {"plain_key": f"{self.kind}-ключ-{len(self.made)}"}


def fresh_db(with_admin=True):
    path = os.path.join(tempfile.mkdtemp(prefix="self-setup-"), "vsgen.db")
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, role TEXT,
        password_hash TEXT, tenant_id TEXT, disabled INTEGER DEFAULT 0);
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
    """)
    # Заводские учётки без пароля есть всегда — под ними никто не работает.
    c.execute("INSERT INTO users(name,role,password_hash,tenant_id) VALUES('admin','admin',NULL,NULL)")
    c.execute("INSERT INTO users(name,role,password_hash,tenant_id) VALUES('demo','user',NULL,NULL)")
    if with_admin:
        c.execute("INSERT INTO users(name,role,password_hash,tenant_id) "
                  "VALUES('GeiAndrei','admin','pbkdf2$200000$aa$bb','3')")
    c.commit()
    c.close()

    def db():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    return path, db


def install(text=None, covers=None):
    sys.modules["server"] = text if text is not None else FakeModule("текст")
    sys.modules["cover_server"] = covers if covers is not None else FakeModule("обложка")
    return sys.modules["server"], sys.modules["cover_server"]


def keys(path):
    c = sqlite3.connect(path)
    try:
        return {k: v for k, v in c.execute(
            "SELECT k, v FROM admin_state WHERE k LIKE 'pub_key_%'")}
    finally:
        c.close()


def main() -> int:
    quiet = lambda *a: None                                        # noqa: E731

    print("Свежая студия настраивается сама")
    path, db = fresh_db()
    text, covers = install()
    report = self_setup.ensure_internal_access(db, quiet)
    check("отчёт: всё вышло", report["ok"], True)
    check("обе части настроены", sorted(report["выдано"]), ["обложки", "тексты"])
    got = keys(path)
    check("ключи легли в базу", sorted(got), ["pub_key_cover", "pub_key_rewrite"])
    check("ключ текста — настоящий", got["pub_key_rewrite"], "текст-ключ-1")
    check("учётка модуля заведена под именем хозяина", text.license_mgr.users, ["GeiAndrei"])
    check("номер учётной записи хозяина, а не выдуманный",
          covers.made[0][0], "3")
    check("пароль модулю выдан случайный и длинный",
          len(text.license_mgr.passwords[0]) >= 20, True)

    print("Повторный запуск ничего не портит")
    before = keys(path)
    report2 = self_setup.ensure_internal_access(db, quiet)
    check("второй раз — нечего делать", report2["выдано"], [])
    check("…и всё на месте", sorted(report2["было"]), ["обложки", "тексты"])
    check("ключи не переписаны", keys(path), before)
    check("вторая учётка не заводится", text.license_mgr.users, ["GeiAndrei"])

    print("Пароль человека не трогается никогда")
    c = sqlite3.connect(path)
    pw = c.execute("SELECT password_hash FROM users WHERE name='GeiAndrei'").fetchone()[0]
    c.close()
    check("пароль хозяина прежний", pw, "pbkdf2$200000$aa$bb")
    src = open(os.path.join(BASE, "server", "self_setup.py"), encoding="utf-8").read()
    check("в коде самонастройки нет записи в users",
          "UPDATE users" in src or "INSERT INTO users" in src, False)

    print("Пустая студия: администратора ещё нет")
    path2, db2 = fresh_db(with_admin=False)
    install()
    report3 = self_setup.ensure_internal_access(db2, quiet)
    check("это не ошибка, а «позже»", report3["ok"], False)
    check("сказано, чего ждём", "хозяин студии ещё не заведён", report3["why"])
    check("ключей не появилось", keys(path2), {})

    print("Сбой одной части не мешает другой")
    path3, db3 = fresh_db()
    broken = FakeModule("текст", broken_key=True)
    ok_mod = FakeModule("обложка")
    install(text=broken, covers=ok_mod)
    report4 = self_setup.ensure_internal_access(db3, quiet)
    check("часть, которая упала, названа", report4["не вышло"], ["тексты"])
    check("вторая всё равно настроена", report4["выдано"], ["обложки"])
    check("и её ключ в базе", sorted(keys(path3)), ["pub_key_cover"])
    check("общий итог — не «всё хорошо»", report4["ok"], False)

    print("Модуль не поднят в процессе")
    path4, db4 = fresh_db()
    sys.modules.pop("server", None)
    sys.modules["cover_server"] = FakeModule("обложка")
    report5 = self_setup.ensure_internal_access(db4, quiet)
    check("отсутствующая часть названа", report5["не вышло"], ["тексты"])
    check("остальное сделано", report5["выдано"], ["обложки"])

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
