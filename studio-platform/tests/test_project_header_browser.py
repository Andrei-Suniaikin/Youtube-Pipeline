#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Шапка проекта: до вкладок ничего лишнего (жалоба владельца 21.09.2026 «перегружен»).

До правки перед вкладками стояли три карточки — предупреждение стенда, «Пресет» и «Во что
обошёлся», — и вкладки начинались на 700 px. Теперь:

* «Пресет» — кнопка в шапке рядом со «Сделать всё»; панель открывается по ней, а что
  применено (или что пресет удалён), сказано на самой кнопке;
* «Во что обошёлся» — строка под названием, словами экрана («кадры», а не `images`),
  с правильным склонением; пояснение про диск — под знаком вопроса;
* между шапкой и вкладками карточек нет (кроме предупреждения, когда исполнитель
  выключен, — оно про деньги и время и стоит до кнопок законно).

    .venv/bin/python tests/test_project_header_browser.py
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
  const q = s => document.querySelector(s);
  const видно = s => { const el = q(s); return !!el && el.getClientRects().length > 0; };
  const flat = el => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
  try {
  let APPLIED = {id: 3, name: 'Польский длинный', missing: false, links: []};
  let PAID = [{kind: 'images', delivered: 32, cost: 0}, {kind: 'tts', delivered: 1, cost: 12}];
  const ONE = () => ({
    project: {id: 'p1', name: 'Первый', status: '', created: 1, updated: 1,
              release_id: '', phase: '', stage: {segments: [], caption: '', tone: 'dim'},
              applied_preset: APPLIED, planned: []},
    ready: {}, jobs: [], files: [], paid: PAID, source: 'текст', origin: {},
    file_base: '/p/p1/file/', zip_base: '/z/', scheduler: {known: true, on: true}});
  window.api = async p => {
    if (p === '/ui/projects/p1') return ONE();
    if (p === '/ui/presets') return {groups: [{service: 'vsgen', items: [
      {key: '3', name: 'Польский длинный', scope: 'full'}]}]};
    if (p.startsWith('/ui/text')) return {jobs: [], modes: [], presets: {generation: []},
      models: [], languages: []};
    return {};
  };
  window.post = async () => ({ok: true});
  history.replaceState(null, '', '#/project/p1/scenario');
  await viewProject('p1');
  await pause(200);

  // «Сделать всё» открывает свою панель — проверяется ВИДИМОСТЬ, а не нажатие
  // программой: `.click()` по скрытой кнопке срабатывает, и прежний тест не заметил,
  // что человек панель не видел никогда.
  out.запуск_до = видно('#rgo');
  q('#runall').click(); await pause(100);
  out.запуск_открылся = видно('#rgo');
  q('#runall').click(); await pause(100);
  out.запуск_закрылся = !видно('#rgo');
  out.кнопка_пресета = flat(q('#ppopen'));
  out.панель_спрятана = !видно('#pp-pick');
  q('#ppopen').click();
  out.панель_открылась = видно('#pp-pick');
  out.раскрыто = q('#ppopen').getAttribute('aria-expanded');
  q('#ppopen').click();
  out.панель_закрылась = !видно('#pp-pick');

  const line = q('#paidline');
  out.расходы = line ? flat(line).replace('?', '').trim() : null;
  out.пояснение = line && line.querySelector('.q')
    ? line.querySelector('.q').getAttribute('data-hint') : null;
  // Между шапкой и вкладками — ни одной видимой карточки.
  const head = q('#runall').closest('.head');
  const tabs = q('.subtabs');
  const между = [];
  for (let el = head.nextElementSibling; el && el !== tabs; el = el.nextElementSibling)
    if (el.getClientRects().length) между.push(el.className || el.outerHTML.slice(0, 80));
  out.между_шапкой_и_вкладками = между;

  APPLIED = {id: 9, name: 'Старый', missing: true, links: []};
  PAID = [];
  await viewProject('p1');
  await pause(200);
  out.удалённый = flat(q('#ppopen'));
  out.удалённый_красный = q('#ppopen').classList.contains('bad');
  out.без_расходов_строки_нет = !q('#paidline');
  } catch (e) { out.errs.push('сценарий: ' + (e && e.stack || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=12000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2
    check("ошибок в браузере нет", res.get("errs"), [])

    print("\n«Пресет» — кнопкой в шапке")
    check("на кнопке название применённого пресета", res.get("кнопка_пресета"),
          "Пресет: Польский длинный")
    check("панель спрятана, пока не попросили", res.get("панель_спрятана"), True)
    check("кнопка её открывает", res.get("панель_открылась"), True)
    check("и говорит читалке, что раскрыто", res.get("раскрыто"), "true")
    check("повторное нажатие закрывает", res.get("панель_закрылась"), True)
    check("удалённый пресет назван на кнопке", res.get("удалённый"), "Пресет удалён")
    check("и выделен", res.get("удалённый_красный"), True)

    print("\n«Сделать всё» открывает панель — видимо для человека")
    check("панель закрыта сначала", res.get("запуск_до"), False)
    check("нажатие её показывает", res.get("запуск_открылся"), True)
    check("повторное — прячет", res.get("запуск_закрылся"), True)

    print("\n«Во что обошёлся» — строкой, словами экрана")
    check("виды словами и склонение верное", res.get("расходы"),
          "Во что обошёлся: кадры — 32 оплаченных вызова; озвучка — 1 оплаченный "
          "вызов на 12 кр.")
    check("пояснение под знаком вопроса", res.get("пояснение"),
          "Это то, что будет выброшено, если проект убрать с диска.")
    check("нет расходов — нет и строки", res.get("без_расходов_строки_нет"), True)

    print("\nДо вкладок ничего лишнего")
    check("между шапкой и вкладками ни одной видимой карточки",
          res.get("между_шапкой_и_вкладками"), [])

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
