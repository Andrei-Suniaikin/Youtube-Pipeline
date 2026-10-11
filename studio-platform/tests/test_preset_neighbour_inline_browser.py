#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сценарий и обложка правятся ВНУТРИ пресета, а не на другом экране.

Владелец просил это трижды. Формально «перенос» был сделан ещё 18.09: в пресете появились
группы «Сценарий» и «Обложка». Но в каждой стояло ровно одно поле — выпадающий список с
ИМЕНЕМ чужого пресета, а сами настройки (промпт стиля, анализ превью, модель, языковые
строки рерайта) жили на экране списка пресетов. Чтобы поправить обложку, надо было уйти из
формы, найти нужный пресет среди чужих, поправить и вернуться — то есть переноса не было.

11.10.2026: кнопки «Настроить»/«Завести новый» убраны — владелец: «какого черта не
показываются сразу настройки». Поля выбранного пресета стоят сразу под выбором, при пустом
выборе — форма нового; правки уезжают общей кнопкой «Сохранить» пресета.

Сети и стенда не нужно: сервер подменён, браузер настоящий.

    .venv/bin/python tests/test_preset_neighbour_inline_browser.py
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
  const out = {errs: [], запрошено: []};
  addEventListener('error', e => out.errs.push(e.message));
  const wait = ms => new Promise(r => setTimeout(r, ms));
  try {
  window.api = async (p) => {
    out.запрошено.push(p);
    if (p.includes('/new/form'))
      return {fields: [{key: 'style_prompt', kind: 'text', label: 'Промпт стиля', value: ''}]};
    if (p.includes('/form'))
      return {fields: [{key: 'style_prompt', kind: 'text', label: 'Промпт стиля',
                        value: 'как было'}]};
    return {};
  };
  window.post = async (p, body) => { out.запрошено.push('POST ' + p + ' ' + JSON.stringify(body));
                                     return {ok: true}; };

  const card = fieldForm([{title: 'Обложка', fields: [
    {key: 'preset_cover', kind: 'select', label: 'Пресет обложки', value: 'Базовый',
     options: [{value: '', title: '— как по умолчанию —'},
               {value: 'Базовый', title: 'Базовый'}, {value: 'Второй', title: 'Второй'}]}]}]);
  document.getElementById('app').append(card);
  await wait(300);
  out.кнопок_нет = !card.querySelector('.np-edit') && !card.querySelector('.np-add');
  out.знак_есть = !!card.querySelector('.fld .q');
  out.знаков_в_подписи = card.querySelectorAll('label .q').length;
  out.подпись_поля = (card.querySelector('label[for="f-preset_cover"]') || {}).textContent;
  out.окон = document.querySelectorAll('dialog[open]').length;

  let pan = card.querySelector('.inpanel');
  out.настройки_сразу = pan ? !!pan.querySelector('[data-key="style_prompt"]') : false;
  out.текущее_значение = pan ? pan.querySelector('[data-key="style_prompt"]').value : '';
  // Поля соседа не попадают в поля пресета кабинета.
  out.чтение_формы = Object.keys(card.read ? card.read() : {});

  // Правка соседа уезжает общей кнопкой (PRESET_NEIGHBOUR_FLUSH).
  pan.querySelector('[data-key="style_prompt"]').value = 'новое';
  pan.querySelector('[data-key="style_prompt"]').dispatchEvent(new Event('input', {bubbles: true}));
  for (const f of PRESET_NEIGHBOUR_FLUSH) await f();
  out.правка_уехала = out.запрошено.map(decodeURIComponent).some(x => x.startsWith('POST /ui/presets/cover/Базовый/form')
                                            && x.includes('новое'));

  // Пустое поле — сразу форма нового; заведённый подставляется в выбор.
  const sel = card.querySelector('#f-preset_cover');
  sel.value = ''; sel.dispatchEvent(new Event('change'));
  await wait(300);
  pan = card.querySelector('.inpanel');
  out.форма_нового = pan ? !!pan.querySelector('#np-name') : false;
  if (pan){
    pan.querySelector('#np-name').value = 'Свежий';
    pan.querySelector('.m-ok').click();
    await wait(600);
  }
  out.выбран_после_заведения = sel.value;
  out.панелей = card.querySelectorAll('.inpanel').length;
  pan = card.querySelector('.inpanel');
  out.после_заведения_его_настройки = pan ? !pan.querySelector('#np-name') : false;
  } catch (e) { out.упало = String(e && e.message || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=9000)
    if res.get("error"):
        print("  ПРОПУЩЕНО:", res["error"])
        return 2          # без браузера проверка не выполнялась — это не успех

    print("Настройки сценария/обложки видны сразу, без кликов")
    check("падений нет", (res.get("упало"), res.get("errs")), (None, []))
    check("кнопок «Настроить»/«Завести новый» больше нет", res.get("кнопок_нет"), True)
    check("окон поверх формы нет", res.get("окон"), 0)
    check("поля выбранного пресета — сразу под выбором", res.get("настройки_сразу"), True)
    check("и текущие значения, а не пустота", res.get("текущее_значение"), "как было")
    check("у подписи есть знак вопроса", res.get("знак_есть"), True)
    check("и он рядом с подписью, а не внутри <label>", res.get("знаков_в_подписи"), 0)
    check("подпись поля — только её слова", (res.get("подпись_поля") or "").strip(),
          "Пресет обложки")
    check("поля соседа не читаются как поля кабинета", res.get("чтение_формы"),
          ["preset_cover"])
    check("правка соседа уезжает общей кнопкой «Сохранить»", res.get("правка_уехала"), True)

    print("\nПустой выбор — сразу форма нового")
    check("форма нового на месте", res.get("форма_нового"), True)
    check("заведённый подставлен в поле", res.get("выбран_после_заведения"), "Свежий")
    check("панель одна", res.get("панелей"), 1)
    check("и показывает уже его настройки", res.get("после_заведения_его_настройки"), True)

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
