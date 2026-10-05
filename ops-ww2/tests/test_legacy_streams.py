#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выгрузка ВВ2 и манхвы из старого кабинета для утренней сводки (решение А10, 04.10.2026).

Проверяется:
  • числа на поток — по тем же определениям, что у сводки новой студии: каналы (архив не
    в счёт, пауза отдельно), «в плане», «принято за сутки», блокеры и главные причины,
    «сегодня» (ожидалось / подтверждено / дошло / задержано / не знаем), залито за сутки;
  • чужие ниши и архивные каналы в потоки не попадают;
  • без проверки выхода всё дошедшее до канала — «не знаем», а не «вышло»;
  • база открывается только на чтение — запись в неё падает;
  • файл пишется целиком: сбой посреди записи оставляет прежний файл и не мусорит;
  • копии PHASES и LIVE_PLAN совпадают с кодом студии.

Таблицы — из базы старого кабинета (sqlite_master, 04–05.10.2026).

    studio-platform/.venv/bin/python ops-ww2/tests/test_legacy_streams.py
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "ops-ww2"))

import legacy_streams as L                                         # noqa: E402

FAILS = []
W = ZoneInfo("Europe/Warsaw")


def at(day, hh, mm=0):
    return dt.datetime(2026, 10, day, hh, mm, tzinfo=W).timestamp()


NOW = at(5, 12)          # 05.10.2026 12:00 по Варшаве

SCHEMA = """
CREATE TABLE pub_upload_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL, channel_id INTEGER NOT NULL, uc TEXT NOT NULL, profile_id INTEGER, host_id INTEGER, channel_url TEXT NOT NULL, title TEXT NOT NULL, description TEXT, schedule_iso TEXT, visibility TEXT NOT NULL DEFAULT 'public', collaborator TEXT, video_path TEXT NOT NULL, video_sha256 TEXT, cover_path TEXT, slot_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'queued', phase TEXT, claim_token TEXT, host TEXT, worker TEXT, claimed REAL, lease_until REAL, heartbeat REAL, verdict TEXT, video_id TEXT, evidence TEXT, error TEXT, finished REAL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE pub_plan_items(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, channel_id INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1, donor_url TEXT NOT NULL, position REAL NOT NULL DEFAULT 0, slot_at REAL, slot_mode TEXT NOT NULL DEFAULT 'auto', overrides TEXT NOT NULL DEFAULT '{}', note TEXT, status TEXT NOT NULL DEFAULT 'planned', frozen INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL, donor_title TEXT);
CREATE TABLE pub_releases(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL, plan_item_id INTEGER NOT NULL, plan_item_version INTEGER NOT NULL, channel_id INTEGER NOT NULL, uc TEXT NOT NULL, slot_at REAL NOT NULL, project_id TEXT, status TEXT NOT NULL DEFAULT 'pending', phase TEXT NOT NULL DEFAULT 'created', video_id TEXT, blocked_reason TEXT, evidence TEXT, heartbeat REAL, deadline REAL, attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(plan_item_id,plan_item_version));
CREATE TABLE pub_release_events(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, seq INTEGER NOT NULL, phase TEXT NOT NULL, video_id TEXT, payload TEXT, created REAL NOT NULL, UNIQUE(release_id,seq));
CREATE INDEX idx_pub_release_events_rid ON pub_release_events(release_id,seq);
CREATE TABLE pub_channels(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, uc TEXT NOT NULL UNIQUE, name TEXT NOT NULL, handle TEXT, lang TEXT, niche TEXT, profile_id INTEGER, host_id INTEGER, tz TEXT NOT NULL DEFAULT 'Europe/Warsaw', schedule TEXT NOT NULL DEFAULT '{}', horizon_days INTEGER NOT NULL DEFAULT 3, preset_rewrite TEXT, preset_vsgen_id INTEGER, preset_cover TEXT, collaborator TEXT, video_mode TEXT NOT NULL DEFAULT 'slideshow', t2v_first_n INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active', note TEXT, created REAL NOT NULL, updated REAL NOT NULL, lead_days INTEGER NOT NULL DEFAULT 2, deferred INTEGER NOT NULL DEFAULT 1, deferred_limit INTEGER NOT NULL DEFAULT 5);
"""


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def build(path):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    t0 = at(1, 0)

    def channel(uc, niche, status="active"):
        c.execute("INSERT INTO pub_channels(tenant_id,uc,name,niche,status,created,updated) "
                  "VALUES('3',?,?,?,?,?,?)", (uc, uc, niche, status, t0, t0))
        return c.execute("SELECT id FROM pub_channels WHERE uc=?", (uc,)).fetchone()[0]

    def plan(ch, slot, status="planned"):
        c.execute("INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,slot_at,status,"
                  "created,updated) VALUES('3',?,?,?,?,?,?)",
                  (ch, f"https://youtu.be/{ch}-{slot}", slot, status, t0, t0))
        return c.execute("SELECT max(id) FROM pub_plan_items").fetchone()[0]

    def release(rid, item, ch, phase, status="running", reason=None, created=None,
                updated=None):
        c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
                  "channel_id,uc,slot_at,status,phase,blocked_reason,created,updated) "
                  "VALUES(?,'3',?,1,?,?,0,?,?,?,?,?)",
                  (rid, item, ch, f"UC{ch}", status, phase, reason, created or t0,
                   updated or created or t0))

    def event(rid, seq, phase, created):
        c.execute("INSERT INTO pub_release_events(release_id,seq,phase,payload,created) "
                  "VALUES(?,?,?,'{}',?)", (rid, seq, phase, created))

    def upload(rid, ch, uc, status, finished):
        c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,channel_url,"
                  "title,video_path,slot_at,status,finished,created,updated) "
                  "VALUES(?,'3',?,?,'u','t','v',0,?,?,?,?)",
                  (rid, ch, uc, status, finished, t0, t0))

    ch1 = channel("UCw1", "ww2")
    ch2 = channel("UCw2", "ww2")
    ch3 = channel("UCw3", "ww2", "paused")
    cha = channel("UCwA", "ww2", "archived")
    chm = channel("UCm1", "манхва")
    cht = channel("UCt1", "тайга")
    for ch, uc in ((ch1, "UCw1"), (ch2, "UCw2"), (ch3, "UCw3"), (cha, "UCwA"),
                   (chm, "UCm1"), (cht, "UCt1")):
        c.execute("UPDATE pub_channels SET uc=? WHERE id=?", (uc, ch))

    # ВВ2, канал 1
    p1 = plan(ch1, at(5, 8), "done")
    release("r1", p1, ch1, "scheduled", "done", updated=at(5, 8, 30))  # дошёл, проверки нет
    event("r1", 1, "scheduled", at(5, 8, 30))                         # принят сегодня
    p2 = plan(ch1, at(5, 9), "running")
    release("r2", p2, ch1, "media_ready", "blocked",
            "обложка не нарисовалась дважды: 0/1 обложек")              # задержан + блокер
    plan(ch1, at(5, 18))                                             # впереди сегодня
    plan(ch1, at(6, 10))                                             # впереди
    plan(ch1, None)                                                  # без слота — в плане
    plan(ch1, at(5, 7))                                              # выпуска нет — задержан
    plan(ch1, at(6, 11), "cancelled")                                # выпал из плана
    # ВВ2, канал 2
    p8 = plan(ch2, at(5, 6), "done")
    release("r8", p8, ch2, "published", "done", created=at(4, 20),
            updated=at(5, 7))                                        # подтверждён сегодня
    event("r8", 1, "scheduled", at(4, 21))                           # принят ВЧЕРА
    event("r8", 2, "published", at(5, 7))
    p9 = plan(ch2, at(4, 9), "error")
    release("r9", p9, ch2, "upload_pending", "blocked", "AdsPower: Check Proxy Fail (профиль 12)")
    p12 = plan(ch2, at(4, 10), "error")
    release("r12", p12, ch2, "media_ready", "blocked",
            "обложка не нарисовалась дважды: 0/1 обложек", created=at(5, 3))
    upload("r8", ch2, "UCw2", "done", at(5, 6))                      # залит сегодня
    upload("r1", ch1, "UCw1", "done", at(4, 22))                     # залит вчера
    # Проверка выхода ходила, но вышедшим не подтвердила — причина другая
    p13 = plan(ch2, at(5, 11), "done")
    release("r13", p13, ch2, "scheduled", "done", created=at(5, 1), updated=at(5, 1))
    event("r13", 1, "public_check", at(5, 11, 30))
    # ВВ2, на паузе: строка плана впереди считается, как у сводки
    plan(ch3, at(6, 12))
    # ВВ2, архив: не в счёт ничего
    pa = plan(cha, at(6, 9))
    release("ra", pa, cha, "scheduled", "done", created=at(5, 11))
    event("ra", 1, "scheduled", at(5, 9))
    upload("ra", cha, "UCwA", "done", at(5, 9))
    # манхва
    pm = plan(chm, at(5, 10), "running")
    release("rm", pm, chm, "upload_running", "running", created=at(5, 2))
    # тайга — чужая ниша
    pt = plan(cht, at(5, 8), "done")
    release("rt", pt, cht, "scheduled", "done")
    c.commit()
    c.close()


tmp = tempfile.mkdtemp(prefix="legacy-streams-")
db = os.path.join(tmp, "vsgen.db")
build(db)

print("\nЧисла на поток")
c = L.connect_ro(db)
data = L.collect(c, now=NOW, tz_name="Europe/Warsaw")
ww2, manga = data["streams"]
check("версия схемы", data["schema_version"], 1)
check("сутки — по Варшаве", data["day"], "2026-10-05")
check("потоки и порядок", [(s["key"], s["name"]) for s in data["streams"]],
      [("ww2", "ВВ2"), ("манхва", "Манхва")])
check("ВВ2: каналов (архив не в счёт)", ww2["channels"], 3)
check("ВВ2: на паузе", ww2["paused"], 1)
check("ВВ2: в плане — впереди и без слота, отменённая не в счёт", ww2["plan"], 4)
check("ВВ2: принято за сутки — только событие сегодня", ww2["accepted"], 1)
check("ВВ2: блокеры", ww2["blocked"], 3)
check("ВВ2: главные причины, сложенные по сути",
      ww2["reasons"], [{"why": "обложка не нарисовалась дважды", "count": 2},
                       {"why": "AdsPower: Check Proxy Fail", "count": 1}])
t = ww2["today"]
check("ВВ2 сегодня: ожидалось (время наступило)", t["expected"], 5)
check("ВВ2 сегодня: ещё впереди", t["ahead"], 1)
check("ВВ2 сегодня: подтверждено — только published", t["confirmed"], 1)
check("ВВ2 сегодня: дошло до канала", t["reached"], 3)
check("ВВ2 сегодня: задержано (не дошёл и без выпуска)", t["delayed"], 2)
check("ВВ2 сегодня: дошёл, но не подтверждён — «не знаем», не «вышло»", t["unknown"], 2)
check("ВВ2 сегодня: причины незнания названы, у каждого вида своя",
      sorted(t["why"]), sorted(["в старой студии выход ролика не проверяется — только расписание",
                                "проверка выхода ответила, но вышедшим ролик не подтверждён"]))
check("ВВ2: залито за сутки (вчерашнее и архив не в счёт)", ww2["uploaded"], 1)
check("ВВ2: последний новый выпуск", ww2["last_new_release"], at(5, 3))
check("принят вчера, а тронут сегодня — в «принято за сутки» не попал", ww2["accepted"], 1)
check("манхва: каналов", manga["channels"], 1)
check("манхва: загрузка идёт — до канала не дошёл, задержан",
      (manga["today"]["expected"], manga["today"]["delayed"], manga["today"]["reached"]),
      (1, 1, 0))
check("манхва: последний новый выпуск", manga["last_new_release"], at(5, 2))

print("\nТолько чтение")
try:
    c.execute("DELETE FROM pub_releases")
    wrote = True
except sqlite3.OperationalError:
    wrote = False
check("запись в базу старого кабинета невозможна", wrote, False)
c.close()
check("база не изменилась",
      sqlite3.connect(db).execute("SELECT count(*) FROM pub_releases").fetchone()[0], 9)
try:
    L.connect_ro(os.path.join(tmp, "нет.db"))
    missing = None
except FileNotFoundError as exc:
    missing = "базы старой студии нет" in str(exc)
check("нет базы — понятная ошибка, а не пустая новая база", missing, True)
check("и файл базы не создан", os.path.exists(os.path.join(tmp, "нет.db")), False)

print("\nЗапись целиком")
out = os.path.join(tmp, "feeds", "legacy_streams.json")
check("запуск с --now пишет файл", L.main(["--db", db, "--out", out, "--now", str(NOW)]), 0)
saved = json.load(open(out, encoding="utf-8"))
check("в файле те же числа", saved["streams"][0]["blocked"], 3)
before = open(out, encoding="utf-8").read()
try:
    L.write_atomic(out, {"schema_version": 1, "bad": object()})
    raised = False
except TypeError:
    raised = True
check("сбой посреди записи — ошибка наружу", raised, True)
check("прежний файл цел", open(out, encoding="utf-8").read(), before)
check("и рядом не осталось временных файлов",
      sorted(os.listdir(os.path.dirname(out))), ["legacy_streams.json"])

print("\nКопии совпадают с кодом студии")


def literal(path, name):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            return ast.literal_eval(node.value)
    return None


server = os.path.join(ROOT, "studio-platform", "server")
check("PHASES = pub_engine.PHASES", L.PHASES, literal(os.path.join(server, "pub_engine.py"), "PHASES"))
check("LIVE_PLAN = pub_digest.LIVE_PLAN", L.LIVE_PLAN,
      literal(os.path.join(server, "pub_digest.py"), "LIVE_PLAN"))

print(f"\nпровалов: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
