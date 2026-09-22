#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Дни канала в форме настроек — проверка в браузере на данных стенда (18.09.2026).

Жалоба владельца: отмеченные дни «не сохранились». Две разные причины, обе тут:
  * ежедневное расписание хранится как `{"*": [...]}` — галочки дней должны стоять все;
  * выбранные дни без времени сохранять нельзя (это проверяет `tests/test_web_pult.py`).

Требует стенд и Chrome:

    VS_PLATFORM_BASE=http://127.0.0.1:8082 VS_DEV_PASSWORD=… \
    .venv/bin/python tests/test_channel_days_browser.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "helpers"))

import live_page                                                  # noqa: E402

PLATFORM = os.environ.get("VS_PLATFORM_BASE", "http://127.0.0.1:8082")
LOGIN = os.environ.get("VS_DEV_LOGIN", "dev")
PASSWORD = os.environ.get("VS_DEV_PASSWORD", "dev-local-only")
DB = os.environ.get("VS_DEV_DB", os.path.join(live_page.BASE, "dev", "vsgen.dev.db"))
TENANT = os.environ.get("VS_DEV_TENANT", "dev")
FAILS = []

MEASURE = r"""
setTimeout(() => {
  const boxes = [...document.querySelectorAll('#editbox span.langs label')];
  document.documentElement.setAttribute('data-result', JSON.stringify({
    days: boxes.map(l => l.dataset.day),
    checked: boxes.filter(l => l.querySelector('input').checked).map(l => l.dataset.day),
    times: (document.querySelector('#f-times') || {}).value || '',
  }));
}, 5000);
"""


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def schedule_of(cid: int):
    c = sqlite3.connect(DB, timeout=5)
    try:
        return json.loads(c.execute("SELECT schedule FROM pub_channels WHERE id=?",
                                    (cid,)).fetchone()[0] or "{}")
    finally:
        c.close()


def set_schedule(cid: int, sched: dict) -> None:
    """Прямо в базу: тест проверяет ОТРИСОВКУ уже сохранённого расписания, а не запись."""
    c = sqlite3.connect(DB, timeout=5)
    try:
        c.execute("UPDATE pub_channels SET schedule=? WHERE id=?",
                  (json.dumps(sched, ensure_ascii=False), cid))
        c.commit()
    finally:
        c.close()


def main() -> int:
    cid = 1
    before = schedule_of(cid)
    sid = live_page.login(PLATFORM, LOGIN, PASSWORD)
    base, stop = live_page.serve(PLATFORM, sid, MEASURE)
    try:
        print("Ежедневное расписание («*») — отмечены все дни")
        set_schedule(cid, {"*": ["10:00"]})
        res = live_page.chrome_dump(f"{base}/studio#/channel/{cid}/nastroyki")
        if "error" in res:
            check("форма отрисована", res["error"], None)
        else:
            check("дней в форме семь", len(res["days"]), 7)
            check("отмечены все", res["checked"], res["days"])
            check("время подставлено", res["times"], "10:00")

        print("\nРасписание по дням — отмечены ровно эти дни")
        set_schedule(cid, {"mon": ["09:30"], "fri": ["09:30"]})
        res = live_page.chrome_dump(f"{base}/studio#/channel/{cid}/nastroyki")
        if "error" not in res:
            check("отмечены выбранные дни", sorted(res["checked"]), ["fri", "mon"])
            check("время подставлено", res["times"], "09:30")

        print("\nБез расписания — не отмечено ничего")
        set_schedule(cid, {})
        res = live_page.chrome_dump(f"{base}/studio#/channel/{cid}/nastroyki")
        if "error" not in res:
            check("галочек нет", res["checked"], [])
            check("время пустое", res["times"], "")
    finally:
        set_schedule(cid, before)
        stop()
    check("расписание канала возвращено как было", schedule_of(cid), before)

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
