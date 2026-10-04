#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Строка группы ошибок: метка, кнопка свёртки и текст — на одной линии (04.10.2026).

Владелец, со снимком «Выпуск ⌄ 2 выпуска · ждём: очередь не приняла этап «render»
[в работе]»: «везде, где текст ошибки, оно немного разъезжается». Кнопка свёртки — 30 px,
строка текста — 20 px, ячейки таблицы прижаты к верху: значок сидел ниже текста, а
метка «Выпуск» в соседней ячейке — выше. Меряем в настоящем браузере.

    .venv/bin/python tests/test_group_row_alignment_browser.py
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
    const g = {id: 'qg1', why: 'ждём: очередь не приняла этап «render»'};
    const app = document.querySelector('#app');
    app.innerHTML = `<div class="card"><table><tbody><tr class="grp">
      <td><span class="pill s-idle">Выпуск</span></td>
      <td colspan="4">${groupHeadHtml(g, '2 выпуска',
        ' <span class="pill s-running">в работе</span>')}</td>
      <td class="acts"></td></tr></tbody></table></div>
      <div class="card grpcard"><div class="grpbar">${groupHeadHtml(
        {id: 'rg1', why: 'обложка не нарисовалась дважды: 0/1 обложек'}, '3 выпуска',
        ' <span class="pill s-error">остановлен</span>')}</div></div>`;
    await new Promise(r => setTimeout(r, 100));
    const mid = el => { const r = el.getBoundingClientRect(); return r.top + r.height / 2; };
    const row = document.querySelector('tr.grp');
    const lineMid = el => {   // середина ПЕРВОЙ строки текста
      const rs = el.getClientRects(); const r = rs[0]; return r.top + r.height / 2; };
    out.очередь = {
      метка: mid(row.querySelector('td .pill')),
      кнопка: mid(row.querySelector('button.fold')),
      текст: lineMid(row.querySelector('.grpsum b')),
    };
    const card = document.querySelector('.grpcard');
    out.карточка = {
      кнопка: mid(card.querySelector('button.fold')),
      текст: lineMid(card.querySelector('.grpsum b')),
    };
  } catch (e) { out.errs.push(e.message); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    print("Строка группы ошибок — на одной линии")
    r = browser_harness.run_scenario(SCENARIO, budget_ms=8000)
    if "error" in r:
        print("ПРОПУЩЕНО:", r["error"])
        return 2
    check("ошибок JS нет", r.get("errs"), [])
    q = r.get("очередь") or {}
    k = r.get("карточка") or {}
    near = lambda a, b: a is not None and b is not None and abs(a - b) <= 2
    check("очередь: значок по линии текста", near(q.get("кнопка"), q.get("текст")), True)
    check("очередь: метка «Выпуск» по линии текста", near(q.get("метка"), q.get("текст")), True)
    check("выпуски: значок по линии текста", near(k.get("кнопка"), k.get("текст")), True)
    print("  замеры:", q, k)
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
