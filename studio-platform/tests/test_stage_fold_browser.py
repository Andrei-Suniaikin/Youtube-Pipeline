#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Настройки этапа сворачиваются, пока этап идёт, ждёт в очереди или стоит в плане.

Просьба владельца 21.09.2026: «если шаг поставлен в план, допустим озвучка, то
настройки сворачиваются и их можно развернуть, но если задача в процессе то сворачиваем
настройки, они много места занимают». Правка в них поставленное уже не изменит, а место
на экране нужно логу и результату.

Что закрепляется:

* идёт / в очереди (`project.stage.segments`) и «в плане» (`project.planned` — отложенный
  авто-запуск «Сделать всё») — карточка свёрнута в строку заголовка, и сказано почему;
* «Начать» при этом на месте — свёрнуто только то, что занимает место;
* развернуть можно, и раскрытое переживает перерисовку экрана;
* этап закончился — не сворачивается, а память о раскрытии сброшена: следующий запуск
  снова свернёт по умолчанию.

    .venv/bin/python tests/test_stage_fold_browser.py
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
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  addEventListener('unhandledrejection',
                   e => out.errs.push('promise: ' + (e.reason && e.reason.message || e.reason)));
  const pause = ms => new Promise(r => setTimeout(r, ms));
  const q = sel => document.querySelector(sel);
  const видно = sel => { const el = q(sel);
    return !!el && el.getClientRects().length > 0; };
  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {
  const ГРУППЫ = {groups: [
    {title: 'Озвучка', fields: [{key: 'speed', label: 'Скорость', kind: 'text',
                                 value: '1.0', placeholder: '', hint: ''}]},
    {title: 'Сборка', fields: [{key: 'quality', label: 'Качество', kind: 'text',
                                value: 'high', placeholder: '', hint: ''}]}]};
  window.api = async p => p.endsWith('/settings') ? ГРУППЫ : {};
  let ОТКАЗ = null;
  window.post = async () => { if (ОТКАЗ) throw new Error(ОТКАЗ); return {ok: true}; };
  const D = (state, planned, tab) => ({
    project: {id: 'p1', title: 'Проба', planned: planned || [],
              stage: {segments: [{stage: tab || 'tts', state}], caption: '', tone: 'dim'}},
    files: [], jobs: []});
  const открыть = async (state, planned, tab) => {
    app.innerHTML = '';
    await renderStageTab(D(state, planned, tab), tab || 'tts');
    await pause(50);
  };
  const снимок = () => ({
    свёрнуто: !!q('.card.folded'),
    поля_видны: видно('#f-speed') || видно('#f-quality'),
    начать_видно: видно('#stage-go'),
    почему: q('#stage-fold-why') ? q('#stage-fold-why').textContent.trim() : null,
    кнопка: q('#stage-fold') ? q('#stage-fold').textContent.trim() : null,
    раскрыто: q('#stage-fold') ? q('#stage-fold').getAttribute('aria-expanded') : null});

  await открыть('running');
  out.идёт = снимок();
  q('#stage-fold').click();
  await pause(50);
  out.раскрыли = снимок();
  await открыть('running');                // экран перерисовался сам
  out.после_перерисовки = снимок();
  await открыть('done');
  out.готово = снимок();
  await открыть('queued');
  out.снова_в_очереди = снимок();
  await открыть('pending', ['render'], 'render');
  out.в_плане = снимок();
  await открыть('pending', [], 'render');
  out.ничего = снимок();

  // Свернули с непрошедшей правкой — «не сохранено» и «Повторить» остаются на виду:
  // свёрнутая строка, где сказано только «идёт», прятала бы потерю.
  delete STAGE_FOLD_OPEN['p1:tts'];
  await открыть('running');
  q('#stage-fold').click();                // развернули
  ОТКАЗ = 'кабинет не ответил';
  const f = q('#f-speed');
  f.value = '1.7';
  f.dispatchEvent(new Event('input', {bubbles: true}));
  await pause(1200);
  q('#stage-fold').click();                // свернули обратно
  await pause(50);
  out.свёрнуто_с_отказом = снимок();
  out.отказ_виден = видно('#stage-save') ? q('#stage-save').textContent.trim() : null;
  out.повторить_виден = видно('#stage-retry');
  ОТКАЗ = null;
  LEAVE_GUARD = null;
  } catch (e) { out.errs.push('сценарий: ' + (e && e.stack || e)); }
  finish();
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=15000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2
    check("ошибок в браузере нет", res.get("errs"), [])

    print("\nЭтап идёт")
    идёт = res.get("идёт") or {}
    check("настройки свёрнуты", идёт.get("свёрнуто"), True)
    check("полей не видно", идёт.get("поля_видны"), False)
    check("«Начать» на месте", идёт.get("начать_видно"), True)
    check("сказано почему", идёт.get("почему"), "идёт")
    check("кнопка развернуть названа", идёт.get("кнопка"), "Настройки")
    check("и читалке сказано, что свёрнуто", идёт.get("раскрыто"), "false")

    print("\nРазвернуть можно")
    раскрыли = res.get("раскрыли") or {}
    check("поля видны", раскрыли.get("поля_видны"), True)
    check("кнопка стала «Свернуть настройки»", раскрыли.get("кнопка"), "Свернуть настройки")
    check("раскрытое переживает перерисовку экрана",
          (res.get("после_перерисовки") or {}).get("поля_видны"), True)

    print("\nЭтап закончился")
    готово = res.get("готово") or {}
    check("не свёрнуто", готово.get("свёрнуто"), False)
    check("поля видны", готово.get("поля_видны"), True)
    check("кнопки сворачивания нет", готово.get("кнопка"), None)
    снова = res.get("снова_в_очереди") or {}
    check("следующий запуск снова сворачивает по умолчанию", снова.get("свёрнуто"), True)
    check("и говорит «в очереди»", снова.get("почему"), "в очереди")

    print("\nЭтап в плане «Сделать всё»")
    в_плане = res.get("в_плане") or {}
    check("свёрнут", в_плане.get("свёрнуто"), True)
    check("и сказано, что стартует сам", в_плане.get("почему"),
          "в плане — стартует сам, когда дозреют материалы")
    check("без плана и без задачи — развёрнут", (res.get("ничего") or {}).get("свёрнуто"),
          False)

    print("\nСвёрнутая карточка не прячет «не сохранено»")
    check("карточка свёрнута", (res.get("свёрнуто_с_отказом") or {}).get("свёрнуто"), True)
    check("поля спрятаны", (res.get("свёрнуто_с_отказом") or {}).get("поля_видны"), False)
    check("а отказ на виду", res.get("отказ_виден"), "не сохранено: кабинет не ответил")
    check("и «Повторить» тоже", res.get("повторить_виден"), True)

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
