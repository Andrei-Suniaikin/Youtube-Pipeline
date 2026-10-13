#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Часы для монетизации на экране (13.10.2026).

Карточка на вкладке «Статистика» канала и загрузка сбора с Минска со страницы каналов.
Проверяется то, на чём экран способен соврать:

  • часы показаны против порога партнёрки, с датой «данные на» от самой Studio;
  • свежий сбой не прячется за вчерашним числом;
  • неподтверждённое чтение — красным и без числа;
  • канал без сбора — объяснение, а не ноль.

    .venv/bin/python tests/test_earn_browser.py
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


def flat(text):
    return " ".join(str(text or "").replace(" ", " ").replace(" ", " ").split())


def has(label, text, part):
    check(label, part in flat(text), True)


SCENARIO = r"""
(async () => {
  const out = {errs: []};
  addEventListener('error', e => out.errs.push(e.message));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    ME = {name: 'boss', role: 'admin'};
    const good = {read_at: 1791900000, state: 'ok', state_title: 'прочитано', hours_365: 1500,
                  shorts_90: 5000, subs: 321, life_views: null, life_hours: null,
                  as_of: 'Oct 12, 2026', reason: ''};
    let earn = {last: good, good: good,
                prev: Object.assign({}, good, {read_at: 1791800000, hours_365: 1234}),
                goal_hours: 4000, goal_subs: 1000};
    const channel = () => ({
      channel: {id: 1, name: 'Канал', handle: '@ch', lang: 'PL', niche: '', uc: 'UC1',
                status: 'active', tz: 'Europe/Warsaw', horizon_days: 3, lead_days: 2,
                deferred: false, deferred_limit: 5, schedule: {mon: ['10:00']},
                per_week: 1, host: '', profile: '', presets: {}, note: '',
                profile_id: 1, host_id: 1, preset_vsgen_id: 1,
                preset_rewrite: '', preset_cover: ''},
      calendar: [], counts: {}, uploads: [], by_day: {}, tz: 'Europe/Warsaw',
      channel_preset: {}, waiting_without_slot: 0, waiting_items: [],
      history_days: 14, stats: {key: false, day: '', delta: {}, series: []},
      analytics: {access: {}}, earn: earn});
    window.api = async p => {
      if (p === '/ui/me') return ME;
      if (p === '/ui/channels') return {channels: [{id: 1, name: 'Канал', handle: '@ch',
        lang: 'PL', status: 'active', per_day: 1, horizon_days: 3, lead_days: 2,
        has_profile: true, has_uc: true}]};
      if (p === '/ui/channels/stats') return {channels: []};
      if (p === '/ui/channels/1/email') return {email: 'm07@example.com', read_at: 1791900000,
        profile: '13', assigned_profile: '21', other_profile: true, earlier: ['old@example.com']};
      if (p === '/ui/analytics/earn/emails') return {channels: 2, emails: 1, without: ['Бета'],
        groups: [{email: 'm07@example.com', channels: [
          {channel_id: 1, channel: 'Канал', profile: '13', assigned_profile: '21', other_profile: true},
          {channel_id: 2, channel: 'Второй', profile: '21', assigned_profile: '21', other_profile: false}]}]};
      if (p.startsWith('/ui/channels/1')) return channel();
      return {};
    };
    addEventListener('hashchange', render);
    const card = () => [...document.querySelectorAll('.card')]
      .find(el => (el.textContent || '').includes('Часы для монетизации'));
    const open = async () => {
      location.hash = '#/channels'; await sleep(200);
      location.hash = '#/channel/1/statistika'; await sleep(400);
    };

    await open();
    out.хорошо = (card() || {}).textContent || '';
    out.почта = (document.getElementById('earn-email') || {}).textContent || '';
    out.сбояНет = !(card() || document.body).querySelector('.note.bad');

    earn = Object.assign({}, earn, {last: {read_at: 1792000000, state: 'unverified',
      state_title: 'не подтверждено, что читали этот канал — число не принято',
      hours_365: null, subs: null, as_of: '', reason: 'считыватель не подтвердил канал и сессию'}});
    await open();
    out.сбойПоверх = (card() || {}).textContent || '';
    out.сбойКрасный = !!(card() || document.body).querySelector('.note.bad');

    earn = {last: earn.last, good: null, prev: null, goal_hours: 4000, goal_subs: 1000};
    await open();
    out.толькоСбой = (card() || {}).textContent || '';

    earn = {};
    await open();
    out.пусто = (card() || {}).textContent || '';

    location.hash = '#/channels'; await sleep(400);
    const btn = document.getElementById('earnimp');
    out.кнопка = !!btn;
    if (btn){ btn.click(); await sleep(200); }
    const ic = document.getElementById('earn-card');
    out.загрузка = ic ? ic.textContent : '';
    out.проверкаЗаперта = ic ? ic.querySelector('#earn-check').disabled : null;
    out.записьЗаперта = ic ? ic.querySelector('#earn-apply').disabled : null;
    const eb = document.getElementById('emails');
    if (eb){ eb.click(); await sleep(300); }
    out.почты = (document.getElementById('emails-card') || {}).textContent || '';

    ME = {name: 'op', role: 'user'};
    location.hash = '#/channel/1/statistika'; await sleep(300);
    location.hash = '#/channels'; await sleep(300);
    out.кнопкаПочтОператору = !!document.getElementById('emails');
    earn = {last: good, good: good, prev: null, goal_hours: 4000, goal_subs: 1000};
    await open();
    out.почтаОператору = !!document.getElementById('earn-email');
  } catch (e) { out.errs.push(String(e && e.message || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=9000)
    if res.get("error"):
        print("ПРОПУЩЕНО:", res["error"])
        return 0
    check("ошибок на странице нет", res.get("errs"), [])
    print("Принятое число")
    t = res.get("хорошо")
    has("часы против порога", t, "1 500 из 4 000 ч")
    has("процент до порога", t, "38%")
    has("подписчики против порога", t, "321 из 1 000")
    has("прирост к прошлому сбору", t, "+266 ч")
    has("дата от Studio", t, "данные на Oct 12, 2026")
    check("сбоя не показано", res.get("сбояНет"), True)
    print("Свежий сбой поверх вчерашнего числа")
    t = res.get("сбойПоверх")
    has("вчерашнее число на месте", t, "1 500 из 4 000 ч")
    has("сбой назван", t, "не подтверждено, что читали этот канал")
    check("и красным", res.get("сбойКрасный"), True)
    print("Только сбой")
    t = res.get("толькоСбой")
    check("числа нет", "из 4 000" in flat(t), False)
    has("сказано почему", t, "не подтвердил канал")
    print("Сбора не было")
    has("объяснение вместо нуля", res.get("пусто"), "Сбора ещё не было")
    print("Загрузка сбора")
    check("кнопка на странице каналов", res.get("кнопка"), True)
    has("подсказка, что грузить", res.get("загрузка"), "reader_result.json")
    check("без файла «Проверить» заперта", res.get("проверкаЗаперта"), True)
    check("«Записать» — только после проверки", res.get("записьЗаперта"), True)
    print("Почты")
    t = res.get("почта")
    has("почта в карточке часов", t, "m07@example.com")
    has("чужой профиль назван", t, "читали из профиля №13, а закреплён №21")
    has("прежняя почта видна", t, "old@example.com")
    t = res.get("почты")
    has("почты: сводка", t, "Каналов с почтой: 2, почт: 1")
    has("каналы одной почты вместе", t, "2 канала(ов) падают вместе")
    has("без почты названы", t, "Без почты (1): Бета")
    check("оператору кнопка «Почты» тоже есть", res.get("кнопкаПочтОператору"), True)
    check("и строка почты в карточке", res.get("почтаОператору"), True)
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
