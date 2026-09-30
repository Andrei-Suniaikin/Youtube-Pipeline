#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Строка сверху канала и просмотры у роликов (28.09.2026).

Владелец: «надо, чтобы на страницу канала сверху выводились просмотры, подписчики и часы
просмотра» и «интересна стата по каждому ролику, а именно просмотры».

Три числа в строке приходят из РАЗНЫХ источников, и экран не имеет права это скрывать:
подписчики и просмотры — публичные, их даёт ключ студии; часы просмотра публичными не
бывают вовсе и приходят только от подключённого канала. Поэтому у неподключённого канала
на месте часов не ноль и не прочерк, а приглашение подключить.

То же правило у роликов: «ещё не спрашивали» — это не «ноль просмотров».

    .venv/bin/python tests/test_channel_topline_browser.py
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


def flat(text: str) -> str:
    out = str(text or "")
    for space in (" ", " ", " ", "\n", "\t"):
        out = out.replace(space, " ")
    return " ".join(out.split())


def has(label, text, part):
    check(label, part in flat(text), True)


def hasnt(label, text, part):
    check(label, part in flat(text), False)


SCENARIO = r"""
(async () => {
  const out = {errs: []};
  addEventListener('error', e => out.errs.push(e.message));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    ME = {name: 'boss', role: 'admin'};
    let stats = {subs: 12340, views: 1204880, videos: 214, hidden: false, key: true,
                 day: '2026-09-28', taken: 1790000000,
                 delta: {'7': {subs: 58}}, series: []};
    let analytics = {access: {connected: false, client: true, state: '', error: ''},
                     days: 0, totals: {}, series: []};
    let videoStats = {
      'vid1': {views: 45200, likes: 900, comments: 40, day: '2026-09-28',
               delta: 3100, since: '2026-09-21'},
      'vid2': {views: 800, likes: 10, comments: 1, day: '2026-09-28',
               delta: null, since: ''}};

    const uploads = [
      {release_id: 'r1', title: 'Первый ролик', status: 'done', phase: 'video_id_known',
       phase_title: 'ролик у YouTube', phase_rank: 3, video_id: 'vid1',
       url: 'https://youtu.be/vid1', verdict: 'published', verdict_title: 'опубликован',
       visibility: 'public', schedule_iso: '', error: '',
       phases: [{key: 'a', title: 'а', done: true}]},
      {release_id: 'r2', title: 'Второй ролик', status: 'done', phase: 'video_id_known',
       phase_title: 'ролик у YouTube', phase_rank: 3, video_id: 'vid2',
       url: 'https://youtu.be/vid2', verdict: 'published', verdict_title: 'опубликован',
       visibility: 'public', schedule_iso: '', error: '',
       phases: [{key: 'a', title: 'а', done: true}]},
      {release_id: 'r3', title: 'Только что залит', status: 'done',
       phase: 'video_id_known', phase_title: 'ролик у YouTube', phase_rank: 3,
       video_id: 'vid3', url: 'https://youtu.be/vid3', verdict: 'published',
       verdict_title: 'опубликован', visibility: 'public', schedule_iso: '', error: '',
       phases: [{key: 'a', title: 'а', done: true}]}];

    const channel = () => ({
      channel: {id: 1, name: 'Канал', handle: '@ch', lang: 'PL', niche: '', uc: 'UC1',
                status: 'active', tz: 'Europe/Warsaw', horizon_days: 3, lead_days: 2,
                deferred: false, deferred_limit: 5, schedule: {mon: ['10:00']},
                per_week: 1, host: '', profile: '', presets: {}, note: '',
                profile_id: 1, host_id: 1, preset_vsgen_id: 1,
                preset_rewrite: '', preset_cover: ''},
      calendar: [{at: 1790000000, state: 'done', past: true, what: 'ролик',
                  release_id: 'r1', url: 'https://youtu.be/vid1'}],
      counts: {done: 1}, uploads: uploads, by_day: {'2026-09-28': {done: 1}},
      tz: 'Europe/Warsaw', channel_preset: {}, waiting_without_slot: 0,
      waiting_items: [], history_days: 14,
      stats: stats, analytics: analytics, video_stats: videoStats});

    window.api = async p => {
      if (p === '/ui/me') return ME;
      if (p === '/ui/channels') return {channels: []};
      if (p === '/ui/channels/stats') return {channels: []};
      if (p.startsWith('/ui/channels/1')) return channel();
      return {};
    };
    window.post = async () => ({});
    addEventListener('hashchange', render);

    const line = () => document.querySelector('.topline');
    const go = async (tab) => { location.hash = '#/channels'; await sleep(120);
      location.hash = '#/channel/1/' + tab; await sleep(350); };

    // Публичные числа есть, канал не подключён.
    await go('roliki');
    out.строка = (line() || {}).textContent || '';
    out.строкаЕсть = !!line();
    out.ссылкаНаПодключение = !!(line() || document.createElement('div'))
      .querySelector('a[href*="statistika"]');
    out.строкаВышеВкладок = (() => {
      const l = line(), t = document.querySelector('.subtabs');
      if (!l || !t) return false;
      return !!(l.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING);
    })();
    const up = [...document.querySelectorAll('.stage')].map(e => e.textContent || '');
    out.ролики = up.join(' ||| ');

    // Канал подключён — появились часы просмотра.
    analytics = {access: {connected: true, client: true, state: 'ok', error: ''},
                 days: 100, first: '2026-06-20', last: '2026-09-26',
                 totals: {span: 28, now: {views: 44000, minutes: 132000, subs_net: 760,
                          subs_gained: 900, subs_lost: 140, avg_sec: 310, avg_pct: 42,
                          impressions: null, from: '2026-08-31', to: '2026-09-27'},
                          was: {views: 40000, minutes: 120000, subs_net: 500}},
                 series: []};
    await go('roliki');
    out.сЧасами = (line() || {}).textContent || '';

    // Публичных чисел ещё нет (ключа нет) — часы всё равно показываем.
    stats = {subs: null, views: null, videos: null, hidden: false, key: false,
             day: '', taken: 0, delta: {}, series: []};
    await go('roliki');
    out.безКлюча = (line() || {}).textContent || '';

    // Подписчики скрыты владельцем канала.
    stats = {subs: null, views: 1204880, videos: 214, hidden: true, key: true,
             day: '2026-09-28', taken: 1790000000, delta: {}, series: []};
    await go('roliki');
    out.скрытые = (line() || {}).textContent || '';
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

    print("Строка сверху: публичные числа есть, канал не подключён")
    line = res.get("строка") or ""
    check("строка вообще есть", res.get("строкаЕсть"), True)
    check("и стоит выше вкладок", res.get("строкаВышеВкладок"), True)
    has("подписчики", line, "12 340 подписчиков")
    has("просмотры", line, "1 204 880 просмотров")
    has("вместо часов — приглашение", line, "часы просмотра — подключить")
    check("и это ссылка на вкладку", res.get("ссылкаНаПодключение"), True)
    hasnt("нуля часов нет", line, "0 ч просмотра")

    print("Канал подключён — часы появились")
    withh = res.get("сЧасами") or ""
    has("часы просмотра названы", withh, "2 200 ч просмотра")
    has("и сказано, за какой срок", withh, "за 28 дн")
    hasnt("приглашения больше нет", withh, "подключить")

    print("Ключа нет — публичных чисел нет, но часы остаются")
    nokey = res.get("безКлюча") or ""
    hasnt("подписчиков не выдумываем", nokey, "подписчик")
    hasnt("и просмотров тоже", nokey, "просмотров")
    has("часы на месте", nokey, "ч просмотра")

    print("Подписчики скрыты владельцем канала")
    hidden = res.get("скрытые") or ""
    has("так и написано", hidden, "подписчики скрыты")
    has("просмотры при этом видны", hidden, "1 204 880")

    print("Просмотры у каждого ролика")
    rows = (res.get("ролики") or "").split(" ||| ")
    check("строк роликов три", len(rows), 3)
    has("у первого — просмотры", rows[0], "45 200 просмотров")
    has("и прирост за неделю", rows[0], "+3 100")
    has("у второго — свои просмотры", rows[1], "800 просмотров")
    hasnt("у второго прироста не выдумано", rows[1], "за неделю")
    hasnt("у третьего чисел нет вовсе", rows[2], "просмотр")

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
