#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фильтр списка пресетов по названию и типу (17.09.2026) — поведение в браузере.

Настоящий `web/index.html` в headless Chrome, `api()` подменён набором пресетов всех
видов. Без стенда и сети.

    .venv/bin/python tests/test_preset_filter_browser.py
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
const sleep = ms => new Promise(r => setTimeout(r, ms));
const item = (key, name, scope) => ({key, name, scope, facts: [], channels: [],
  users: {channels: 0, projects: 0, presets: 0}});
const DATA = {broken: [], groups: [
  {service: 'vsgen', title: 'Производство', items: [
    item('1', 'Польский длинный', 'full'), item('2', 'Английский короткий', 'full'),
    item('3', 'Голос Анна', 'voice'), item('4', 'Кадры польские', 'images'),
    item('5', 'Сборка быстрая', 'render')]},
  {service: 'rewrite', title: 'Рерайт', items: [
    {key: 'r', name: 'Польский рерайт', facts: [], channels: []}]},
]};
api = async (path) => {
  if (path === '/ui/presets') return JSON.parse(JSON.stringify(DATA));
  throw new Error('неожиданный запрос ' + path);
};
(async () => {
  const out = {};
  try {
    try { localStorage.removeItem('presetFilter'); } catch (e) {}
    presetFilter.q = ''; presetFilter.types = [];
    await viewPresets();
    const names = () => [...app.querySelectorAll('.stage .name')].map(x => x.textContent.trim());
    const click = code => app.querySelector(`[data-type="${code}"]`).click();
    // Выбор стал множественным, поэтому «показать ровно этот вид» — это сброс («Все»)
    // и один клик. Так же поступает и человек: «Все» стоит первым пунктом списка.
    const only = code => { click('all'); click(code); };
    const label = () => app.querySelector('#pf-open').textContent.trim();
    const type = q => { const i = app.querySelector('#pf-q'); i.value = q; i.dispatchEvent(new Event('input')); };
    out.all = names();
    out.labelAll = label();
    out.buttons = [...app.querySelectorAll('[data-type]')].map(b => b.textContent.trim());
    out.menuHidden = app.querySelector('#pf-types').hidden;
    only('full');    out.full = names();
    out.fullOn = app.querySelector('[data-type="full"]').classList.contains('on');
    out.labelFull = label();
    // Множественный выбор: «Целые» + «Картинки» показывают и то и другое, а подпись
    // кнопки перечисляет выбранное.
    click('images'); out.twoKinds = names(); out.labelTwo = label();
    click('voice'); click('clips'); out.labelMany = label();
    only('partial'); out.partial = names();
    only('images');  out.images = names();
    only('clips');   out.clipsEmpty = app.textContent.includes('Под фильтр ничего не подошло');
    click('all');     type('польск'); out.search = names();
    click('full');    out.searchFull = names();
    // Повторный клик по выбранному виду снимает его — иначе из набора не выйти.
    click('full');   out.offAgain = names();
    type('нет такого'); click('all'); out.noneEmpty = app.textContent.includes('Под фильтр ничего не подошло');
    let saved = null; try { saved = JSON.parse(localStorage.getItem('presetFilter')); } catch (e) {}
    out.saved = saved;
  } catch (e) { out.error = String(e && e.stack || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO)
    check("сценарий выполнен без ошибок", res.get("error"), None)
    # 21.09.2026: к шести частям производства добавились два «боковых» вида — пресеты
    # каналов и шаблоны Lumean. До этого они висели внизу страницы всегда, мимо фильтра:
    # их нельзя было ни найти, ни убрать с глаз, а страница получалась перегруженной.
    # Поведение этих видов проверяет tests/test_presets_filter_sides_browser.py; здесь
    # они закреплены в общем списке, чтобы их не потеряли молча.
    check("кнопки типа — все варианты сразу", res.get("buttons"),
          ["Все", "Целые", "Частичные", "Озвучка", "Картинки", "Клипы", "Сборка",
           "Сценарий", "Обложка", "Каналы", "Озвучка Lumean"])
    check("без фильтра видно всё", sorted(res.get("all") or []),
          sorted(["Польский длинный", "Английский короткий", "Голос Анна", "Кадры польские",
                  "Сборка быстрая", "Польский рерайт"]))
    check("«Целые» — только целые пресеты производства", res.get("full"),
          ["Польский длинный", "Английский короткий"])
    check("выбранная кнопка подсвечена", res.get("fullOn"), True)
    check("«Частичные» — все частичные, по частям", res.get("partial"),
          ["Голос Анна", "Кадры польские", "Сборка быстрая"])
    check("«Картинки» — только частичные картинки", res.get("images"), ["Кадры польские"])
    check("повторный клик снимает вид — набор пустеет обратно во «все»",
          sorted(res.get("offAgain") or []),
          sorted(["Польский длинный", "Кадры польские", "Польский рерайт"]))
    check("пустой результат подписан, а не пустое место", res.get("clipsEmpty"), True)
    check("поиск по названию без учёта регистра, во всех сервисах", res.get("search"),
          ["Польский длинный", "Кадры польские", "Польский рерайт"])
    check("поиск и тип вместе", res.get("searchFull"), ["Польский длинный"])
    check("ничего не найдено — подпись", res.get("noneEmpty"), True)
    check("фильтр запоминается у зрителя", res.get("saved"),
          {"q": "нет такого", "types": []})

    print("\nОдиннадцать кнопок свёрнуты в один список (жалоба владельца 21.09.2026)")
    check("список закрыт, пока его не открыли", res.get("menuHidden"), True)
    check("подпись кнопки без выбора — «Все виды»", res.get("labelAll"), "Все виды")
    check("подпись кнопки называет выбранное", res.get("labelFull"), "Целые")

    print("\nВыбор множественный")
    check("«Целые» + «Картинки» показывают и то и другое", res.get("twoKinds"),
          ["Польский длинный", "Английский короткий", "Кадры польские"])
    check("подпись перечисляет оба", res.get("labelTwo"), "Целые, Картинки")
    check("длинный набор подписан коротко, а не списком",
          res.get("labelMany"), "Целые, Картинки +2")

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
