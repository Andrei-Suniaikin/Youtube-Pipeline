#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Карточка общих ключей на экране «Ключи» (24.09.2026) — поведение в браузере.

Владелец просил менять серверные ключи из интерфейса, без SSH. Проверяется: карточку
видит только администратор; видно «задан / не задан» и хвост, но не сам ключ; замена
уходит на сервер и не оставляет значение в странице; снятие спрашивает; отказ сервера
виден и не стирает вставленное.

    .venv/bin/python tests/test_server_keys_browser.py
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
  const KEYS = {providers: [], resting: [], own_keys: [], rr_cursor: [], busy: [],
                fastgen: null, unavailable: [], rewrite: {ok: true}};
  const SRV = {file_exists: true, warning: '', items: [
    {family: 'googler', title: 'FastGen — картинки и видео', set: true, tail: '…1234',
     count: 1, pooled: false},
    {family: 'elevenlabs', title: 'Lumean — озвучка', set: true, tail: '…5555',
     count: 3, pooled: true},
    {family: 'veo', title: 'Veo — клипы', set: false, tail: '', count: 0, pooled: false}]};
  let refuse = '';
  try {
    api = async (path, opts) => {
      const method = (opts && opts.method) || 'GET';
      if (method !== 'GET')
        out.sent.push([method, path, opts && opts.body ? JSON.parse(opts.body) : null]);
      if (path === '/ui/me') return ME;
      if (path === '/ui/accounts') return {users: [], roles: []};
      if (path === '/ui/keys') return JSON.parse(JSON.stringify(KEYS));
      if (path === '/ui/subscriptions') return {items: [], env: {}, pool: {}};
      if (path === '/ui/keys/server' && method === 'GET')
        return JSON.parse(JSON.stringify(SRV));
      if (path.startsWith('/ui/keys/server') && refuse) throw new Error(refuse);
      if (path.startsWith('/ui/keys/server')) return {ok: true, item: {}};
      return {};
    };
    const card = () => document.querySelector('#srvkeys-card');
    const row = f => card().querySelector(`[data-family="${f}"]`);
    const txt = el => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';

    ME = {name: 'u', role: 'user'};
    await viewKeys();
    out.обычныйВидит = !!card();

    ME = {name: 'boss', role: 'admin'};
    await viewKeys();
    out.админВидит = !!card();
    out.строки = SRV.items.map(i => ({
      точка: row(i.family).querySelector('.dot').className.replace('dot ', ''),
      текст: txt(row(i.family).querySelector('.body')),
      действия: [...row(i.family).querySelectorAll('.pacts .iconbtn')]
        .map(b => b.getAttribute('aria-label')),
    }));
    out.ключаВСтранице = [...document.querySelectorAll('#srvkeys-card *')]
      .some(e => /fg_live|el_/.test(e.textContent || ''));

    // Замена: пустое не уходит.
    row('googler').querySelector('.srvkey-set').click();
    out.формаВидна = !card().querySelector('#srvkey-form').hidden;
    out.полеПароля = card().querySelector('#sk-val').type;
    card().querySelector('#sk-ok').click();
    await sleep(60);
    out.пустоеНеУшло = out.sent.length;
    out.пустоеСказано = txt(card().querySelector('#sk-err'));

    // Отказ сервера: причина видна, вставленное на месте.
    refuse = 'это не похоже на ключ: короче 8 знаков';
    card().querySelector('#sk-val').value = 'fg_live_НОВЫЙ';
    card().querySelector('#sk-ok').click();
    await sleep(120);
    out.отказ = txt(card().querySelector('#sk-err'));
    out.вставленноеЦело = card().querySelector('#sk-val').value;

    // Принято: ушло на сервер, поле очищено, список перечитан.
    refuse = '';
    card().querySelector('#sk-ok').click();
    await sleep(200);
    out.отправлено = out.sent[out.sent.length - 1];
    out.секретВПолях = [...document.querySelectorAll('input,textarea')]
      .some(e => (e.value || '').includes('fg_live_НОВЫЙ'));

    // Снятие спрашивает и только потом шлёт.
    const n = out.sent.length;
    row('elevenlabs').querySelector('.srvkey-drop').click();
    await sleep(60);
    const dlg = document.querySelector('dialog.modal');
    out.вопрос = txt(dlg);
    out.доОтветаТихо = out.sent.length - n;
    dlg.querySelector('.m-ok').click();
    await sleep(200);
    out.снятие = out.sent[out.sent.length - 1];

    // У незаданного ключа снимать нечего.
    out.действияНезаданного = [...row('veo').querySelectorAll('.pacts .iconbtn')]
      .map(b => b.getAttribute('aria-label'));
  } catch (e) { out.errs.push(String(e && e.stack || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=30000)
    for e in res.get("errs") or []:
        print("  ошибка на странице:", e)
    rows = res.get("строки") or [{}, {}, {}]

    print("Кто видит")
    check("обычному человеку карточки нет", res.get("обычныйВидит"), False)
    check("администратору — есть", res.get("админВидит"), True)

    print("Что показано")
    check("заданный — зелёная точка", rows[0].get("точка"), "s-done")
    check("незаданный — тихая", rows[2].get("точка"), "s-idle")
    check("у незаданного сказано, чем это грозит",
          "упадут «нет ключа»" in rows[2].get("текст", ""), True)
    check("несколько ключей — сказано, сколько",
          "ключей 3" in rows[1].get("текст", ""), True)
    check("значения ключей на странице нет", res.get("ключаВСтранице"), False)
    check("у заданного две кнопки", rows[0].get("действия"),
          ["Заменить ключ «FastGen — картинки и видео»",
           "Убрать ключ «FastGen — картинки и видео»"])
    check("у незаданного только «задать»", res.get("действияНезаданного"),
          ["Задать ключ «Veo — клипы»"])

    print("Замена")
    check("форма открылась", res.get("формаВидна"), True)
    check("поле скрытое", res.get("полеПароля"), "password")
    check("пустое не отправляется", res.get("пустоеНеУшло"), 0)
    check("…и сказано, что делать", res.get("пустоеСказано"), "вставьте ключ")
    check("отказ сервера виден", res.get("отказ"),
          "это не похоже на ключ: короче 8 знаков")
    check("вставленное не стёрто", res.get("вставленноеЦело"), "fg_live_НОВЫЙ")
    sent = res.get("отправлено") or [None, None, {}]
    check("ушло на сервер", sent[:2], ["POST", "/ui/keys/server"])
    check("с именем семейства", (sent[2] or {}).get("family"), "googler")
    check("ключ не остался в странице", res.get("секретВПолях"), False)

    print("Снятие")
    check("спрашивает", "Убрать общий ключ" in res.get("вопрос", ""), True)
    check("…и предупреждает о последствиях",
          "нет ключа" in res.get("вопрос", ""), True)
    check("до ответа ничего не уходит", res.get("доОтветаТихо"), 0)
    check("после «Да» — снятие", res.get("снятие"),
          ["DELETE", "/ui/keys/server/elevenlabs", None])

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
