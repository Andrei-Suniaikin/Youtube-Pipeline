#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Готовый ролик занимает ближайший выход, а не свой дальний (08.10.2026).

Nocne Biografie: выход 05.10 вышел, 08–10.10 застряли на входе в профиль, а два готовых
ролика ушли в отложку на 17–18.10 — выходы раздаются на 18 дней вперёд в момент появления
строки плана. Неделя пустая, хотя ролики есть.

    .venv/bin/python tests/test_pull_forward.py
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

import pub_schema                                                 # noqa: E402
import pub_services                                               # noqa: E402

Z = zoneinfo.ZoneInfo("Europe/Warsaw")
NOW = dt.datetime(2026, 10, 8, 13, 0, tzinfo=Z).timestamp()
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def at(day, hour=12):
    return dt.datetime(2026, 10, day, hour, 0, tzinfo=Z).timestamp()


def fresh():
    """Канал с выходом каждый день в 12:00; готовый выпуск rel-R стоит на 17.10."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    c.execute("INSERT INTO pub_channels(id,tenant_id,uc,name,lang,tz,schedule,deferred,deferred_limit,"
              "horizon_days,profile_id,created,updated) VALUES(1,'t1','UC1','Канал','pl',"
              "'Europe/Warsaw',?,1,0,18,7,?,?)", (json.dumps({"*": ["12:00"]}), NOW, NOW))
    item(c, 1, at(17), "running")
    release(c, "rel-R", 1, at(17), "running", "cover_ready")
    c.commit()
    return c


def item(c, iid, slot, status, slot_mode="auto"):
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,donor_video_id,"
              "donor_title,slot_at,slot_mode,status,created,updated) VALUES(?,'t1',1,1,'u',?,'Д',?,?,?,?,?)",
              (iid, f"v{iid}", slot, slot_mode, status, NOW, NOW))


def release(c, rid, iid, slot, status, phase, hand=None):
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,project_id,status,phase,hand_confirmed,created,updated) "
              "VALUES(?,'t1',?,1,1,'UC1',?,'p1',?,?,?,?,?)", (rid, iid, slot, status, phase, hand, NOW, NOW))


def job(c, rid, slot, status, claim=None, video=None):
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,channel_url,title,"
              "video_path,slot_at,schedule_iso,status,claim_token,video_id,created,updated) "
              "VALUES(?,'t1',1,'UC1','u','T','/v.mp4',?,'x',?,?,?,?,?)",
              (rid, slot, status, claim, video, NOW, NOW))


class Svc(pub_services.PubServices):
    def _clock(self):
        return NOW


def svc():
    return Svc({"create_project": lambda *a, **k: "", "enqueue": lambda *a, **k: None,
                "project_dir": lambda *a, **k: ""})


def step(c):
    """Шаг заливки; без mp4 в тестовом проекте он падает уже ПОСЛЕ решения о выходе."""
    try:
        return svc().do_upload_pending(c, rel_of(c))
    except pub_services.ItemFailed as exc:
        return {"failed": str(exc)}


def rel_of(c, rid="rel-R"):
    return dict(c.execute("SELECT r.*, ch.deferred, ch.deferred_limit, ch.profile_id "
                          "FROM pub_releases r JOIN pub_channels ch ON ch.id=r.channel_id "
                          "WHERE r.release_id=?", (rid,)).fetchone())


def slot(c, rid):
    return c.execute("SELECT slot_at FROM pub_releases WHERE release_id=?", (rid,)).fetchone()[0]


def item_slot(c, iid):
    return c.execute("SELECT slot_at FROM pub_plan_items WHERE id=?", (iid,)).fetchone()[0]


def main() -> int:
    print("Сейчас 08.10 13:00, выход 12:00 прошёл. Ближние выходы держат выпущенные — двигаться некуда")
    c = fresh()
    for n, day in enumerate(range(9, 17), start=2):
        item(c, n, at(day), "done")
        release(c, f"rel-{n}", n, at(day), "done", "scheduled")
    out = step(c)
    check("выход не тронут — двигаться некуда", slot(c, "rel-R"), at(17))
    check("шаг пошёл дальше (не ждёт переноса)", out != {"wait": True}, True)

    print("\nСвободный выход 09.10 — готовый ролик встаёт туда")
    c = fresh()
    out = step(c)
    check("ждёт тик, задание ещё не создано", out, {"wait": True})
    check("выпуск на 09.10", slot(c, "rel-R"), at(9))
    check("строка плана тоже", item_slot(c, 1), at(9))

    print("\n09.10 держит выпуск в производстве — меняются местами")
    c = fresh()
    item(c, 2, at(9), "running")
    release(c, "rel-P", 2, at(9), "running", "render_running")
    step(c)
    check("готовый — на 09.10", (slot(c, "rel-R"), item_slot(c, 1)), (at(9), at(9)))
    check("неготовый — на 17.10", (slot(c, "rel-P"), item_slot(c, 2)), (at(17), at(17)))

    print("\n09.10 держит остановленный с мёртвым заданием — уступает, время в задании тоже сдвинуто")
    c = fresh()
    item(c, 2, at(9), "error")
    release(c, "rel-B", 2, at(9), "blocked", "upload_running")
    job(c, "rel-B", at(9), "error")
    step(c)
    check("готовый — на 09.10", slot(c, "rel-R"), at(9))
    check("остановленный — на 17.10", slot(c, "rel-B"), at(17))
    check("и его задание", c.execute("SELECT slot_at FROM pub_upload_jobs WHERE release_id='rel-B'"
                                     ).fetchone()[0], at(17))

    print("\nКого НЕ сдвигаем: живое задание, начатая загрузка, подтверждено руками, карантин без выпуска")
    cases = {
        "задание в очереди": lambda c: (release(c, "rel-H", 2, at(9), "running", "upload_pending"),
                                       job(c, "rel-H", at(9), "queued")),
        "задание взято машиной": lambda c: (release(c, "rel-H", 2, at(9), "blocked", "upload_running"),
                                           job(c, "rel-H", at(9), "error", claim="tok")),
        "у ролика есть video_id": lambda c: (release(c, "rel-H", 2, at(9), "blocked", "upload_running"),
                                            job(c, "rel-H", at(9), "error", video="abc")),
        "подтверждён руками": lambda c: release(c, "rel-H", 2, at(9), "blocked", "upload_running",
                                                hand=NOW),
        "карантин без выпуска": lambda c: None,
    }
    for label, setup in cases.items():
        c = fresh()
        item(c, 2, at(9), "error" if label == "карантин без выпуска" else "running")
        setup(c)
        # остальные дни до 17-го заняты выпущенными
        for n, day in enumerate(range(10, 17), start=3):
            item(c, n, at(day), "done")
        step(c)
        check(f"{label}: 09.10 не отдан", item_slot(c, 2), at(9))
        check(f"{label}: готовый остался на 17.10", slot(c, "rel-R"), at(17))

    print("\nВремя, закреплённое руками (slot_mode≠auto), не двигаем")
    c = fresh()
    c.execute("UPDATE pub_plan_items SET slot_mode='manual' WHERE id=1")
    step(c)
    check("выход не тронут", slot(c, "rel-R"), at(17))

    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
