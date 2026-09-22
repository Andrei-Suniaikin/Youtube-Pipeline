#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Один список пользователей на три сервиса: карта, план и перенос.

Сегодня один человек — это три несвязанные учётки. Кабинет знает его как
`users.id` + `tenant_id`, Rewrite и Cover — каждый под своим `user_id`, который
родился как `os.urandom(6).hex()` при создании аккаунта. Единственное, что их
склеивает, — совпадение строки логина, и делает это `pub_neighbors.py`, читая чужие
базы напрямую. Это анти-контракт: он держится на том, что никто никогда не заведёт
двух разных людей с одинаковым логином.

Здесь этот `user_id` приводится к общему значению — `tenant_id` кабинета. Выбран он
не случайно: по нему уже раскладываются проекты (`/data/tenants/<tid>/projects/…`),
значит после переноса все три сервиса будут хранить данные человека под одним ключом.

**Внутренний id нигде не виден снаружи** — ни в адресах, ни в закладках, ни в данных
кабинета (каналы ссылаются на пресеты по имени). Поэтому его можно переназначить;
надо лишь не пропустить ни одного места, где он лежит.

Мест ровно столько:

    Rewrite  sessions.db   users.user_id, sessions.user_id, api_keys.user_id,
                           user_keys.user_id
             job_queue.db  jobs.user_id, fair_queue_users.user_id,
                           fair_queue_state.last_user_id,
                           create_intents.user_id
             файлы         presets/<uid>/, outputs/<uid>/, uploads/<uid>/
    Cover    cover_sessions.db    users.user_id, sessions.user_id
             cover_job_queue.db   jobs.user_id, api_keys.user_id
             файлы         presets/<uid>/, outputs/<uid>/, uploads/<uid>/

Плюс пути внутри `jobs.data`: там лежит JSON задачи, и в нём — `output_dir` со старым
`user_id` в середине. Переименовать каталог и не поправить JSON значит оставить готовые
результаты недоступными.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time

# Логин у соседей — первичный ключ таблицы users; у кабинета он лежит в `name`.
LOGIN_COL = {"cabinet": "name", "rewrite": "username", "cover": "username"}


class Service:
    """Один сосед: где его базы, какие колонки держат user_id, какие каталоги."""

    def __init__(self, name, root, dbs, dirs, json_tables):
        self.name = name
        self.root = root
        self.dbs = dbs                    # {файл: [(таблица, колонка), …]}
        self.dirs = dirs                  # ["presets", "outputs", "uploads"]
        self.json_tables = json_tables    # [(файл, таблица, колонка_с_json)]

    def db_path(self, filename: str) -> str:
        return os.path.join(self.root, filename)

    def users(self) -> dict:
        """{логин: user_id} — то, что сервис считает своими людьми."""
        path = self.db_path(next(iter(self.dbs)))
        if not os.path.exists(path):
            return {}
        c = sqlite3.connect(path)
        try:
            return {r[0]: r[1] for r in c.execute(
                f"SELECT {LOGIN_COL[self.name]}, user_id FROM users")}
        finally:
            c.close()


def services(base: str) -> list:
    return [
        Service("rewrite", os.path.join(base, "rewrite"),
                dbs={"sessions.db": [("users", "user_id"), ("sessions", "user_id"),
                                     ("api_keys", "user_id"), ("user_keys", "user_id")],
                     # create_intents приехала с ребейзом рерайта на боевой 18.09.
                     # Это ключи повтора создания задачи: (user_id, scope, key_hash)
                     # → job_id. Если не переписать здесь user_id, перенос сменит
                     # владельца у задач, но не у ключей, `lookup_create` под новым id
                     # ничего не найдёт — и тот же Idempotency-Key создаст ВТОРУЮ
                     # ОПЛАЧЕННУЮ задачу. Молча: снаружи это выглядит как обычный
                     # повтор. Ровно от этого идемпотентность и защищает.
                     "job_queue.db": [("jobs", "user_id"),
                                      ("fair_queue_users", "user_id"),
                                      ("fair_queue_state", "last_user_id"),
                                      ("create_intents", "user_id")]},
                dirs=["presets", "outputs", "uploads"],
                json_tables=[("job_queue.db", "jobs", "data")]),
        Service("cover", os.path.join(base, "cover"),
                dbs={"cover_sessions.db": [("users", "user_id"), ("sessions", "user_id")],
                     "cover_job_queue.db": [("jobs", "user_id"), ("api_keys", "user_id")]},
                dirs=["presets", "outputs", "uploads"],
                json_tables=[("cover_job_queue.db", "jobs", "data")]),
    ]


def cabinet_users(db_path: str) -> dict:
    """{логин: (id, tenant_id)} — кабинет как источник личности."""
    c = sqlite3.connect(db_path)
    try:
        out = {}
        for r in c.execute("SELECT name, id, tenant_id, COALESCE(disabled,0) FROM users"):
            out[r[0]] = {"id": r[1], "tenant": (r[2] or str(r[1])), "disabled": bool(r[3])}
        return out
    finally:
        c.close()


# ────────────────────────────────── план ──────────────────────────────────

OK = "ok"                 # перенести
DONE = "done"             # уже под общим id
NO_ACCOUNT = "no_account"  # есть у соседа, нет в кабинете — переносить некуда
COLLISION = "collision"    # целевой id уже занят другим человеком


def plan(cabinet_db: str, base: str) -> dict:
    """Кто есть где и что с ним будет. Ничего не читает, кроме таблиц users."""
    cab = cabinet_users(cabinet_db)
    svcs = services(base)
    have = {s.name: s.users() for s in svcs}

    # Обратная карта на каждого соседа: какой логин сидит под каким id. Нужна, чтобы
    # поймать столкновение до того, как оно случится.
    owner = {name: {uid: login for login, uid in users.items()}
             for name, users in have.items()}

    rows, problems = [], []
    logins = sorted(set(cab) | {l for u in have.values() for l in u})
    for login in logins:
        target = cab[login]["tenant"] if login in cab else ""
        row = {"login": login, "in_cabinet": login in cab, "target": target,
               "disabled": cab[login]["disabled"] if login in cab else False,
               "services": {}}
        for name, users in have.items():
            if login not in users:
                row["services"][name] = {"uid": "", "state": "—"}
                continue
            uid = users[login]
            if not target:
                state = NO_ACCOUNT
            elif uid == target:
                state = DONE
            elif owner[name].get(target) not in (None, login):
                # Целевой id занят другим логином. Теоретически цепочку переименований
                # можно распутать порядком переноса, но мы этого не делаем: остановиться
                # и позвать человека дешевле, чем ошибиться в порядке и затереть чужое.
                state = COLLISION
            else:
                state = OK
            row["services"][name] = {"uid": uid, "state": state}
            if state == NO_ACCOUNT:
                problems.append(
                    f"«{login}» есть в {name}, но не в кабинете: переносить некуда. "
                    f"Сначала завести учётку (create_partner.py) либо решить, что данные "
                    f"этого логина не нужны.")
            elif state == COLLISION:
                problems.append(
                    f"в {name} id «{target}» уже занят логином "
                    f"«{owner[name][target]}» — перенос «{login}» затёр бы чужое.")
        rows.append(row)
    return {"rows": rows, "problems": problems,
            "movers": [r for r in rows
                       if any(v["state"] == OK for v in r["services"].values())]}


# ────────────────────────────────── подсчёт ──────────────────────────────────

def _table_exists(c, table: str) -> bool:
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                          (table,)).fetchone())


def count(svc: Service, old: str) -> dict:
    """Сколько строк и каталогов тронет перенос одного человека. Ничего не меняет."""
    out = {"rows": {}, "dirs": [], "json": 0}
    for filename, columns in svc.dbs.items():
        path = svc.db_path(filename)
        if not os.path.exists(path):
            continue
        c = sqlite3.connect(path)
        try:
            for table, col in columns:
                if not _table_exists(c, table):
                    continue
                n = c.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?",
                              (old,)).fetchone()[0]
                if n:
                    out["rows"][f"{filename}:{table}.{col}"] = n
        finally:
            c.close()
    for filename, table, col in svc.json_tables:
        path = svc.db_path(filename)
        if not os.path.exists(path):
            continue
        c = sqlite3.connect(path)
        try:
            if _table_exists(c, table):
                out["json"] = c.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE ?",
                    (f"%/{old}/%",)).fetchone()[0]
        finally:
            c.close()
    for d in svc.dirs:
        src = os.path.join(svc.root, d, old)
        if os.path.isdir(src):
            out["dirs"].append(src)
    return out


# ────────────────────────────────── перенос ──────────────────────────────────

def backup(paths: list, into: str) -> list:
    """Копия каждой базы до изменений. Без неё переносить нельзя."""
    os.makedirs(into, exist_ok=True)
    made = []
    for p in paths:
        if not os.path.exists(p):
            continue
        dst = os.path.join(into, os.path.basename(os.path.dirname(p)) + "." +
                           os.path.basename(p))
        shutil.copy2(p, dst)
        made.append(dst)
    return made


def move_one(svc: Service, old: str, new: str, log=print) -> dict:
    """Перенести одного человека в одном сервисе. По транзакции на базу.

    Порядок важен: сначала базы, потом каталоги. Если упадёт на каталогах, строки уже
    указывают на новый id, а каталог остался под старым — это чинится переименованием
    и видно в отчёте. Обратный порядок дал бы данные, на которые никто не ссылается.
    """
    done = {"rows": 0, "json": 0, "dirs": []}
    for filename, columns in svc.dbs.items():
        path = svc.db_path(filename)
        if not os.path.exists(path):
            continue
        c = sqlite3.connect(path, timeout=15)
        try:
            c.execute("BEGIN IMMEDIATE")
            for table, col in columns:
                if not _table_exists(c, table):
                    continue
                cur = c.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new, old))
                done["rows"] += cur.rowcount
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    # Пути внутри JSON задачи. Меняем только то, что выглядит как участок пути:
    # «/<old>/». Голое вхождение id в тексте не трогаем — там может быть что угодно.
    for filename, table, col in svc.json_tables:
        path = svc.db_path(filename)
        if not os.path.exists(path):
            continue
        c = sqlite3.connect(path, timeout=15)
        try:
            if not _table_exists(c, table):
                continue
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(f"UPDATE {table} SET {col}=REPLACE({col}, ?, ?) "
                            f"WHERE {col} LIKE ?",
                            (f"/{old}/", f"/{new}/", f"%/{old}/%"))
            done["json"] += cur.rowcount
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    for d in svc.dirs:
        src = os.path.join(svc.root, d, old)
        dst = os.path.join(svc.root, d, new)
        if not os.path.isdir(src):
            continue
        if os.path.exists(dst):
            raise FileExistsError(f"{dst} уже существует — перенос остановлен")
        os.rename(src, dst)
        done["dirs"].append((src, dst))
        log(f"      {os.path.relpath(src, svc.root)} → {os.path.relpath(dst, svc.root)}")
    return done


def journal_path(base: str) -> str:
    return os.path.join(base, "dev", "identity_migration.json")


def write_journal(base: str, record: dict) -> str:
    path = journal_path(base)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    history = []
    if os.path.exists(path):
        try:
            history = json.load(open(path, encoding="utf-8"))
        except ValueError:
            history = []
    history.append(record)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, indent=2)
    return path


def rollback(base: str, log=print) -> dict:
    """Откатить последний перенос: каталоги обратно, строки обратно.

    Работает по журналу, а не по догадке. Если журнала нет — откатывать нечего, и
    молча «восстанавливать» ничего нельзя.
    """
    path = journal_path(base)
    if not os.path.exists(path):
        raise SystemExit(f"нет журнала {path} — откатывать нечего")
    history = json.load(open(path, encoding="utf-8"))
    if not history:
        raise SystemExit("журнал пуст")
    last = history[-1]
    svcs = {s.name: s for s in services(base)}
    back = {"rows": 0, "dirs": 0}
    for item in reversed(last["moved"]):
        svc = svcs[item["service"]]
        old, new = item["old"], item["new"]
        for d in svc.dirs:                       # каталоги первыми: они переносились последними
            src = os.path.join(svc.root, d, new)
            dst = os.path.join(svc.root, d, old)
            if os.path.isdir(src) and not os.path.exists(dst):
                os.rename(src, dst)
                back["dirs"] += 1
        res = move_one(svc, new, old, log=log)
        back["rows"] += res["rows"]
        log(f"  {item['service']}: {new} → {old}")
    history.pop()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, indent=2)
    return back


# Литерал, который перестанет совпадать после переноса. Это не мелочь: админ у соседей
# опознаётся сравнением с ним, и после смены id он потеряет права в Rewrite и Cover.
ADMIN_LITERAL_SITES = (
    ("rewrite/auth.py", "ADMIN_USER_ID"),
    ("rewrite/rewrite_core.py", "ADMIN_USER_ID"),
    ("cover/auth.py", "COVER_ADMIN_USER_ID (читается из окружения)"),
)


def admin_ids(base: str) -> dict:
    """Какие значения зашиты сейчас — чтобы отчёт называл их, а не намекал."""
    out = {}
    for rel, _label in ADMIN_LITERAL_SITES:
        path = os.path.join(base, rel)
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8", errors="replace"):
            m = re.search(r'ADMIN_USER_ID\s*=\s*(?:os\.environ\.get\([^,]+,\s*)?"([^"]+)"',
                          line)
            if m:
                out[rel] = m.group(1)
                break
    return out
