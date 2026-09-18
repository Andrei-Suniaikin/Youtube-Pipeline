#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кадры проекта дочитываются при прокрутке (18.09.2026) — поведение в браузере.

В проекте бывает 300+ кадров: сервер отдаёт первую порцию, остальные подтягиваются,
когда человек досматривает список до конца. Настоящий `web/index.html` в headless
Chrome, `api()` подменён; без стенда и сети.

    .venv/bin/python tests/test_thumb_scroll_browser.py
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
const sleep = ms => new Promise(r => setTimeout(r, ms));
const TOTAL = 305;
const ALL = Array.from({length: TOTAL}, (_, i) => `googler/${String(i + 1).padStart(3, '0')}.jpg`);
const CALLS = [];
let netFail = false;   // «fail» на верхнем уровне столкнулось бы с функцией страницы
api = async (path) => {
  CALLS.push(path);
  if (netFail) throw new Error('сеть отвалилась');
  const u = new URL(path, 'http://x/');
  const off = Number(u.searchParams.get('offset') || 0);
  const lim = Number(u.searchParams.get('limit') || 40);
  return {code: 'images', count: TOTAL, offset: off,
          names: ALL.slice(off, off + lim), more: Math.max(0, TOTAL - (off + lim))};
};
(async () => {
  const out = {};
  try {
    // Объявления только внутри — на верхнем уровне они бы столкнулись с именами самой
    // страницы, и весь инлайн-скрипт не запустился бы вовсе.
    const d = {project: {id: 'p1'}, file_base: '/f/', zip_base: '/z/'};
    const g = {code: 'images', title: 'кадры', kind: 'image', count: TOTAL,
               names: ALL.slice(0, 40), more: TOTAL - 40};
    const shown = card => card.querySelectorAll('.thumbs .thumb').length;
    const card = fileCard(d, g);
    app.innerHTML = '';
    app.append(card);
    out.first = shown(card);
    out.firstNote = (card.querySelector('.more-mark') || {}).textContent || '';
    out.lazy = [...card.querySelectorAll('.thumbs img')].every(i => i.loading === 'lazy');
    out.callsBefore = CALLS.length;

    // Досматриваем список до конца: каждая прокрутка дочитывает следующие порции.
    let guard = 0;
    while (shown(card) < TOTAL && guard++ < 40){
      card.querySelector('.more-mark').scrollIntoView();
      window.dispatchEvent(new Event('scroll'));
      await sleep(200);
    }
    out.afterScroll = shown(card);
    out.calls = CALLS.length;
    out.unique = new Set([...card.querySelectorAll('.thumbs img')]
                          .map(i => i.getAttribute('src'))).size;
    out.noteAtEnd = (card.querySelector('.more-mark') || {}).textContent.trim();
    const before = CALLS.length;
    card.querySelector('.more-mark').scrollIntoView();
    window.dispatchEvent(new Event('scroll'));
    await sleep(400);
    out.callsAfterEnd = CALLS.length - before;

    // Сетевая ошибка: показанное не стирается, причина видна.
    const card2 = fileCard(d, {...g, names: ALL.slice(0, 40)});
    app.innerHTML = '';
    app.append(card2);
    netFail = true;
    card2.querySelector('.more-mark').scrollIntoView();
    window.dispatchEvent(new Event('scroll'));
    await sleep(400);
    out.keptOnError = shown(card2);
    out.errorShown = /не дочитались/.test(card2.querySelector('.more-mark').textContent);
    netFail = false;
  } catch (e) { out.error = String(e && e.stack || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=60000)
    check("сценарий выполнен без ошибок", res.get("error"), None)
    check("на первом экране только первая порция", res.get("first"), 40)
    check("сказано, сколько показано из скольки", res.get("firstNote", "").strip(),
          "показано 40 из 305")
    check("картинки грузятся лениво", res.get("lazy"), True)
    check("до прокрутки сервер не дёргали", res.get("callsBefore"), 0)
    check("после прокрутки дочитаны все кадры", res.get("afterScroll"), 305)
    check("каждый кадр ровно один раз", res.get("unique"), 305)
    check("дочитано ровно семью порциями по 40", res.get("calls"), 7)
    check("в конце подпись убрана", res.get("noteAtEnd"), "")
    check("в конце опрос прекращается", res.get("callsAfterEnd"), 0)
    check("ошибка сети не стирает показанное", res.get("keptOnError"), 40)
    check("причина ошибки видна", res.get("errorShown"), True)

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
