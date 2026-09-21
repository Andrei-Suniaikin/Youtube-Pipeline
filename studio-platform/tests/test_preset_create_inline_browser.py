#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пресет заводится прямо в форме настроек — без экрана «название + тип».

Жалоба владельца 21.09.2026: «имя — как будто уже настройка пресета», и до настроек
приходилось идти через два экрана. Так и было: `#/preset/new` показывал карточку типа и
карточку имени, и только «Завести» открывала поля. Имя при этом требовалось заранее,
хотя нужно оно ровно в момент записи: дальше его никто не проверяет, а пустым кабинет
имя и затереть-то не может (`COALESCE(NULLIF(?,''),name)`).

Что закрепляет тест:

* **настройки видны сразу** — на `#/preset/new` есть поля и вкладки, а не одна кнопка;
* **тип переключается в самой форме**, и набор вкладок за ним следует;
* **пустое имя не мешает настраивать**, а останавливает только запись — и говорит об
  этом у самого поля, а не тостом поверх экрана; на сервер при этом ничего не уходит;
* **первое «Сохранить» заводит пресет** и переносит человека в редактор заведённого,
  вместе со всем набранным;
* **уход с недозаведённого спрашивает**: набранное нигде не записано, и терять его
  молча нельзя.

Ни сети, ни стенда: сервер подменён, браузер настоящий.

    .venv/bin/python tests/test_preset_create_inline_browser.py
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
  const out = {errs: [], posts: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  const tick = ms => new Promise(r => setTimeout(r, ms || 200));
  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {

  const fld = (key, label) => ({key, label, kind: 'text', value: ''});
  const group = (title, part, key) => ({title, part, fields: [fld(key, title)],
    source: 'own', readonly: false, link_id: null, link_name: null,
    link_problem: '', link_options: []});
  const ALL = [group('Озвучка', 'voice', 'engine'), group('Картинки', 'images', 'style'),
               group('Обложка', 'cover', 'preset_cover')];

  window.api = async (p) => {
    if (p === '/ui/me') return {name: 'dev'};
    if (p.startsWith('/ui/presets/vsgen/new/form')){
      const scope = decodeURIComponent((p.split('scope=')[1] || 'full'));
      return {id: null, name: '', scope, links: {}, link_problems: [], users: {},
              groups: scope === 'full' ? ALL : ALL.filter(g => g.part === scope),
              note: 'подсказка'};
    }
    if (p === '/ui/presets/vsgen/7')
      return {id: 7, name: 'Обложечный', scope: 'cover', links: {}, link_problems: [],
              users: {channels: [], projects: []},
              groups: ALL.filter(g => g.part === 'cover'), note: 'подсказка'};
    return {};
  };
  window.post = async (p, body) => {
    out.posts.push({p, body});
    if (p === '/ui/presets/vsgen') return {id: 7, name: body.name, scope: body.scope};
    return {ok: true};
  };

  addEventListener('hashchange', render);
  location.hash = '#/preset/new';
  await tick(500);

  // 1. Настройки на первом же экране, без промежуточного «Завести».
  out.поля_сразу = !!document.querySelector('.fields .fld');
  out.вкладок_у_целого = document.querySelectorAll('.subtabs a').length;
  out.поле_имени_пустое = document.querySelector('#p-name').value;
  out.есть_кнопка_сохранить = !!document.querySelector('#save');

  // 2. Пустое имя не мешает набирать настройки, но останавливает запись — и говорит
  //    об этом у поля.
  const first = document.querySelector('.fields [data-key]');
  first.value = 'набрано до имени';
  first.dispatchEvent(new Event('input', {bubbles: true}));
  document.querySelector('#save').click();
  await tick(300);
  out.без_имени_ничего_не_ушло = out.posts.length;
  out.отказ_у_поля = !document.querySelector('#p-name-err').hidden;
  out.отказ_называет_причину =
      (document.querySelector('#p-name-err').textContent || '').includes('имя');
  out.набранное_на_месте = document.querySelector('.fields [data-key]').value;

  // 3. Тип переключается в самой форме, вкладки следуют за ним.
  out.кнопок_типа = document.querySelectorAll('#scope-modes button').length;
  [...document.querySelectorAll('#scope-modes button')]
      .find(b => b.dataset.scope === 'cover').click();
  await tick(500);
  out.вкладок_у_частичного = document.querySelectorAll('.subtabs a').length;
  out.тип_подсвечен = document.querySelector('[data-scope="cover"]').classList
      .contains('on');
  // Набранное для «Картинок» в пресет «Только обложка» уехать не должно: этого поля
  // у него нет и поправить его негде.
  const nm = document.querySelector('#p-name');
  nm.value = 'Обложечный';
  nm.dispatchEvent(new Event('input', {bubbles: true}));
  const own = document.querySelector('.fields [data-key]');
  own.value = 'свой пресет обложки';
  own.dispatchEvent(new Event('input', {bubbles: true}));

  // 4. Первое «Сохранить» заводит пресет и уводит в его редактор.
  document.querySelector('#save').click();
  await tick(800);
  out.posts_после = out.posts.map(x => x.p);
  const создание = out.posts.find(x => x.p === '/ui/presets/vsgen');
  out.создание_тело = создание ? создание.body : null;
  const запись = out.posts.find(x => x.p === '/ui/presets/vsgen/7');
  out.записанные_поля = запись ? запись.body.values : null;
  out.адрес_после = location.hash;
  await tick(400);
  out.заголовок_после = (document.querySelector('#app h1') || {}).textContent;
  out.кнопок_типа_после = document.querySelectorAll('#scope-modes button').length;

  // 5. Уход с недозаведённого спрашивает.
  location.hash = '#/preset/new';
  await tick(500);
  const nm2 = document.querySelector('#p-name');
  nm2.value = 'Брошенный';
  nm2.dispatchEvent(new Event('input', {bubbles: true}));
  location.hash = '#/presets';
  await tick(400);
  const dlg = document.querySelector('dialog[open]');
  out.спросили_при_уходе = !!dlg && dlg.textContent.includes('не заведён');
  out.адрес_остался = location.hash;

  } catch (e) { out.errs.push('сценарий: ' + ((e && e.stack) || e)); }
  finish();
})();
"""


def main() -> int:
    print("Проверка: заведение пресета идёт внутри формы настроек")
    res = browser_harness.run_scenario(SCENARIO, budget_ms=12000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2
    check("ошибок в браузере нет", res.get("errs"), [])

    print("\nНастройки открываются сразу, без экрана «название + тип»")
    check("поля видны на первом же экране", res.get("поля_сразу"), True)
    check("у целого — все вкладки частей", res.get("вкладок_у_целого"), 3)
    check("имя пустое и настраивать не мешает", res.get("поле_имени_пустое"), "")
    check("кнопка записи — обычное «Сохранить»", res.get("есть_кнопка_сохранить"), True)

    print("\nИмя требуется в момент записи, и отказ показан у поля")
    check("на сервер без имени ничего не ушло",
          res.get("без_имени_ничего_не_ушло"), 0)
    check("отказ показан у самого поля", res.get("отказ_у_поля"), True)
    check("и называет, чего не хватает", res.get("отказ_называет_причину"), True)
    check("набранное никуда не делось", res.get("набранное_на_месте"),
          "набрано до имени")

    print("\nТип переключается в самой форме")
    check("вариантов семь — целый и шесть частей", res.get("кнопок_типа"), 7)
    check("у частичного вкладка одна", res.get("вкладок_у_частичного"), 1)
    check("выбранный тип подсвечен", res.get("тип_подсвечен"), True)

    print("\nПервое «Сохранить» заводит пресет")
    check("сначала заведение, потом запись полей", res.get("posts_после"),
          ["/ui/presets/vsgen", "/ui/presets/vsgen/7"])
    check("заведение унесло имя и выбранный тип", res.get("создание_тело"),
          {"name": "Обложечный", "scope": "cover"})
    # Набранное для «Картинок» осталось в прошлом типе: у пресета «Только обложка»
    # такого поля нет, и уехать оно туда не должно.
    check("уехали поля выбранного типа, и только они",
          res.get("записанные_поля"), {"preset_cover": "свой пресет обложки"})
    check("человек оказался в редакторе заведённого", res.get("адрес_после"),
          "#/preset/7")
    check("и видит его имя в заголовке", res.get("заголовок_после"), "Обложечный")
    check("кнопок типа у заведённого больше нет — тип выбирают один раз",
          res.get("кнопок_типа_после"), 0)

    print("\nУход с недозаведённого спрашивает, а не теряет молча")
    check("спросили, и сказали про незаведённый пресет",
          res.get("спросили_при_уходе"), True)
    check("адрес остался на форме", res.get("адрес_остался"), "#/preset/new")

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
