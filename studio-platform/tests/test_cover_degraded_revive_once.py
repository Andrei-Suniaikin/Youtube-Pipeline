#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""После выкатки «лучшая из трёх» выпуски с браком обложки возвращаются сами — один раз.

06.10.2026: 40 выпусков в разборе «обложка деградировала дважды». Владелец: «после
обновы нужно оживить выпуски». Движок делает это сам на первом тике: забывает старый
заказ обложки и возвращает выпуск в работу. Повторно не делает — пометка в admin_state.

    .venv/bin/python tests/test_cover_degraded_revive_once.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_engine as E                                            # noqa: E402
import pub_schema                                                 # noqa: E402

NOW = 1_800_000_000.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    c.execute("CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL)")
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
              "deferred_limit,created,updated) VALUES('t1','UC1','Канал','cs',"
              "'Europe/Prague','{}',1,0,?,?)", (NOW, NOW))
    for i, why in enumerate(["обложка деградировала дважды: текст наложен поверх картинки",
                             "обложка деградировала дважды: текст наложен поверх картинки",
                             "у ролика нет субтитров (VideoUnplayable)"], 1):
        c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
                  "donor_video_id,donor_title,slot_at,status,created,updated) "
                  "VALUES(?,'t1',1,1,'https://youtu.be/x','x','Донор',?,'error',?,?)",
                  (i, NOW + 86400 * i, NOW, NOW))
        c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
                  "plan_item_version,channel_id,uc,slot_at,project_id,status,phase,"
                  "attempts,blocked_reason,created,updated) VALUES(?,'t1',?,1,1,'UC1',?,?,"
                  "'blocked','media_ready',1,?,?,?)",
                  (f"r{i}", i, NOW + 86400 * i, f"p{i}", why, NOW, NOW))
        c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,"
                  "status,error,created,updated) VALUES(?,'cover','job',?,'error',?,?,?)",
                  (f"r{i}", f"job{i}", why, NOW, NOW))
    c.commit()
    return c


def status(c, rid):
    return c.execute("SELECT status FROM pub_releases WHERE release_id=?",
                     (rid,)).fetchone()["status"]


def cover_job(c, rid):
    return c.execute("SELECT COUNT(*) n FROM pub_release_jobs WHERE release_id=? AND "
                     "service='cover'", (rid,)).fetchone()["n"]


def main() -> int:
    c = db()
    print("Первый тик после выкатки")
    back = E.revive_cover_degraded_once(c, NOW)
    check("вернулись оба с браком обложки", sorted(back), ["r1", "r2"])
    check("r1 в работе", status(c, "r1") in ("pending", "running"), True)
    check("старый заказ обложки забыт", cover_job(c, "r1"), 0)
    check("чужая причина не тронута", status(c, "r3"), "blocked")
    check("и её задача на месте", cover_job(c, "r3"), 1)

    print("\nВторой раз не срабатывает")
    c.execute("UPDATE pub_releases SET status='blocked', blocked_reason="
              "'обложка деградировала дважды: текст наложен поверх картинки' "
              "WHERE release_id='r1'")
    check("повтор ничего не возвращает", E.revive_cover_degraded_once(c, NOW + 60), [])
    check("r1 остался в разборе", status(c, "r1"), "blocked")

    print("\nБаза без admin_state — тик не падает")
    c2 = sqlite3.connect(":memory:")
    c2.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c2)
    check("пусто, без исключения", E.revive_cover_degraded_once(c2, NOW), [])

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
