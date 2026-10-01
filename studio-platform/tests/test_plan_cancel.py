#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Снятие ссылки с плана: либо снято, либо сказано почему (01.10.2026).

Жалоба владельца: «нажимает в попапе „да“, страница перезагружается, а ролик на месте».
Пульт отвечал `200 {"ok": true}` в том числе тогда, когда не убирал ничего, — он
пересылал нажатие формой в кабинет и считал успехом сам факт ответа, а кабинет на отказ
отвечает СТРАНИЦЕЙ с кодом 200 (она писалась для браузера). Ручку чинит
`tests/test_web_pult.py`, там она проверяется на живом стенде.

Здесь — контракт функции, на котором эта починка стоит: **`cancel_item` либо снимает
строку, либо бросает ValueError с текстом для человека. Третьего не бывает.** Пока это
верно, «не бросила — значит сняла» в вызывающем коде не ложь. Стоит появиться ветке,
которая молча ничего не делает, — и жалоба вернётся в том же виде, но уже из другого
места, и искать её будут заново.

Проверяются обе стороны контракта и всё, что `cancel_item` обязана сделать ПОПУТНО:
ждущее задание заливки гасится (иначе нода заберёт его позже и зальёт ролик, которого в
плане уже нет), а выпуск уводится в `cancelled` — снять строку и оставить выпуск
работать было бы худшим из исходов.

    .venv/bin/python tests/test_plan_cancel.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_schema                                                  # noqa: E402
import pub_tags                                                    # noqa: E402

NOW = 1_800_000_000.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def check_has(label, text, piece):
    if piece.lower() in str(text or "").lower():
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        искали: {piece!r}\n        в:      {text!r}")
        FAILS.append(label)


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
              "deferred_limit,created,updated) "
              "VALUES('t1','UC1','Канал','ru','Europe/Warsaw','{}',1,0,?,?)", (NOW, NOW))
    c.commit()
    return c


def item(c, pid, slot=None, status="planned"):
    """Строка плана. `slot=None` — та самая «ждёт даты» из жалобы."""
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
              "donor_title,slot_at,status,created,updated) "
              "VALUES(?,'t1',1,1,?,'Донор',?,?,?,?)",
              (pid, f"https://youtu.be/d{pid}", slot, status, NOW, NOW))
    c.commit()
    return pid


def release(c, pid, phase="upload_pending", job_status=None, events=()):
    rid = f"rel-{pid}"
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
              "plan_item_version,channel_id,uc,slot_at,status,phase,created,updated) "
              "VALUES(?,'t1',?,1,1,'UC1',?,'running',?,?,?)",
              (rid, pid, NOW, phase, NOW, NOW))
    for seq, ph in enumerate(events, start=1):
        c.execute("INSERT INTO pub_release_events(release_id,seq,phase,created) "
                  "VALUES(?,?,?,?)", (rid, seq, ph, NOW))
    if job_status:
        c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,"
                  "channel_url,title,video_path,slot_at,status,created,updated) "
                  "VALUES(?,'t1',1,'UC1','http://c','Ролик','/v.mp4',?,?,?,?)",
                  (rid, NOW, job_status, NOW, NOW))
    c.commit()
    return rid


def статус(c, pid):
    r = c.execute("SELECT status FROM pub_plan_items WHERE id=?", (pid,)).fetchone()
    return r["status"] if r else ""


def main() -> int:
    print("Ссылка без даты снимается — это случай из жалобы")
    c = fresh()
    item(c, 1)
    check("вернулся пустой номер выпуска (его и не было)",
          pub_tags.cancel_item(c, 1, NOW), "")
    check("строка снята", статус(c, 1), "cancelled")

    print("\nСо ссылкой снимается и начатая по ней работа")
    # Снять строку и оставить выпуск работать было бы худшим из вариантов: из таблицы
    # ролик пропал, а система продолжает его делать и потом зальёт.
    c = fresh()
    item(c, 2)
    rid = release(c, 2, phase="media_ready", job_status="queued")
    check("вернулся номер выпуска", pub_tags.cancel_item(c, 2, NOW), rid)
    check("строка снята", статус(c, 2), "cancelled")
    check("выпуск снят",
          c.execute("SELECT status FROM pub_releases WHERE release_id=?",
                    (rid,)).fetchone()["status"], "cancelled")
    job = c.execute("SELECT status, error FROM pub_upload_jobs WHERE release_id=?",
                    (rid,)).fetchone()
    check("ждущее задание заливки погашено", job["status"], "cancelled")
    check_has("и сказано, почему", job["error"], "убран владельцем")

    print("\nЗагрузка уже начиналась — отказ словами, и ничего не тронуто")
    c = fresh()
    item(c, 3)
    rid = release(c, 3, phase="upload_running",
                  events=("upload_pending", "upload_running"))
    try:
        pub_tags.cancel_item(c, 3, NOW)
        check("отказ", "не отказала", "ValueError")
    except ValueError as exc:
        check("отказ", "ValueError", "ValueError")
        check_has("и это текст для человека", exc, "убирать поздно")
        check_has("названа причина", exc, "уже заливается на канал")
    check("строка осталась в плане", статус(c, 3), "planned")
    check("и выпуск не тронут",
          c.execute("SELECT status FROM pub_releases WHERE release_id=?",
                    (rid,)).fetchone()["status"], "running")

    print("\nМашина взяла ролик в работу — тоже отказ")
    c = fresh()
    item(c, 4)
    release(c, 4, phase="upload_pending", job_status="running")
    try:
        pub_tags.cancel_item(c, 4, NOW)
        check("отказ", "не отказала", "ValueError")
    except ValueError as exc:
        check("отказ", "ValueError", "ValueError")
        check_has("и это текст для человека", exc, "убирать поздно")
    check("строка осталась в плане", статус(c, 4), "planned")

    print("\nНесуществующая строка — отказ, а не тихое «готово»")
    c = fresh()
    try:
        pub_tags.cancel_item(c, 999, NOW)
        check("отказ", "не отказала", "ValueError")
    except ValueError as exc:
        check("отказ", "ValueError", "ValueError")
        check_has("сказано, что строки нет", exc, "не найдена")

    print("\nКОНТРАКТ: либо сняла, либо бросила — третьего не бывает")
    # На этом стоит вся починка ручки: вызывающий имеет право считать «не бросила —
    # значит сняла». Ветка, которая молча ничего не делает, вернула бы жалобу владельца
    # в том же виде, но уже из другого места.
    случаи = [
        ("без даты", dict(), None),
        ("с выпуском в производстве", dict(phase="media_ready"), None),
        ("с ждущим заданием", dict(phase="upload_pending", job_status="queued"), None),
        ("заливка началась", dict(phase="upload_running",
                                  events=("upload_pending", "upload_running")), ValueError),
        ("машина взяла в работу", dict(phase="upload_pending", job_status="running"),
         ValueError),
    ]
    нарушения = []
    for n, (имя, как, ждём) in enumerate(случаи, start=10):
        c = fresh()
        item(c, n)
        if как:
            release(c, n, **как)
        try:
            pub_tags.cancel_item(c, n, NOW)
            брошено = None
        except ValueError:
            брошено = ValueError
        снято = статус(c, n) == "cancelled"
        # Нарушение контракта: не бросила И не сняла.
        if брошено is None and not снято:
            нарушения.append(f"{имя}: молча ничего не сделала")
        if брошено is not None and снято:
            нарушения.append(f"{имя}: бросила, но строку всё равно сняла")
        if ждём is ValueError and брошено is None:
            нарушения.append(f"{имя}: должна была отказать")
        if ждём is None and брошено is not None:
            нарушения.append(f"{имя}: отказала там, где снимать можно")
    check("ни одного нарушения контракта", нарушения, [])

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
