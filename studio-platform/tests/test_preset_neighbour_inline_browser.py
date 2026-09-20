#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сценарий и обложка правятся ВНУТРИ пресета, а не на другом экране.

Владелец просил это трижды. Формально «перенос» был сделан ещё 18.09: в пресете появились
группы «Сценарий» и «Обложка». Но в каждой стояло ровно одно поле — выпадающий список с
ИМЕНЕМ чужого пресета, а сами настройки (промпт стиля, анализ превью, модель, языковые
строки рерайта) жили на экране списка пресетов. Чтобы поправить обложку, надо было уйти из
формы, найти нужный пресет среди чужих, поправить и вернуться — то есть переноса не было.

Теперь рядом с полем стоят «Настроить» и «Завести новый»: то же окно, что и на списке, но
без ухода с экрана и без перерисовки формы (иначе незаписанные правки пресета пропали бы).

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
  window.post = async (p) => { out.запрошено.push('POST ' + p); return {ok: true}; };

  const card = fieldForm([{title: 'Обложка', fields: [
    {key: 'preset_cover', kind: 'select', label: 'Пресет обложки', value: 'Базовый',
     options: [{value: 'Базовый', title: 'Базовый'}, {value: 'Второй', title: 'Второй'}]}]}]);
  document.getElementById('app').append(card);
  out.кнопка_настроить = !!card.querySelector('.np-edit');
  out.кнопка_завести = !!card.querySelector('.np-add');

  card.querySelector('.np-edit').click();
  await new Promise(r => setTimeout(r, 400));
  let dlg = document.querySelector('dialog[open]');
  out.окно_правки = !!dlg;
  out.настройки_соседа_в_окне = dlg ? !!dlg.querySelector('[data-key="style_prompt"]') : false;
  out.текущее_значение = dlg ? dlg.querySelector('[data-key="style_prompt"]').value : '';
  if (dlg) [...dlg.querySelectorAll('button')].find(b => /Отмена|×/.test(b.textContent)).click();
  await new Promise(r => setTimeout(r, 200));

  card.querySelector('.np-add').click();
  await new Promise(r => setTimeout(r, 400));
  const dlg2 = document.querySelector('dialog[open]');
  out.окно_заведения = !!dlg2;
  if (dlg2){
    dlg2.querySelector('#np-name').value = 'Свежий';
    [...dlg2.querySelectorAll('button')]
      .find(b => /Завести|Сохранить/.test(b.textContent)).click();
    await new Promise(r => setTimeout(r, 600));
  }
  out.выбран_после_заведения = card.querySelector('#f-preset_cover').value;
  out.форма_не_перерисована = !!card.querySelector('.np-edit');

  // Пустое поле: «Настроить» нечего, и человеку это сказано до нажатия.
  const sel = card.querySelector('#f-preset_cover');
  sel.value = ''; sel.dispatchEvent(new Event('change'));
  out.на_пустом_поле_заперта = card.querySelector('.np-edit').disabled;
  } catch (e) { out.упало = String(e && e.message || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=9000)
    if res.get("error"):
        print("  ПРОПУЩЕНО:", res["error"])
        return 2          # без браузера проверка не выполнялась — это не успех

    print("Правка пресета соседа прямо в форме пресета")
    check("падений нет", (res.get("упало"), res.get("errs")), (None, []))
    check("кнопка «Настроить» рядом с полем", res.get("кнопка_настроить"), True)
    check("кнопка «Завести новый» рядом с ней", res.get("кнопка_завести"), True)

    print("\nНастройки соседа открываются на месте")
    check("окно правки открылось", res.get("окно_правки"), True)
    check("в нём настоящие поля пресета соседа",
          res.get("настройки_соседа_в_окне"), True)
    check("и текущие значения, а не пустота", res.get("текущее_значение"), "как было")

    print("\nЗаведённый пресет сразу выбран здесь же")
    check("окно заведения открылось", res.get("окно_заведения"), True)
    check("новый пресет подставлен в поле", res.get("выбран_после_заведения"), "Свежий")
    # Ради этого и заводился отдельный путь: общая перерисовка стёрла бы незаписанные
    # правки пресета кабинета, в котором человек стоит.
    check("форма пресета не перерисована", res.get("форма_не_перерисована"), True)

    print("\nПустое поле")
    check("«Настроить» нечего — кнопка заперта",
          res.get("на_пустом_поле_заперта"), True)

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
