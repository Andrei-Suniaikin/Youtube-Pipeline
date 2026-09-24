#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экран канала: месячная сетка и лента без «пропусков» до его завода (пункт 2, фаза 2).

`test_channel_created_lower_bound.py` уже проверил данные — `_calendar`/`past_slots`
и ответ `/ui/channels/{id}` сами по себе. Здесь другое звено: тот же настоящий ответ,
пройдя весь путь до настоящего `web/index.html` (`viewChannel` → `monthGrid` +
`calendarFeed`), не рисует пропуск (`.day.miss`, `.cal.miss`, красная точка
`.dot.s-error`) там, где канала ещё не было.

Фикстура — НАСТОЯЩИЙ вызов `web.app.ui_channel()` на временной базе (та же схема,
что и в бою, `ensure_pub_schema`), а не набранный вручную JSON: если фикс когда-нибудь
разъедется, тест ловит расхождение на реальном ответе сервера, а не на своей
выдумке. Сети и стенда нет: `window.api` подменён ровно на этот один ответ.

    .venv/bin/python tests/test_channel_created_lower_bound_browser.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(HERE, "helpers"))

import browser_harness                                            # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


DAY = 86400.0

# ───────── фикстура: настоящий /ui/channels/{id}, без сети и без стенда ─────────
work = tempfile.mkdtemp(prefix="channel-created-browser-")
db_path = os.path.join(work, "vsgen.db")
os.environ["VS_DEV_DB"] = db_path
os.environ.setdefault("VS_DEV_TENANT", "dev")

from pub_schema import ensure_pub_schema                          # noqa: E402
_c0 = sqlite3.connect(db_path)
_c0.row_factory = sqlite3.Row
ensure_pub_schema(_c0)
_c0.commit()
_c0.close()

import web.app as pult                                            # noqa: E402
import pub_ui as U                                                # noqa: E402

NOW = time.time()
# Заведён двое суток и час назад — расписание при этом рвётся на 09:00/21:00
# каждый день: без фикса `past_slots` насочиняла бы «пропуски» на все 14 суток
# истории, которых канал не застал.
CREATED = NOW - 2 * DAY - 3600
SCHEDULE = {"*": ["09:00", "21:00"]}

_c = sqlite3.connect(db_path)
_c.row_factory = sqlite3.Row
CID = U.create_channel(_c, "dev", "", "Свежий канал (браузер)", now=CREATED)
_c.execute("UPDATE pub_channels SET schedule=?, tz=? WHERE id=?",
           (json.dumps(SCHEDULE), "UTC", CID))
_c.commit()
_c.close()

# Настоящий вызов ручки — как её вызвал бы GET /ui/channels/{id}?history=14, тем же
# кодом, что и в бою (`web/app.py:ui_channel`), только без HTTP-слоя. Фаза 3
# (22.09.2026) сделала `ui_channel` `async def` (дозагрузка названий доноров пулом
# потоков) — вызов через `asyncio.run`, канал только что заведён и без единой ссылки
# в плане, так что дозагрузке всё равно нечего тянуть, сети это не касается.
D = asyncio.run(pult.ui_channel(CID, history=14))

MISSED_ROWS = [r for r in D["calendar"] if r["state"] == "missed"]
# Мутация-проверка (уровень данных, работает и без Chrome в окружении): убери
# `created=ch["created"]` из вызова `past_slots` внутри `_calendar` (web/app.py) —
# эта проверка первой поймает разницу: у канала младше трёх суток пропусков от
# 14-суточной истории набралось бы под три десятка, а не единицы.
if len(MISSED_ROWS) > 6:
    print(f"  FAIL данные уже сломаны до браузера: {len(MISSED_ROWS)} «пропусков» "
          "у канала младше трёх суток — фикстура берётся как есть, дальше упадёт рендер")
    FAILS.append("ui_channel() вернул слишком много 'missed' для свежего канала")
for r in MISSED_ROWS:
    if r["at"] < CREATED - 1:
        FAILS.append(f"'missed' раньше created: at={r['at']} < created={CREATED}")
        print(f"  FAIL 'missed'-ряд раньше created: at={r['at']!r} < created={CREATED!r}")

MISSED_DAYS = [day for day, box in D["by_day"].items() if box.get("missed")]

SCENARIO = r"""
(async () => {
  const out = {};
  try {
    const D = __D_JSON__;
    const CID = __CID__;
    window.api = async (p) => {
      if (p === '/ui/me') return {name: 'dev', role: 'user'};
      if (p === ('/ui/channels/' + CID + '?history=14')) return D;
      return {};
    };
    ME = {name: 'dev', role: 'user'};
    addEventListener('hashchange', render);
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    location.hash = '#/channel/' + CID;
    await sleep(300);

    out.title = (document.querySelector('#app h1') || {}).textContent || '';
    out.monthMiss = document.querySelectorAll('.day.miss').length;
    out.feedMiss = document.querySelectorAll('.cal.miss').length;
    out.feedRedDots = document.querySelectorAll('.cal .dot.s-error').length;
    out.calRows = document.querySelectorAll('.cal').length;
    out.hasMonthGrid = !!document.querySelector('#months .mon');
    out.hasFeed = !!document.querySelector('.today');
  } catch (e) { out.error = String(e && e.stack || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""
SCENARIO = (SCENARIO.replace("__D_JSON__", json.dumps(D, ensure_ascii=False))
                    .replace("__CID__", json.dumps(CID)))


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=10000)
    if res.get("error") and "Chrome не найден" in str(res.get("error")):
        print("  ПРОПУЩЕНО (браузер): " + res["error"])
        print("  Данные для рендера уже проверены Python-частью выше.")
    else:
        print("Экран канала: свежий канал, месячная сетка и лента")
        check("сценарий выполнен без ошибок", res.get("error"), None)
        check("заголовок — тот самый канал", "Свежий канал" in (res.get("title") or ""), True)
        check("сетка есть (по_day непустой)", res.get("hasMonthGrid"), True)
        check("лента есть (черта «сейчас» нарисована)", res.get("hasFeed"), True)
        check("строк ленты столько же, сколько в ответе сервера",
              res.get("calRows"), len(D["calendar"]))
        # Мутация-проверка: реверт границы по created в `past_slots`/`_calendar`
        # добавляет в D фиктивные 'missed' задолго до создания канала — тогда и
        # `monthMiss`, и `feedMiss`, и `feedRedDots` подскочат заметно выше того,
        # что канал младше трёх суток мог реально «пропустить» (см. верхнюю
        # Python-проверку MISSED_ROWS/MISSED_DAYS — числа здесь обязаны совпасть
        # с ней один в один, потому что рисуется тот же самый D).
        check("красных дней в сетке ровно столько, сколько дней с пропуском в by_day",
              res.get("monthMiss"), len(MISSED_DAYS))
        check("красных строк в ленте ровно столько, сколько 'missed' в calendar",
              res.get("feedMiss"), len(MISSED_ROWS))
        check("красных точек в ленте столько же",
              res.get("feedRedDots"), len(MISSED_ROWS))
        check("и это небольшое число — не годы придуманной истории",
              res.get("feedMiss") <= 6, True)

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
