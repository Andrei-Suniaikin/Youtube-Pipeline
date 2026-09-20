#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Настройка пресета не начинается заново после каждого «Сохранить».

Жалоба владельца 20.09.2026: «в пресетах когда настраиваю полноценный и нажимаю
сохранить меня перекидывает на страницу с пресетами, удобнее было бы мини пресеты
сразу кидать на главную, а для полноценных держать настройки в стейте и сохранять
когда человек всё настроил, потому что каждый раз приходится настраивать».

Потерь было две, и вторая хуже первой:
  * запись уводила на список — настройка целого пресета шла по одному полю за заход;
  * форма перерисовывается не только при уходе: вкладки частей — это адреса
    (`#/preset/12/kadry`), а переключатель «свои поля / из частичного» перечитывает
    пресет целиком. Каждая перерисовка брала значения с сервера, то есть молча
    стирала набранное. Уход по ссылке «← ко всем пресетам» стирал так же молча.

Что проверяется здесь, в настоящем браузере (разметку формы собирает JS, и «поле
в стейте» ещё не значит «поле на экране»):
  * целый пресет после записи ОСТАЁТСЯ открытым, частичный уходит на список;
  * набранное переживает перерисовку и смену вкладки, а сохраняется — со всех вкладок
    сразу, а не только с открытой;
  * уход с незаписанными правками спрашивает нашим <dialog> (не `confirm`), «Остаться»
    возвращает человека к его же правкам, «Уйти без сохранения» ничего не записывает;
  * поля группы, закрытой ссылкой на частичный пресет, на сервер не уезжают — он такую
    попытку отвергает 400-м (`ui_preset_save`).

Ни сети, ни стенда: сервер подменён, браузер настоящий.

    .venv/bin/python tests/test_preset_save_stay_browser.py
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


def check_in(label, needle, haystack):
    if needle in (haystack or ""):
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        искали:  {needle!r}\n        в тексте: {haystack!r}")
        FAILS.append(label)


SCENARIO = r"""
(async () => {
  const out = {errs: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  addEventListener('unhandledrejection',
                   e => out.errs.push('promise: ' + (e.reason && e.reason.message || e.reason)));
  const tick = ms => new Promise(r => setTimeout(r, ms || 150));
  const txt = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const val = sel => { const el = document.querySelector(sel); return el ? el.value : null; };
  const type = (sel, v) => {
    const el = document.querySelector(sel);
    el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
  };
  const btn = word => [...document.querySelectorAll('dialog[open] button')]
    .find(b => b.textContent.trim() === word);

  // Итог пишется ВСЕГДА, даже если сценарий сорвался на полпути. Иначе сломанная
  // правка выглядит как сломанный прогон («сценарий не отчитался»), и проверка
  // молчит там, где должна назвать, что именно потерялось.
  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {

  // Пресет с сервера. Каждый раз НОВЫЕ объекты: форма подставляет в них черновик,
  // и общая копия соврала бы «набранное сохранилось само».
  const preset = (id) => ({
    id: Number(id), name: 'Длинные ролики',
    scope: String(id) === '13' ? 'voice' : 'full',
    note: 'Пустое поле означает «как по умолчанию».',
    users: {channels: [], projects: [], presets: [], via: []},
    groups: [
      {title: 'Озвучка', part: 'voice', readonly: false, link_id: null,
       link_options: [], link_problem: '', fields: [
         {key: 'voice_speed', kind: 'text', label: 'Скорость', value: '1.0',
          placeholder: '', hint: '', off: ''}]},
      {title: 'Картинки', part: 'images', readonly: false, link_id: null,
       link_options: [], link_problem: '', fields: [
         {key: 'image_model', kind: 'text', label: 'Модель', value: 'gpt-image',
          placeholder: '', hint: '', off: ''}]},
      // Группа, закрытая ссылкой на частичный пресет: её поля не наши.
      {title: 'Клипы', part: 'clips', readonly: true, link_id: 7, link_name: 'Клипы-общие',
       link_options: [{id: 7, name: 'Клипы-общие'}], link_problem: '', fields: [
         {key: 'clip_len', kind: 'text', label: 'Длина клипа', value: '5',
          placeholder: '', hint: '', off: ''}]},
    ]});

  const POSTS = [];
  window.post = async (p, body) => { POSTS.push({p, body}); return {ok: true}; };
  window.api = async (p) => {
    const m = p.match(/^\/ui\/presets\/vsgen\/(\d+)$/);
    if (m) return preset(m[1]);
    if (p === '/ui/presets') return {broken: [], groups: []};
    if (p === '/ui/channel-presets') return {presets: [], note: ''};
    if (p === '/ui/me') return {name: 'dev'};
    return {};
  };
  // Точка запуска, которую заменил этот сценарий: без неё откат адреса сторожем
  // никто не подхватит, и проверка разошлась бы с боевой страницей.
  addEventListener('hashchange', render);

  // ── 1. Целый пресет: записали и остались ──────────────────────────────────
  location.hash = '#/preset/12';
  await tick(300);
  out.форма_открылась = !!document.querySelector('#f-voice_speed');
  type('#f-voice_speed', '1.3');
  document.querySelector('#save').click();
  await tick(300);
  out.адрес_после_записи = location.hash;
  out.форма_осталась = !!document.querySelector('#f-voice_speed');
  out.поле_после_записи = val('#f-voice_speed');
  out.тост = txt(document.querySelector('.toast'));
  out.кнопка_снова_жива = !document.querySelector('#save').disabled;
  out.ушло_на_сервер = POSTS.length ? POSTS[POSTS.length - 1].body : null;

  // Записали — терять нечего: уход больше ничего не спрашивает.
  location.hash = '#/presets';
  await tick(300);
  out.после_записи_ушли_молча = location.hash;
  out.диалога_нет = !document.querySelector('dialog[open]');

  // ── 2. Набранное переживает вкладки и перерисовку ─────────────────────────
  location.hash = '#/preset/12';
  await tick(300);
  // Ключ чужой группы мог остаться в черновике с прошлого захода (ссылку поставили
  // уже после того, как поле набрали) — на сервер он уехать не должен.
  PRESET_DRAFT.values.clip_len = '9';
  type('#f-voice_speed', '1.7');
  location.hash = '#/preset/12/kadry';
  await tick(300);
  out.вкладка_сменилась = !!document.querySelector('#f-image_model');
  out.вкладка_не_спросила = !document.querySelector('dialog[open]');
  type('#f-image_model', 'своя-модель');
  location.hash = '#/preset/12/ozvuchka';
  await tick(300);
  out.правка_пережила_вкладку = val('#f-voice_speed');
  // Та же перерисовка, что делает переключатель «свои поля / из частичного».
  await render();
  await tick(200);
  out.правка_пережила_перерисовку = val('#f-voice_speed');

  POSTS.length = 0;
  document.querySelector('#save').click();
  await tick(300);
  const sent = POSTS.length ? POSTS[POSTS.length - 1].body : {values: {}};
  out.ушло_с_обеих_вкладок = sent.values;
  out.чужое_поле_не_ушло = !('clip_len' in sent.values);
  out.черновик_после_записи = Object.keys(PRESET_DRAFT.values).length;

  // ── 3. Уход с незаписанными правками спрашивает ───────────────────────────
  type('#f-voice_speed', '2.5');
  POSTS.length = 0;
  location.hash = '#/presets';
  await tick(300);
  out.спросили = txt(document.querySelector('dialog[open]'));
  out.адрес_вернулся = location.hash;
  out.правка_на_месте = val('#f-voice_speed');
  const stay = btn('Остаться');
  out.кнопка_остаться = !!stay;
  if (stay) stay.click();
  await tick(300);
  out.после_остаться_адрес = location.hash;
  out.после_остаться_поле = val('#f-voice_speed');
  out.после_остаться_окно = !!document.querySelector('dialog[open]');

  location.hash = '#/presets';
  await tick(300);
  const leave = btn('Уйти без сохранения');
  out.кнопка_уйти = !!leave;
  if (leave) leave.click();
  await tick(400);
  out.после_ухода_адрес = location.hash;
  out.после_ухода_черновик = Object.keys(PRESET_DRAFT.values).length;
  out.уход_ничего_не_записал = POSTS.length;

  // ── 4. Частичный пресет: записали и ушли на список ────────────────────────
  location.hash = '#/preset/13';
  await tick(300);
  out.частичный_открылся = !!document.querySelector('#f-voice_speed');
  out.частичный_только_своя_часть = !document.querySelector('#f-image_model');
  type('#f-voice_speed', '3.0');
  document.querySelector('#save').click();
  await tick(400);
  out.частичный_адрес = location.hash;
  out.частичный_не_спросил = !document.querySelector('dialog[open]');

  } catch (e) { out.сорвалось = String((e && e.message) || e); }
  finish();
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=20000)
    if res.get("error"):
        print("НЕ ПРОГНАТЬ:", res["error"])
        return 2
    check("ошибок в консоли нет", res.get("errs"), [])
    check("сценарий дошёл до конца", res.get("сорвалось"), None)

    print("Целый пресет: записали — и остались настраивать дальше")
    check("форма открылась", res.get("форма_открылась"), True)
    check("после записи остались на форме", res.get("адрес_после_записи"), "#/preset/12")
    check("форма на месте", res.get("форма_осталась"), True)
    check("набранное в поле не сбросилось", res.get("поле_после_записи"), "1.3")
    check("человек видит, что сохранено", res.get("тост"), "Сохранено")
    check("кнопка снова нажимается", res.get("кнопка_снова_жива"), True)
    check("правка уехала на сервер",
          (res.get("ушло_на_сервер") or {}).get("values", {}).get("voice_speed"), "1.3")
    check("после записи уход не спрашивает", res.get("после_записи_ушли_молча"), "#/presets")
    check("окна не было", res.get("диалога_нет"), True)

    print("\nНабранное живёт в стейте: вкладки и перерисовки его не стирают")
    check("вкладка «Картинки» открылась", res.get("вкладка_сменилась"), True)
    check("смена вкладки внутри формы ничего не спрашивает",
          res.get("вкладка_не_спросила"), True)
    check("правка пережила смену вкладки", res.get("правка_пережила_вкладку"), "1.7")
    check("правка пережила перерисовку формы",
          res.get("правка_пережила_перерисовку"), "1.7")
    check("на сервер уехали обе вкладки", res.get("ушло_с_обеих_вкладок"),
          {"voice_speed": "1.7", "image_model": "своя-модель"})
    check("поле чужой (связанной) группы не уехало", res.get("чужое_поле_не_ушло"), True)
    check("после записи черновик пуст", res.get("черновик_после_записи"), 0)

    print("\nУход с незаписанными правками: спрашиваем, а не теряем")
    check_in("окно объясняет, что пропадёт", "не сохранены", res.get("спросили"))
    check_in("и что будет, если уйти", "они пропадут", res.get("спросили"))
    check("адрес вернулся на форму", res.get("адрес_вернулся"), "#/preset/12/ozvuchka")
    check("правка на месте, пока спрашиваем", res.get("правка_на_месте"), "2.5")
    check("есть «Остаться»", res.get("кнопка_остаться"), True)
    check("остались на форме", res.get("после_остаться_адрес"), "#/preset/12/ozvuchka")
    check("правка цела", res.get("после_остаться_поле"), "2.5")
    check("окно закрылось", res.get("после_остаться_окно"), False)
    check("есть «Уйти без сохранения»", res.get("кнопка_уйти"), True)
    check("ушли, куда шли", res.get("после_ухода_адрес"), "#/presets")
    check("черновик выброшен", res.get("после_ухода_черновик"), 0)
    check("уход ничего не записал", res.get("уход_ничего_не_записал"), 0)

    print("\nЧастичный («мини») пресет: записали — и на список")
    check("частичный открылся", res.get("частичный_открылся"), True)
    check("у частичного только своя часть", res.get("частичный_только_своя_часть"), True)
    check("после записи ушли на список", res.get("частичный_адрес"), "#/presets")
    check("и ничего не спросили", res.get("частичный_не_спросил"), True)

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
