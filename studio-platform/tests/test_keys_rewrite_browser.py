#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ключ рерайта на экране ключей (18.09.2026) — поведение в браузере.

Владелец не нашёл, куда вписать ключ для текста: искал его в «Своих ключах
пользователей». Теперь состояние ключа рерайта — там же, а вписывается он той же
кнопкой «Добавить ключ», отдельным провайдером. Настоящий `web/index.html` в headless
Chrome, `api()` подменён — ни стенда, ни сети, ни настоящих ключей.

    .venv/bin/python tests/test_keys_rewrite_browser.py
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
  const KEYS = {providers: [], resting: [], own_keys: [], rr_cursor: [], busy: [],
                fastgen: null, unavailable: [],
                rewrite: {ok: true, own_openai: false, own_claude: false,
                          server_openai: false, server_claude: false, source: 'none'}};
  try {
    api = async (path, opts) => {
      if (opts && opts.method === 'POST') out.sent.push([path, JSON.parse(opts.body)]);
      if (path === '/ui/keys') return JSON.parse(JSON.stringify(KEYS));
      if (path === '/ui/keys/families')
        return {families: [{key: 'googler', title: 'Googler / Recrafter'}], note: 'заметка'};
      if (path === '/ui/keys/rewrite') return {ok: true};
      if (path === '/ui/me') return {name: 'dev', role: 'admin'};
      return {};
    };
    const cardText = () => {
      const c = [...document.querySelectorAll('.card')]
        .find(x => /Свои ключи пользователей/.test(x.textContent));
      // Подсказки «?» — тоже текст для человека: имя провайдера живёт в ней.
      const hints = c ? [...c.querySelectorAll('.q')].map(q => q.getAttribute('data-hint'))
                         .join(' ') : '';
      return c ? (c.textContent + ' ' + hints).replace(/\s+/g, ' ') : '';
    };

    await viewKeys();
    out.noKey = cardText();

    // Вписываем ключ той же кнопкой «Добавить ключ» — форма открывается окном
    // (18.09.2026), поэтому ждём его и жмём «Сохранить» в нём.
    document.querySelector('#addkey').click();
    await sleep(300);
    out.opts = [...document.querySelectorAll('#kfam option')].map(o => o.value);
    out.inModal = !!document.querySelector('dialog.modal #kfam');
    document.querySelector('#kfam').value = '__rewrite';
    document.querySelector('#kval').value = 'sk-проверочный';
    document.querySelector('dialog.modal .m-ok').click();
    await sleep(300);

    // Ключ задан: строка это показывает, рядом «снять свой».
    KEYS.rewrite = {ok: true, own_openai: true, own_claude: false,
                    server_openai: false, server_claude: false, source: 'own'};
    await viewKeys();
    out.withKey = cardText();
    out.hasDrop = !!document.querySelector('#rw-drop');
    document.querySelector('#rw-drop').click();
    await sleep(50);
    const yes = [...document.querySelectorAll('button')]
      .find(b => /Да|Снять|Убрать|Ок/i.test(b.textContent));
    if (yes) yes.click();
    await sleep(200);

    // Рерайт не ответил — говорим об этом, а не молчим.
    KEYS.rewrite = {ok: false, why: 'рерайт не отвечает'};
    await viewKeys();
    out.down = cardText();
  } catch (e) { out.error = String(e && e.stack || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=30000)
    sent = res.get("sent") or []
    check("сценарий выполнен без ошибок", res.get("error"), None)
    check("без ключа так и написано — текст делать нечем",
          "ключа нет — текст делать нечем" in (res.get("noKey") or ""), True)
    # С 22.09.2026 — одна студия: провайдер называется «OpenAI · текст», без имени
    # отдельного сервиса.
    check("сказано, какой кнопкой вписать",
          "Добавить ключ" in (res.get("noKey") or "")
          and "«OpenAI · текст»" in (res.get("noKey") or ""), True)
    check("в списке провайдеров есть пункт рерайта",
          "__rewrite" in (res.get("opts") or []), True)
    check("форма ключа открывается окном, а не строкой в карточке",
          res.get("inModal"), True)
    check("ключ уходит в рерайт, а не в кабинет",
          [p for p, _b in sent], ["/ui/keys/rewrite", "/ui/keys/rewrite"])
    check("значение передано как есть", sent[0][1] if sent else None,
          {"openai_api_key": "sk-проверочный"})
    check("свой ключ виден в карточке",
          "ваш ключ OpenAI" in (res.get("withKey") or ""), True)
    check("есть «снять свой»", res.get("hasDrop"), True)
    check("снятие — пустым значением", sent[1][1] if len(sent) > 1 else None,
          {"openai_api_key": ""})
    check("недоступный рерайт назван причиной",
          "рерайт не отвечает" in (res.get("down") or ""), True)

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
