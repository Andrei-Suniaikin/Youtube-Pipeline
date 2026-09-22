#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Черновик пресета видит ЛЮБУЮ правку, а не только набор с клавиатуры.

Находка рецензента 20.09.2026 по свежей правке пресетов. Снимок черновика подписан на
`input` и `change` формы, а браузер шлёт эти события только тогда, когда значение
поменял ЧЕЛОВЕК — набрал в поле, выбрал в списке. Всё остальное меняет поле
присваиванием (`el.value = …`), и браузер об этом молчит:

  * крестик у строки карты голосов — это клик, а строка живёт в скрытом поле;
  * «+ шаблон в Lumean» подставляет заведённый id в строку — тоже клик;
  * «Завести новый» у пресета соседа подставляет его имя в выпадающий список.

Итог для человека был такой: удалил язык, ушёл на вкладку другой части, вернулся — а
строка снова на месте, и никто не сказал, что правку выбросили. Пресет «каждый раз
приходится настраивать» ровно этим и продолжался.

Что проверяется здесь, в настоящем браузере (разметку карты собирает JS, и «поле
в стейте» ещё не значит «строка на экране»):
  * удаление строки переживает смену вкладки и перерисовку и уезжает на сервер;
  * добавленная и заполненная строка — тоже;
  * шаблон Lumean, заведённый прямо из формы, — тоже;
  * пресет соседа, выбранный окном «Завести новый», — тоже;
  * переименование целого пресета меняет ЗАГОЛОВОК экрана: пресет после записи
    остаётся открытым, и старое имя в шапке человек читает как «не сохранилось».

Ни сети, ни стенда: сервер подменён, браузер настоящий.

    .venv/bin/python tests/test_preset_draft_clicks_browser.py
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
  addEventListener('unhandledrejection',
                   e => out.errs.push('promise: ' + (e.reason && e.reason.message || e.reason)));
  const tick = ms => new Promise(r => setTimeout(r, ms || 150));
  const val = sel => { const el = document.querySelector(sel); return el ? el.value : null; };
  const langs = () => [...document.querySelectorAll('.langmap-row')].map(r => [
    r.querySelector('.lm-lang').value, r.querySelector('.lm-voice').value]);
  const dbtn = word => [...document.querySelectorAll('dialog[open] button')]
    .find(b => b.textContent.trim() === word);

  // Пресет с сервера. Каждый раз НОВЫЕ объекты: форма подставляет в них черновик, и
  // общая копия соврала бы «набранное сохранилось само».
  const preset = () => ({
    id: 12, name: 'Длинные ролики', scope: 'full',
    note: 'Пустое поле означает «как по умолчанию».',
    users: {channels: [], projects: [], presets: [], via: []},
    groups: [
      {title: 'Озвучка', part: 'voice', readonly: false, link_id: null,
       link_options: [], link_problem: '', fields: [
         {key: 'engine', kind: 'select', label: 'Движок', value: 'lumean', hint: '',
          off: '', placeholder: '',
          options: [{value: 'lumean', title: 'Lumean'}, {value: 'edge', title: 'Edge'}]},
         {key: 'voice_by_lang', kind: 'langmap', label: 'Карта голосов',
          value: 'ru: v-ru, en: v-en', placeholder: '', hint: '', off: '',
          options: [{value: 'ru', title: 'Русский'}, {value: 'en', title: 'Английский'},
                    {value: 'pl', title: 'Польский'}],
          by_engine: {lumean: {label: 'Карта шаблонов', hint: 'id пресета Lumean'},
                      other: {label: 'Карта голосов', hint: 'id голоса'}}}]},
      {title: 'Сценарий', part: 'text', readonly: false, link_id: null,
       link_options: [], link_problem: '', fields: [
         {key: 'preset_rewrite', kind: 'select', label: 'Пресет текста', value: '',
          placeholder: '', hint: '', off: '',
          options: [{value: 'старый', title: 'старый'}]}]},
      {title: 'Картинки', part: 'images', readonly: false, link_id: null,
       link_options: [], link_problem: '', fields: [
         {key: 'image_model', kind: 'text', label: 'Модель', value: 'gpt-image',
          placeholder: '', hint: '', off: ''}]},
    ]});

  const POSTS = [];
  window.post = async (p, body) => {
    POSTS.push({p, body});
    return {ok: true, id: 'tpl-ЗАВЕДЁН'};
  };
  window.api = async (p) => {
    if (/^\/ui\/presets\/vsgen\/\d+$/.test(p)) return preset();
    if (p === '/ui/lumean/templates')
      return {ok: true, templates: [], fields: [
        {key: 'name', kind: 'text', label: 'Название', value: ''},
        {key: 'voice_id', kind: 'text', label: 'ID голоса', value: ''}]};
    if (p === '/ui/presets/rewrite/new/form') return {fields: []};
    if (p === '/ui/presets') return {broken: [], groups: []};
    if (p === '/ui/me') return {name: 'dev'};
    return {};
  };
  // Точка запуска, которую заменил этот сценарий: без неё откат адреса сторожем никто
  // не подхватит, и проверка разошлась бы с боевой страницей.
  addEventListener('hashchange', render);

  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {

  // ── 1. Крестик: удалённая строка не возвращается ──────────────────────────
  location.hash = '#/preset/12';
  await tick(400);
  out.карта_открылась = langs();
  // Убираем английскую строку ровно так, как это делает человек, — кликом.
  const rows = [...document.querySelectorAll('.langmap-row')];
  rows.find(r => r.querySelector('.lm-lang').value === 'en')
      .querySelector('.lm-del').click();
  await tick(200);
  out.после_крестика = val('[data-key="voice_by_lang"]');
  out.удаление_в_черновике = PRESET_DRAFT.values.voice_by_lang;

  // Смена вкладки — это перерисовка формы с сервера. Именно здесь удаление и пропадало.
  location.hash = '#/preset/12/kadry';
  await tick(300);
  location.hash = '#/preset/12/ozvuchka';
  await tick(300);
  out.после_вкладки_строки = langs();
  out.после_вкладки_значение = val('[data-key="voice_by_lang"]');

  // ── 2. Добавленная строка ─────────────────────────────────────────────────
  document.querySelector('.langmap-add').click();
  await tick(150);
  const fresh = [...document.querySelectorAll('.langmap-row')].pop();
  fresh.querySelector('.lm-lang').value = 'pl';
  fresh.querySelector('.lm-lang').dispatchEvent(new Event('change', {bubbles: true}));
  fresh.querySelector('.lm-voice').value = 'v-pl';
  fresh.querySelector('.lm-voice').dispatchEvent(new Event('input', {bubbles: true}));
  await tick(150);
  location.hash = '#/preset/12/kadry';
  await tick(300);
  location.hash = '#/preset/12/ozvuchka';
  await tick(300);
  out.добавленная_пережила = langs();

  // ── 3. Шаблон Lumean, заведённый прямо из формы ───────────────────────────
  document.querySelector('.langmap-lum').click();
  await tick(400);
  out.окно_шаблона = !!document.querySelector('dialog[open]');
  const dlg = document.querySelector('dialog[open]');
  if (dlg){
    dlg.querySelector('[data-lt="name"]').value = 'Проба';
    dlg.querySelector('[data-lt="voice_id"]').value = 'voice-1';
    dbtn('Завести').click();
    await tick(700);
  }
  // Шаблон встал новой строкой; язык человек выбирает сам — без него строка в пресет
  // не уходит и уйти не должна (озвучке нечем читать строку без языка).
  const tpl = [...document.querySelectorAll('.langmap-row')]
    .find(r => r.querySelector('.lm-voice').value === 'tpl-ЗАВЕДЁН');
  out.шаблон_встал_строкой = !!tpl;
  if (tpl){
    tpl.querySelector('.lm-lang').value = 'en';
    tpl.querySelector('.lm-lang').dispatchEvent(new Event('change', {bubbles: true}));
  }
  await tick(150);
  location.hash = '#/preset/12/kadry';
  await tick(300);
  location.hash = '#/preset/12/ozvuchka';
  await tick(300);
  out.шаблон_пережил = val('[data-key="voice_by_lang"]');

  // ── 4. Пресет соседа, выбранный окном «Завести новый» ─────────────────────
  location.hash = '#/preset/12/stsenariy';
  await tick(300);
  document.querySelector('.np-add').click();
  await tick(400);
  const nd = document.querySelector('dialog[open]');
  out.окно_соседа = !!nd;
  if (nd){
    nd.querySelector('#np-name').value = 'новый-текстовый';
    dbtn('Завести').click();
    await tick(700);
  }
  out.сосед_выбран = val('#f-preset_rewrite');
  location.hash = '#/preset/12/kadry';
  await tick(300);
  location.hash = '#/preset/12/stsenariy';
  await tick(300);
  out.сосед_пережил = val('#f-preset_rewrite');

  // ── 5. Всё набранное уезжает одним сохранением ────────────────────────────
  POSTS.length = 0;
  document.querySelector('#save').click();
  await tick(400);
  const saved = POSTS.filter(x => x.p === '/ui/presets/vsgen/12').pop();
  out.ушло_на_сервер = saved ? saved.body.values : null;

  // ── 6. Переименование меняет заголовок экрана ─────────────────────────────
  const nm = document.querySelector('#p-name');
  out.заголовок_до = (document.querySelector('#app h1') || {}).textContent;
  nm.value = 'Короткие ролики';
  nm.dispatchEvent(new Event('input', {bubbles: true}));
  POSTS.length = 0;
  document.querySelector('#save').click();
  await tick(400);
  out.заголовок_после = (document.querySelector('#app h1') || {}).textContent;
  out.имя_ушло = (POSTS.pop() || {body: {}}).body.name;
  // Экран не перерисовывали: заголовок обязан смениться сам, а не «при следующем заходе».
  out.адрес_не_менялся = location.hash;

  } catch (e) { out.сорвалось = String((e && e.message) || e); }
  finish();
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=25000)
    if res.get("error"):
        print("НЕ ПРОГНАТЬ:", res["error"])
        return 2
    check("ошибок в консоли нет", res.get("errs"), [])
    check("сценарий дошёл до конца", res.get("сорвалось"), None)

    print("Крестик у строки — такая же правка, как набор с клавиатуры")
    check("карта открылась двумя строками", res.get("карта_открылась"),
          [["ru", "v-ru"], ["en", "v-en"]])
    check("после крестика в поле осталась одна строка",
          res.get("после_крестика"), "ru: v-ru")
    check("и это записано в черновик", res.get("удаление_в_черновике"), "ru: v-ru")
    check("удалённая строка не вернулась после смены вкладки",
          res.get("после_вкладки_строки"), [["ru", "v-ru"]])
    check("и значение поля осталось прежним",
          res.get("после_вкладки_значение"), "ru: v-ru")

    print("\nДобавленная строка тоже переживает перерисовку")
    check("обе строки на месте", res.get("добавленная_пережила"),
          [["ru", "v-ru"], ["pl", "v-pl"]])

    print("\nШаблон Lumean, заведённый прямо из формы")
    check("окно шаблона открылось", res.get("окно_шаблона"), True)
    check("шаблон встал строкой карты", res.get("шаблон_встал_строкой"), True)
    check("и пережил смену вкладки", res.get("шаблон_пережил"),
          "ru: v-ru, pl: v-pl, en: tpl-ЗАВЕДЁН")

    print("\nПресет соседа, заведённый из формы")
    check("окно соседа открылось", res.get("окно_соседа"), True)
    check("заведённый выбран здесь же", res.get("сосед_выбран"), "новый-текстовый")
    check("и пережил смену вкладки", res.get("сосед_пережил"), "новый-текстовый")

    print("\nОдно сохранение увозит всё, что человек наменял мышью")
    # Уезжают ОТЛИЧИЯ от сохранённого, со всех вкладок сразу: карту голосов правили на
    # «Озвучке», пресет текста — на «Сценарии», а нетронутые `engine` и `image_model`
    # на сервер не идут вовсе — в черновике их нет, и переписывать их нечем.
    check("на сервер уехало всё наменянное и только оно", res.get("ушло_на_сервер"), {
        "voice_by_lang": "ru: v-ru, pl: v-pl, en: tpl-ЗАВЕДЁН",
        "preset_rewrite": "новый-текстовый",
    })

    print("\nПереименование видно в заголовке, а не только в поле")
    check("заголовок был прежним", res.get("заголовок_до"), "Длинные ролики")
    check("новое имя уехало на сервер", res.get("имя_ушло"), "Короткие ролики")
    check("заголовок сменился без перезахода",
          res.get("заголовок_после"), "Короткие ролики")
    check("экран остался тем же", res.get("адрес_не_менялся"), "#/preset/12/stsenariy")

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  •", f)
        return 1
    print("Всё сошлось.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
