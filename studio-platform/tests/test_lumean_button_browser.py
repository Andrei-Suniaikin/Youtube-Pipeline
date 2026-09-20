#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Шаблон Lumean заводится там, где человек его ищет — в карте голосов пресета.

Жалоба владельца 20.09.2026: «выбираю Lumean в пресете и не вижу, где создать голос».
И правда: карточка шаблонов жила ТОЛЬКО на экране списка пресетов, то есть id надо было
завести на одном экране, скопировать и принести на другой. Теперь кнопка стоит рядом с
«+ строка» в самой карте.

Три вещи, которые тут проверяются, и все три уже ломались:
  * кнопка видна СРАЗУ при открытии пресета с Lumean, а не после того, как человек
    потрогает «Движок» (начальное состояние ставится в JS, разметка его не знает);
  * заведённый шаблон подставляется в строку карты — ради этого всё и делалось;
  * окно шаблона НЕ перерисовывает экран: человек стоит в незаполненной форме пресета,
    и render() стёр бы всё, что он успел набрать.

Сети и стенда не нужно: сервер подменён, браузер настоящий.

    .venv/bin/python tests/test_lumean_button_browser.py
"""
from __future__ import annotations

import json
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
  // Сервер на бумажке: те же ответы, что у настоящих ручек.
  window.api = async (p) => (p === '/ui/lumean/templates')
    ? {ok: true, templates: [], fields: [
        {key: 'name', kind: 'text', label: 'Название', value: ''},
        {key: 'voice_id', kind: 'text', label: 'ID голоса', value: ''}]}
    : {};
  window.post = async () => ({ok: true, id: 'tpl-ЗАВЕДЁН', name: 'Проба'});

  const group = (value) => ({fields: [
    {key: 'engine', kind: 'select', label: 'Движок', value: 'lumean',
     options: [{value: 'lumean', title: 'Lumean'}, {value: 'edge', title: 'Edge'}]},
    {key: 'langmap', kind: 'langmap', label: 'Карта голосов', value: value,
     options: [{value: 'pl', title: 'Польский'}, {value: 'cs', title: 'Чешский'}],
     by_engine: {lumean: {label: 'Карта шаблонов', hint: ''},
                 other: {label: 'Карта голосов', hint: ''}}}]});

  // ── пустая карта: кнопка видна сразу, шаблон становится новой строкой ──
  const card = fieldForm([group('')]);
  document.getElementById('app').append(card);
  const btn = card.querySelector('.langmap-lum');
  out.кнопка_есть = !!btn;
  out.видна_сразу = btn ? !btn.hidden : null;

  const sel = card.querySelector('#f-engine');
  sel.value = 'edge'; sel.dispatchEvent(new Event('change'));
  out.на_другом_движке_спрятана = btn ? btn.hidden : null;
  sel.value = 'lumean'; sel.dispatchEvent(new Event('change'));
  out.вернулась = btn ? !btn.hidden : null;

  btn.click();
  await new Promise(r => setTimeout(r, 400));
  const dlg = document.querySelector('dialog[open]');
  out.окно_открылось = !!dlg;
  if (dlg) {
    dlg.querySelector('[data-lt="name"]').value = 'Проба';
    dlg.querySelector('[data-lt="voice_id"]').value = 'voice-1';
    [...dlg.querySelectorAll('button')].find(b => b.textContent.trim() === 'Завести').click();
    await new Promise(r => setTimeout(r, 700));
  }
  out.id_в_карте = [...card.querySelectorAll('.lm-voice')].map(i => i.value);
  out.форма_на_месте = !!card.querySelector('.langmap-lum');

  // Язык человек выбирает сам: пока его нет, в пресет ничего не уходит — иначе в
  // настройки уехала бы строка без языка, и озвучка не нашла бы, чем читать.
  out.скрытое_поле_пустое = (card.querySelector('[data-key="langmap"]') || {}).value === '';
  const row = card.querySelector('.langmap-row');
  row.querySelector('.lm-lang').value = 'pl';
  row.querySelector('.lm-lang').dispatchEvent(new Event('change'));
  out.после_выбора_языка = (card.querySelector('[data-key="langmap"]') || {}).value;

  // ── занятая строка: её значение не затирается, шаблон уходит в новую ──
  const card2 = fieldForm([group('pl: уже-стоит')]);
  document.getElementById('app').append(card2);
  card2.querySelector('.langmap-lum').click();
  await new Promise(r => setTimeout(r, 400));
  const dlg2 = document.querySelector('dialog[open]');
  if (dlg2) {
    dlg2.querySelector('[data-lt="name"]').value = 'Второй';
    dlg2.querySelector('[data-lt="voice_id"]').value = 'voice-2';
    [...dlg2.querySelectorAll('button')].find(b => b.textContent.trim() === 'Завести').click();
    await new Promise(r => setTimeout(r, 700));
  }
  out.занятая_строка_цела = [...card2.querySelectorAll('.lm-voice')].map(i => i.value);

  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=9000)
    if res.get("error"):
        print("  ПРОПУЩЕНО:", res["error"])
        return 2          # пропуск не успех: без браузера проверка не выполнялась

    print("Кнопка «+ шаблон в Lumean» в карте голосов пресета")
    check("кнопка есть", res.get("кнопка_есть"), True)
    check("видна сразу, до того как трогали «Движок»", res.get("видна_сразу"), True)
    check("на другом движке спрятана", res.get("на_другом_движке_спрятана"), True)
    check("вернулась при возврате к Lumean", res.get("вернулась"), True)

    print("\nЗаведённый шаблон попадает в карту")
    check("окно шаблона открылось", res.get("окно_открылось"), True)
    check("id подставлен в строку", res.get("id_в_карте"), ["tpl-ЗАВЕДЁН"])
    check("форма пресета не перерисована", res.get("форма_на_месте"), True)
    check("без языка в пресет ничего не уходит", res.get("скрытое_поле_пустое"), True)
    check("после выбора языка строка собрана",
          res.get("после_выбора_языка"), "pl: tpl-ЗАВЕДЁН")

    print("\nЧужое не затирается")
    check("занятая строка цела, шаблон ушёл в новую",
          res.get("занятая_строка_цела"), ["уже-стоит", "tpl-ЗАВЕДЁН"])

    print("\nОшибок в консоли нет")
    check("консоль чистая", res.get("errs"), [])

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
