#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Канал в цифрах: что видно на экране (25.09.2026).

Числа сами по себе безобидны — опасна их подпись. Проверяется ровно то, на чём экран
способен соврать:

  • у молодого канала прирост НЕИЗВЕСТЕН, и это не ноль. «0 за 7 дней» означало бы
    «не растёт», то есть приговор каналу, которого никто не выносил;
  • скрытые владельцем подписчики — «скрыты», а не «0»;
  • без ключа YouTube экран говорит, чего не хватает, а не рисует пустую таблицу;
  • падение статистики не имеет права утащить с собой список каналов.

    .venv/bin/python tests/test_channel_stats_browser.py
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
    """Сплющить пробелы. Числа браузер разделяет НЕРАЗРЫВНЫМ пробелом (U+00A0), и
    сравнение с обычным пробелом провалилось бы не потому, что экран неправ."""
    out = str(text or "")
    for space in ("\u00a0", "\u202f", "\u2009", "\n", "\t"):
        out = out.replace(space, " ")
    return out


def has(label, text, part):
    check(label, part in flat(text), True)


SCENARIO = r"""
(async () => {
  const out = {errs: []};
  addEventListener('error', e => out.errs.push(e.message));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    ME = {name: 'boss', role: 'admin'};
    let stats = {
      subs: 12340, views: 1204880, videos: 214, hidden: false, key: true,
      day: '2026-09-25', taken: 1790000000,
      delta: {'7': {subs: 58, views: 12400, videos: null, since: '2026-09-18'},
              '28': {subs: null, views: null, videos: null, since: ''}},
      series: [{day: '2026-09-21', subs: 12200, views: 1190000},
               {day: '2026-09-22', subs: 12250, views: 1195000},
               {day: '2026-09-23', subs: 12280, views: 1199000},
               {day: '2026-09-24', subs: 12300, views: 1202000},
               {day: '2026-09-25', subs: 12340, views: 1204880}]
    };
    let listStats = {channels: [{id: 1, name: 'Канал', uc: 'UC1', subs: 12340,
      views: 1204880, videos: 214, hidden: false, day: '2026-09-25',
      delta: {'7': {subs: 58}, '28': {subs: null}}}]};
    let listBoom = false;

    const channel = () => ({
      channel: {id: 1, name: 'Канал', handle: '@ch', lang: 'PL', niche: '', uc: 'UC1',
                status: 'active', tz: 'Europe/Warsaw', horizon_days: 3, lead_days: 2,
                deferred: false, deferred_limit: 5, schedule: {mon: ['10:00']},
                per_week: 1, host: '', profile: '', presets: {}, note: '',
                profile_id: 1, host_id: 1, preset_vsgen_id: 1,
                preset_rewrite: '', preset_cover: ''},
      calendar: [], counts: {}, uploads: [], by_day: {}, tz: 'Europe/Warsaw',
      channel_preset: {}, waiting_without_slot: 0, waiting_items: [],
      history_days: 14, stats: stats});

    window.api = async p => {
      if (p === '/ui/me') return ME;
      if (p === '/ui/channels') return {channels: [{id: 1, name: 'Канал', handle: '@ch',
        lang: 'PL', status: 'active', per_day: 1, horizon_days: 3, lead_days: 2,
        has_profile: true, has_uc: true}]};
      if (p === '/ui/channels/stats'){
        if (listBoom) throw new Error('статистика не ответила');
        return listStats;
      }
      if (p.startsWith('/ui/channels/1')) return channel();
      return {};
    };
    window.post = async () => ({run: {saved: 1}, state: {}});
    addEventListener('hashchange', render);

    const card = () => [...document.querySelectorAll('.card')]
      .find(el => (el.textContent || '').includes('Канал в цифрах'));

    location.hash = '#/channel/1/roliki';
    await sleep(400);
    out.карточка = (card() || {}).textContent || '';
    out.искра = !!(card() || document.body).querySelector('svg');
    out.порядок = (() => {
      const cards = [...document.querySelectorAll('.card')].map(e => e.textContent || '');
      const i = cards.findIndex(t => t.includes('Канал в цифрах'));
      const j = cards.findIndex(t => t.includes('Поставить ролики'));
      return i >= 0 && j >= 0 && i < j;
    })();

    // Молодой канал: ряда ещё нет, прироста не знаем.
    stats = Object.assign({}, stats, {
      delta: {'7': {subs: null, views: null, videos: null, since: ''},
              '28': {subs: null, views: null, videos: null, since: ''}},
      // Две точки — не линия, а обещание линии: рисовать по ним график значит
      // показать «тренд» там, где наблюдений на тренд ещё не набралось.
      series: [{day: '2026-09-24', subs: 12300, views: 1202000},
               {day: '2026-09-25', subs: 12340, views: 1204880}]});
    location.hash = '#/channels';
    await sleep(200);
    location.hash = '#/channel/1/roliki';
    await sleep(400);
    out.молодой = (card() || {}).textContent || '';
    out.молодойБезИскры = !(card() || document.createElement('div')).querySelector('svg');

    // Подписчики скрыты владельцем канала.
    stats = Object.assign({}, stats, {hidden: true, subs: null});
    location.hash = '#/channels';
    await sleep(200);
    location.hash = '#/channel/1/roliki';
    await sleep(400);
    out.скрытые = (card() || {}).textContent || '';

    // Снимков нет вовсе и ключа нет.
    stats = {key: false, day: '', delta: {}, series: []};
    location.hash = '#/channels';
    await sleep(200);
    location.hash = '#/channel/1/roliki';
    await sleep(400);
    out.безКлюча = (card() || {}).textContent || '';
    out.кнопкаЗаперта = !!(card() || document.body)
      .querySelector('#refresh') && (card().querySelector('#refresh').disabled);

    // Список каналов: подписчики на плитке.
    location.hash = '#/channels';
    await sleep(400);
    out.плитка = (document.querySelector('.chcard') || {}).textContent || '';

    // Статистика упала — список каналов обязан остаться.
    listBoom = true;
    location.hash = '#/channel/1/roliki';
    await sleep(200);
    location.hash = '#/channels';
    await sleep(500);
    out.плиткаБезСтатистики = (document.querySelector('.chcard') || {}).textContent || '';
    out.списокЖив = document.querySelectorAll('.chcard').length;
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

    print("Канал с историей наблюдений")
    card = res.get("карточка") or ""
    has("подписчики показаны", card, "12 340")
    has("просмотры показаны", card, "1 204 880")
    has("роликов показано", card, "214")
    has("прирост за неделю", card, "+58")
    has("дата снимка", card, "2026-09-25")
    has("сказано, что это публичные числа", card, "публичные")
    has("и чего в них нет", card, "CTR")
    check("график ряда нарисован", res.get("искра"), True)
    check("цифры идут выше работы со ссылками", res.get("порядок"), True)

    print("Молодой канал: «не знаем» вместо нуля")
    young = res.get("молодой") or ""
    has("сказано, что ещё наблюдаем", young, "ещё наблюдаем")
    check("нуля в приросте нет", "+0" in flat(young) or "−0" in flat(young), False)
    check("двух точек для графика мало", res.get("молодойБезИскры"), True)

    print("Скрытые подписчики")
    hidden = res.get("скрытые") or ""
    has("названы скрытыми", hidden, "скрыты")
    has("просмотры при этом на месте", hidden, "1 204 880")

    print("Без ключа")
    nokey = res.get("безКлюча") or ""
    has("сказано, чего не хватает", nokey, "ключа YouTube Data API")
    has("и что он бесплатный", nokey, "бесплатный")
    check("кнопка «Обновить» заперта", res.get("кнопкаЗаперта"), True)

    print("Плитка канала в списке")
    tile = res.get("плитка") or ""
    has("подписчики на плитке", tile, "12 340")
    has("и прирост за неделю", tile, "+58")

    print("Отказ статистики не уносит список каналов")
    check("плитки на месте", res.get("списокЖив"), 1)
    has("имя канала видно", res.get("плиткаБезСтатистики"), "Канал")
    check("чисел нет — вместо них пусто, а не ноль",
          "0 подписчиков" in flat(res.get("плиткаБезСтатистики")), False)

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
