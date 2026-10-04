#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вкладка «Обложка» проекта показывает обложку ВЫПУСКА первой (04.10.2026).

Владелец, со снимком вкладки: «пишется, что с обложкой что-то не так, нажимаю посмотреть
обложки и её не вижу — бред какой-то». Обложку выпуска заказывает движок, в задачах
вкладки её нет. Теперь первой карточкой — «Обложка выпуска»: картинка, вердикт сверки и
решения; у проекта без выпуска такой карточки нет.

    .venv/bin/python tests/test_project_cover_tab_release_browser.py
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
  const out = {errs: [], posts: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  const pause = ms => new Promise(r => setTimeout(r, ms));
  try {
    const COVERS = {modes: [{key: 'url', title: 'По ссылке', note: ''}], presets: [],
      options: [{label: 'Модель', provider: 'x', model: 'y'}], languages: ['Русский'],
      aspect: '16:9', jobs: [], rules: []};
    window.api = async p => {
      if (p === '/ui/releases/r9/cover') return {job: 'cj9', mirror: 'error', blocked: true,
        status: 'completed_partial', verdict: 'rejected',
        notes: ['❌ Проблема: MOARTE -> CARNE'], image: '/ui/releases/r9/cover/image'};
      if (p.startsWith('/ui/covers')) return COVERS;
      return {};
    };
    window.post = async (p) => { out.posts.push(p); return {ok: true}; };

    app.innerHTML = '';
    await renderCoverTab({project: {id: 'p9', name: 'Wymazane Zwoje', release_id: 'r9'},
                          files: [], jobs: [], origin: null});
    await pause(300);
    const cards = [...app.querySelectorAll('.card')];
    out.первая = cards.length ? cards[0].querySelector('.sec').textContent.replace(/\s+/g, ' ').trim() : '';
    const box = cards.length ? cards[0].querySelector('[data-cvr]') : null;
    out.картинка = box && box.querySelector('img') ? box.querySelector('img').getAttribute('src') : '';
    out.вердикт = box ? box.textContent.replace(/\s+/g, ' ') : '';
    out.кнопки = box ? [...box.querySelectorAll('button')].map(b => b.textContent) : [];
    const reo = box && box.querySelector('.cv-reorder');
    if (reo){ reo.click(); await pause(100);
              const ok = document.querySelector('.ask-ok'); if (ok) ok.click(); await pause(300); }
    out.перезаказ = out.posts.includes('/ui/releases/r9/cover/reorder');

    // Строк сверки нет (задачу перезапускали) — не «…не приняла надпись:» с пустотой.
    window.api = async p => {
      if (p === '/ui/releases/r7/cover') return {job: 'cj7', mirror: 'error', blocked: true,
        status: 'completed_partial', verdict: 'rejected', notes: [],
        image: '/ui/releases/r7/cover/image'};
      if (p.startsWith('/ui/covers')) return COVERS;
      return {};
    };
    app.innerHTML = '';
    await renderCoverTab({project: {id: 'p7', name: 'Перезапускали', release_id: 'r7'},
                          files: [], jobs: [], origin: null});
    await pause(300);
    out.безСтрок = (app.querySelector('[data-cvr]') || {}).textContent || '';

    app.innerHTML = '';
    await renderCoverTab({project: {id: 'p8', name: 'Без выпуска'}, files: [], jobs: [],
                          origin: null});
    await pause(200);
    out.безВыпуска = [...app.querySelectorAll('.card .sec')].map(x => x.textContent)
      .some(t => t.includes('Обложка выпуска'));
  } catch (e) { out.errs.push('сценарий: ' + e.message); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    print("Вкладка «Обложка» проекта: обложка выпуска первой")
    r = browser_harness.run_scenario(SCENARIO, budget_ms=12000)
    if "error" in r:
        print("ПРОПУЩЕНО:", r["error"])
        return 2
    check("ошибок JS нет", r.get("errs"), [])
    check("первая карточка — «Обложка выпуска»", (r.get("первая") or "").startswith("Обложка выпуска r9"), True)
    check("в ней картинка выпуска", (r.get("картинка") or "").endswith("/ui/releases/r9/cover/image"), True)
    check("и что сказала сверка", "MOARTE -> CARNE" in (r.get("вердикт") or ""), True)
    check("и решения", r.get("кнопки"), ["Взять эту обложку", "Перезаказать — новая обложка, платно"])
    check("«Перезаказать» уходит на сервер", r.get("перезаказ"), True)
    check("у проекта без выпуска карточки нет", r.get("безВыпуска"), False)
    check("строк сверки нет — так и сказано, без пустого двоеточия",
          "подробностей не сохранилось" in (r.get("безСтрок") or ""), True)
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
