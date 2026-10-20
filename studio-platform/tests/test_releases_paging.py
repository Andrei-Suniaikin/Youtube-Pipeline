#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экран выпусков просит по двадцать штук, а не всю тысячу (20.10.2026).

Экран грузил ВСЕ выпуски тенанта разом: 996 строк, из них 842 давно вышедших, и на
каждую считались полоска этапов, объяснение причины и пробелы настройки канала. На
холодную это 2,8 секунды в одних только запросах — владелец и жаловался на долгую
загрузку.

Здесь проверяется договор подгрузки: страница отдаёт ровно столько, сколько просили,
страницы не перекрываются и не теряют строк, счётчик `total` считает ВСЕ подходящие
выпуски (иначе «показано 20 из 20» выглядит как конец списка), а вызов без `limit`
по-прежнему отдаёт всё — так ходят карточка выпуска и старые места.

    .venv/bin/python tests/test_releases_paging.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="releases-paging-")
DB_PATH = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB_PATH
os.environ["VS_DEV_TENANT"] = "t1"
sys.path.insert(0, os.path.join(BASE, "web"))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "shared"))

FAILS = []
TOTAL = 47                                   # больше двух страниц — видно и стык, и хвост


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ок   {name}")
    else:
        FAILS.append(f"{name}: получили {got!r}, ждали {want!r}")
        print(f"  ПЛОХО {name}: получили {got!r}, ждали {want!r}")


def make_db() -> None:
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
    CREATE TABLE pub_releases(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT UNIQUE,
      tenant_id TEXT, plan_item_id INTEGER, channel_id INTEGER, uc TEXT, slot_at REAL,
      project_id TEXT, status TEXT, phase TEXT, video_id TEXT, blocked_reason TEXT,
      attempts INTEGER DEFAULT 0, updated REAL, hand_confirmed REAL, hand_by TEXT);
    CREATE TABLE pub_channels(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT, uc TEXT,
      lang TEXT, preset_vsgen_id TEXT, preset_rewrite TEXT, preset_cover TEXT);
    CREATE TABLE pub_plan_items(id INTEGER PRIMARY KEY, donor_url TEXT, donor_title TEXT);
    CREATE TABLE pub_release_jobs(release_id TEXT, service TEXT, kind TEXT, status TEXT);
    CREATE TABLE queue_tasks(job_id INTEGER, project_id TEXT, kind TEXT, status TEXT);
    CREATE TABLE pub_upload_jobs(release_id TEXT, status TEXT);
    """)
    c.execute("INSERT INTO pub_channels(id,tenant_id,name,uc,lang) VALUES(1,'t1','Канал','UC1','pl')")
    # Половина выпусков с ОДИНАКОВЫМ временем выхода: без второго ключа сортировки
    # страницы бы перекрывались именно здесь.
    base = time.time()
    for i in range(TOTAL):
        slot = base + (i // 2) * 60
        c.execute("INSERT INTO pub_plan_items(id,donor_url,donor_title) VALUES(?,?,?)",
                  (i + 1, f"https://y/{i}", f"Донор {i}"))
        c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,channel_id,uc,"
                  "slot_at,status,phase,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                  (f"r_{i:03d}", "t1", i + 1, 1, "UC1", slot, "pending", "created", base))
    # Чужой тенант: в счёт попасть не должен.
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,channel_id,uc,"
              "slot_at,status,phase,updated) VALUES('r_alien','t2',1,1,'UC1',?,'pending','created',?)",
              (base, base))
    c.commit()
    c.close()


def main() -> int:
    make_db()
    import app as A                                            # noqa: E402

    c = A.db()
    try:
        print("1. Страница отдаёт ровно столько, сколько просили")
        first = A._releases(c, limit=20, offset=0)
        check("первая страница — 20 строк", len(first), 20)
        check("всего по отборам — свои, без чужого тенанта",
              A._releases_total(c), TOTAL)

        print("\n2. Страницы не перекрываются и не теряют строк")
        second = A._releases(c, limit=20, offset=20)
        third = A._releases(c, limit=20, offset=40)
        ids = [r["release_id"] for r in first + second + third]
        check("собрали все выпуски", len(ids), TOTAL)
        check("ни одного повтора", len(set(ids)), TOTAL)
        check("хвост короче страницы", len(third), TOTAL - 40)

        print("\n3. Порядок тот же, что без подгрузки")
        whole = [r["release_id"] for r in A._releases(c)]
        check("без limit отдаётся всё", len(whole), TOTAL)
        check("порядок строк совпал", ids, whole)

        print("\n4. Отборы считаются вместе со счётчиком")
        check("по чужому статусу ничего нет", A._releases_total(c, status="done"), 0)
        check("по своему статусу — все", A._releases_total(c, status="pending"), TOTAL)
        check("страница под отбор тоже пуста", len(A._releases(c, status="done", limit=20)), 0)
    finally:
        c.close()

    print("\nитог:", "всё сошлось" if not FAILS else f"ПРОВАЛОВ {len(FAILS)}")
    for line in FAILS:
        print("  -", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
