#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пресет обложки заводится одним подтверждением, а не двумя заведениями подряд.

Жалоба владельца 21.09.2026: «второе создать» у пресета обложки. Так и было: чтобы
завести пресет обложки из пульта, надо было сперва завести ДРУГОЙ объект — частичный
пресет производства «Только обложка», у которого всё содержимое — одна ссылка на пресет
Cover Studio (`server/preset_fields.py`: часть «Обложка» — единственное поле
`preset_cover`), — и только из его редактора добраться до настоящего заведения. Два
заведения на одно намерение, семь нажатий.

Плюс мелочь того же узла: при заведении поле «образец стиля» стояло запертым, потому что
Cover Studio принимает файл только по имени УЖЕ существующего пресета. Чтобы приложить
картинку, окно надо было открывать второй раз — то самое «второе создать».

Что закрепляет тест:

* **«Создать» → «Пресет обложки» открывает окно соседа напрямую** — без экрана и без
  обёртки-частичного;
* **подтверждение одно** — кнопка «Завести» в этом окне, и после неё пресет есть;
* **образец стиля не заперт при заведении** и уходит тем же нажатием, следом за
  созданием, потому что до него имени у пресета ещё не было.

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
  const tick = ms => new Promise(r => setTimeout(r, ms || 200));
  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {

  window.api = async (p) => {
    if (p === '/ui/presets') return {broken: [], groups: [
      {service: 'vsgen', title: 'Производство', items: []}]};
    if (p === '/ui/channel-presets') return {presets: [], note: ''};
    if (p === '/ui/lumean/templates') return {ok: true, templates: [], fields: []};
    if (p === '/ui/me') return {name: 'dev'};
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

  // «Создать» знает про пресет обложки — заводить обёртку не надо.
  document.querySelector('#newpr').click();
  await tick(200);
  const пункт = [...document.querySelectorAll('.mi')].find(
      b => b.textContent.includes('Пресет обложки'));
  out.пункт_есть = !!пункт;
  if (пункт) пункт.click();
  await tick(500);

  const dlg = document.querySelector('dialog[open]');
  out.окно_открылось = !!dlg;
  out.адрес_не_менялся = location.hash;
  if (dlg){
    out.поля_соседа = !!dlg.querySelector('[data-key="style_prompt"]');
    const ref = dlg.querySelector('#np-ref');
    out.образец_не_заперт = !!ref && !ref.disabled;
    const кнопки = [...dlg.querySelectorAll('.mfoot button')].map(
        b => b.textContent.trim());
    out.подтверждение = кнопки;
    // Имя живёт в соседнем сервисе — об этом сказано и на главном пути, а не только
    // когда окно открыто из пресета производства с подставленным именем.
    const знак = dlg.querySelector('#np-name').closest('.fld').querySelector('.q');
    out.про_имя = знак ? знак.getAttribute('data-hint') : null;
    out.знак_не_в_подписи = !!знак && !знак.closest('label');
    dlg.querySelector('#np-name').value = 'Обложки польские';
    // Кладём файл в поле так же, как это делает человек мышью.
    const dt = new DataTransfer();
    dt.items.add(new File([new Uint8Array([1, 2, 3])], 'proba.png', {type: 'image/png'}));
    ref.files = dt.files;
    dlg.querySelector('.m-ok').click();
    await tick(700);
  }
  out.окно_закрылось = !document.querySelector('dialog[open]');
  out.ушло = out.posts.map(x => x.p);
  const создание = out.posts.find(x => x.p === '/ui/presets/cover/new');
  out.имя_ушло = создание ? создание.body.name : null;

  } catch (e) { out.errs.push('сценарий: ' + ((e && e.stack) || e)); }
  finish();
})();
"""


def main() -> int:
    print("Проверка: пресет обложки — одно заведение и одно подтверждение")
    res = browser_harness.run_scenario(SCENARIO, budget_ms=9000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2
    check("ошибок в браузере нет", res.get("errs"), [])

    print("\nЗаведение начинается прямо из «Создать»")
    check("в меню есть «Пресет обложки»", res.get("пункт_есть"), True)
    check("оно открывает окно соседа", res.get("окно_открылось"), True)
    check("никуда не уводя с экрана списка", res.get("адрес_не_менялся"), "#/presets")
    check("в окне настоящие поля Cover Studio", res.get("поля_соседа"), True)
    check("у имени сказано, что оно живёт в соседнем сервисе",
          res.get("про_имя"), "Это пресет в соседнем сервисе — у него своё имя.")
    check("знак вопроса рядом с подписью, а не внутри <label>",
          res.get("знак_не_в_подписи"), True)

    print("\nПодтверждение одно")
    check("в подвале окна — «Завести» и «Отмена», второго заведения нет",
          res.get("подтверждение"), ["Завести", "Отмена"])
    check("после него пресет заведён", res.get("ушло"), ["/ui/presets/cover/new"])
    check("под своим именем", res.get("имя_ушло"), "Обложки польские")
    check("и окно закрылось", res.get("окно_закрылось"), True)

    print("\nОбразец стиля прикладывается тем же нажатием")
    check("поле не заперто при заведении", res.get("образец_не_заперт"), True)
    check("картинка ушла следом за созданием, по имени нового пресета",
          res.get("загрузки"),
          ["/ui/presets/cover/%D0%9E%D0%B1%D0%BB%D0%BE%D0%B6%D0%BA%D0%B8"
           "%20%D0%BF%D0%BE%D0%BB%D1%8C%D1%81%D0%BA%D0%B8%D0%B5/reference"])

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
