#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Монетка и почта канала в настоящем пульте — headless Chrome (17.10.2026).

Что здесь проверяется и почему:

  • МОНЕТКА ВИДНА С КАРТОЧКИ. Владелец ищет монетные каналы глазами по сетке, а не
    открывая каждый.
  • ОТБОР «НА МОНЕТЕ» РАБОТАЕТ И ЗАПОМИНАЕТСЯ, «Сбросить» его снимает — как и у
    остальных отборов на этом экране.
  • НА ЭКРАНЕ КАНАЛА: кнопка поставить/снять, а на вкладке «Статистика» — письма с
    русскими названиями категорий и пометкой «по почте аккаунта».

    .venv/bin/python tests/test_channel_coin_browser.py
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
    const ch = (id, name, coin, note, mail_n) => ({id, name, handle: '@h' + id,
      uc: 'UC' + id + 'x'.repeat(20), lang: 'PL', niche: 'история', status: 'active',
      tags: [], tz: 'Europe/Warsaw', horizon_days: 3, lead_days: 2, per_day: 1,
      has_profile: true, has_uc: true, supply: {known: true, items: 5, days: 5, covered: 5},
      coin, coin_note: note, mail_n});
    const ALL = [ch(1, 'Монетный', true, 'одобрен 12.10', 2), ch(2, 'Обычный', false, '', 0)];
    const DETAIL = {
      channel: Object.assign({}, ALL[1], {schedule: {}, per_week: 1, presets: {}, note: '',
        deferred: true, deferred_limit: 5, lang_raw: 'pl'}),
      calendar: [], counts: {}, uploads: [], video_stats: [], by_day: {}, tz: 'Europe/Warsaw',
      channel_preset: {id: null, name: '', inherited: [], own: {}, all: []},
      waiting_without_slot: 0, waiting_items: [], history_days: 14,
      stats: {}, analytics: {}, earn: {}, earn_request: {},
      mail_events: [
        {id: 1, type: 'monetization_eligible', title: 'можно подавать на монетизацию',
         uc: ALL[1].uc, account_level: false, confidence: 'high', confidence_title: '',
         sender: 'no-reply@youtube.com', subject: 'Your channel is eligible', snippet: '',
         date_text: 'Mon, Oct 12, 2026, 1:00 PM', event_time: '2026-10-12T13:00', unread: true},
        {id: 2, type: 'account_security', title: 'безопасность аккаунта', uc: '',
         account_level: true, confidence: 'review', confidence_title: '', login: 'abc…@gmail.com',
         sender: 'no-reply@accounts.google.com', subject: 'Security alert', snippet: 'new sign-in',
         date_text: '', event_time: '2026-10-11T09:00', unread: false}],
    };
    out.posts = [];
    window.api = async (p, o) => {
      if (p === '/ui/me') return ME;
      if (p === '/ui/channels') return {channels: ALL};
      if (p === '/ui/channels/stats') return {channels: []};
      if (p.startsWith('/ui/channels/2?')) return DETAIL;
      if (p === '/ui/channels/2/coin'){ out.posts.push(JSON.parse(o.body)); return {ok: true, coin: true, coin_note: 'тест'}; }
      if (p === '/ui/attention') return {low_supply: []};
      return {};
    };
    addEventListener('hashchange', render);
    try { localStorage.removeItem('vs.channels.sieve'); } catch (e) {}
    // Название без значка монетки: первый текстовый узел заголовка.
    const names = () => [...document.querySelectorAll('.chcard h3')].map(h => h.firstChild.textContent.trim());
    const set = (id, v) => { const n = document.querySelector(id); n.value = v;
      n.dispatchEvent(new Event('change', {bubbles: true})); };

    location.hash = '#/channels';
    await sleep(400);
    out.карточки = names();
    out.монеткаНаКарточке = !!document.querySelector('.chcard[data-id="1"] h3 .coin');
    out.монеткиНетУОбычного = !document.querySelector('.chcard[data-id="2"] h3 .coin');
    out.подсказкаМонетки = (document.querySelector('.chcard[data-id="1"] .coin') || {}).title || '';
    out.счётПисем = (document.querySelector('.chcard[data-id="1"] .pill[title*="письма"]') || {}).textContent || '';
    out.естьОтбор = !!document.querySelector('#s-coin');

    set('#s-coin', '1'); await sleep(60);
    out.наМонете = names();
    set('#s-coin', '0'); await sleep(60);
    out.безМонеты = names();
    out.запомнено = (JSON.parse(localStorage.getItem('vs.channels.sieve') || '{}') || {}).coin;
    document.querySelector('#s-off').click(); await sleep(60);
    out.послеСброса = names();
    out.сброшено = document.querySelector('#s-coin').value;

    location.hash = '#/channel/2/statistika';
    await sleep(500);
    out.кнопкаПоставить = !!document.querySelector('#coin-on');
    const rows = [...document.querySelectorAll('.mailrow')];
    out.писем = rows.length;
    out.названия = rows.map(r => r.querySelector('.pill').textContent.trim());
    out.уровеньАккаунта = rows.map(r => /по почте аккаунта/.test(r.textContent));
    out.непрочитанное = rows.map(r => r.classList.contains('unread'));
    out.датаGmail = (rows[0].querySelector('.mdate') || {}).textContent || '';

    document.querySelector('#coin-on').click(); await sleep(100);
    const inp = document.querySelector('#coin-note');
    out.окноОткрылось = !!inp;
    if (inp){ inp.value = 'тест'; document.querySelector('dialog[open] .m-ok').click(); await sleep(150); }
    out.отправлено = out.posts;
    out.монеткаВШапке = !!document.querySelector('h1 .coin');
    out.кнопкаСнять = !!document.querySelector('#coin-off');
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
    check("карточки нарисованы", r.get("карточки"), ["Монетный", "Обычный"])
    check("монетка у монетного", r.get("монеткаНаКарточке"), True)
    check("у обычного монетки нет", r.get("монеткиНетУОбычного"), True)
    check("подсказка несёт заметку", r.get("подсказкаМонетки"), "Монетный канал: одобрен 12.10")
    check("счёт писем на карточке", r.get("счётПисем"), "✉ 2")
    check("есть отбор по монетке", r.get("естьОтбор"), True)
    check("«на монете» — только монетный", r.get("наМонете"), ["Монетный"])
    check("«без монеты» — только обычный", r.get("безМонеты"), ["Обычный"])
    check("отбор запомнен", r.get("запомнено"), "0")
    check("«Сбросить» возвращает всех", r.get("послеСброса"), ["Монетный", "Обычный"])
    check("после сброса отбор пустой", r.get("сброшено"), "")
    check("на экране канала — кнопка поставить", r.get("кнопкаПоставить"), True)
    check("писем на вкладке — 2", r.get("писем"), 2)
    check("категории по-русски", r.get("названия"),
          ["можно подавать на монетизацию", "безопасность аккаунта"])
    check("второе — уровня аккаунта", r.get("уровеньАккаунта"), [False, True])
    check("непрочитанное помечено", r.get("непрочитанное"), [True, False])
    check("дата как у Gmail, без выдуманного пояса", r.get("датаGmail"), "12.10 13:00")
    check("окно заметки открылось", r.get("окноОткрылось"), True)
    check("ушёл POST с монеткой и заметкой", r.get("отправлено"), [{"coin": True, "note": "тест"}])
    check("монетка появилась в шапке", r.get("монеткаВШапке"), True)
    check("кнопка сменилась на «Снять»", r.get("кнопкаСнять"), True)
    print()
    if FAILS:
        print(f"ПРОВАЛОВ: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
