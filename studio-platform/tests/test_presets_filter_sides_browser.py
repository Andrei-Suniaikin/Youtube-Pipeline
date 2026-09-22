#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пресеты каналов и шаблоны Lumean — такие же виды, как остальные.

Жалоба владельца 21.09.2026: «пресеты для канала спрятаны снизу страницы пресетов, нужно
их также поместить в фильтр и в создание новых» и «туда же добавить создание шаблона под
Lumean, а то страница слишком нагружена».

Так и было: фильтр управлял только пресетами производства, а две нижние секции висели
всегда — их нельзя было ни найти фильтром, ни убрать с глаз. Завести их можно было лишь
кнопками внутри этих секций, то есть долистав до низа.

Что закрепляет тест:

* **вид «Каналы» показывает каналы и убирает всё остальное**, включая шаблоны Lumean, —
  иначе «убрать лишнее» не работает;
* **и наоборот**;
* **«Все» возвращает обе секции** — фильтр прячет, а не удаляет;
* **выбор бокового вида не пишет «под фильтр ничего не подошло»**: на экране как раз то,
  что просили, просто не из списка производства. Эта надпись рядом с нужной секцией
  читается как поломка;
* **в шапке заводятся все пять видов**, а не один: производство, обложка, рерайт,
  канал, шаблон Lumean.

Ни сети, ни стенда: соседи подменены, браузер настоящий.

    .venv/bin/python tests/test_presets_filter_sides_browser.py
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
  const tick = ms => new Promise(r => setTimeout(r, ms || 150));
  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {

  window.api = async (p) => {
    if (p === '/ui/presets') return {broken: [], groups: [
      // Пресеты производства тут не проверяются, и лепить их неполными незачем:
      // на неполной строке отрисовка падает раньше фильтра.
      {service: 'vsgen', title: 'Производство', items: []}]};
    if (p === '/ui/channel-presets') return {presets: [
      {id: 1, name: 'Канальный', channels: [], given: []}], note: ''};
    if (p === '/ui/lumean/templates') return {ok: true, templates: [
      {id: 'tpl1', name: 'Шаблон'}], fields: []};
    if (p === '/ui/me') return {name: 'dev'};   // без него оболочка не доходит до экрана
    return {};
  };
  window.post = async () => ({ok: true});

  const has = t => [...document.querySelectorAll('.sec')].some(
      s => s.textContent.includes(t));
  const visible = t => [...document.querySelectorAll('.card')].some(c =>
      c.textContent.includes(t) && c.offsetParent !== null);
  const pick = name => {
    const b = [...document.querySelectorAll('[data-type]')].find(
        x => x.textContent.trim() === name);
    if (b) b.click();
    return !!b;
  };
  // 21.09.2026 выбор видов стал множественным: «Каналы» и «Озвучка Lumean» вместе —
  // законный набор, он показывает обе секции. Поэтому «показать ровно этот вид» —
  // это сброс («Все») и один клик, ровно как делает человек.
  const only = name => { pick('Все'); return pick(name); };

  // Точку запуска приложения заменил этот сценарий — слушателя адреса ставим сами,
  // иначе смена hash никого не разбудит и экран не отрисуется вовсе.
  addEventListener('hashchange', render);

  try{ localStorage.removeItem('presetFilter'); }catch(e){}
  location.hash = '#/presets';
  await tick(500);

  out.есть_вид_каналы = !!document.querySelector('[data-type="channel"]');
  out.есть_вид_lumean = !!document.querySelector('[data-type="lumean"]');
  out.все_видно_сразу = visible('Пресеты каналов') && visible('Шаблоны озвучки Lumean');

  only('Каналы'); await tick(200);
  out.каналы_видны = visible('Пресеты каналов');
  out.lumean_скрыт = !visible('Шаблоны озвучки Lumean');
  out.производство_скрыто = !visible('Производство');
  // Смотреть надо в область экрана, а не в document.body: туда попадает и текст
  // самого <script>, то есть исходник страницы — проверка ловила бы саму себя.
  out.нет_ложного_пусто = !(document.getElementById('app') || {textContent: ''})
      .textContent.includes('Под фильтр');

  only('Озвучка Lumean'); await tick(200);
  out.lumean_виден = visible('Шаблоны озвучки Lumean');
  out.каналы_скрыты = !visible('Пресеты каналов');

  // Набор из двух боковых видов показывает обе секции и по-прежнему убирает
  // производство: выбор нескольких видов читается как «покажи вот это».
  pick('Каналы'); await tick(200);
  out.два_вида_вместе = visible('Пресеты каналов') && visible('Шаблоны озвучки Lumean');
  out.два_вида_без_производства = !visible('Производство');

  only('Все'); await tick(200);
  out.все_вернулось = visible('Пресеты каналов') && visible('Шаблоны озвучки Lumean');

  // Создание: одна кнопка в шапке должна предлагать все пять видов.
  document.querySelector('#newpr').click();
  await tick(150);
  out.заголовки_меню = [...document.querySelectorAll('.mi')].map(
      b => b.firstChild.textContent.trim());
  out.пунктов_меню = [...document.querySelectorAll('.mi')].map(
      b => b.textContent.split('\n')[0].trim().slice(0, 24));

  } catch (e) { out.errs.push('сценарий: ' + (e && e.message || e)); }
  finish();
})();
"""


def main() -> int:
    print("Проверка: боковые виды пресетов в фильтре и в создании")
    res = browser_harness.run_scenario(SCENARIO, budget_ms=8000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2
    check("ошибок в браузере нет", res.get("errs"), [])

    print("\nВиды появились в фильтре")
    check("есть вид «Каналы»", res.get("есть_вид_каналы"), True)
    check("есть вид «Озвучка Lumean»", res.get("есть_вид_lumean"), True)
    check("при «Все» видно обе секции", res.get("все_видно_сразу"), True)

    print("\nВид «Каналы» убирает лишнее")
    check("каналы видны", res.get("каналы_видны"), True)
    check("шаблоны Lumean скрыты", res.get("lumean_скрыт"), True)
    check("производство скрыто", res.get("производство_скрыто"), True)
    check("нет ложного «под фильтр ничего не подошло»",
          res.get("нет_ложного_пусто"), True)

    print("\nВид «Озвучка Lumean» — наоборот")
    check("шаблоны видны", res.get("lumean_виден"), True)
    check("каналы скрыты", res.get("каналы_скрыты"), True)
    check("«Все» возвращает обе", res.get("все_вернулось"), True)

    print("\nДва боковых вида вместе — законный набор, а не пустой экран")
    check("видны обе секции", res.get("два_вида_вместе"), True)
    check("производство по-прежнему убрано", res.get("два_вида_без_производства"), True)

    # 21.09.2026: сюда же пресеты обложки и рерайта. До этого завести пресет обложки
    # значило сперва завести обёртку — частичный пресет производства «Только обложка»,
    # у которого всё содержимое — одна ссылка на пресет Cover Studio, — и только из его
    # редактора добраться до настоящего заведения. Два заведения на одно намерение.
    # 22.09.2026 владелец: одна студия — пресеты текста и обложки стали частями общего
    # пресета, отдельных пунктов у них в «Создать» больше нет.
    print("\nСоздание: пресет, пресет канала, шаблон Lumean — всё из шапки")
    items = res.get("пунктов_меню") or []
    check("пунктов три", len(items), 3)
    titles = res.get("заголовки_меню") or []
    check("заголовки пунктов", titles, ["Пресет", "Пресет канала", "Шаблон озвучки Lumean"])
    check("есть канал", any("канал" in x.lower() for x in items), True)
    check("есть Lumean", any("lumean" in x.lower() for x in items), True)

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
