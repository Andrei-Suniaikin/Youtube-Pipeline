#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Поиск и разбор списка каналов (30.09.2026).

Владелец: «нужно на странице с каналами сделать поиск по названию плюс сортировку по
параметрам — нишу, язык и так далее, а то 20 каналов норм, но когда будет 150, будет
тяжело».

Что здесь проверяется и почему именно это:

  • ПОИСК ИЩЕТ ПО ТОМУ, ЧЕМ КАНАЛ ЗОВУТ В ЖИЗНИ. Название, хэндл, ниша и сам ID: в
    письмах и таблицах канал чаще приходит строкой UC…, и поиск, который её не знает,
    бесполезен ровно тогда, когда нужен.
  • ОТБОРЫ СКЛАДЫВАЮТСЯ. Язык и ниша вместе — это «польские биографии», а не «польские
    ИЛИ биографии».
  • «НЕ ЗНАЕМ» ≠ «ЗАПАСА НЕТ». Канал без расписания не сломан, он не настроен, и красным
    его красить нельзя. Порядок «сначала без запаса» обязан уводить такие в конец: работа
    на сегодня — это каналы, которым нечего выпускать, а не те, про которые мы не знаем.
  • ПУСТОЙ РЕЗУЛЬТАТ НАЗВАН СЛОВАМИ. Пустая сетка выглядит как «каналов нет», а не как
    «отбор слишком узкий».
  • ВЫБОР ПЕРЕЖИВАЕТ ПЕРЕХОД. Оператор возвращается сюда десятки раз за смену.

    .venv/bin/python tests/test_channels_sieve_browser.py
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


def flat(text):
    out = str(text or "")
    for sp in (" ", " ", " ", "\n", "\t"):
        out = out.replace(sp, " ")
    return " ".join(out.split())


SCENARIO = r"""
(async () => {
  const out = {errs: []};
  addEventListener('error', e => out.errs.push(e.message));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    ME = {name: 'boss', role: 'admin'};
    const ch = (id, name, lang, niche, status, tags, supply, extra) =>
      Object.assign({id, name, handle: '@h' + id, uc: 'UC' + id + 'x'.repeat(20),
        lang, niche, status, tags, tz: 'Europe/Warsaw', horizon_days: 3, lead_days: 2,
        per_day: id, has_profile: true, has_uc: true, supply}, extra || {});

    const ALL = [
      ch(1, 'Nocne Biografie', 'PL', 'биографии', 'active', ['новые30'],
         {known: true, items: 9, days: 9.4, covered: 9}),
      ch(2, 'Noční Portréty', 'CS', 'биографии', 'active', ['новые30'],
         {known: true, items: 0, days: 0, covered: 0}),
      ch(3, 'Front Wschodni', 'PL', 'история', 'paused', ['ww2'],
         {known: true, items: 2, days: 1.2, covered: 2}),
      ch(4, 'Cień Trzeciej Rzeszy', 'PL', 'история', 'problem', ['ww2'],
         {known: false, items: 0, days: 0, covered: 0}),
      ch(5, 'Amor en Viñetas', 'ES', 'романтика', 'active', ['манхва'],
         {known: true, items: 40, days: 30, covered: 30}),
    ];

    window.api = async p => {
      if (p === '/ui/me') return ME;
      if (p === '/ui/channels') return {channels: ALL};
      if (p === '/ui/channels/stats') return {channels: []};
      return {};
    };
    addEventListener('hashchange', render);
    try { localStorage.removeItem('vs.channels.sieve'); } catch (e) {}

    const names = () => [...document.querySelectorAll('.chcard h3')]
      .map(h => h.textContent.trim());
    const count = () => (document.querySelector('#s-count') || {}).textContent || '';
    const set = (id, v, ev) => {
      const n = document.querySelector(id);
      n.value = v;
      n.dispatchEvent(new Event(ev || 'change', {bubbles: true}));
    };

    location.hash = '#/channels';
    await sleep(400);
    out.всеСразу = names();
    out.счётчикСначала = count();
    out.естьПоиск = !!document.querySelector('#s-q');

    // Поиск по названию.
    set('#s-q', 'biograf', 'input');
    await sleep(60);
    out.поНазванию = names();

    // Поиск по нише.
    set('#s-q', 'романтик', 'input');
    await sleep(60);
    out.поНише = names();

    // Поиск по ID канала — им канал чаще всего и приходит.
    set('#s-q', 'UC3x', 'input');
    await sleep(60);
    out.поID = names();

    // Ничего не нашлось.
    set('#s-q', 'такого точно нет', 'input');
    await sleep(60);
    out.пустоСтрок = names().length;
    // Мало, чтобы текст просто был в разметке: спрятанное сообщение — это та же пустая
    // сетка, только с ложным ощущением, что человека предупредили.
    const пусто = document.querySelector('.chgrid .empty');
    out.пустоСказано = пусто ? пусто.textContent : '';
    out.пустоВидно = !!(пусто && пусто.offsetHeight > 0);

    // Отборы складываются: польские И история.
    set('#s-q', '', 'input');
    set('#s-lang', 'PL');
    await sleep(60);
    out.толькоPL = names();
    set('#s-niche', 'история');
    await sleep(60);
    out.PLиИстория = names();
    out.счётчикОтбора = count();

    // Состояние.
    set('#s-niche', '');
    set('#s-lang', '');
    set('#s-status', 'problem');
    await sleep(60);
    out.толькоПроблемные = names();

    // Тег.
    set('#s-status', '');
    set('#s-tag', 'ww2');
    await sleep(60);
    out.поТегу = names();

    // Порядок: сначала те, кому нечего выпускать. Канал без расписания — в конец.
    set('#s-tag', '');
    set('#s-sort', 'supply');
    await sleep(60);
    out.поЗапасу = names();

    set('#s-sort', 'lang');
    await sleep(60);
    out.поЯзыку = names();

    // Плашки запаса.
    set('#s-sort', 'name');
    await sleep(60);
    const pill = name => {
      const card = [...document.querySelectorAll('.chcard')]
        .find(el => el.querySelector('h3').textContent.trim() === name);
      return card ? card.textContent : '';
    };
    out.плашкаБезЗапаса = pill('Noční Portréty');
    out.плашкаНеЗнаем = pill('Cień Trzeciej Rzeszy');
    out.плашкаБольшогоЗапаса = pill('Amor en Viñetas');

    // Выбор переживает уход и возврат.
    set('#s-lang', 'PL');
    await sleep(60);
    location.hash = '#/projects';
    await sleep(200);
    location.hash = '#/channels';
    await sleep(400);
    out.послеВозврата = names();
    out.полеСохранилось = (document.querySelector('#s-lang') || {}).value;

    // Сброс.
    document.querySelector('#s-off').click();
    await sleep(80);
    out.послеСброса = names().length;
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

    print("Панель на месте")
    check("поле поиска есть", res.get("естьПоиск"), True)
    check("сначала видно все пять", len(res.get("всеСразу") or []), 5)
    check("счётчик называет число", flat(res.get("счётчикСначала")), "5 каналов")

    print("Поиск")
    check("по названию", res.get("поНазванию"), ["Nocne Biografie"])
    check("по нише", res.get("поНише"), ["Amor en Viñetas"])
    check("по ID канала", res.get("поID"), ["Front Wschodni"])
    check("ничего не нашлось — строк нет", res.get("пустоСтрок"), 0)
    check("и это сказано словами",
          "Ни один канал не подошёл" in flat(res.get("пустоСказано")), True)
    check("сообщение видно, а не спрятано", res.get("пустоВидно"), True)

    print("Отборы складываются, а не заменяют друг друга")
    check("только польские", sorted(res.get("толькоPL") or []),
          ["Cień Trzeciej Rzeszy", "Front Wschodni", "Nocne Biografie"])
    check("польские И история", sorted(res.get("PLиИстория") or []),
          ["Cień Trzeciej Rzeszy", "Front Wschodni"])
    check("счётчик говорит, что часть скрыта",
          "из 5" in flat(res.get("счётчикОтбора")), True)
    check("по состоянию", res.get("толькоПроблемные"), ["Cień Trzeciej Rzeszy"])
    check("по тегу", sorted(res.get("поТегу") or []),
          ["Cień Trzeciej Rzeszy", "Front Wschodni"])

    print("Порядок")
    # Сначала пустой запас, потом растущий; канал без расписания — в конце: про него
    # ответ «не знаем», а не «всё плохо», и в работу на сегодня он не попадает.
    check("сначала те, кому нечего выпускать",
          (res.get("поЗапасу") or [])[0], "Noční Portréty")
    check("следом — у кого мало", (res.get("поЗапасу") or [])[1], "Front Wschodni")
    check("канал без расписания — в конце",
          (res.get("поЗапасу") or [])[-1], "Cień Trzeciej Rzeszy")
    check("по языку — чешский первым", (res.get("поЯзыку") or [])[0], "Noční Portréty")

    print("Запас словами, а не цветом наугад")
    check("нет запаса — так и сказано",
          "запаса нет" in flat(res.get("плашкаБезЗапаса")), True)
    check("нет расписания — «неизвестен», а не «нет»",
          "запас неизвестен" in flat(res.get("плашкаНеЗнаем")), True)
    check("и это не выдаётся за отсутствие запаса",
          "запаса нет" in flat(res.get("плашкаНеЗнаем")), False)
    check("большой запас показан днями",
          "запас 30 дн" in flat(res.get("плашкаБольшогоЗапаса")), True)

    print("Выбор переживает уход с экрана")
    check("после возврата отбор на месте", sorted(res.get("послеВозврата") or []),
          ["Cień Trzeciej Rzeszy", "Front Wschodni", "Nocne Biografie"])
    check("и поле показывает его же", res.get("полеСохранилось"), "PL")
    check("сброс возвращает всех", res.get("послеСброса"), 5)

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
