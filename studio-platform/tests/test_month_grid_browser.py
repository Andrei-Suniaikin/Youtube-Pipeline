#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Календарь канала различает состояния дня (проверка облика 22.09.2026).

Было: сервер присылал по дню `blocked` (выход остановлен), а сетка его не читала —
остановленный день выглядел как свободный, хотя под сеткой горело «остановлен: 2».
«Идёт» и «запланировано» рисовались одной точкой цвета действия. Дни без выхода были
прозрачны до 1.78:1 — дату не разобрать.

    .venv/bin/python tests/test_month_grid_browser.py
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
  try {
  const z = {done: 0, running: 0, missed: 0, planned: 0, free: 0, blocked: 0};
  const byDay = {
    '2026-09-01': {...z, done: 1}, '2026-09-02': {...z, blocked: 1},
    '2026-09-03': {...z, running: 1}, '2026-09-04': {...z, planned: 1},
    '2026-09-05': {...z, missed: 1}, '2026-09-06': {...z, free: 1}};
  const box = monthGrid(byDay, 'Europe/Warsaw');
  document.getElementById('app').append(box);
  const cell = n => [...box.querySelectorAll('.day')].find(
    d => (d.querySelector('.n') || {}).textContent === String(n));
  const look = n => { const c = cell(n), m = c.querySelector('.cnt');
    return {cls: c.className.replace(/\s+/g, ' ').trim(), mark: m ? m.textContent : '',
            markCls: m ? m.className : '', title: c.getAttribute('title') || ''}; };
  for (const n of [1, 2, 3, 4, 5, 6, 7]) out['d' + n] = look(n);
  const c7 = cell(7);
  out.out_n_color = getComputedStyle(c7.querySelector('.n')).color;
  out.out_opacity = getComputedStyle(c7).opacity;
  out.work_color = getComputedStyle(cell(3).querySelector('.cnt')).color;
  out.plan_color = getComputedStyle(cell(4).querySelector('.cnt')).color;
  } catch (e) { out.errs.push(String(e && e.stack || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=4000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2
    check("ошибок нет", res.get("errs"), [])
    check("вышел — число опубликованных", (res.get("d1") or {}).get("mark"), "1")
    d2 = res.get("d2") or {}
    check("остановлен — красная рамка", "blocked" in d2.get("cls", ""), True)
    check("и «!»", (d2.get("mark"), d2.get("markCls")), ("!", "cnt bad"))
    check("и подсказка словами", d2.get("title"), "остановлено: 1")
    check("идёт — свой знак", ((res.get("d3") or {}).get("mark"),
                               (res.get("d3") or {}).get("markCls")), ("◐", "cnt run"))
    check("запланирован — свой", ((res.get("d4") or {}).get("mark"),
                                  (res.get("d4") or {}).get("markCls")), ("·", "cnt plan"))
    check("«идёт» и «запланирован» разного цвета",
          res.get("work_color") != res.get("plan_color"), True)
    check("пропущен — прочерк", (res.get("d5") or {}).get("mark"), "—")
    check("свободный — без знака", (res.get("d6") or {}).get("mark"), "")
    check("день без выхода — без рамки, но не прозрачный", res.get("out_opacity"), "1")
    check("и дата читается", res.get("out_n_color"), "rgb(154, 156, 163)")
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
