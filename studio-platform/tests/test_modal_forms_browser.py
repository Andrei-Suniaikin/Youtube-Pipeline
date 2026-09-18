#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Формы в окне: ключ и пресет соседа (18.09.2026) — поведение в браузере.

Владелец попросил окно вместо формы, вписанной в карточку. Это не браузерный попап
(`alert`/`confirm`/`prompt` по-прежнему запрещены), а обычный `<dialog>`: закрывается
Esc и кликом по фону, внутри живёт нормальная форма. Настоящий `web/index.html` в
headless Chrome, `api()` подменён — ни стенда, ни сети, ни настоящих ключей.

    .venv/bin/python tests/test_modal_forms_browser.py
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
  const out = {sent: []};
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    const FORM = {service: 'rewrite', name: 'Базовый рерайт', fields: [
      {key: 'model', kind: 'select', label: 'Модель', value: 'gpt-5',
       options: [{value: 'gpt-5', title: 'GPT-5'}, {value: 'gpt-4.1-nano', title: 'nano'}]},
      {key: 'chunk', kind: 'number', label: 'Размер куска', min: 500, max: 20000, value: 3500},
      {key: 'preserve_paragraphs', kind: 'switch', label: 'Абзацы', value: true},
      {key: 'address', kind: 'text', label: 'Обращение', value: 'вы'},
      {key: 'languages', kind: 'langrows', label: 'Языки и ведущие',
       value: [{code: 'pl', host: 'Анна', channel: 'Демо'}]},
    ]};
    api = async (path, opts) => {
      if (opts && opts.method === 'POST') out.sent.push([path, JSON.parse(opts.body)]);
      if (path.endsWith('/form')) return JSON.parse(JSON.stringify(FORM));
      if (path === '/ui/keys/families')
        return {families: [{key: 'googler', title: 'Googler'}], note: 'заметка'};
      return {ok: true};
    };

    // 1. Окно пресета соседа: поля пришли с сервера, второй копии полей в браузере нет.
    await neighbourPresetModal('rewrite', 'Базовый рерайт');
    await sleep(100);
    const dlg = document.querySelector('dialog.modal');
    out.opened = !!dlg && dlg.open;
    out.labels = [...dlg.querySelectorAll('.mbody label')].map(l => l.textContent.trim());
    out.langRows = dlg.querySelectorAll('.np-lang').length;
    dlg.querySelector('#np-address').value = 'ты';
    dlg.querySelector('.np-lang .np-host').value = 'Пётр';
    dlg.querySelector('.np-add').click();
    const rows = dlg.querySelectorAll('.np-lang');
    rows[1].querySelector('.np-code').value = 'cs';
    rows[1].querySelector('.np-host').value = 'Ева';
    dlg.querySelector('.m-ok').click();
    await sleep(300);
    out.closedAfterSave = !document.querySelector('dialog.modal');

    // 2. Esc закрывает окно и ничего не отправляет.
    await neighbourPresetModal('rewrite', 'Базовый рерайт');
    await sleep(100);
    const d2 = document.querySelector('dialog.modal');
    const before = out.sent.length;
    d2.dispatchEvent(new Event('cancel', {cancelable: true}));
    await sleep(100);
    out.closedByEsc = !document.querySelector('dialog.modal');
    out.sentAfterEsc = out.sent.length - before;

    // 3. Ключ вписывается в том же окне и уходит в нужную ручку.
    const card = document.createElement('div');
    card.innerHTML = '<button id="addkey"></button>';
    document.body.append(card);
    await wireKeys(card, {});
    card.querySelector('#addkey').click();
    await sleep(300);
    const d3 = document.querySelector('dialog.modal');
    out.keyModal = !!d3;
    out.keyFieldHidden = d3.querySelector('#kval').type;
    d3.querySelector('#kfam').value = '__rewrite';
    d3.querySelector('#kval').value = 'sk-проверочный';
    d3.querySelector('.m-ok').click();
    await sleep(300);
    out.keyClosed = !document.querySelector('dialog.modal');
  } catch (e) { out.error = String(e && e.stack || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=30000)
    sent = res.get("sent") or []
    check("сценарий выполнен без ошибок", res.get("error"), None)
    check("окно открылось", res.get("opened"), True)
    check("подписи полей — с сервера", res.get("labels"),
          ["Модель", "Размер куска", "Абзацы", "Обращение", "Языки и ведущие"])
    check("языковые строки отрисованы", res.get("langRows"), 1)
    check("сохранение ушло в ручку пресета",
          [p for p, _b in sent][0] if sent else None,
          "/ui/presets/rewrite/%D0%91%D0%B0%D0%B7%D0%BE%D0%B2%D1%8B%D0%B9%20%D1%80%"
          "D0%B5%D1%80%D0%B0%D0%B9%D1%82/form")
    saved = (sent[0][1] if sent else {}).get("values") or {}
    check("изменённое поле уехало", saved.get("address"), "ты")
    check("непонятых полей не добавилось", sorted(saved),
          ["address", "chunk", "languages", "model", "preserve_paragraphs"])
    check("языки собраны строками", saved.get("languages"),
          [{"code": "pl", "host": "Пётр", "channel": "Демо"},
           {"code": "cs", "host": "Ева", "channel": ""}])
    check("после сохранения окно закрылось", res.get("closedAfterSave"), True)
    check("Esc закрывает окно", res.get("closedByEsc"), True)
    check("Esc ничего не отправляет", res.get("sentAfterEsc"), 0)
    check("ключ вписывается в окне", res.get("keyModal"), True)
    check("поле ключа скрытое", res.get("keyFieldHidden"), "password")
    check("ключ рерайта ушёл в свою ручку", [p for p, _b in sent][-1],
          "/ui/keys/rewrite")
    check("значение ключа передано", (sent[-1][1] if sent else {}),
          {"openai_api_key": "sk-проверочный"})
    check("после сохранения ключа окно закрылось", res.get("keyClosed"), True)

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
