#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Удаление канала и учётной записи (решение владельца 24.09.2026).

Удаление необратимо, поэтому проверяется в первую очередь то, чего случиться НЕ должно.

Главное про канал: внешних ключей в этой базе нет. Удалить канал, за которым есть
выпуски и заливки, — значит оставить их висеть на номере удалённого канала, а номера
выдаются заново. Однажды такая запись нашлась бы у другого канала: это ровно «ролик на
чужом канале», худшая ловушка системы. Поэтому канал с историей не удаляется, а
убирается в архив.

Про учётную запись — то же самое: за человеком может числиться работа, и удалять его
нельзя; для этого есть блокировка.

    .venv/bin/python tests/test_delete_channel_account.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="delete-guards-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

from pub_schema import ensure_pub_schema                           # noqa: E402

_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
    token TEXT, role TEXT, created TEXT, password_hash TEXT, daily_quota INTEGER,
    tenant_id TEXT, disabled INTEGER DEFAULT 0, keys TEXT);
  CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
  CREATE TABLE IF NOT EXISTS tenants(id TEXT PRIMARY KEY, name TEXT, created TEXT,
    is_active INTEGER, daily_quota INTEGER);
  CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT);
""")
_c.commit()
_c.close()

import accounts                                                    # noqa: E402
import app as W                                                    # noqa: E402
from fastapi import HTTPException                                  # noqa: E402

FAILS = []
TENANT = "t1"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def refused(label, fn, code, part=""):
    try:
        fn()
        check(label, "прошло без отказа", f"отказ {code}")
    except HTTPException as exc:
        check(label, (exc.status_code, part in str(exc.detail)), (code, True))


def sql(q, args=()):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()


def run(q, args=()):
    c = sqlite3.connect(DB)
    try:
        c.execute(q, args)
        c.commit()
    finally:
        c.close()


def channel(name):
    run("INSERT INTO pub_channels(tenant_id,name,uc,lang,status,tz,schedule,"
        "horizon_days,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (TENANT, name, "UC" + name.ljust(22, "x")[:22], "pl", "active",
         "Europe/Warsaw", "{}", 14, time.time(), time.time()))
    return sql("SELECT id FROM pub_channels WHERE name=?", (name,))[0]["id"]


class Req:
    def __init__(self, role="admin", uid=1, name="boss"):
        class U(dict):
            def keys(self):
                return super().keys()
        self.state = type("St", (), {"user": U(id=uid, name=name, role=role)})()


def main() -> int:
    print("Канал без истории удаляется вместе со ссылками плана")
    cid = channel("Чистый")
    run("INSERT INTO pub_plan_items(tenant_id,channel_id,version,donor_url,position,"
        "slot_mode,overrides,status,frozen,created,updated) "
        "VALUES(?,?,1,?,1,'auto','{}','queued',0,?,?)",
        (TENANT, cid, "https://youtu.be/aaa", time.time(), time.time()))
    out = W.ui_channel_delete(cid, Req())
    check("удалён", (out["ok"], out["deleted"]), (True, "Чистый"))
    check("сказано, сколько ссылок ушло с ним", out["plan_items"], 1)
    check("строки канала нет", sql("SELECT 1 FROM pub_channels WHERE id=?", (cid,)), [])
    check("ссылок плана тоже нет",
          sql("SELECT 1 FROM pub_plan_items WHERE channel_id=?", (cid,)), [])

    print("Канал с историей НЕ удаляется")
    cid2 = channel("С историей")
    run("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
        "channel_id,uc,slot_at,status,phase,attempts,created,updated) "
        "VALUES(?,?,1,1,?,?,?,'active','created',0,?,?)",
        ("rel-1", TENANT, cid2, "UCy", time.time(), time.time(), time.time()))
    refused("выпуски держат канал", lambda: W.ui_channel_delete(cid2, Req()),
            400, "выпусков: 1")
    refused("и сказано, что делать вместо", lambda: W.ui_channel_delete(cid2, Req()),
            400, "в архив")
    check("канал на месте", len(sql("SELECT 1 FROM pub_channels WHERE id=?", (cid2,))), 1)
    check("выпуск на месте",
          len(sql("SELECT 1 FROM pub_releases WHERE channel_id=?", (cid2,))), 1)

    cid3 = channel("С заливкой")
    run("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,channel_url,"
        "title,visibility,video_path,slot_at,status,created,updated) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("rel-9", TENANT, cid3, "UCx", "https://studio.youtube.com/channel/UCx",
         "ролик", "public", "/tmp/v.mp4", 0, "done", 1, 1))
    refused("заливки держат канал", lambda: W.ui_channel_delete(cid3, Req()),
            400, "заливок: 1")

    print("Чужого и несуществующего не трогаем")
    refused("канала нет", lambda: W.ui_channel_delete(9999, Req()), 404, "нет")
    refused("удаляет только администратор",
            lambda: W.ui_channel_delete(cid2, Req(role="user")), 403, "")

    print("Учётная запись: кого удалять нельзя")
    hasher = lambda pw: "pbkdf2$1$aa$" + pw[::-1]                  # noqa: E731
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    boss = accounts.create_user(c, "boss", "оченьсекретно", hasher, role="admin",
                                tenants_dir=os.path.join(TMP, "tenants"))
    mate = accounts.create_user(c, "mate", "оченьсекретно", hasher, role="user",
                                tenants_dir=os.path.join(TMP, "tenants"))
    c.close()

    def as_boss(role="admin"):
        return Req(role=role, uid=boss["id"], name="boss")

    refused("себя удалить нельзя",
            lambda: W.ui_account_delete(boss["id"], as_boss()), 400, "самого себя")
    refused("не администратору нельзя",
            lambda: W.ui_account_delete(mate["id"], Req(role="user")), 403, "")

    print("За человеком числится работа — только блокировка")
    run("INSERT INTO pub_channels(tenant_id,name,uc,lang,status,tz,schedule,"
        "horizon_days,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (str(mate["tenant_id"]), "Канал напарника", "UCmate", "pl", "active",
         "Europe/Warsaw", "{}", 14, time.time(), time.time()))
    refused("канал держит учётку",
            lambda: W.ui_account_delete(mate["id"], as_boss()), 400, "каналов: 1")
    refused("…и предложена блокировка",
            lambda: W.ui_account_delete(mate["id"], as_boss()), 400, "Заблокируйте")
    check("учётка на месте",
          len(sql("SELECT 1 FROM users WHERE id=?", (mate["id"],))), 1)

    print("Свободная учётка удаляется вместе с сессиями")
    run("DELETE FROM pub_channels WHERE tenant_id=?", (str(mate["tenant_id"]),))
    run("INSERT INTO sessions(id,user_id,expires) VALUES('s1',?,?)",
        (mate["id"], time.time() + 3600))
    out = W.ui_account_delete(mate["id"], as_boss())
    check("удалена", out["deleted"], "mate")
    check("строки нет", sql("SELECT 1 FROM users WHERE id=?", (mate["id"],)), [])
    check("живые сессии погашены",
          sql("SELECT 1 FROM sessions WHERE user_id=?", (mate["id"],)), [])

    print("Последнего администратора не удалить")
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    other = accounts.create_user(c, "second", "оченьсекретно", hasher, role="admin",
                                 tenants_dir=os.path.join(TMP, "tenants"))
    c.close()
    # Удаляем «boss» от имени второго администратора — так можно.
    out = W.ui_account_delete(boss["id"], Req(role="admin", uid=other["id"], name="second"))
    check("одного из двух удалить можно", out["deleted"], "boss")
    refused("а последнего — нет",
            lambda: W.ui_account_delete(other["id"],
                                        Req(role="admin", uid=999, name="кто-то")),
            400, "последний администратор")
    check("он на месте", len(sql("SELECT 1 FROM users WHERE id=?", (other["id"],))), 1)

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
