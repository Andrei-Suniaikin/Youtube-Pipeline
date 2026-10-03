#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Страница выпуска: подсвечены «Проекты», лента событий — от новых к старым (03.10.2026).

Владелец: «когда захожу в проект, в топбаре показывается, что я на вкладке с каналами»
и «логи во вкладке проекта — от новых к старым, для понимания текущей ситуации».

Страница одного выпуска (`#/release/{id}`) — это работа над роликом, человек заходит в
неё как в «проект». В таблице маршрутов она была приписана к «Каналам» — отсюда ложная
подсветка. Список выпусков остаётся в «Каналах»: там это про канал.

Лента «Что происходило» шла от старых к новым, и текущее состояние оказывалось в самом
низу длинной ленты. Теперь новое сверху — у событий и у журнала одинаково.

    .venv/bin/python tests/test_release_page_tab_and_order_browser.py
"""
from __future__ import annotations

import os
import sys

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


SCENARIO = r"""
(async () => {
  const out = {errs: []};
  addEventListener('error', e => out.errs.push(e.message));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    ME = {name: 'хозяин', role: 'admin'};
    const r = {release_id: 'rel-1', channel_id: 1, channel: 'Первый', lang: 'RU',
      donor_title: 'Донор', donor_url: 'https://youtu.be/x', slot_at: 1790000000,
      phase: 'project_ready', phase_title: 'проект готов', phase_index: 3,
      phase_total: 9, status: 'running', attempts: 0, blocked_reason: '', bar: [],
      video_id: '', project_id: 'p1', idle_sec: 60, hand_confirmed: 0, hand_by: ''};
    window.api = async (p) => {
      if (p === '/ui/me') return ME;
      if (p.startsWith('/ui/releases/')) return {release: r, stages: [],
        events: [{created: 1790000100, phase_title: 'первое событие', video_id: '', repeats: 1},
                 {created: 1790000300, phase_title: 'второе событие', video_id: '', repeats: 1}],
        log: [{ts: 1790000200, level: 'info', step: 'ролик', message: 'старая запись', repeats: 1, last_ts: 1790000200},
              {ts: 1790000400, level: 'info', step: 'ролик', message: 'НОВЕЙШАЯ запись', repeats: 1, last_ts: 1790000400}]};
      if (p === '/ui/releases') return {releases: [r]};
      if (p === '/ui/channels') return {channels: []};
      return {};
    };
    window.post = async () => ({ok: true});
    addEventListener('hashchange', render);

    location.hash = '#/release/rel-1';
    await sleep(400);
    const on = [...document.querySelectorAll('nav .tab.on')].map(a => a.id);
    out.подсвечено = on;
    const evs = [...document.querySelectorAll('.card .ev')].map(e => e.textContent.replace(/\s+/g, ' ').trim());
    out.лента = evs;
    out.первая = evs[0] || '';
    out.последняя = evs[evs.length - 1] || '';

    // Список выпусков — по-прежнему «Каналы»: там это про канал, не про ролик.
    location.hash = '#/releases';
    await sleep(300);
    out.подсвеченоСписок = [...document.querySelectorAll('nav .tab.on')].map(a => a.id);
  } catch (e) { out.errs.push('scenario: ' + (e && e.message || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=12000)
    if res.get("error"):
        print("ПРОПУЩЕНО:", res["error"])
        return 0
    check("ошибок на странице нет", res.get("errs"), [])

    print("Страница выпуска подсвечивает «Проекты», а не «Каналы»")
    check("подсвечена ровно одна вкладка", len(res.get("подсвечено") or []), 1)
    check("и это «Проекты»", res.get("подсвечено"), ["tab-projects"])
    check("а список выпусков остался в «Каналах»", res.get("подсвеченоСписок"), ["tab-channels"])

    print("\nЛента «Что происходило» — от новых к старым")
    лента = res.get("лента") or []
    check("все четыре записи на месте", len(лента), 4)
    check("первой стоит новейшая запись журнала", "НОВЕЙШАЯ" in res.get("первая", ""), True)
    check("последним — самое старое событие", "первое событие" in res.get("последняя", ""), True)
    # События и журнал — две ленты подряд, каждая от новых к старым.
    события = [x for x in лента if "событие" in x]
    записи = [x for x in лента if "запись" in x]
    check("события: новое раньше старого", события and "второе" in события[0], True)
    check("журнал: новое раньше старого", записи and "НОВЕЙШАЯ" in записи[0], True)

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
