#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Облик «Монтажная» (выбор владельца 22.09.2026) — то, что он выбрал, и то, что
нашла проверка облика на всех экранах.

Выбор владельца: Монтажная и три доработки —
  * «готово» — самое тихое (бледный тон и ✓), яркой заливкой — только ошибка;
  * плашка «задачи никто не выполняет» — янтарная, с полосой слева;
  * выбор режима «Ссылка / С нуля» — сегментом, НЕ цветом платной кнопки;
и отдельно: кнопки на вкладке «Пресеты» — значками, как IconButton в MUI.

Находки проверки облика, которые закреплены здесь: нативные части тёмные (белая пустая
галочка читалась как отмеченная), у галочек видно кольцо фокуса, подсказки полей
читаются, недоступное поле не выглядит редактируемым, кнопка действия — тёмным текстом
на «кости», статус канала — словами и глаголами.

    .venv/bin/python tests/test_look_montazh_browser.py
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
  const cs = (el, p) => getComputedStyle(el).getPropertyValue(p).trim();
  try {
  const add = html => { const d = document.createElement('div'); d.innerHTML = html;
                        document.getElementById('app').append(d); return d; };
  out.схема = cs(document.documentElement, 'color-scheme');

  // «Кость» — действие: текст на ней тёмный, а не белый.
  const btn = add('<button id="t-act">Начать</button>').firstElementChild;
  out.текст_на_кости = cs(btn, 'color');

  // Доработка 1: «готово» в дорожке списка — бледный тон и ✓, ошибка — сплошная.
  const t = add(`<table><tbody><tr class="row"><td></td><td></td><td>
      <span class="pill s-done" id="t-done">текст</span>
      <span class="pill s-idle" id="t-idle">ролик</span>
      <span class="pill s-error" id="t-err">сбой</span></td></tr></tbody></table>`);
  const done = t.querySelector('#t-done'), err = t.querySelector('#t-err');
  out.готово_фон = cs(done, 'background-color');
  out.готово_галочка = getComputedStyle(done, '::before').content;
  out.ошибка_фон = cs(err, 'background-color');
  out.ошибка_знак = getComputedStyle(err, '::before').content;
  out.не_начато_цвет = cs(t.querySelector('#t-idle'), 'color');

  // Доработка 2: янтарная плашка.
  const wrap = schedulerCard({known: true, on: false, why: 'исполнитель выключен'});
  document.getElementById('app').append(wrap);
  const alert = wrap.querySelector('.card');          // `$()` отдаёт обёртку
  out.плашка_полоса = cs(alert, 'box-shadow');
  out.плашка_заголовок = cs(alert.querySelector('.name'), 'color');

  // Доработка 3: выбранный режим — не цвет «кости».
  const modes = add(`<div class="modes"><button class="on" id="t-on">Ссылка</button>
                     <button>С нуля</button></div>`);
  out.режим_фон = cs(modes.querySelector('#t-on'), 'background-color');

  // Значки как в MUI: круглые, без рамки, с подписью.
  const ib = add(iconBtn('pedit', 'edit', 'Править', 'Базовый')).firstElementChild;
  out.значок = {радиус: cs(ib, 'border-radius'), рамка: cs(ib, 'border-top-width'),
                svg: !!ib.querySelector('svg'), подпись: ib.getAttribute('aria-label')};

  // Галочка: кольцо фокуса с клавиатуры видно.
  const cb = add('<input type="checkbox" id="t-cb">').firstElementChild;
  cb.focus();
  out.фокус_галочки = cs(cb, 'outline-style');
  // Подсказка поля читается; недоступное поле — не как редактируемое.
  const inp = add('<input id="t-in" placeholder="1.0"><input id="t-dis" disabled value="x">');
  out.подсказка_поля = getComputedStyle(inp.querySelector('#t-in'), '::placeholder').color;
  out.недоступное = cs(inp.querySelector('#t-dis'), 'border-top-style');

  // Шапка проекта: этап, который ещё впереди, — пустой контур, а не зачёркнутый.
  const strip = add(`<div class="head"><div>${stageStripHtml({segments: [
    {stage: 'tts', state: 'done'}, {stage: 'video', state: 'pending'}], caption: 'x',
    tone: 'dim'})}</div></div>`);
  const pend = strip.querySelectorAll('.segs i')[1];
  out.впереди = {cls: pend.className, фон: cs(pend, 'background-image')};

  // Скругления (владелец 22.09.2026: «немного квадратненько»).
  const card = add('<div class="card"><div class="sec">x</div></div>').firstElementChild;
  out.радиусы = {панель: cs(card, 'border-top-left-radius'),
                 кнопка: cs(btn, 'border-top-left-radius'),
                 поле: cs(inp.querySelector('#t-in'), 'border-top-left-radius')};

  // Каналы: кнопки — в строке заголовка.
  window.api = async p => p === '/ui/channels' ? {channels: []} : {};
  await viewChannels();
  const h = document.querySelector('#app .head');
  out.каналы_шапка = h ? [!!h.querySelector('h1'), !!h.querySelector('#impch'),
                          !!h.querySelector('#newch')] : null;
  document.getElementById('app').innerHTML = '';

  // Статус канала: текущий виден, переходы — глаголами.
  const root = add('<div class="sec"><span id="statusbtns"></span></div><div id="editbox"></div>');
  window.api = async () => ({statuses: [{key: 'active', title: 'работает'},
    {key: 'paused', title: 'пауза'}, {key: 'archived', title: 'в архиве'}], fields: []});
  try { await wireChannelEdit(root, {id: 1, status: 'active'}, {}); } catch (e) {}
  out.статус_канала = [...root.querySelectorAll('#statusbtns > *')].map(x => x.textContent.trim());
  } catch (e) { out.errs.push(String(e && e.stack || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=5000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2
    check("ошибок нет", res.get("errs"), [])
    check("нативные части тёмные", res.get("схема"), "dark")
    check("на «кости» тёмный текст", res.get("текст_на_кости"), "rgb(27, 28, 31)")

    print("\nДоработка 1: «готово» тихое, яркое — только ошибка")
    check("«готово» — бледный тон, а не сплошная заливка", res.get("готово_фон"),
          "rgba(141, 219, 166, 0.14)")
    check("и с ✓", res.get("готово_галочка"), '"✓ "')
    check("ошибка — сплошная яркая", res.get("ошибка_фон"), "rgb(232, 96, 77)")
    check("и с «!»", res.get("ошибка_знак"), '"! "')
    check("«не начато» читается (5.35:1)", res.get("не_начато_цвет"), "rgb(163, 165, 172)")

    print("\nДоработка 2: плашка тревоги янтарная")
    check("полоса слева янтарная", "rgb(239, 192, 99)" in (res.get("плашка_полоса") or ""), True)
    check("заголовок янтарный", res.get("плашка_заголовок"), "rgb(239, 192, 99)")

    print("\nДоработка 3: режим — сегментом, не цветом платной кнопки")
    check("выбранный режим не «кость»", res.get("режим_фон") != "rgb(230, 226, 216)", True)

    print("\nЗначки как в MUI")
    check("круглые, без рамки, со значком и подписью", res.get("значок"),
          {"радиус": "50%", "рамка": "0px", "svg": True, "подпись": "Править «Базовый»"})

    print("\nНаходки проверки облика")
    check("у галочки видно кольцо фокуса", res.get("фокус_галочки"), "solid")
    check("подсказка поля читается (5.4:1)", res.get("подсказка_поля"), "rgb(148, 150, 157)")
    check("недоступное поле — пунктиром", res.get("недоступное"), "dashed")
    check("этап впереди — контур, не зачёркнут", res.get("впереди"),
          {"cls": "idle", "фон": "none"})
    check("скругления: панель 10, кнопка и поле 7", res.get("радиусы"),
          {"панель": "10px", "кнопка": "7px", "поле": "7px"})
    check("каналы: заголовок и обе кнопки — одной строкой", res.get("каналы_шапка"),
          [True, True, True])
    check("статус канала виден, переходы — глаголами", res.get("статус_канала"),
          ["работает", "Поставить на паузу", "Убрать в архив"])
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
