#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пока экран грузится, это видно (24.09.2026).

Владелец: «тыкаю на вкладку каналы и сижу на старой с мыслью что ничего не произошло».
Вкладка подсвечивается сразу, а содержимое приходит позже — без признака загрузки
переход выглядит как несработавшее нажатие.

Проверяется: на медленном экране появляется полоса и старое гасится; на быстром —
не мигает; после отрисовки всё снимается; отказ экрана тоже снимает полосу (иначе она
висела бы вечно над сообщением об ошибке).

    .venv/bin/python tests/test_busy_indicator_browser.py
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
  let delay = 0, boom = false;
  try {
    ME = {name: 'dev', role: 'user'};
    window.api = async p => {
      if (delay) await sleep(delay);
      if (boom) throw new Error('сервер не ответил');
      if (p === '/ui/me') return ME;
      if (p === '/ui/channels') return {channels: []};
      if (p === '/ui/projects') return {projects: [], counts: {}};
      return {};
    };
    addEventListener('hashchange', render);
    const bar = () => document.getElementById('busy');
    const state = () => ({
      видна: bar() ? !bar().hidden : null,
      погашен: document.body.classList.contains('loading'),
    });

    // Быстрый переход: полоса не должна успеть мигнуть.
    location.hash = '#/channels';
    await sleep(60);
    out.быстро = state();
    await sleep(300);
    out.послеБыстрого = state();

    // Медленный экран: через мгновение появляется полоса, старое гаснет.
    delay = 700;
    location.hash = '#/projects';
    await sleep(320);
    out.вГрузке = state();
    out.кликиПерехвачены = getComputedStyle(document.getElementById('app')).pointerEvents;
    await sleep(900);
    out.послеЗагрузки = state();

    // Отказ экрана: полоса обязана сняться.
    boom = true;
    location.hash = '#/channels';
    await sleep(1200);
    out.послеОшибки = state();
    boom = false; delay = 0;
  } catch (e) { out.errs.push(String(e && e.stack || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=30000)
    for e in res.get("errs") or []:
        print("  ошибка на странице:", e)

    print("Быстрый переход")
    check("полоса не мигает", (res.get("быстро") or {}).get("видна"), False)
    check("экран не гасится", (res.get("быстро") or {}).get("погашен"), False)
    check("после — тоже чисто", res.get("послеБыстрого"), {"видна": False, "погашен": False})

    print("Медленный экран")
    check("полоса видна", (res.get("вГрузке") or {}).get("видна"), True)
    check("старое приглушено", (res.get("вГрузке") or {}).get("погашен"), True)
    check("по старому экрану не нажать", res.get("кликиПерехвачены"), "none")
    check("после отрисовки всё снято", res.get("послеЗагрузки"),
          {"видна": False, "погашен": False})

    print("Экран отказал")
    check("полоса снялась и тут", res.get("послеОшибки"),
          {"видна": False, "погашен": False})

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
