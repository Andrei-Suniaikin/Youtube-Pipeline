#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экран канала через НАСТОЯЩИЙ маршрут `#/channel/<id>` (пункт 1, фаза 3, 22.09.2026).

`test_calendar_feed_links_browser.py` уже проверил `calendarFeed()` саму по себе, на
рукописной фикстуре, вызванную напрямую. Здесь — другое звено: та же лента, но
собранная НАСТОЯЩИМ `web.app.ui_channel()` на временной базе (реальные `pub_releases`,
реальные `projects`, реальный join на `video_id`/`project_name`), доставленная через
подлинный путь `location.hash = '#/channel/<id>'` → `hashchange` → `render()` →
`viewChannel()` → `channelReleases()` → `calendarFeed()` — то есть тот самый код,
которым владелец пользуется в бою, а не сокращённый путь мимо роутера.

Проверяется:

  1. `ui_channel()` кладёт в ответ настоящие `video_id`/`project_name`, взятые из
     `pub_releases`/`projects` (FR-12), а не пустые заглушки.
  2. Служебное поле `_plan_item_id` (используется только сервером для ленивой
     дозагрузки, см. `test_donor_title_links.py`) не просачивается наружу ни в одном
     ряду настоящего ответа — включая ряды "за горизонтом" (`beyond_horizon=True`),
     которые получают обогащение тем же кодом (Edge Case #7 спеки).
  3. Через настоящий роутер строка с `project_id` кликабельна и ведёт на
     `#/project/<id>`, а название и подпись в DOM берутся из тех самых
     `video_id`/`project_name`, что вернул сервер — не из мимо-фикстуры теста.

`window.api` подменён РОВНО на один настоящий ответ `ui_channel()`; `ME` задан,
`addEventListener('hashchange', render)` — тот же приём, что и в
`test_channel_created_lower_bound_browser.py` (фаза 2).

    VS_TEST_NO_NET=1 .venv/bin/python tests/test_channel_route_video_project_browser.py
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

# ───────── фикстура: настоящий /ui/channels/{id}, реальные projects/pub_releases ─────────
work = tempfile.mkdtemp(prefix="channel-route-video-project-")
db_path = os.path.join(work, "vsgen.db")
os.environ["VS_DEV_DB"] = db_path
os.environ.setdefault("VS_DEV_TENANT", "dev")

from pub_schema import ensure_pub_schema                          # noqa: E402
_c0 = sqlite3.connect(db_path)
_c0.row_factory = sqlite3.Row
ensure_pub_schema(_c0)
# `ensure_pub_schema` заводит только `pub_*`-таблицы — `projects` в этой схеме кабинета
# (`server/main.py:2546`), не пульта. Join на имя проекта (`web/app.py`, `_calendar`)
# читает её напрямую, значит её нужно завести и здесь, тем же DDL, что и в бою.
_c0.execute("CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, tenant_id TEXT, "
            "name TEXT, dir_path TEXT, status TEXT, created REAL, updated REAL)")
_c0.commit()
_c0.close()

import web.app as pult                                            # noqa: E402
import pub_ui as U                                                # noqa: E402

NOW = time.time()
CREATED = NOW - 30 * DAY
SCHEDULE = {"*": ["09:00"]}

_c = sqlite3.connect(db_path)
_c.row_factory = sqlite3.Row
CID = U.create_channel(_c, "dev", "", "Канал с настоящими выпусками", now=CREATED)
_c.execute("UPDATE pub_channels SET schedule=?, tz=? WHERE id=?",
           (json.dumps(SCHEDULE), "UTC", CID))

_c.execute("INSERT INTO projects(id,tenant_id,name,status,created,updated) "
           "VALUES('proj-past','dev','Проект прошлого выпуска','draft',?,?)", (NOW, NOW))
_c.execute("INSERT INTO projects(id,tenant_id,name,status,created,updated) "
           "VALUES('proj-future','dev','Проект будущего выпуска','draft',?,?)", (NOW, NOW))

# Строка A — слот уже прошёл, выпуск "done", есть video_id и project_id (не
# выровнена по расписанию нарочно: код обязан показать её через "за горизонтом",
# Edge Case #7 — и обогатить video_id/project_name тем же путём, что и обычные ряды).
_c.execute("INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,donor_title,"
           "slot_at,status,created,updated) VALUES('dev',?,'https://youtu.be/donorA',"
           "'Донор A',?,'frozen',?,?)", (CID, NOW - 2 * DAY, NOW, NOW))
_ITEM_A = _c.execute("SELECT id FROM pub_plan_items WHERE donor_url='https://youtu.be/donorA'"
                     ).fetchone()["id"]
_c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
           "channel_id,uc,slot_at,project_id,status,phase,video_id,attempts,created,updated) "
           "VALUES('rel-a','dev',?,1,?,'',?,'proj-past','done','done','VIDEOAAA',0,?,?)",
           (_ITEM_A, CID, NOW - 2 * DAY, NOW, NOW))

# Строка B — слот ещё впереди, выпуск уже "done" (расписание подтверждено, залить ещё
# не наступило время — именно та ситуация, которую CLAUDE.md запрещает подписывать
# «опубликован»), с video_id и project_id.
_c.execute("INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,donor_title,"
           "slot_at,status,created,updated) VALUES('dev',?,'https://youtu.be/donorB',"
           "'Донор B',?,'frozen',?,?)", (CID, NOW + 2 * DAY, NOW, NOW))
_ITEM_B = _c.execute("SELECT id FROM pub_plan_items WHERE donor_url='https://youtu.be/donorB'"
                     ).fetchone()["id"]
_c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
           "channel_id,uc,slot_at,project_id,status,phase,video_id,attempts,created,updated) "
           "VALUES('rel-b','dev',?,1,?,'',?,'proj-future','done','done','VIDEOBBB',0,?,?)",
           (_ITEM_B, CID, NOW + 2 * DAY, NOW, NOW))

# Строка C — просто planned, без release: video_id/project_id обязаны остаться пустыми.
_c.execute("INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,donor_title,"
           "slot_at,status,created,updated) VALUES('dev',?,'https://youtu.be/donorC',"
           "'Донор C',?,'planned',?,?)", (CID, NOW + 4 * DAY, NOW, NOW))

_c.commit()
_c.close()

# Настоящий вызов ручки (`ui_channel` стала `async def` в фазе 3 — дозагрузка названий
# пулом потоков). У всех трёх строк донор уже назван, backfill'у дотягивать нечего.
# `history=14` — то самое число, что жёстко зашито в `viewChannel()` (`web/index.html`,
# `api('/ui/channels/' + id + '?history=14')`), чтобы Python- и браузерная часть теста
# смотрели на ОДИН и тот же настоящий ответ, а не на два разных вызова случайно
# совпавшей формы.
D = asyncio.run(pult.ui_channel(CID, history=14))

# ─────────────────────── проверки на самих данных, ДО браузера ───────────────────────
ROW_A = next((r for r in D["calendar"] if r["release_id"] == "rel-a"), None)
ROW_B = next((r for r in D["calendar"] if r["release_id"] == "rel-b"), None)
ROW_C = next((r for r in D["calendar"] if r["donor_url"] == "https://youtu.be/donorC"), None)

if ROW_A is None or ROW_B is None or ROW_C is None:
    print("  FAIL фикстура не собралась — строки A/B/C не найдены в calendar")
    FAILS.append("фикстура ui_channel() не вернула ожидаемые ряды")

if ROW_A is not None:
    check("ряд A: video_id из pub_releases", ROW_A.get("video_id"), "VIDEOAAA")
    check("ряд A: project_name из projects (join по project_id)",
          ROW_A.get("project_name"), "Проект прошлого выпуска")
    check("ряд A: помечен 'за горизонтом' — расписание не совпадает по времени "
          "(тот самый путь Edge Case #7)", ROW_A.get("beyond_horizon"), True)
if ROW_B is not None:
    check("ряд B: video_id из pub_releases", ROW_B.get("video_id"), "VIDEOBBB")
    check("ряд B: project_name из projects", ROW_B.get("project_name"), "Проект будущего выпуска")
if ROW_C is not None:
    check("ряд C (planned, без release): video_id пуст", ROW_C.get("video_id"), "")
    check("ряд C: project_name пуст", ROW_C.get("project_name"), "")

# Мутация-проверка (данные, без браузера): убери снятие `_plan_item_id` в
# `_strip_internal`/вызов `[_strip_internal(r) for r in cal]` в `ui_channel` — эта
# проверка поймает разницу первой: служебное поле окажется в каждом ряду настоящего
# ответа, включая ряды "за горизонтом".
for _r in D["calendar"]:
    if "_plan_item_id" in _r:
        FAILS.append(f"'_plan_item_id' просочилось наружу в ряду at={_r.get('at')!r}")
        print(f"  FAIL '_plan_item_id' в наружном ответе: {_r!r}")
if not any("_plan_item_id" in _r for _r in D["calendar"]):
    print("  OK   ни один ряд (включая 'за горизонтом') не содержит служебный "
          "'_plan_item_id'")

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
    const rows = [...document.querySelectorAll('.cal')];
    out.calRows = rows.length;

    const byHref = (needle) => rows.find(r => {
      const a = r.querySelector('.what > a');
      return a && a.getAttribute('href').includes(needle);
    });

    const rowA = byHref('VIDEOAAA') || byHref('donorA');
    if (rowA){
      const a = rowA.querySelector('.what > a');
      out.rowA_href = a ? a.getAttribute('href') : null;
      out.rowA_muted = (rowA.querySelector('.muted') || {}).textContent || '';
      out.rowA_clickable = rowA.classList.contains('clickable');
      out.rowA_dataProject = rowA.dataset.project;
    }
    const rowB = byHref('donorB');
    if (rowB){
      const a = rowB.querySelector('.what > a');
      out.rowB_href = a ? a.getAttribute('href') : null;
      out.rowB_muted = (rowB.querySelector('.muted') || {}).textContent || '';
    }

    // Клик ВНЕ ссылки (по <time>) по строке A — переход на #/project/proj-past,
    // тем самым проектом, что настоящий сервер вернул в project_id (не фикстура теста).
    location.hash = '';
    if (rowA) rowA.querySelector('time').dispatchEvent(
      new MouseEvent('click', {bubbles: true}));
    out.afterRowAClick_hash = location.hash;
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
        print("  Данные для рендера уже проверены Python-частью выше (video_id/"
              "project_name/отсутствие утечки _plan_item_id).")
    else:
        print("Экран канала через настоящий #/channel/<id>: video_id, project_name, клик")
        check("сценарий выполнен без ошибок", res.get("error"), None)
        check("заголовок — тот самый канал",
              "Канал с настоящими выпусками" in (res.get("title") or ""), True)
        check("строк ленты столько же, сколько в настоящем ответе сервера",
              res.get("calRows"), len(D["calendar"]))

        print("\nряд A — слот прошёл, video_id/project_id настоящие, из сервера")
        check("ссылка ведёт на youtu.be/VIDEOAAA (слот A уже прошёл)",
              res.get("rowA_href"), "https://youtu.be/VIDEOAAA")
        check("подпись — «опубликован»", "опубликован" in (res.get("rowA_muted") or ""), True)
        check("подпись содержит имя проекта из настоящего project_name",
              "Проект прошлого выпуска" in (res.get("rowA_muted") or ""), True)
        check("строка кликабельна", res.get("rowA_clickable"), True)
        check("data-project — настоящий project_id с сервера (не выдумка теста)",
              res.get("rowA_dataProject"), "proj-past")

        print("\nряд B — слот ещё впереди, done, но 'опубликован' говорить рано")
        check("ссылка ведёт на донора, НЕ на youtu.be (слот B ещё впереди)",
              res.get("rowB_href"), "https://youtu.be/donorB")
        check("подпись честная — «загружен, выйдет …»",
              "загружен, выйдет" in (res.get("rowB_muted") or ""), True)
        check("и не «опубликован» раньше времени",
              "опубликован" in (res.get("rowB_muted") or ""), False)
        check("подпись содержит имя проекта будущего выпуска",
              "Проект будущего выпуска" in (res.get("rowB_muted") or ""), True)

        print("\nклик по настоящему ряду A ведёт в настоящий project_id")
        check("переход на #/project/proj-past", res.get("afterRowAClick_hash"),
              "#/project/proj-past")

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
