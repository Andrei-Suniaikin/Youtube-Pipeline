#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Врезки у кнопок («Создать», «Копия», «Удалить») не двигают вёрстку.

Жалоба владельца 21.09.2026: «нажимаю "Создать" — и всё съезжает». Так и было: врезка
вставлялась соседом кнопки прямо в поток, то есть становилась ещё одним элементом
flex-шапки. Замер до правки: кнопка уезжала с x=646 на x=278, заголовок сужался с 273 до
242 px, высота страницы росла с 1709 до 1851 px. Это общий механизм `inlineBox`, поэтому
то же самое происходило у «Копии» и «Удалить» в строках списка.

Что закрепляет тест:

* **шапка не шевелится**: кнопка, заголовок и высота страницы те же до и после;
* **врезка висит под своей кнопкой**, а не в углу карточки, — иначе непонятно, к чему
  она относится;
* **и не вылезает за правый край**: «Создать» и «Удалить» стоят справа;
* **закрывается мимо выбора** — Esc и клик в стороне; висящее поверх текста меню,
  которое нечем закрыть, хуже прежнего, стоявшего в потоке;
* **то же и в строке списка** — «Копия» не раздвигает строку.

Ни сети, ни стенда: сервер подменён, браузер настоящий.

    .venv/bin/python tests/test_inline_menu_overlay_browser.py
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
  const tick = ms => new Promise(r => setTimeout(r, ms || 200));
  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {

  window.api = async (p) => {
    if (p === '/ui/presets') return {broken: [], groups: [
      {service: 'vsgen', title: 'Производство', items: [
        {key: '1', name: 'Польский длинный', scope: 'full', facts: [], channels: [],
         users: {channels: 0, projects: 0, presets: 0}}]}]};
    if (p === '/ui/channel-presets') return {presets: [], note: ''};
    if (p === '/ui/lumean/templates') return {ok: true, templates: [], fields: []};
    if (p === '/ui/me') return {name: 'dev'};
    return {};
  };
  window.post = async () => ({ok: true});

  addEventListener('hashchange', render);
  location.hash = '#/presets';
  await tick(500);

  const newpr = document.querySelector('#newpr');
  const h1 = document.querySelector('#app h1');
  const snap = () => [Math.round(newpr.getBoundingClientRect().x),
                      Math.round(h1.getBoundingClientRect().width),
                      Math.round(document.body.scrollHeight)];
  out.шапка_до = snap();
  newpr.click();
  await tick(250);
  out.шапка_после = snap();

  const menu = document.querySelector('.inline-ask');
  out.меню_открылось = !!menu;
  if (menu){
    const m = menu.getBoundingClientRect(), b = newpr.getBoundingClientRect();
    out.под_кнопкой = Math.round(m.top - b.bottom);
    // Меню шире кнопки и прижато к тому же правому краю: слева от кнопки его место,
    // справа — край карточки.
    out.не_вылезло_вправо = Math.round(m.right) <= Math.round(
        document.documentElement.clientWidth);
    out.не_ушло_в_угол = Math.round(m.left) > 0;
  }

  // Закрывается Esc, не выбирая пункт.
  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  await tick(150);
  out.esc_закрыл = !document.querySelector('.inline-ask');
  out.шапка_после_esc = snap();

  // Клик в стороне — тоже закрытие.
  newpr.click();
  await tick(200);
  document.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
  await tick(150);
  out.клик_мимо_закрыл = !document.querySelector('.inline-ask');

  // Строка списка: «Копия» не раздвигает строку.
  const copy = document.querySelector('button.pcopy');
  const row = copy.closest('.stage');
  const rowH = () => Math.round(row.getBoundingClientRect().height);
  out.строка_до = rowH();
  copy.click();
  await tick(250);
  out.строка_после = rowH();
  const ask = document.querySelector('.inline-ask');
  out.врезка_в_строке = !!ask;
  if (ask){
    const a = ask.getBoundingClientRect(), c = copy.getBoundingClientRect();
    out.врезка_под_копией = Math.round(a.top - c.bottom);
    out.врезка_не_вылезла = Math.round(a.right) <= Math.round(
        document.documentElement.clientWidth);
  }

  // Узкая ячейка у правого края экрана: так стоят «×» в строках плана и «Снять» у
  // ключей. Врезка вынута из потока, а ширину абсолютного блока браузер отмеряет от
  // его контейнера — в ячейке шириной 64 px вопрос складывался в столбик по слову
  // (замер ревью: 440×50 → 64×169).
  // Ячейка со статичным положением, как в строках кабинета: позицию ей ставит сам
  // `inlineBox`, и ширину врезки браузер отмеряет уже от неё.
  const ряд = document.createElement('div');
  ряд.style.cssText = 'display:flex;justify-content:flex-end;padding:0 12px';
  ряд.innerHTML = '<div id="cell" style="width:64px">'
                + '<button class="small" id="narrow">×</button></div>';
  document.body.prepend(ряд);
  const narrow = ряд.querySelector('#narrow');
  out.ширина_ячейки = ряд.querySelector('#cell').clientWidth;
  const мерить = () => {
    const a = document.querySelector('.inline-ask');
    if (!a) return null;
    const r = a.getBoundingClientRect();
    return {w: Math.round(r.width), h: Math.round(r.height),
            left: Math.round(r.left), right: Math.round(r.right)};
  };
  confirmInline(narrow, 'Отменить у отмеченных? Снимутся и отложенные авто-запуски.',
                () => {});
  await tick(100);
  out.вопрос_в_узкой = мерить();
  askInline(narrow, {ok: 'Задать', placeholder: 'новый пароль'}, () => {});
  await tick(100);
  out.поле_в_узкой = мерить();
  out.ширина_экрана = document.documentElement.clientWidth;

  } catch (e) { out.errs.push('сценарий: ' + ((e && e.stack) || e)); }
  finish();
})();
"""


def main() -> int:
    print("Проверка: врезки у кнопок висят поверх, а не раздвигают страницу")
    res = browser_harness.run_scenario(SCENARIO, budget_ms=9000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2
    check("ошибок в браузере нет", res.get("errs"), [])

    print("\nШапка со «Создать»")
    check("меню открылось", res.get("меню_открылось"), True)
    check("кнопка, заголовок и высота страницы не изменились",
          res.get("шапка_после"), res.get("шапка_до"))
    check("и вернулись теми же после закрытия",
          res.get("шапка_после_esc"), res.get("шапка_до"))
    check("меню стоит вплотную под своей кнопкой", res.get("под_кнопкой"), 6)
    check("не вылезло за правый край", res.get("не_вылезло_вправо"), True)
    check("и не ускакало в левый угол карточки", res.get("не_ушло_в_угол"), True)

    print("\nМеню закрывается, не выбирая пункт")
    check("Esc закрывает", res.get("esc_закрыл"), True)
    check("клик в стороне закрывает", res.get("клик_мимо_закрыл"), True)

    print("\nСтрока списка: «Копия»")
    check("врезка появилась", res.get("врезка_в_строке"), True)
    check("строка не стала выше", res.get("строка_после"), res.get("строка_до"))
    check("врезка под своей кнопкой", res.get("врезка_под_копией"), 6)
    check("и не вылезла за правый край", res.get("врезка_не_вылезла"), True)

    print("\nУзкая ячейка у правого края")
    vw = res.get("ширина_экрана") or 0
    check("ячейка и правда узкая", res.get("ширина_ячейки"), 64)
    for key, what in (("вопрос_в_узкой", "вопрос"), ("поле_в_узкой", "поле ввода")):
        m = res.get(key) or {}
        check(f"{what}: читается строкой, а не столбиком по слову",
              bool(m) and m["w"] >= 240 and m["h"] <= 100, True)
        check(f"{what}: целиком на экране",
              bool(m) and m["left"] >= 0 and m["right"] <= vw, True)

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
