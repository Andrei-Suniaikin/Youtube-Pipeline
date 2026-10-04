#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Готовый ролик всегда находит выход: запас 40 минут и поиск за горизонтом (04.10.2026).

Первый ролик новой студии дошёл до заливки и погиб у ворот: задание создано в 11:28 с
выходом в 12:00, машина взяла его в 11:31, ворота отказали «задание состарилось в очереди»
(они отсекают всё, что ближе 30 минут). Шаг заливки переносил выход, только если до него
меньше 10 минут, а перенос в очереди не нашёл места — все выходы в горизонте канала заняты
планом. Ещё два выпуска встали с «свободных выходов в горизонте нет».

    .venv/bin/python tests/test_reslot_ready.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import zoneinfo

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_engine as E                                            # noqa: E402
import pub_schema                                                 # noqa: E402
import pub_services                                               # noqa: E402

Z = zoneinfo.ZoneInfo("Europe/Warsaw")
NOW = dt.datetime(2026, 10, 4, 11, 28, tzinfo=Z).timestamp()
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def at(day, hour):
    return dt.datetime(2026, 10, day, hour, 0, tzinfo=Z).timestamp()


def fresh(busy_days=()):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    c.execute("INSERT INTO pub_channels(id,tenant_id,uc,name,lang,tz,schedule,deferred,deferred_limit,"
              "horizon_days,profile_id,created,updated) VALUES(1,'t1','UC1','Канал','pl',"
              "'Europe/Warsaw',?,1,0,3,7,?,?)", (json.dumps({"*": ["12:00"]}), NOW, NOW))
    # Выпуск: выход сегодня в 12:00 — через 32 минуты.
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,donor_video_id,"
              "donor_title,slot_at,status,created,updated) VALUES(1,'t1',1,1,'u','v','Д',?,"
              "'running',?,?)", (at(4, 12), NOW, NOW))
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,project_id,status,phase,created,updated) VALUES('rel-1','t1',1,1,"
              "1,'UC1',?,'p1','running','cover_ready',?,?)", (at(4, 12), NOW, NOW))
    for n, day in enumerate(busy_days, start=2):
        c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,donor_video_id,"
                  "donor_title,slot_at,status,created,updated) VALUES(?,'t1',1,1,'u','v','Д',?,"
                  "'planned',?,?)", (n, at(day, 12), NOW, NOW))
    c.commit()
    return c


class Svc(pub_services.PubServices):
    def _clock(self):
        return NOW


def svc():
    return Svc({"create_project": lambda *a, **k: "", "enqueue": lambda *a, **k: None,
                "project_dir": lambda *a, **k: ""})


def rel_of(c):
    r = dict(c.execute("SELECT r.*, ch.deferred, ch.deferred_limit, ch.profile_id "
                       "FROM pub_releases r JOIN pub_channels ch ON ch.id=r.channel_id").fetchone())
    return r


def main() -> int:
    print("Выход через полчаса — переносим сразу, а не создаём задание на гибель у ворот")
    c = fresh()
    out = svc().do_upload_pending(c, rel_of(c))
    check("шаг заливки ждёт, задание не создано", out, {"wait": True})
    check("выход перенесён на завтра 12:00", rel_of(c)["slot_at"], at(5, 12))
    check("и в строке плана тоже",
          c.execute("SELECT slot_at FROM pub_plan_items WHERE id=1").fetchone()[0], at(5, 12))

    print("\nВесь горизонт занят планом — готовый ролик встаёт за горизонтом, а не умирает")
    c = fresh(busy_days=(5, 6, 7))
    out = svc().do_upload_pending(c, rel_of(c))
    check("перенесён за горизонт (8.10 12:00)", (out, rel_of(c)["slot_at"]), ({"wait": True}, at(8, 12)))

    print("\nЗадание в очереди, до выхода меньше 40 минут — перенос находит место и за горизонтом")
    c = fresh(busy_days=(5, 6, 7))
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,channel_url,title,"
              "video_path,slot_at,schedule_iso,status,created,updated) VALUES('rel-1','t1',1,'UC1',"
              "'u','T','/v.mp4',?,'2026-10-04T12:00+02:00','queued',?,?)", (at(4, 12), NOW, NOW))
    c.commit()
    E.reslot_pending_uploads(c, NOW)
    job = c.execute("SELECT slot_at, schedule_iso FROM pub_upload_jobs").fetchone()
    check("задание переехало на 8.10 12:00", job["slot_at"], at(8, 12))
    check("и время для YouTube тоже", job["schedule_iso"].startswith("2026-10-08T12:00"), True)

    print("\nЗапас один на всех: 40 минут, а не 10")
    check("UPLOAD_LEAD_SEC", E.UPLOAD_LEAD_SEC, 2400)
    check("поиск — не меньше 30 дней", E.ready_slot_horizon({"horizon_days": 3}), 30)
    check("свой горизонт канала больше — берём его", E.ready_slot_horizon({"horizon_days": 45}), 45)

    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
