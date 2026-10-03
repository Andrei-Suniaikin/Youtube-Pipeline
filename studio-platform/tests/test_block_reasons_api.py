#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выдача пульта несёт перевод причины остановки и признак «нет пресета обложек» (03.10.2026).

Браузерная проверка (`test_block_reasons_browser.py`) подкладывает экрану готовое поле
`block`. Здесь — что сервер его действительно отдаёт: в списке выпусков, в карточке, в
календаре канала, — что у живого, а не остановленного выпуска его нет («ждём: …» — не
приговор), и что плитка канала знает, выбран ли у него пресет обложек.

    .venv/bin/python tests/test_block_reasons_api.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="block-reasons-")
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
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    role TEXT, tenant_id TEXT, disabled INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
  CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
    kind TEXT, status TEXT, created REAL, finished REAL, project_id TEXT, error TEXT);
  CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT, tenant_id TEXT,
    settings TEXT);
""")
from pub_calls import ensure_calls_schema                          # noqa: E402

ensure_calls_schema(_c)
NOW = time.time()


# Календарь канала строится по его расписанию: выход каждый день в 18:00 по Варшаве,
# выпуски стоят ровно на этих выходах.
SCHEDULE = '{"*": ["18:00"]}'


def slot(days):
    import datetime as dt
    import zoneinfo
    z = zoneinfo.ZoneInfo("Europe/Warsaw")
    day = dt.datetime.fromtimestamp(NOW, z).date() + dt.timedelta(days=days)
    return dt.datetime(day.year, day.month, day.day, 18, 0, tzinfo=z).timestamp()


def channel(n, preset_cover=""):
    uc = "UC" + str(n) * 22
    _c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
               "deferred_limit,preset_cover,status,horizon_days,created,updated) "
               "VALUES('t1',?,?,'pl','Europe/Warsaw',?,1,0,?,'active',14,?,?)",
               (uc, f"Канал {n}", SCHEDULE, preset_cover, NOW, NOW))
    return _c.execute("SELECT id FROM pub_channels WHERE uc=?", (uc,)).fetchone()[0]


def release(n, ch, status, phase, reason):
    _c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
               "donor_video_id,donor_title,slot_at,status,created,updated) "
               "VALUES(?,'t1',?,1,?,?,'Донор',?,'error',?,?)",
               (n, ch, f"https://youtu.be/donor{n}aaaaa", f"donor{n}aaaaa",
                slot(n), NOW, NOW))
    _c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
               "plan_item_version,channel_id,uc,slot_at,status,phase,blocked_reason,"
               "created,updated) VALUES(?,'t1',?,1,?,'',?,?,?,?,?,?)",
               (f"rel-{n}", n, ch, slot(n), status, phase, reason, NOW, NOW))


без = channel(1)
с = channel(2, preset_cover="Default RU")
release(1, без, "blocked", "media_ready", "у канала не выбран пресет обложек")
release(2, с, "blocked", "created", "у ролика нет субтитров (TranscriptsDisabled)")
release(3, с, "pending", "created", "ждём: все выходы исчерпали квоту, ближайший через 15 мин")
release(4, с, "blocked", "upload_running", "зависло на шаге «upload_running»")
# Выпуск уже в работе, а старая причина в поле осталась: приговором это не показываем.
release(5, с, "running", "created", "у ролика нет субтитров (TranscriptsDisabled)")
_c.commit()
_c.close()

import app as W                                                    # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


async def _empty():
    return []


def main() -> int:
    c = W.db()
    try:
        rels = {r["release_id"]: r for r in W._releases(c)}
    finally:
        c.close()
    print("Список выпусков")
    check("пресет обложек → настройка канала", rels["rel-1"]["block"].get("kind"), "channel")
    check("донор → проблема в доноре", rels["rel-2"]["block"].get("kind"), "donor")
    check("и смысл словами", rels["rel-2"]["block"].get("title"), "автор ролика отключил субтитры")
    check("живой выпуск с «ждём: …» — не приговор, перевода нет", rels["rel-3"]["block"], {})
    check("незнакомая причина — пусто, экран покажет как есть", rels["rel-4"]["block"], {})
    check("выпуск в работе — старая причина приговором не показывается",
          rels["rel-5"]["block"], {})
    check("строка плана отдана — «убрать ссылку» есть чем делать",
          rels["rel-2"]["plan_item_id"], 2)
    check("сырая причина на месте — для разбора",
          rels["rel-2"]["blocked_reason"], "у ролика нет субтитров (TranscriptsDisabled)")

    print("\nКарточка выпуска")
    d = W.ui_release("rel-1")
    check("в карточке тот же перевод", d["release"]["block"].get("tag"), "Настройка канала")

    print("\nКалендарь канала")
    import asyncio
    # Названия доноров календарь дотягивает из сети — в тесте сети нет, отвечаем пусто.
    W._backfill_donor_titles_network = (lambda *a, **k: _empty())
    cal = asyncio.run(W.ui_channel(без))
    rows = [r for r in cal.get("calendar", []) if r.get("release_id") == "rel-1"]
    check("строка календаря несёт перевод",
          rows and rows[0].get("block", {}).get("kind"), "channel")

    print("\nПлитки каналов")
    tiles = {t["id"]: t for t in W.ui_channels()["channels"]}
    check("без пресета обложек — признак ложь", tiles[без]["has_cover_preset"], False)
    check("с пресетом — истина", tiles[с]["has_cover_preset"], True)

    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
