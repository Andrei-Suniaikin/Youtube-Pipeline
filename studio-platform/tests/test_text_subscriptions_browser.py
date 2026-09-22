#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Подписки для текста на экране «Ключи» (22.09.2026) — поведение в браузере.

Администратор добавляет подписку Codex или Claude, меняет вход, ставит на паузу и
удаляет — без SSH. Проверяется: состояние каждой подписки видно словами; вход уходит
на сервер и не остаётся в странице; пустой вход не отправляется; отказ сервера виден
и не стирает вставленное; блок видит только администратор.

Настоящий `web/index.html` в headless Chrome, `api()` подменён — ни стенда, ни сети.

    .venv/bin/python tests/test_text_subscriptions_browser.py
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
                fastgen: null, unavailable: [], rewrite: {ok: true}};
  const NOW = Date.now() / 1000;
  let SUBS = {items: [
      {id: 'aaaaaaaa0001', provider: 'codex', label: 'Основная', tail: 'an…@example.com',
       state: 'ok', last_ok: NOW - 60, since: NOW - 3600},
      {id: 'aaaaaaaa0002', provider: 'codex', label: 'Вторая', tail: 'bo…@example.com',
       state: 'limit', until: null, since: NOW - 60},
      {id: 'aaaaaaaa0003', provider: 'claude', label: 'Claude Max', tail: '…WXYZ',
       state: 'dead', since: NOW - 600, note: 'CODEX_AUTH: 401 unauthorized'},
      {id: 'aaaaaaaa0004', provider: 'claude', label: 'Резерв', tail: '…ABCD',
       state: 'paused'}],
    env: {known: true, codex: [{tail: 'codex-a'}], claude: []},
    pool: {codex: {credentials: 3, in_use: 1, capacity: 3}, claude: null}};
  let refuse = '';
  try {
    api = async (path, opts) => {
      const method = (opts && opts.method) || 'GET';
      if (method !== 'GET')
        out.sent.push([method, path, opts.body ? JSON.parse(opts.body) : null]);
      if (path === '/ui/accounts') return {users: [], roles: []};
      if (path === '/ui/keys') return JSON.parse(JSON.stringify(KEYS));
      if (path === '/ui/subscriptions' && method === 'GET') { out.listed = (out.listed || 0) + 1;
                                          return JSON.parse(JSON.stringify(SUBS)); }
      if (path.startsWith('/ui/subscriptions') && refuse) throw new Error(refuse);
      if (path.startsWith('/ui/subscriptions')) return {ok: true, item: {}};
      return {};
    };
    const card = () => document.querySelector('#subs-card');
    const row = id => card().querySelector(`[data-sub="${id}"]`);
    const txt = el => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';

    // Не администратор — блока нет вовсе (и запроса за ним тоже).
    ME = {name: 'u', role: 'user'};
    await viewKeys();
    out.userSees = !!card();
    out.userListed = out.listed || 0;

    ME = {name: 'boss', role: 'admin'};
    await viewKeys();
    out.adminSees = !!card();
    out.order = [...document.querySelectorAll('#app > .card .sec, #app .card > .sec')]
      .map(s => s.textContent.trim().split(/\s+/).slice(0, 3).join(' '));
    out.rows = SUBS.items.map(i => ({
      dot: row(i.id).querySelector('.dot').className.replace('dot ', ''),
      text: txt(row(i.id).querySelector('.body')),
      acts: [...row(i.id).querySelectorAll('.pacts .iconbtn')].map(b => b.getAttribute('aria-label')),
    }));
    out.footer = txt(card());

    // Добавить Codex: пустой вход не уходит.
    card().querySelector('#sub-add-codex').click();
    out.formOpen = !card().querySelector('#sub-form').hidden;
    out.formText = txt(card().querySelector('#sub-form'));
    card().querySelector('#sf-ok').click();
    await sleep(50);
    out.emptyErr = txt(card().querySelector('#sf-err'));
    out.sentAfterEmpty = out.sent.length;

    // Файл auth.json выбран — содержимое встаёт в поле.
    const dt = new DataTransfer();
    dt.items.add(new File(['{"auth_mode":"chatgpt","tokens":{"x":"FILESECRET"}}'],
                          'auth.json', {type: 'application/json'}));
    const file = card().querySelector('#sf-file');
    file.files = dt.files;
    file.dispatchEvent(new Event('change'));
    await sleep(100);
    out.fromFile = card().querySelector('#sf-secret').value;
    out.fname = txt(card().querySelector('#sf-fname'));

    // Сервер отказал — причина видна, вставленное не стёрто.
    refuse = 'это не вход по подписке ChatGPT';
    card().querySelector('#sf-label').value = 'Новая';
    card().querySelector('#sf-ok').click();
    await sleep(100);
    out.refusedErr = txt(card().querySelector('#sf-err'));
    out.keptAfterRefuse = card().querySelector('#sf-secret').value.includes('FILESECRET');

    // Принято — поле очищено, список перечитан, форма закрыта.
    refuse = '';
    const listedBefore = out.listed;
    card().querySelector('#sf-ok').click();
    await sleep(150);
    out.addSent = out.sent[out.sent.length - 1];
    out.relisted = out.listed > listedBefore;
    out.formClosedAfter = card().querySelector('#sub-form').hidden;
    // Сам сценарий лежит в странице текстом, поэтому ищем не по всему HTML, а по полям.
    out.secretInDom = [...document.querySelectorAll('textarea,input')]
      .some(e => (e.value || '').includes('FILESECRET'));

    // Claude — поле пароля, а не открытый текст.
    card().querySelector('#sub-add-claude').click();
    out.claudeType = card().querySelector('#sf-secret').type;
    out.claudeText = txt(card().querySelector('#sub-form'));
    card().querySelector('#sf-no').click();
    out.cancelClosed = card().querySelector('#sub-form').hidden;

    // Заменить вход у протухшей.
    row('aaaaaaaa0003').querySelector('.sub-replace').click();
    out.replaceTitle = txt(card().querySelector('#sub-form .name'));
    out.replaceHasLabel = !!card().querySelector('#sf-label');
    card().querySelector('#sf-secret').value = 'sk-ant-oat01-NEW';
    card().querySelector('#sf-ok').click();
    await sleep(150);
    out.replaceSent = out.sent[out.sent.length - 1];

    // Пауза, возврат, удаление.
    row('aaaaaaaa0001').querySelector('.sub-pause').click();
    await sleep(100);
    out.pauseSent = out.sent[out.sent.length - 1];
    row('aaaaaaaa0004').querySelector('.sub-resume').click();
    await sleep(100);
    out.resumeSent = out.sent[out.sent.length - 1];
    const n = out.sent.length;
    row('aaaaaaaa0002').querySelector('.sub-drop').click();
    await sleep(50);
    const dlg = document.querySelector('dialog.modal');
    out.dropAsk = txt(dlg);
    out.sentBeforeYes = out.sent.length - n;
    dlg.querySelector('.m-ok').click();
    await sleep(150);
    out.dropSent = out.sent[out.sent.length - 1];

    // Переименовать — окном с текущим именем.
    row('aaaaaaaa0001').querySelector('.sub-rename').click();
    await sleep(50);
    const rn = document.querySelector('dialog.modal #rn');
    out.renameValue = rn.value;
    rn.value = 'Главная';
    document.querySelector('dialog.modal .m-ok').click();
    await sleep(150);
    out.renameSent = out.sent[out.sent.length - 1];

    // Профиль администратора — блок тоже там.
    api = (orig => async (p, o) => p === '/ui/me' ? ME : orig(p, o))(api);
    await viewProfile();
    out.inProfile = !!card();
  } catch (e) { out.error = String(e && e.stack || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=30000)
    if res.get("error"):
        print(res["error"])
    rows = res.get("rows") or [{}] * 4

    print("Кто видит")
    check("не администратору блока нет", res.get("userSees"), False)
    check("…и список у сервера не спрашивается", res.get("userListed"), 0)
    check("администратору — есть", res.get("adminSees"), True)
    check("и в профиле", res.get("inProfile"), True)

    print("Состояние словами")
    check("работает — зелёная ✓", rows[0].get("dot"), "s-done")
    check("…с временем последнего вызова", "последний вызов" in rows[0].get("text", ""), True)
    check("лимит Codex — без выдуманного часа",
          "Codex не говорит, когда" in rows[1].get("text", ""), True)
    check("протух — красная", rows[2].get("dot"), "s-error")
    check("…сказано, что вынута и что делать",
          "вынута из раздачи" in rows[2].get("text", "")
          and "Замените вход" in rows[2].get("text", ""), True)
    check("…и причина", "401" in rows[2].get("text", ""), True)
    check("у протухшей — «вернуть без замены», а не пауза",
          rows[2].get("acts", [None, None])[1], "Вернуть в работу без замены «Claude Max»")
    check("на паузе — «вернуть в работу»",
          rows[3].get("acts", [None, None])[1], "Вернуть в работу «Резерв»")
    check("четыре действия у работающей, со словом в подписи", rows[0].get("acts"),
          ["Заменить вход «Основная»", "Поставить на паузу «Основная»",
           "Переименовать «Основная»", "Удалить «Основная»"])
    check("подписки из настроек сервера видны",
          "Из файла настроек сервера: Codex — 1 (codex-a)" in res.get("footer", ""), True)
    check("сколько пишут прямо сейчас", "Codex 1 из 3" in res.get("footer", ""), True)

    print("Добавить")
    check("форма открылась в карточке", res.get("formOpen"), True)
    check("подсказано войти в отдельную папку",
          "CODEX_HOME=~/codex-studio codex login" in res.get("formText", ""), True)
    check("пустой вход не отправляется", res.get("sentAfterEmpty"), 0)
    check("…и сказано, что вставить", res.get("emptyErr"), "вставьте содержимое auth.json")
    check("файл встал в поле", "FILESECRET" in (res.get("fromFile") or ""), True)
    check("имя файла видно", res.get("fname"), "auth.json")
    check("отказ сервера виден", res.get("refusedErr"), "это не вход по подписке ChatGPT")
    check("…и вставленное не стёрто", res.get("keptAfterRefuse"), True)
    sent = res.get("addSent") or [None, None, {}]
    check("ушло на сервер", sent[:2], ["POST", "/ui/subscriptions"])
    check("с провайдером и именем",
          ((sent[2] or {}).get("provider"), (sent[2] or {}).get("label")), ("codex", "Новая"))
    check("список перечитан", res.get("relisted"), True)
    check("вход не остался в странице", res.get("secretInDom"), False)
    check("Claude — поле пароля", res.get("claudeType"), "password")
    check("…и подсказка про setup-token", "claude setup-token" in res.get("claudeText", ""), True)
    check("«Отмена» закрывает форму", res.get("cancelClosed"), True)

    print("Заменить, пауза, удалить, переименовать")
    check("заголовок замены с именем", res.get("replaceTitle"),
          "Новый вход для «Claude Max»")
    check("при замене имя не спрашивается", res.get("replaceHasLabel"), False)
    check("замена — в ту же запись", res.get("replaceSent"),
          ["POST", "/ui/subscriptions/aaaaaaaa0003", {"secret": "sk-ant-oat01-NEW"}])
    check("пауза", res.get("pauseSent"),
          ["POST", "/ui/subscriptions/aaaaaaaa0001", {"paused": True}])
    check("возврат", res.get("resumeSent"),
          ["POST", "/ui/subscriptions/aaaaaaaa0004", {"paused": False}])
    check("удаление спрашивает", "Удалить «Вторая»?" in res.get("dropAsk", ""), True)
    check("…и до ответа ничего не уходит", res.get("sentBeforeYes"), 0)
    check("удаление после «Да»", res.get("dropSent"),
          ["DELETE", "/ui/subscriptions/aaaaaaaa0002", None])
    check("переименование подставляет текущее имя", res.get("renameValue"), "Основная")
    check("…и отправляет новое", res.get("renameSent"),
          ["POST", "/ui/subscriptions/aaaaaaaa0001", {"label": "Главная"}])

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
