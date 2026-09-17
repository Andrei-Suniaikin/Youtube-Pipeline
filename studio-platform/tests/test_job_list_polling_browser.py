#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Опрос списков задач текста и обложек на экране проекта — поведение в браузере.

Две ошибки Ф4 (цикл 3) прошли статические проверки строк, поэтому здесь проверяется
то, что видит человек: настоящий `web/index.html` в headless Chrome, `api()` подменён,
время виртуальное. Без стенда и сети.

1. Пока задача идёт, прогресс и подпись обновляются (раньше застывали до смены состояния).
2. Запоздавший ответ старого экрана не гасит опрос нового (раньше гасил по общему ключу).
3. Два наложившихся запуска опроса не оставляют «вечный» таймер после ухода с экрана.

    .venv/bin/python tests/test_job_list_polling_browser.py
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


# Сервисный помощник сценария: `api` подменяется функцией, которая считает вызовы и
# отдаёт то, что сейчас положено в `SERVER` (по источнику), с задержкой `DELAY[src]`.
COMMON = r"""
const sleep = ms => new Promise(r => setTimeout(r, ms));
const CALLS = {text: 0, covers: 0};
const SERVER = {text: [], covers: []};
const DELAY = {text: 0, covers: 0};
api = async (path) => {
  const src = path.startsWith('/ui/text') ? 'text' : path.startsWith('/ui/covers') ? 'covers' : '';
  if (!src) throw new Error('неожиданный запрос ' + path);
  CALLS[src]++;
  const snapshot = JSON.parse(JSON.stringify(SERVER[src]));
  if (DELAY[src]) await sleep(DELAY[src]);
  return {jobs: snapshot};
};
const job = (id, state, progress, detail) =>
  ({id, title: 'задача ' + id, kind: 'rewrite', langs: ['ru'], state, progress, detail, created: 1});
const newBox = () => { const b = document.createElement('div'); document.body.append(b); return b; };
"""

SCENARIO = COMMON + r"""
(async () => {
  const out = {};
  try {
    location.hash = '#/project/p1/scenario';
    await sleep(10);

    // 1. Прогресс внутри running
    SERVER.text = [job('j1', 'running', 0.1, 'часть 1 из 10')];
    const box1 = newBox();
    await refreshScenarioJobs(box1, 'p1', null);
    const cell = () => box1.querySelector('tr[data-job="j1"] .jl-state');
    out.p_first = cell().textContent.includes('часть 1 из 10');
    SERVER.text = [job('j1', 'running', 0.6, 'часть 6 из 10')];
    await sleep(3500);
    out.p_detail = cell().textContent.includes('часть 6 из 10');
    out.p_bar = (cell().querySelector('.bar i') || {}).style ? cell().querySelector('.bar i').style.width : '';
    SERVER.text = [job('j1', 'done', 1, '')];
    await sleep(3500);
    out.p_done = cell().textContent.includes(STATE_TITLE.done);
    const afterDone = CALLS.text;
    await sleep(10000);
    out.p_stopped = CALLS.text === afterDone;
    clearAllLogTimers(); box1.remove();

    // 2. Запоздавший ответ старого экрана не гасит опрос нового
    SERVER.covers = [job('c1', 'running', 0.2, 'идёт')];
    const oldBox = newBox();
    await refreshCoverJobs(oldBox, 'p1');
    DELAY.covers = 2000;                 // следующий ответ старого тика задержится
    await sleep(3100);                   // тик старого экрана ушёл и висит
    clearAllLogTimers(); oldBox.remove(); // перерисовка экрана на том же hash
    DELAY.covers = 0;
    const newBoxEl = newBox();
    await refreshCoverJobs(newBoxEl, 'p1');
    await sleep(2500);                   // старый ответ пришёл, box отсоединён
    const before = CALLS.covers;
    await sleep(9500);
    out.a_new_alive = CALLS.covers - before;
    clearAllLogTimers(); newBoxEl.remove();

    // 3. Два наложившихся запуска — после ухода с экрана опроса нет
    SERVER.text = [job('j2', 'running', 0.3, 'x')];
    DELAY.text = 500;
    const box3 = newBox();
    await Promise.all([refreshScenarioJobs(box3, 'p1', null), refreshScenarioJobs(box3, 'p1', null)]);
    DELAY.text = 0;
    await sleep(3500);
    out.b_timers = Object.keys(LOG_TIMERS).length;
    clearAllLogTimers();                 // так делает смена hash
    location.hash = '#/projects';
    await sleep(100);
    const left = CALLS.text;
    await sleep(12000);
    out.b_leak = CALLS.text - left;
  } catch (e) { out.error = String(e && e.stack || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=120000)
    check("сценарий выполнен без ошибок", res.get("error"), None)

    print("\n1. Прогресс и подпись, пока задача идёт")
    check("первая отрисовка показывает подпись", res.get("p_first"), True)
    check("подпись обновилась внутри running", res.get("p_detail"), True)
    check("полоска прогресса обновилась до 60%", res.get("p_bar"), "60%")
    check("переход в «готово» отрисован", res.get("p_done"), True)
    check("после готовности опрос остановился", res.get("p_stopped"), True)

    print("\n2. Запоздавший ответ старого экрана")
    check("опрос нового экрана продолжается (≥3 запроса за 9,5 с)",
          (res.get("a_new_alive") or 0) >= 3, True)

    print("\n3. Наложившиеся запуски опроса")
    check("в реестре один таймер", res.get("b_timers"), 1)
    check("после ухода с экрана запросов нет", res.get("b_leak"), 0)

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
