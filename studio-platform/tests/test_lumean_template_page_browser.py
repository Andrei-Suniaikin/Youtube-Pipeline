#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Шаблон озвучки Lumean — своей страницей, а не окном (22.09.2026).

Владелец: «когда хочешь создать пресет для Lumean — попап, а для обычного пресета нас
на страницу перекидывает; давай и для Lumean отдельной страничкой, просто перенесём
попап». Проверяется:
  • «Создать → Шаблон озвучки Lumean», «Завести» и «Править» на списке пресетов ведут
    на `#/lumean/new` и `#/lumean/<id>` — окна не открывается;
  • страница — та же форма: поля от сервера, «Завести» шлёт их, после заведения
    открыт сам шаблон, с id и кнопкой копирования;
  • правка шлёт в свой шаблон и остаётся на странице;
  • набранное и не сохранённое не теряется молча при уходе;
  • Lumean не ответил — сказано словами, пустой формы нет;
  • в форме пресета «Завести шаблон» по-прежнему окном: уход оттуда стёр бы пресет.

Настоящий `web/index.html` в headless Chrome, `api()` подменён — ни стенда, ни сети.

    .venv/bin/python tests/test_lumean_template_page_browser.py
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
  const out = {sent: [], errs: []};
  addEventListener('error', e => out.errs.push(e.message));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const FIELDS = [
    {key: 'name', label: 'Название', kind: 'text', value: ''},
    {key: 'voice_id', label: 'Голос', kind: 'text', value: ''},
    {key: 'stability', label: 'Стабильность', kind: 'number', min: 0, max: 1, value: ''},
    {key: 'speaker_boost', label: 'Усиление', kind: 'switch', value: ''}];
  let lumOk = true;
  try {
    window.api = async (p, opts) => {
      const method = (opts && opts.method) || 'GET';
      if (method !== 'GET') out.sent.push([method, p, opts.body ? JSON.parse(opts.body) : null]);
      if (p === '/ui/me') return {name: 'dev', role: 'user'};
      if (p === '/ui/presets') return {broken: [], groups: [{service: 'vsgen',
        title: 'Производство', items: []}]};
      if (p === '/ui/channel-presets') return {presets: [], note: ''};
      if (p === '/ui/lumean/templates' && method === 'POST') return {ok: true, id: 'tpl-new'};
      if (p === '/ui/lumean/templates')
        return lumOk ? {ok: true, fields: FIELDS, note: 'заметка',
                        templates: [{id: 'T1', name: 'Анна, польский'}]}
                     : {ok: false, why: 'ключа Lumean нет в профиле'};
      if (p === '/ui/lumean/templates/T1/form')
        return {fields: FIELDS.map(f => ({...f, value: f.key === 'name' ? 'Анна, польский'
                                                   : f.key === 'voice_id' ? 'v-42' : f.value})),
                used_by: ['Польский']};
      if (p.startsWith('/ui/lumean/templates/')) return {ok: true};
      return {};
    };
    const txt = el => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
    ME = {name: 'dev', role: 'user'};
    // Боевой запуск роутера харнесс убирает — подключаем его, как в бою.
    addEventListener('hashchange', render);
    const go = async h => { location.hash = h; await sleep(250); };

    // Со списка пресетов — адресом, не окном.
    await go('#/presets');
    document.querySelector('#newpr').click();
    await sleep(50);
    const item = [...document.querySelectorAll('.inline-ask button, .inline-ask [data-i], .menu-item, .inline-ask .mi')]
      .find(b => /Шаблон озвучки Lumean/.test(b.textContent));
    out.menuFound = !!item;
    if (item) item.click();
    await sleep(250);
    out.fromMenu = location.hash;
    out.modalFromMenu = !!document.querySelector('dialog.modal');

    await go('#/presets');
    const lumNew = document.querySelector('#lum-new');
    lumNew.click(); await sleep(250);
    out.fromCardNew = location.hash;
    await go('#/presets');
    document.querySelector('.lum-edit').click(); await sleep(250);
    out.fromCardEdit = location.hash;
    out.modalFromCard = !!document.querySelector('dialog.modal');

    // Страница нового шаблона.
    await go('#/lumean/new');
    out.newTitle = txt(document.querySelector('#app h1'));
    out.back = document.querySelector('#app a.back') && document.querySelector('#app a.back').getAttribute('href');
    out.fieldKeys = [...document.querySelectorAll('#app [data-lt]')].map(e => e.dataset.lt);
    out.okText = txt(document.querySelector('#app .m-ok'));
    out.modalOnPage = !!document.querySelector('dialog.modal');

    // Набрали и уходим — спрашивают.
    const nm = document.querySelector('#lt-name');
    nm.value = 'Ева'; nm.dispatchEvent(new Event('input', {bubbles: true}));
    location.hash = '#/presets'; await sleep(250);
    const ask = document.querySelector('dialog.modal');
    out.leaveAsk = txt(ask);
    out.stayedOn = location.hash;
    // «Остаться» — ответ «нет».
    if (ask) (ask.querySelector('.m-no') || ask.querySelector('.x')).click();
    await sleep(150);
    out.valueKept = (document.querySelector('#lt-name') || {}).value;

    // Завести.
    document.querySelector('#lt-voice_id').value = 'v-7';
    document.querySelector('#app .m-ok').click();
    await sleep(350);
    out.created = out.sent[out.sent.length - 1];
    out.afterCreate = location.hash;
    out.createdTitle = txt(document.querySelector('#app h1'));
    out.idShown = txt(document.querySelector('#app .sub'));
    out.hasCopy = !!document.querySelector('#app .sub .lum-copy');

    // Правка существующего.
    await go('#/lumean/T1');
    out.editTitle = txt(document.querySelector('#app h1'));
    out.editVoice = document.querySelector('#lt-voice_id').value;
    out.usedBy = /На него ссылаются: Польский/.test(txt(document.querySelector('#app')));
    out.editOk = txt(document.querySelector('#app .m-ok'));
    document.querySelector('#lt-voice_id').value = 'v-43';
    document.querySelector('#lt-voice_id').dispatchEvent(new Event('input', {bubbles: true}));
    document.querySelector('#app .m-ok').click();
    await sleep(350);
    out.saved = out.sent[out.sent.length - 1];
    out.afterSave = location.hash;
    // После записи уход без вопроса.
    location.hash = '#/presets'; await sleep(250);
    out.leftFreely = location.hash;
    out.askAfterSave = !!document.querySelector('dialog.modal');

    // Lumean не ответил.
    lumOk = false;
    await go('#/lumean/new');
    out.downText = txt(document.querySelector('#app'));
    out.downFields = document.querySelectorAll('#app [data-lt]').length;
  } catch (e) { out.errs.push(String(e && e.stack || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=40000)
    for e in res.get("errs") or []:
        print("  ошибка на странице:", e)

    print("Со списка пресетов — страницей")
    check("пункт меню нашёлся", res.get("menuFound"), True)
    check("«Создать → Шаблон озвучки Lumean» ведёт на страницу", res.get("fromMenu"),
          "#/lumean/new")
    check("…окна нет", res.get("modalFromMenu"), False)
    check("«Завести» в карточке — туда же", res.get("fromCardNew"), "#/lumean/new")
    check("«Править» — на страницу шаблона", res.get("fromCardEdit"), "#/lumean/T1")
    check("…окна нет", res.get("modalFromCard"), False)

    print("Новый шаблон")
    check("заголовок", res.get("newTitle"), "Новый шаблон озвучки Lumean")
    check("путь назад — к пресетам", res.get("back"), "#/presets")
    check("поля — от сервера", res.get("fieldKeys"),
          ["name", "voice_id", "stability", "speaker_boost"])
    check("кнопка «Завести»", res.get("okText"), "Завести")
    check("это страница, не окно", res.get("modalOnPage"), False)
    check("уход с набранным — спрашивают", "не заведён" in (res.get("leaveAsk") or ""), True)
    check("…и адрес не ушёл", res.get("stayedOn"), "#/lumean/new")
    check("«остаться» — набранное на месте", res.get("valueKept"), "Ева")
    created = res.get("created") or [None, None, {}]
    check("заведение шлёт поля", created[:2], ["POST", "/ui/lumean/templates"])
    check("…со значениями", ((created[2] or {}).get("values") or {}).get("voice_id"), "v-7")
    check("после заведения открыт сам шаблон", res.get("afterCreate"), "#/lumean/tpl-new")
    check("id виден", "tpl-new" in (res.get("idShown") or ""), True)
    check("и кнопка «Копировать id»", res.get("hasCopy"), True)

    print("Правка")
    check("заголовок — имя шаблона", res.get("editTitle"), "Анна, польский")
    check("значения подставлены", res.get("editVoice"), "v-42")
    check("видно, кто на нём", res.get("usedBy"), True)
    check("кнопка «Сохранить»", res.get("editOk"), "Сохранить")
    saved = res.get("saved") or [None, None, {}]
    check("правка — в свой шаблон", saved[:2], ["POST", "/ui/lumean/templates/T1"])
    check("…с новым значением", ((saved[2] or {}).get("values") or {}).get("voice_id"), "v-43")
    check("после записи остаёмся на странице", res.get("afterSave"), "#/lumean/T1")
    check("записанное — уход без вопроса", (res.get("leftFreely"), res.get("askAfterSave")),
          ("#/presets", False))

    print("Lumean не ответил")
    check("причина словами", "ключа Lumean нет в профиле" in (res.get("downText") or ""), True)
    check("пустой формы нет", res.get("downFields"), 0)

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
