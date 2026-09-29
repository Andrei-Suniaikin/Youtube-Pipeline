#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экран: «файл готов», «назначено на дату» и «вышел публично» — три разные подписи.

Требование ТЗ §11: эти три состояния показываются ОТДЕЛЬНО. Закон проекта: `done` не
означает «опубликовано», он означает только проверенное расписание. До этой правки
лента канала считала выход по часам — «слот прошёл, значит вышел» — и писала
«опубликован» роликам, которых никто не проверял; сетка месяца теми же `done` рисовала
месяц выпущенным целиком, а плашка над лентой называла их «вышел». Подтверждает выход
одна лишь поздняя проверка (`server/pub_verify.py`), и приходит он отдельным
состоянием `public` (`web/app.py` `_calendar`).

Опасность именно в слиянии подписей: оператор ведёт сотню каналов и верит экрану. Если
непроверенное называется тем же словом, что подтверждённое, приватный или удалённый
ролик молча числится вышедшим — и узнают об этом от заказчика, а не от студии.

Проверяется прямой вызов НАСТОЯЩИХ `calendarFeed()` и `monthGrid()` из `web/index.html`
(тот же приём, что в `test_calendar_feed_links_browser.py`) на своей фикстуре — без
сети и без стенда. Пять строк ленты закрывают все ветки подписи:

  A. `running`  — работа идёт, ролика ещё нет;
  B. `done`, слот в будущем, `video_id` пуст   — «файл готов, выйдет …»;
  C. `done`, слот в будущем, `video_id` есть   — «загружен, выйдет …»;
  D. `done`, слот ПРОШЁЛ, проверки не было     — «назначен …, выход не подтверждён»;
  E. `public` — «вышел публично — подтверждено», и только у неё ссылка на youtu.be.

    .venv/bin/python tests/test_release_states_browser.py
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "helpers"))

import browser_harness                                            # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


NOW = time.time()
HOUR = 3600.0


def row(state, at, video_id, past, tag):
    return {"at": at, "past": past, "beyond_horizon": False, "state": state,
            "donor_title": f"Донор {tag}", "donor_url": f"https://youtu.be/d{tag}",
            "release_id": f"rel-{tag}", "phase": "", "phase_title": "",
            "project_id": f"proj-{tag}", "project_name": "", "blocked_reason": "",
            "video_id": video_id}


D = {"calendar": [
    row("running", NOW + 1 * HOUR, "", False, "WORK"),
    row("done", NOW + 2 * HOUR, "", False, "READY"),
    row("done", NOW + 3 * HOUR, "vidUP", False, "UP"),
    row("done", NOW - 2 * HOUR, "vidWAIT", True, "WAIT"),
    row("public", NOW - 4 * HOUR, "vidOUT", True, "OUT"),
]}

SCENARIO = r"""
(async () => {
  const out = {};
  try {
    const D = __D_JSON__;
    const box = document.createElement('div');
    document.body.append(box);
    box.append(calendarFeed(D));
    const rows = [...box.querySelectorAll('.cal')];
    const find = (tag) => rows.find(r => {
      const a = r.querySelector('.what > a');
      return a && a.getAttribute('href').includes(tag);
    });
    for (const tag of ['WORK', 'READY', 'UP', 'WAIT']){
      const el = find('d' + tag);
      out[tag] = el ? {text: el.querySelector('.muted').textContent.trim(),
                       href: el.querySelector('.what > a').getAttribute('href'),
                       dot: el.querySelector('.dot').textContent,
                       dotCls: el.querySelector('.dot').className} : null;
    }
    // У подтверждённого выхода ссылка ведёт на youtu.be, а не на донора.
    const outRow = rows.find(r => {
      const a = r.querySelector('.what > a');
      return a && a.getAttribute('href').includes('vidOUT');
    });
    out.OUT = outRow ? {text: outRow.querySelector('.muted').textContent.trim(),
                        href: outRow.querySelector('.what > a').getAttribute('href'),
                        dot: outRow.querySelector('.dot').textContent,
                        dotCls: outRow.querySelector('.dot').className} : null;

    // Плашки над лентой берут подпись и цвет из тех же словарей.
    out.titlePublic = CAL_TITLE.public;
    out.titleDone = CAL_TITLE.done;
    out.clsPublic = CAL_CLS.public;
    out.clsDone = CAL_CLS.done;
    out.dotPublic = CAL_DOT.public;
    out.dotDone = CAL_DOT.done;

    // Сетка месяца: день с подтверждённым выходом и день с одним лишь расписанием.
    const z = {public: 0, done: 0, running: 0, missed: 0, planned: 0, free: 0,
               blocked: 0};
    const grid = monthGrid({'2026-09-01': {...z, public: 2},
                            '2026-09-02': {...z, done: 2}}, 'UTC');
    document.body.append(grid);
    const cell = n => [...grid.querySelectorAll('.day')].find(
      d => (d.querySelector('.n') || {}).textContent === String(n));
    const look = n => { const c = cell(n), m = c.querySelector('.cnt');
      return {mark: m ? m.textContent : '', markCls: m ? m.className : '',
              title: c.getAttribute('title') || ''}; };
    out.day1 = look(1);
    out.day2 = look(2);
  } catch (e) { out.error = String(e && e.stack || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""
SCENARIO = SCENARIO.replace("__D_JSON__", json.dumps(D, ensure_ascii=False))


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=10000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        FAILS.append("браузерная часть не выполнена")
        print()
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1

    print("Лента канала: четыре разные подписи вместо прежних двух")
    work = (res.get("WORK") or {}).get("text", "")
    ready = (res.get("READY") or {}).get("text", "")
    up = (res.get("UP") or {}).get("text", "")
    wait = (res.get("WAIT") or {}).get("text", "")
    gone = (res.get("OUT") or {}).get("text", "")
    check("работа идёт — «в работе»", work.startswith("в работе"), True)
    check("файл готов, срок впереди — «файл готов, выйдет …»",
          ready.startswith("файл готов, выйдет"), True)
    check("файл уже у YouTube — «загружен, выйдет …»",
          up.startswith("загружен, выйдет"), True)
    # Мутация-проверка: сделай `confirmed` равным `scheduled && slotPassed` в
    # `calendarFeed` — строка WAIT скажет «вышел публично», и обе проверки ниже
    # поймают разницу первыми.
    check("срок прошёл, а проверки не было — сказано прямо",
          "выход ещё не подтверждён" in wait, True)
    check("и это НЕ «вышел публично»", "вышел публично" in wait, False)
    check("подтверждённый выход — «вышел публично — подтверждено»",
          gone, "вышел публично — подтверждено")
    check("все пять подписей разные",
          len({work, ready, up, wait, gone}), 5)

    print("\n  ссылка на YouTube — только у подтверждённого и у прошедшего срока")
    check("подтверждённый ведёт на youtu.be", (res.get("OUT") or {}).get("href"),
          "https://youtu.be/vidOUT")
    # Мутация-проверка: убери `confirmed ||` из `useYoutube` — подтверждённый выход
    # поведёт на донора, и предыдущая строка поймает разницу.
    check("файл готов — ведёт на донора, не на YouTube",
          (res.get("READY") or {}).get("href"), "https://youtu.be/dREADY")
    check("залит, но срок впереди — тоже на донора",
          (res.get("UP") or {}).get("href"), "https://youtu.be/dUP")

    print("\n  знак и цвет тоже разные, а не только слова")
    check("подтверждённый — «✓»", (res.get("OUT") or {}).get("dot"), "✓")
    # Мутация-проверка: верни `done:'✓'` и `done:'s-done'` в CAL_DOT/CAL_CLS — эти две
    # строки поймают разницу: непроверенный выход снова станет неотличим от факта.
    check("непроверенный — не «✓»", (res.get("WAIT") or {}).get("dot") == "✓", False)
    check("и другого цвета", (res.get("OUT") or {}).get("dotCls")
          != (res.get("WAIT") or {}).get("dotCls"), True)

    print("\nПлашки над лентой: подтверждённое и назначенное — разными словами")
    check("подтверждённое названо выходом", res.get("titlePublic"), "вышел публично")
    # Мутация-проверка: верни `done:'вышел'` в CAL_TITLE — эта строка поймает разницу
    # первой: плашка снова объявит выходом непроверенное расписание.
    check("назначенное выходом НЕ названо",
          "выш" in (res.get("titleDone") or ""), False)
    check("назначенное названо сроком", res.get("titleDone"), "назначен на дату")
    check("и цвета у них разные", res.get("clsPublic") != res.get("clsDone"), True)

    print("\nСетка месяца: подтверждённые выходы и просто назначенные — разные числа")
    d1, d2 = res.get("day1") or {}, res.get("day2") or {}
    check("день с подтверждёнными выходами — яркое число", d1.get("mark"), "2")
    check("и подсказка говорит, что это выход", d1.get("title"), "вышло публично: 2")
    # Мутация-проверка: верни `const done = b ? b.done : 0` первым числом и `done ?
    # <span class="cnt">` — день с одним лишь расписанием снова станет выглядеть
    # вышедшим, и обе строки ниже поймают разницу.
    check("день с одним расписанием — другой знак", d2.get("mark"), "2→")
    check("и другой цвет", d2.get("markCls") != d1.get("markCls"), True)
    check("а подсказка называет вещи своими именами", d2.get("title"),
          "назначено, выход не подтверждён: 2")

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
