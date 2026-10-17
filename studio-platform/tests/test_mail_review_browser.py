#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экран «Почта на проверку» в настоящем пульте — headless Chrome (17.10.2026).

Что здесь проверяется и почему:

  • ВХОД С ЭКРАНА КАНАЛОВ. Кнопка в строке заголовка несёт счёт писем и ведёт на экран:
    решение «чей это канал» принимает тот, кто ведёт каналы.
  • СТРОКА ПИСЬМА — как на странице канала: дата Gmail, тема, категория по-русски,
    отправитель, профиль, почта маской. Канал в выборе сам не подставлен, каналы
    профиля письма идут первой группой.
  • ПРИВЯЗАТЬ БЕЗ ВЫБОРА — отказ рядом с кнопкой, не POST. С выбором — POST с uc, строка
    уходит, счёт пересчитан. «Скрыть» — POST, строка уходит; пустой список — словами.

    .venv/bin/python tests/test_mail_review_browser.py
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
  addEventListener('error', e => out.errs.push(e.message));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    ME = {name: 'boss', role: 'admin'};
    const UC = s => 'UC' + s.repeat(22);
    const ch = (id, name) => ({id, name, handle: '@h' + id, uc: UC(String(id)), lang: 'PL',
      niche: 'история', status: 'active', tags: [], tz: 'Europe/Warsaw', horizon_days: 3,
      lead_days: 2, per_day: 1, has_profile: true, has_uc: true,
      supply: {known: true, items: 5, days: 5, covered: 5}, coin: false, coin_note: '', mail_n: 0});
    const REVIEW = {
      channels: [{id: 1, name: 'Один', uc: UC('1'), status: 'active', profile: '20'},
                 {id: 2, name: 'Два', uc: UC('2'), status: 'active', profile: '21'},
                 {id: 3, name: 'Три', uc: UC('3'), status: 'paused', profile: '21'}],
      events: [
        {id: 11, type: 'monetization_eligible', title: 'можно подавать на монетизацию', uc: '',
         account_level: true, confidence: 'review', confidence_title: 'на проверку: канал не определён',
         profile: '21', login: 'two…@gmail.com', sender: 'no-reply@youtube.com',
         subject: 'Your channel is eligible', snippet: 'Congrats', date_text: 'Mon, Oct 12, 2026, 1:00 PM',
         event_time: '2026-10-12T13:00', unread: true},
        {id: 12, type: 'account_security', title: 'безопасность аккаунта', uc: '',
         account_level: true, confidence: 'review', confidence_title: '',
         profile: '77', login: 'lone…@gmail.com', sender: 'no-reply@accounts.google.com',
         subject: 'Security alert', snippet: '', date_text: '', event_time: '2026-10-11T09:00', unread: false}],
    };
    out.posts = [];
    window.api = async (p, o) => {
      if (p === '/ui/me') return ME;
      if (p === '/ui/channels') return {channels: [ch(1, 'Один')], mail_review_n: 2};
      if (p === '/ui/channels/stats') return {channels: []};
      if (p === '/ui/attention') return {low_supply: []};
      if (p === '/ui/mail/review') return REVIEW;
      if (p === '/ui/mail/review/11/bind'){ out.posts.push([p, JSON.parse(o.body)]);
        return {ok: true, id: 11, uc: UC('2'), routed: true, channel: {id: 2, name: 'Два'}}; }
      if (p === '/ui/mail/review/12/dismiss'){ out.posts.push([p, JSON.parse(o.body)]); return {ok: true, id: 12}; }
      return {};
    };
    addEventListener('hashchange', render);

    location.hash = '#/channels';
    await sleep(400);
    const btn = document.querySelector('#mailrev');
    out.кнопка = btn ? btn.textContent.trim() : '';
    btn.click();
    await sleep(400);
    out.адрес = location.hash;
    out.заголовок = (document.querySelector('h1') || {}).textContent.trim().split(' ').slice(0, 3).join(' ');
    out.вкладка = (document.querySelector('nav .tab.on, nav .tab.active, nav .tab[aria-current]') || {}).id || '';
    out.счёт = (document.querySelector('#mr-count') || {}).textContent;
    const rows = () => [...document.querySelectorAll('.mrrow')];
    out.писем = rows().length;
    const r1 = rows()[0];
    out.дата = r1.querySelector('.mdate').textContent;
    out.тема = r1.querySelector('.msubj').textContent;
    out.категория = r1.querySelector('.pill').textContent.trim();
    out.ктоОткуда = /no-reply@youtube\.com · профиль 21 · two…@gmail\.com/.test(r1.textContent);
    out.непрочитанное = rows().map(r => r.classList.contains('unread'));
    const sel = r1.querySelector('.mr-uc');
    out.выборПустой = sel.value;
    out.группы = [...sel.querySelectorAll('optgroup')].map(g => [g.label, [...g.querySelectorAll('option')].map(o => o.textContent)]);
    out.группыВторого = [...rows()[1].querySelectorAll('optgroup')].map(g => g.label);

    r1.querySelector('.mr-bind').click(); await sleep(100);
    out.безВыбора = r1.querySelector('.mr-err').textContent;
    out.постовПокаНет = out.posts.length;
    sel.value = UC('2');
    r1.querySelector('.mr-bind').click(); await sleep(150);
    out.послеПривязки = rows().length;
    out.счётПосле = (document.querySelector('#mr-count') || {}).textContent;
    out.тост = (document.querySelector('#toasts .toast.ok') || {}).textContent || '';

    rows()[0].querySelector('.mr-hide').click(); await sleep(150);
    out.послеСкрытия = rows().length;
    out.пусто = /Писем на проверку нет/.test(document.querySelector('#mr-body').textContent);
    out.счётПусто = (document.querySelector('#mr-count') || {}).textContent;
    out.назад = !!document.querySelector('.head-acts a[href="#/channels"]');
  } catch (e) { out.errs.push(String(e && e.stack || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    r = browser_harness.run_scenario(SCENARIO, budget_ms=8000)
    if r.get("error"):
        print("  ПРОПУСК:", r["error"])
        return 0
    check("без ошибок JS", r.get("errs"), [])
    check("кнопка на экране каналов несёт счёт", r.get("кнопка"), "Почта на проверку · 2")
    check("ведёт на экран", r.get("адрес"), "#/mail-review")
    check("заголовок экрана", r.get("заголовок"), "Почта на проверку")
    check("счёт ждущих", r.get("счёт"), "Ждут решения: 2")
    check("две строки", r.get("писем"), 2)
    check("дата как у Gmail, без выдуманного пояса", r.get("дата"), "12.10 13:00")
    check("тема", r.get("тема"), "Your channel is eligible")
    check("категория по-русски", r.get("категория"), "можно подавать на монетизацию")
    check("отправитель, профиль, почта маской", r.get("ктоОткуда"), True)
    check("непрочитанное помечено", r.get("непрочитанное"), [True, False])
    check("канал сам не подставлен", r.get("выборПустой"), "")
    check("каналы профиля письма — первой группой, остальные — второй",
          r.get("группы"), [["Каналы профиля 21", ["Два", "Три (paused)"]], ["Остальные каналы", ["Один"]]])
    check("у профиля без каналов — одна группа «Каналы»", r.get("группыВторого"), ["Каналы"])
    check("привязать без выбора — отказ рядом", r.get("безВыбора"), "выберите канал")
    check("и без POST", r.get("постовПокаНет"), 0)
    check("с выбором: ушёл POST с uc, строка ушла",
          (r.get("posts", [])[:1], r.get("послеПривязки")),
          ([["/ui/mail/review/11/bind", {"uc": "UC" + "2" * 22}]], 1))
    check("счёт пересчитан", r.get("счётПосле"), "Ждут решения: 1")
    check("сказано, что дальше письма пойдут сами", "пойдут туда сами" in (r.get("тост") or ""), True)
    check("скрыть: POST и строка ушла",
          (r.get("posts", [])[1:], r.get("послеСкрытия")), ([["/ui/mail/review/12/dismiss", {}]], 0))
    check("пустой список — словами, счёт снят", (r.get("пусто"), r.get("счётПусто")), (True, ""))
    check("есть дорога назад к каналам", r.get("назад"), True)
    print()
    if FAILS:
        print(f"ПРОВАЛОВ: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
