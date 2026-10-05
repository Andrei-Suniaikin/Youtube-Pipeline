#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Одна сводка на три потока: ВВ2 и манхва из старого кабинета (решение А10, 04.10.2026).

Сводка не читает базу старого кабинета сама — это анти-контракт `pub_neighbors.py`. Числа
приходят файлом `ops-ww2/legacy_streams.py`. Здесь опасно одно: показать устаревшее или
отсутствующее как настоящее. Проверяется:

  • файла нет — одна строка «выгрузки нет», а не тишина и не нули;
  • свежий файл — строка на поток в формате сводки и строка «сегодня»;
  • файл старше 3 часов — «устарели», и чисел нет: чужие сутки за сегодня не выдаются;
  • битый, чужая версия, без времени — так и сказано;
  • путь по умолчанию — `feeds/` рядом с базой студии, VS_LEGACY_STREAMS_FILE его меняет;
  • контракт: файл, который пишет сама выгрузка, сводка принимает;
  • collect() кладёт блок в данные экрана и не падает.

    .venv/bin/python tests/test_digest_legacy.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
ROOT = os.path.abspath(os.path.join(BASE, ".."))
TMP = tempfile.mkdtemp(prefix="digest-legacy-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["VS_DIGEST_TZ"] = "UTC"
os.environ.pop("VS_LEGACY_STREAMS_FILE", None)
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(ROOT, "ops-ww2"))

from pub_schema import ensure_pub_schema                          # noqa: E402
import pub_digest as D                                            # noqa: E402
import legacy_streams as L                                        # noqa: E402

FAILS = []
NOW = dt.datetime(2026, 10, 7, 20, 0, tzinfo=dt.timezone.utc).timestamp()
HOUR = 3600
FEED = os.path.join(TMP, "feeds", "legacy_streams.json")


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh() -> sqlite3.Connection:
    c = sqlite3.connect(os.path.join(TMP, f"db{len(os.listdir(TMP))}.sqlite"))
    c.row_factory = sqlite3.Row
    ensure_pub_schema(c)
    c.executescript("""
      CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
        kind TEXT, status TEXT, created REAL, finished REAL, project_id TEXT, error TEXT);
      CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT);
    """)
    c.commit()
    return c


def feed(at, **over):
    data = {"schema_version": 1, "at": at, "tz": "UTC", "day": "2026-10-07",
            "streams": [
                {"key": "ww2", "name": "ВВ2", "channels": 62, "paused": 10, "plan": 7,
                 "accepted": 3, "blocked": 171,
                 "reasons": [{"why": "к времени выхода не успели", "count": 90},
                             {"why": "AdsPower: Check Proxy Fail", "count": 4}],
                 "today": {"expected": 4, "ahead": 2, "confirmed": 0, "reached": 3,
                           "delayed": 1, "unknown": 3,
                           "why": ["в старой студии выход ролика не проверяется — "
                                   "только расписание"]},
                 "uploaded": 2, "last_new_release": NOW - 12 * 24 * HOUR},
                {"key": "манхва", "name": "Манхва", "channels": 20, "paused": 3, "plan": 0,
                 "accepted": 0, "blocked": 0, "reasons": [],
                 "today": {"expected": 0, "ahead": 0, "confirmed": 0, "reached": 0,
                           "delayed": 0, "unknown": 0, "why": []},
                 "uploaded": 0, "last_new_release": None}]}
    data.update(over)
    return data


def put(text):
    os.makedirs(os.path.dirname(FEED), exist_ok=True)
    with open(FEED, "w", encoding="utf-8") as fh:
        fh.write(text)


print("\nПуть по умолчанию")
check("feeds/ рядом с базой студии", D.legacy_path(), FEED)
os.environ["VS_LEGACY_STREAMS_FILE"] = "/elsewhere/x.json"
check("VS_LEGACY_STREAMS_FILE его меняет", D.legacy_path(), "/elsewhere/x.json")
os.environ.pop("VS_LEGACY_STREAMS_FILE")
# Утреннюю сводку шлёт studio-alerts.service, а у него в окружении только VS_DEV_DB.
os.environ.pop("VS_SERVER_DB")
os.environ["VS_DEV_DB"] = "/srv/studio/data/vsgen.db"
check("без VS_SERVER_DB — рядом с VS_DEV_DB (окружение тревог на сервере)",
      D.legacy_path(), "/srv/studio/data/feeds/legacy_streams.json")
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_DB"] = DB

print("\nФайла нет")
lg = D.legacy_feed(NOW)
check("состояние", lg["state"], "missing")
text = "\n".join(D._legacy_lines(lg))
check("одна строка «выгрузки нет»", "· ВВ2 и манхва: выгрузки старой студии нет" in text, True)
check("и никаких нулей вместо неё", "каналов 0" in text, False)

print("\nСвежий файл")
put(json.dumps(feed(NOW - HOUR), ensure_ascii=False))
lg = D.legacy_feed(NOW)
check("состояние", lg["state"], "ok")
text = "\n".join(D._legacy_lines(lg))
check("заголовок с временем выгрузки",
      "Старая студия (выгрузка 07.10 19:00, числа за эти сутки):" in text, True)
check("строка ВВ2 в формате сводки",
      "· ВВ2: каналов 62 (на паузе 10), в плане 7, принято 3, блокеры 171 — главное: "
      "к времени выхода не успели — 90; AdsPower: Check Proxy Fail — 4" in text, True)
check("строка «сегодня» ВВ2",
      "  сегодня: ожидалось 4 выхода, подтверждено 0, дошло до канала 3, задержано 1, "
      "не знаем 3; ещё впереди 2." in text, True)
check("причина незнания названа",
      "    не знаем потому, что в старой студии выход ролика не проверяется" in text, True)
check("залито и последний новый выпуск",
      "  залито за сутки 2; последний новый выпуск 25.09 20:00." in text, True)
check("манхва: ноль не спрятан",
      "· Манхва: каналов 20 (на паузе 3), в плане 0, принято 0, блокеры 0" in text, True)
check("манхва: выпусков не было — так и сказано",
      "последний новый выпуск не заводился." in text, True)

print("\nУстаревший файл")
put(json.dumps(feed(NOW - 4 * HOUR), ensure_ascii=False))
lg = D.legacy_feed(NOW)
check("состояние", lg["state"], "stale")
text = "\n".join(D._legacy_lines(lg))
check("«устарели» с временем", "данные старой студии от 07.10 16:00 — устарели" in text, True)
check("чужие сутки за сегодня не выдаются", "· ВВ2:" in text or lg["streams"] != [], False)

print("\nБитый файл")
put("{не json")
check("не читается", D.legacy_feed(NOW)["why"], "файл выгрузки старой студии не читается")
put(json.dumps(feed(NOW, schema_version=2)))
check("чужая версия названа", D.legacy_feed(NOW)["why"],
      "выгрузка старой студии в незнакомом формате (версия 2)")
put(json.dumps(feed(None)))
check("без времени выгрузки", D.legacy_feed(NOW)["state"], "broken")
put(json.dumps(feed(NOW, streams="ВВ2")))
check("без строк потоков", D.legacy_feed(NOW)["why"],
      "в выгрузке старой студии нет строк потоков")
check("битое в тексте тоже сказано",
      "· ВВ2 и манхва: в выгрузке старой студии нет строк потоков"
      in "\n".join(D._legacy_lines(D.legacy_feed(NOW))), True)

print("\nКонтракт с выгрузкой")
old_db = os.path.join(TMP, "old.db")
oc = sqlite3.connect(old_db)
oc.executescript("CREATE TABLE pub_channels(id INTEGER PRIMARY KEY, uc TEXT, niche TEXT, "
                 "status TEXT);")
oc.commit()
oc.close()
c_old = L.connect_ro(old_db)
L.write_atomic(FEED, L.collect(c_old, now=NOW - 60, tz_name="UTC"))
c_old.close()
lg = D.legacy_feed(NOW)
check("файл самой выгрузки принят", lg["state"], "ok")
check("и потоки названы", [s["name"] for s in lg["streams"]], ["ВВ2", "Манхва"])
check("пустой старый кабинет — нули видны, а не прячутся",
      "· ВВ2: каналов 0, в плане 0, принято 0, блокеры 0" in "\n".join(D._legacy_lines(lg)), True)

print("\ncollect и render")
c = fresh()
data = D.collect(c, "t1", now=NOW, have_key=True)
check("в данных экрана есть блок", data["legacy"]["state"], "ok")
text = D.render(data)
check("render печатает блок после групп",
      text.index("Группы каналов:") < text.index("Старая студия (выгрузка") < text.index("Сегодня:"),
      True)
os.unlink(FEED)
check("без файла render не падает и говорит об этом",
      "выгрузки старой студии нет" in D.render(D.collect(c, "t1", now=NOW, have_key=True)), True)

print(f"\nпровалов: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
