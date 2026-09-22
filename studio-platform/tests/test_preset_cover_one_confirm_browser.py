#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пресет обложки — обычная часть пресета, заводится одним подтверждением.

История: 21.09.2026 владелец пожаловался на «второе создать» у пресета обложки — чтобы
его завести, сперва заводили ДРУГОЙ объект, частичный пресет «Только обложка» со ссылкой
на пресет обложек, и лишь из его редактора добирались до настоящего заведения. Поле
«образец стиля» при этом стояло запертым: картинку принимают только по имени УЖЕ
существующего пресета.

22.09.2026 владелец: «нет больше отдельных студий, у нас одна студия; эти пресеты —
такие же обычные, как картинки и монтаж». Поэтому отдельного пункта «Пресет обложки» в
«Создать» больше нет: обложка — одна из частей на общем экране заведения, как «Только
картинки», и её поля стоят там же, под выбором типа, — без отдельного окна.

Что закрепляет тест:

* в «Создать» нет отдельных пунктов для обложки и текста — один «Пресет»;
* «Только обложка» показывает поля обложки прямо на экране заведения, без окна;
* подтверждение одно — «Завести», и после него пресет есть, а человек — в списке;
* образец стиля не заперт и уходит тем же нажатием, следом за созданием;
* нигде не сказано про «соседний сервис» и «Cover Studio».

Ни сети, ни стенда: сервер подменён, браузер настоящий.

    .venv/bin/python tests/test_preset_cover_one_confirm_browser.py
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
  const out = {errs: [], posts: [], загрузки: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  addEventListener('unhandledrejection',
                   e => out.errs.push('promise: ' + (e.reason && e.reason.message || e.reason)));
  const tick = ms => new Promise(r => setTimeout(r, ms || 200));
  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {
  const fld = (key, label) => ({key, label, kind: 'text', value: ''});
  const group = (title, part, key) => ({title, part, fields: [fld(key, title)],
    source: 'own', readonly: false, link_id: null, link_name: null,
    link_problem: '', link_options: []});
  window.api = async (p) => {
    if (p === '/ui/presets') return {broken: [], groups: [
      {service: 'vsgen', title: 'Производство', items: []}]};
    if (p === '/ui/channel-presets') return {presets: [], note: ''};
    if (p === '/ui/lumean/templates') return {ok: true, templates: [], fields: []};
    if (p === '/ui/me') return {name: 'dev'};
    if (p.startsWith('/ui/presets/vsgen/new/form')) return {id: null, name: '',
      scope: 'full', links: {}, link_problems: [], users: {}, note: '',
      groups: [group('Картинки', 'images', 'style'), group('Обложка', 'cover', 'preset_cover')]};
    if (p === '/ui/presets/cover/new/form') return {service: 'cover', name: '', fields: [
      {key: 'style_prompt', kind: 'text', label: 'Промпт стиля', value: ''},
      {key: 'seed_reference_image', kind: 'file', label: 'Образец стиля', value: ''}]};
    return {};
  };
  window.post = async (p, body) => { out.posts.push({p, body}); return {ok: true}; };
  // Картинка уходит не через post(), а обычным fetch с FormData — файл в JSON не
  // положишь. Подменяем его, чтобы увидеть, ушла ли она и по какому имени.
  window.fetch = async (url, opts) => {
    out.загрузки.push(String(url).split('/studio').pop());
    return {ok: true, json: async () => ({filename: 'proba.png'})};
  };

  addEventListener('hashchange', render);
  location.hash = '#/presets';
  await tick(500);

  // «Создать»: один «Пресет», без отдельных пунктов обложки и текста.
  document.querySelector('#newpr').click();
  await tick(200);
  out.пункты = [...document.querySelectorAll('.mi')].map(
      b => b.firstChild.textContent.trim());
  const пункт = [...document.querySelectorAll('.mi')].find(
      b => b.firstChild.textContent.trim() === 'Пресет');
  if (пункт) пункт.click();
  await tick(500);
  out.экран = location.hash;

  // Имя, набранное до выбора типа, не теряется при переходе к форме части — и сторож
  // формы «Целый» после этого не кричит «не заведён» (ревью 22.09.2026).
  const имя = document.querySelector('#p-name');
  имя.value = 'Обложки польские';
  имя.dispatchEvent(new Event('input', {bubbles: true}));
  // «Только обложка» — поля обложки прямо здесь, без окна.
  const тип = document.querySelector('#scope-modes [data-scope="cover"]');
  if (тип) тип.click();
  await tick(600);
  out.окна_нет = !document.querySelector('dialog[open]');
  const панель = document.querySelector('.inpanel');
  out.панель_есть = !!панель;
  if (панель){
    out.поля = !!панель.querySelector('[data-key="style_prompt"]');
    const ref = панель.querySelector('#np-ref');
    out.образец_не_заперт = !!ref && !ref.disabled;
    out.кнопки = [...панель.querySelectorAll('.mfoot button')].map(b => b.textContent.trim());
    out.текст_панели = панель.textContent + ' ' + [...панель.querySelectorAll('.q')]
      .map(q => q.getAttribute('data-hint')).join(' ');
    out.имя_перенесено = панель.querySelector('#np-name').value;
    // Перенесённое имя — уже набранное: уход без единой правки в форме тоже спрашивает.
    out.имя_под_сторожем = !!(LEAVE_GUARD && LEAVE_GUARD.dirty());
    // Набранное в форме части защищено от ухода, как и у остальных типов.
    const промпт = панель.querySelector('[data-key="style_prompt"]');
    промпт.value = 'тёплый свет';
    промпт.dispatchEvent(new Event('input', {bubbles: true}));
    location.hash = '#/presets';
    await tick(300);
    const спрос = document.querySelector('dialog[open]');
    out.уход_спросил = !!спрос && спрос.textContent.includes('не сохранены');
    out.уход_остался = location.hash;
    if (спрос) спрос.querySelector('.m-no').click();
    await tick(200);
    out.набранное_цело = панель.querySelector('[data-key="style_prompt"]').value;
    const dt = new DataTransfer();
    dt.items.add(new File([new Uint8Array([1, 2, 3])], 'proba.png', {type: 'image/png'}));
    ref.files = dt.files;
    панель.querySelector('.m-ok').click();
    await tick(800);
  }
  out.ушло = out.posts.map(x => x.p);
  const создание = out.posts.find(x => x.p === '/ui/presets/cover/new');
  out.имя_ушло = создание ? создание.body.name : null;
  out.после = location.hash;
  out.ложного_вопроса_нет = !document.querySelector('dialog[open]');
  out.про_сервисы = /Cover Studio|соседн/i.test(document.body.innerText + ' '
    + (out.текст_панели || ''));
  } catch (e) { out.errs.push('сценарий: ' + ((e && e.stack) || e)); }
  finish();
})();
"""


def main() -> int:
    print("Проверка: пресет обложки — часть общего пресета, одно подтверждение")
    res = browser_harness.run_scenario(SCENARIO, budget_ms=12000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2
    check("ошибок в браузере нет", res.get("errs"), [])

    print("\n«Создать» — один «Пресет», без отдельных студий")
    check("пунктов три: пресет, пресет канала, шаблон Lumean", res.get("пункты"),
          ["Пресет", "Пресет канала", "Шаблон озвучки Lumean"])
    check("«Пресет» ведёт на общий экран заведения", res.get("экран"), "#/preset/new")

    print("\n«Только обложка» — поля прямо на экране")
    check("окна поверх нет", res.get("окна_нет"), True)
    check("форма обложки — на странице", res.get("панель_есть"), True)
    check("в ней поля обложки", res.get("поля"), True)
    check("подтверждение одно — «Завести»", res.get("кнопки"), ["Завести"])
    check("образец стиля не заперт", res.get("образец_не_заперт"), True)
    check("имя из карточки «Название» перенесено в форму", res.get("имя_перенесено"),
          "Обложки польские")
    check("перенесённое имя сразу под сторожем ухода", res.get("имя_под_сторожем"), True)
    check("уход с набранным — спрашивает", res.get("уход_спросил"), True)
    check("и без ответа никуда не уводит", res.get("уход_остался"), "#/preset/new")
    check("«Остаться» — набранное цело", res.get("набранное_цело"), "тёплый свет")

    print("\nЗаведение")
    check("ушло одно заведение", res.get("ушло"), ["/ui/presets/cover/new"])
    check("под своим именем", res.get("имя_ушло"), "Обложки польские")
    check("картинка ушла следом, по имени нового пресета", res.get("загрузки"),
          ["/ui/presets/cover/%D0%9E%D0%B1%D0%BB%D0%BE%D0%B6%D0%BA%D0%B8"
           "%20%D0%BF%D0%BE%D0%BB%D1%8C%D1%81%D0%BA%D0%B8%D0%B5/reference"])
    check("и человек вернулся в список", res.get("после"), "#/presets")
    check("без ложного «не сохранено» после заведения", res.get("ложного_вопроса_нет"), True)
    check("нигде ни «Cover Studio», ни «соседнего сервиса»", res.get("про_сервисы"), False)

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
