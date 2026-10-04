#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""«Жду» снимает устаревшее «ждём: …» с выпуска (04.10.2026).

30 выпусков с утра честно ждали своих картинок в очереди (задача стояла с 03.10 и
движок её подхватил: «картинки уже стоят в очереди — жду их»), а экран выпусков
показывал «ждём: очередь не приняла повторный этап картинок» — запись от прошлого
вечера. Ответ обработчика «жду» значит, что ресурс уже ответил; прежняя причина
ожидания устарела и снимается. Сам срок фазы при этом не продлевается.

    .venv/bin/python tests/test_wait_clears_stale_reason.py
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


def db(reason):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,project_id,status,phase,blocked_reason,deadline,created,"
              "updated) VALUES('r1','t',1,1,1,'UC1',?,'p1','pending','project_ready',?,?,?,?)",
              (NOW + 86400, reason, NOW + 600, NOW - 60, NOW - 60))
    c.commit()
    return c


class Waits:
    def do_media_ready(self, c, rel):
        return {"wait": True}


def run(reason):
    c = db(reason)
    rel = dict(c.execute("SELECT * FROM pub_releases").fetchone())
    out = E.advance(c, rel, Waits(), NOW)
    row = c.execute("SELECT status, blocked_reason, deadline FROM pub_releases").fetchone()
    return out, dict(row)


def main() -> int:
    print("Обработчик ответил «жду» — прошлое «ждём: …» снято")
    out, row = run("ждём: очередь не приняла повторный этап картинок")
    check("ход — ожидание", out, "waiting")
    check("причина снята", row["blocked_reason"], None)
    check("выпуск в работе", row["status"], "running")
    check("срок фазы не продлён", row["deadline"], NOW + 600)

    print("\nДругая запись (не «ждём:») — не трогаем")
    out, row = run("заметка, которую ставил не ResourceDown")
    check("осталась как была", row["blocked_reason"], "заметка, которую ставил не ResourceDown")

    print("\nПусто — пусто")
    out, row = run(None)
    check("ничего не появилось", row["blocked_reason"], None)
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
