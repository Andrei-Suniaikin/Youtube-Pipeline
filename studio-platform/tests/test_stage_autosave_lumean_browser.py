#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Автозапись настроек этапа: Lumean без шаблона и «Сделать всё» посреди набора.

Два дефекта, найденные ревью UI-пачки 21.09.2026, — оба появились вместе с автозаписью.
С кнопкой «Сохранить» их не было: движок и шаблон уезжали одним нажатием, а «Сделать
всё» нажимали уже после сохранения.

1. Выбор «Lumean» в «Движке» уезжал сразу, одним ключом `engine`. Кабинет законно
   отвечает на это 400 (`_normalize_lumean_settings`: Lumean без UUID шаблона не
   сохраняется), а шаблон человек впишет только следующим шагом. Первый же клик обычной
   настройки давал красное «не сохранено», тост и сторож ухода. Теперь движок
   придерживается, пока шаблона нет нигде, остальные правки уходят как обычно, а сторож
   ухода знает про придержанное — молча оно не пропадёт.

2. «Сделать всё» стоит в шапке проекта и видна, пока человек набирает. Правка ждёт
   паузы в 700 мс, и платный конвейер, запущенный в эту паузу, ушёл бы со значениями ДО
   правки. Теперь кнопка сперва досылает набранное; не записалось — не запускает.

Настоящий `web/index.html` в headless Chrome, сеть подменена.

    .venv/bin/python tests/test_stage_autosave_lumean_browser.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "helpers"))

import browser_harness                                            # noqa: E402

FAILS = []
UUID = "01a0be22-e505-7076-b88e-2615117febcb"
HOLD = ("Lumean запишется вместе с шаблоном — впишите его UUID в строку языка или "
        "в «Голос по умолчанию».")


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
  const tick = ms => new Promise(r => setTimeout(r, ms || 150));
  const toasts = () => Array.from(document.querySelectorAll('#toasts .toast'))
                            .map(t => t.textContent.trim());
  const поле = k => document.querySelector('#f-' + k);
  const набрать = (k, v, событие) => {
    const el = поле(k);
    el.value = v;
    el.dispatchEvent(new Event(событие || 'input', {bubbles: true}));
  };
  const note = () => { const el = document.querySelector('#stage-save');
    return el ? {text: el.textContent.trim(), cls: el.className} : null; };
  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {

  const UUID = '01a0be22-e505-7076-b88e-2615117febcb';
  const ГРУППА = () => ({title: 'Озвучка', fields: [
    {key: 'engine', kind: 'select', label: 'Движок', value: 'edge', hint: '',
     options: [{value: 'edge', title: 'Edge'}, {value: 'lumean', title: 'Lumean'}]},
    {key: 'speed', label: 'Скорость', kind: 'text', value: '1.0', placeholder: '', hint: ''},
    {key: 'voice', label: 'Голос по умолчанию', kind: 'text', value: '',
     placeholder: '', hint: ''},
  ]});
  const ONE = {
    project: {id: 'p1', name: 'Проба', status: '', created: 1758000000,
              updated: 1758000000, release_id: '', phase: '',
              stage: {segments: [], caption: '', tone: 'dim'}, applied_preset: null},
    ready: {text: true, audio: false, images: 0, clips: 0, render: false},
    jobs: [], files: [], paid: [], source: 'текст',
    file_base: '/p/p1/file/', zip_base: '/api/projects/p1/download/'};
  window.api = async (p) => {
    if (p === '/ui/projects/p1/settings') return {groups: [ГРУППА()]};
    if (p === '/ui/projects/p1') return JSON.parse(JSON.stringify(ONE));
    if (p === '/ui/presets') return {groups: []};
    return {};
  };
  const ушло = [];
  let ОТКАЗ = null;
  window.post = async (path, body) => {
    ушло.push({path, body});
    if (ОТКАЗ && path.endsWith('/settings')) throw new Error(ОТКАЗ);
    if (path.endsWith('/run'))
      return {ok: true, kind: 'pipeline', enqueued: 4, deferred: 0, skipped: 0,
              blocked: 0, stages: ['tts', 'images', 'video', 'render'], note: ''};
    return {ok: true};
  };
  const D = {project: {id: 'p1', title: 'Проба'}, files: [], jobs: []};

  // ── 1. «Lumean» без шаблона придерживается, а не бьётся о кабинет ──────────
  app.innerHTML = '';
  await renderStageTab(D, 'tts');
  const тостов_до = toasts().length;
  набрать('engine', 'lumean', 'change');
  await tick(1200);
  out.движок_не_ушёл = ушло.length;
  out.сказано = note();
  out.тостов = toasts().length - тостов_до;
  out.сторож_стоит = !!(LEAVE_GUARD && LEAVE_GUARD.dirty());
  out.сторож_про_lumean = !!(LEAVE_GUARD && /Lumean/.test(LEAVE_GUARD.what));

  // Остальные правки при этом уезжают — без движка.
  набрать('speed', '1.3');
  await tick(1200);
  out.скорость_ушла = ушло.slice(-1)[0] || null;
  out.сказано_после_скорости = note();
  out.сторож_всё_ещё_стоит = !!(LEAVE_GUARD && LEAVE_GUARD.dirty());

  // Шаблон вписан — движок уезжает вместе с ним одной записью.
  набрать('voice', UUID);
  поле('voice').dispatchEvent(new Event('focusout', {bubbles: true}));
  await tick(1200);
  out.вместе_с_шаблоном = ушло.slice(-1)[0] || null;
  out.сказано_после_шаблона = (note() || {}).text || '';
  out.сторож_снят = (LEAVE_GUARD === null);

  // ── 2. Передумал — вернул прежний движок: придержанное снимается молча ─────
  app.innerHTML = '';
  ушло.length = 0;
  await renderStageTab(D, 'tts');
  набрать('engine', 'lumean', 'change');
  await tick(300);
  набрать('engine', 'edge', 'change');
  await tick(1200);
  out.передумал_ничего_не_ушло = ушло.length;
  out.передумал_подпись_пуста = ((note() || {}).text || '') === '';
  out.передумал_сторож_снят = (LEAVE_GUARD === null);

  // ── 3. «Сделать всё» посреди набора ──────────────────────────────────────
  history.replaceState(null, '', '#/project/p1/tts');
  await viewProject('p1');
  ушло.length = 0;
  набрать('speed', '1.7');                 // пауза в 700 мс ещё не прошла
  document.getElementById('runall').click();
  document.getElementById('rgo').click();
  await tick(600);
  out.порядок = ушло.map(x => x.path.split('/').pop()
    + (x.body && x.body.values ? ':' + x.body.values.speed : ''));

  await tick(1600);                        // перерисовка после запуска
  ушло.length = 0;
  ОТКАЗ = 'кабинет не ответил';
  набрать('speed', '1.9');
  document.getElementById('runall').click();
  document.getElementById('rgo').click();
  await tick(600);
  out.отказ_запуска_нет = !ушло.some(x => x.path.endsWith('/run'));
  out.отказ_сказано = toasts().slice(-1)[0] || '';

  ОТКАЗ = null;
  ушло.length = 0;
  набрать('engine', 'lumean', 'change');
  await tick(300);
  document.getElementById('rgo').click();
  await tick(600);
  out.придержано_запуска_нет = !ушло.some(x => x.path.endsWith('/run'));
  out.придержано_сказано = toasts().slice(-1)[0] || '';

  } catch (e) { out.errs.push('сценарий: ' + (e && e.stack || e)); }
  finish();
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=30000)
    check("ошибок в браузере нет", res.get("errs"), [])

    print("\nLumean без шаблона")
    check("движок не уходит в кабинет одним ключом", res.get("движок_не_ушёл"), 0)
    check("сказано, что нужно дописать, — предупреждением, а не отказом",
          res.get("сказано"), {"text": HOLD, "cls": "note warn"})
    check("ни одного тоста — это не ошибка", res.get("тостов"), 0)
    check("сторож ухода стоит — выбор движка молча не пропадёт",
          res.get("сторож_стоит"), True)
    check("и говорит, что именно не записано", res.get("сторож_про_lumean"), True)
    check("остальные правки уезжают, без движка", res.get("скорость_ушла"),
          {"path": "/ui/projects/p1/settings", "body": {"values": {"speed": "1.3"}}})
    check("подпись про шаблон не сменилась на «сохранено»",
          res.get("сказано_после_скорости"), {"text": HOLD, "cls": "note warn"})
    check("и сторож не снят", res.get("сторож_всё_ещё_стоит"), True)
    check("вписан шаблон — движок уезжает вместе с ним",
          res.get("вместе_с_шаблоном"),
          {"path": "/ui/projects/p1/settings",
           "body": {"values": {"engine": "lumean", "voice": UUID}}})
    check("и экран говорит «сохранено»",
          res.get("сказано_после_шаблона", "").startswith("сохранено"), True)
    check("сторож снят", res.get("сторож_снят"), True)

    print("\nПередумал и вернул прежний движок")
    check("в кабинет ничего не ушло", res.get("передумал_ничего_не_ушло"), 0)
    check("подпись про шаблон убрана", res.get("передумал_подпись_пуста"), True)
    check("сторож снят", res.get("передумал_сторож_снят"), True)

    print("\n«Сделать всё» посреди набора")
    check("сперва записывает набранное, потом запускает",
          res.get("порядок"), ["settings:1.7", "run"])
    check("запись не прошла — не запускает", res.get("отказ_запуска_нет"), True)
    check("и говорит почему", res.get("отказ_сказано"),
          "Не запускаю — настройки этапа не записаны: кабинет не ответил")
    check("движок придержан — не запускает", res.get("придержано_запуска_нет"), True)
    check("и говорит почему", res.get("придержано_сказано"),
          "Не запускаю — настройки этапа не записаны: " + HOLD)

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
