#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Длинные списки ищутся, а не листаются (30.09.2026).

Владелец: «пробегись по дропдаунам и, если там больше 5 айтемов, сделай ещё быстрый
поиск, чтобы было легче искать». Самый больной случай — пятьдесят два профиля AdsPower.

Главное, что здесь проверяется, — НЕ внешний вид, а то, что обёртка ничего не отняла:

  • родной <select> остаётся на месте и остаётся источником правды. Весь существующий
    код читает `.value` и слушает `change` — сорок три места, которые никто не трогал;
  • выбор мышью и с клавиатуры доходит до этого кода событием `change`. Без него выбор
    менялся бы на экране и никуда не шёл — худший вид поломки, её не видно;
  • короткие списки не трогаются: поиск по трём строкам мешает, а не помогает;
  • списки с множественным выбором и выключенные — не трогаются тоже;
  • подпись кнопки не врёт: значение меняют и снаружи, программно, а такая запись
    события `change` не шлёт.

    .venv/bin/python tests/test_select_search_browser.py
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
  try {
    const app = document.getElementById('app');
    const many = n => Array.from({length: n}, (_, i) =>
      `<option value="v${i}">Профиль ${i} · серия ${1000 + i}</option>`).join('');

    app.innerHTML = `
      <select id="short">${many(4)}</select>
      <select id="long" style="width:230px">${many(20)}</select>
      <select id="multi" multiple>${many(20)}</select>
      <select id="off" disabled>${many(20)}</select>`;
    wireSearchableSelects(app);
    await sleep(60);

    const box = id => document.getElementById(id).closest('.sfind');
    const btn = id => box(id) && box(id).querySelector('button');
    out.короткийНеТронут = !box('short');
    out.длинныйОбёрнут = !!box('long');
    out.множественныйНеТронут = !box('multi');
    out.выключенныйОбёрнут = !!box('off');
    out.выключеннаяКнопкаЗаперта = !!(btn('off') && btn('off').disabled);
    out.ширинаУнаследована = box('long').style.width;
    out.подписьСначала = btn('long').textContent;

    // Код снаружи читает .value — он обязан работать как раньше.
    out.значениеЧитается = document.getElementById('long').value;

    // Открываем список.
    btn('long').click();
    await sleep(60);
    const pop = () => document.getElementById('sfind-pop');
    out.списокОткрылся = !!pop();
    out.полеПоискаЕсть = !!(pop() && pop().querySelector('input'));
    out.строкВсего = pop().querySelectorAll('li[data-i]').length;

    // Ищем.
    const find = pop().querySelector('input');
    find.value = 'серия 1017';
    find.dispatchEvent(new Event('input', {bubbles: true}));
    await sleep(40);
    out.строкПослеПоиска = pop().querySelectorAll('li[data-i]').length;
    out.перваяСтрока = (pop().querySelector('li[data-i]') || {}).textContent;

    // Выбираем мышью — обработчики снаружи должны услышать change.
    let heard = 0;
    document.getElementById('long').addEventListener('change', () => heard++);
    pop().querySelector('li[data-i]').click();
    await sleep(40);
    out.выборДошёл = document.getElementById('long').value;
    out.событиеУслышано = heard;
    out.списокЗакрылся = !pop();
    out.подписьОбновилась = btn('long').textContent;

    // Ничего не нашлось — так и сказано, а не пустота.
    btn('long').click();
    await sleep(40);
    const f2 = pop().querySelector('input');
    f2.value = 'такого нет вовсе';
    f2.dispatchEvent(new Event('input', {bubbles: true}));
    await sleep(40);
    out.пустойПоиск = pop().textContent.includes('ничего не нашлось');
    out.строкПриПустом = pop().querySelectorAll('li[data-i]').length;

    // Клавиатура: стрелка вниз и Enter.
    f2.value = '';
    f2.dispatchEvent(new Event('input', {bubbles: true}));
    await sleep(40);
    const key = (el, k) => el.dispatchEvent(new KeyboardEvent('keydown',
      {key: k, bubbles: true, cancelable: true}));
    key(f2, 'ArrowDown');
    key(f2, 'ArrowDown');
    key(f2, 'Enter');
    await sleep(40);
    out.клавиатураВыбрала = document.getElementById('long').value;
    out.послеEnterЗакрылся = !document.getElementById('sfind-pop');

    // Escape закрывает и ничего не меняет.
    const было = document.getElementById('long').value;
    btn('long').click();
    await sleep(40);
    key(pop().querySelector('input'), 'Escape');
    await sleep(40);
    out.escapeЗакрыл = !document.getElementById('sfind-pop');
    out.escapeНеТронулЗначение = document.getElementById('long').value === было;

    // Значение поменяли программно — подпись обязана догнать при открытии.
    const sel = document.getElementById('long');
    sel.value = 'v7';
    btn('long').click();
    await sleep(40);
    out.подписьПослеПрограммной = btn('long').textContent;
    const выбрано = pop().querySelector('li.cur');
    out.выбранноеПомечено = выбрано ? выбрано.textContent : '';
    sfindClose();
    await sleep(30);

    // Список во всю ширину карточки: ширина задана КЛАССОМ, а не строкой стиля.
    const full = document.createElement('div');
    full.className = 'fld';
    full.style.width = '400px';
    full.innerHTML = `<select id="wide">${many(9)}</select>`;
    app.append(full);
    await sleep(120);
    const wbox = document.getElementById('wide').closest('.sfind');
    out.широкийОбёрнут = !!wbox;
    out.широкийНеСжался = wbox ? wbox.offsetWidth >= 396 : 0;

    // Список, дорисованный ПОСЛЕ отрисовки экрана, тоже получает поиск.
    const late = document.createElement('div');
    late.innerHTML = `<select id="late">${many(12)}</select>`;
    app.append(late);
    await sleep(120);
    out.поздний = !!document.getElementById('late').closest('.sfind');

    // Порог: ровно пять — не трогаем, шесть — трогаем.
    const edge = document.createElement('div');
    edge.innerHTML = `<select id="five">${many(5)}</select>
                      <select id="six">${many(6)}</select>`;
    app.append(edge);
    await sleep(120);
    out.пятьНеТронуто = !document.getElementById('five').closest('.sfind');
    out.шестьТронуто = !!document.getElementById('six').closest('.sfind');
  } catch (e) { out.errs.push(String(e && e.message || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=9000)
    if res.get("error"):
        print("ПРОПУЩЕНО:", res["error"])
        return 0
    check("ошибок на странице нет", res.get("errs"), [])

    print("Кого трогаем, а кого нет")
    check("короткий список не тронут", res.get("короткийНеТронут"), True)
    check("длинный получил поиск", res.get("длинныйОбёрнут"), True)
    check("множественный выбор не тронут", res.get("множественныйНеТронут"), True)
    check("выключенный обёрнут, но кнопка заперта",
          (res.get("выключенныйОбёрнут"), res.get("выключеннаяКнопкаЗаперта")), (True, True))
    check("ширина списка унаследована", res.get("ширинаУнаследована"), "230px")
    # Ширина у половины списков задана классом, а не строкой стиля. Обёртка, которая
    # смотрит только на инлайн, сжала бы такой список по содержимому — поля в карточках
    # разъехались бы.
    check("список во всю ширину обёрнут", res.get("широкийОбёрнут"), True)
    check("и не сжался по содержимому", res.get("широкийНеСжался"), True)

    print("Порог — «больше пяти»")
    check("ровно пять оставлены как были", res.get("пятьНеТронуто"), True)
    check("шесть уже с поиском", res.get("шестьТронуто"), True)

    print("Родной список остаётся источником правды")
    check("значение читается как раньше", res.get("значениеЧитается"), "v0")
    check("подпись показывает выбранное",
          (res.get("подписьСначала") or "").startswith("Профиль 0"), True)

    print("Поиск")
    check("список открылся", res.get("списокОткрылся"), True)
    check("поле поиска есть", res.get("полеПоискаЕсть"), True)
    check("сначала видно все двадцать", res.get("строкВсего"), 20)
    check("после поиска осталась одна", res.get("строкПослеПоиска"), 1)
    check("и это нужная", (res.get("перваяСтрока") or "").strip(),
          "Профиль 17 · серия 1017")
    check("пустой результат назван словами", res.get("пустойПоиск"), True)
    check("и строк при этом нет", res.get("строкПриПустом"), 0)

    print("Выбор доходит до кода, который был написан раньше")
    check("значение поменялось", res.get("выборДошёл"), "v17")
    check("событие change услышано ровно один раз", res.get("событиеУслышано"), 1)
    check("список закрылся", res.get("списокЗакрылся"), True)
    check("подпись обновилась",
          (res.get("подписьОбновилась") or "").startswith("Профиль 17"), True)

    print("Клавиатура")
    # Подсветка начинается не с первой строки, а с ВЫБРАННОЙ: в списке на полсотни
    # профилей стрелка должна идти от того, что стоит сейчас, а не от начала.
    # Выбран был 17-й, два шага вниз — 19-й.
    check("стрелка идёт от выбранного, Enter выбирает",
          res.get("клавиатураВыбрала"), "v19")
    check("после Enter список закрыт", res.get("послеEnterЗакрылся"), True)
    check("Escape закрывает", res.get("escapeЗакрыл"), True)
    check("и ничего не меняет", res.get("escapeНеТронулЗначение"), True)

    print("Значение, поставленное снаружи, не расходится с подписью")
    check("подпись догнала программную запись",
          (res.get("подписьПослеПрограммной") or "").startswith("Профиль 7"), True)
    check("и в списке оно помечено выбранным",
          (res.get("выбранноеПомечено") or "").startswith("Профиль 7"), True)

    print("Списки, дорисованные позже")
    check("поздний список тоже с поиском", res.get("поздний"), True)

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
