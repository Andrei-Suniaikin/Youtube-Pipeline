#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Восклицательные знаки: мало доноров и выпуски в разборе (09.10.2026).

Владелец: «если у человека есть каналы, на которых не хватает ссылок на донора, в топбаре
показывать восклицательный знак, и на канале тоже плашку; та же идея на вкладке с
ошибками, и по наведению — кто должен это фиксить и что случилось».

    .venv/bin/python tests/test_attention_browser.py
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
  const txt = el => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
  const hover = el => {
    if (!el) return '';
    el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
    const t = (document.querySelector('.qbox') || {}).textContent || '';
    el.dispatchEvent(new MouseEvent('mouseout', {bubbles: true}));
    return t;
  };
  try {
    ME = {name: 'хозяин', role: 'admin'};
    let ATT = {supply_min_days: 3, blocked: 5,
      low_supply: [{id: 7, name: 'Nocne Biografie', days: 0, text: 'ближайший выход пустой'}],
      blocked_sides: [
        {kind: 'access', tag: 'Вход и профиль', count: 4,
         who: 'кто ведёт профили AdsPower, прокси и входы в Google-аккаунты'},
        {kind: 'studio', tag: 'Студия', count: 1, who: 'студия: сначала «Вернуть в работу»'}]};
    const ch = (id, name, sup) => ({id, name, handle: '@x', lang: 'PL', status: 'active',
      niche: '', uc: 'UC' + id, tags: [], tz: 'Europe/Warsaw', horizon_days: 14,
      lead_days: 3, per_day: 7, has_profile: true, has_uc: true, has_cover_preset: true,
      has_text_preset: true, supply: sup});
    const chs = [
      ch(7, 'Nocne Biografie', {known: true, items: 0, days: 0, covered: 0, low: true,
                                text: 'ближайший выход пустой', min_days: 3}),
      ch(8, 'Mroczne Akta', {known: true, items: 9, days: 9, covered: 9, low: false,
                             text: '9 суток', min_days: 3})];
    const page = id => ({
      channel: {id, name: 'К', handle: '@ch', lang: 'PL', niche: '', uc: 'UC1',
                status: 'active', tz: 'Europe/Warsaw', horizon_days: 3, lead_days: 2,
                deferred: false, deferred_limit: 5, schedule: {mon: ['10:00']},
                per_week: 1, host: '', profile: '', presets: {}, note: '',
                profile_id: 1, host_id: 1, preset_vsgen_id: 1,
                preset_rewrite: '', preset_cover: ''},
      calendar: [], counts: {}, uploads: [], by_day: {}, tz: 'Europe/Warsaw',
      channel_preset: {}, waiting_without_slot: 0, waiting_items: [], history_days: 14,
      stats: {}, analytics: {}, video_stats: {}});
    window.api = async p => {
      if (p === '/ui/me') return ME;
      if (p === '/ui/attention') return ATT;
      if (p === '/ui/channels') return {channels: chs};
      if (p.startsWith('/ui/channels/7')) return page(7);
      if (p.startsWith('/ui/channels/8')) return page(8);
      if (p.startsWith('/ui/releases')) return {releases: [], phases: []};
      return {};
    };
    addEventListener('hashchange', render);

    location.hash = '#/channels';
    await sleep(500);
    const bCh = document.querySelector('#tab-channels .attn');
    const bRel = document.querySelector('#tab-releases .attn');
    out.знакКаналы = txt(bCh);
    out.знакВыпуски = txt(bRel);
    out.подсказкаКаналы = hover(bCh);
    out.подсказкаВыпуски = hover(bRel);
    out.знакаНаПроектахНет = !document.querySelector('#tab-projects .attn');
    const tile = id => document.querySelector(`.chcard[data-id="${id}"]`);
    out.плиткаМало = txt(tile(7));
    out.плиткаНорма = txt(tile(8));
    out.подсказкаПлитки = hover(tile(7) && tile(7).querySelector('.whytip'));

    location.hash = '#/channel/7/roliki';
    await sleep(500);
    out.плашкаКанала = txt(document.querySelector('#ch-low'));
    location.hash = '#/channel/8/roliki';
    await sleep(500);
    out.плашкиНетУНормы = !document.querySelector('#ch-low');

    location.hash = '#/releases';
    await sleep(400);
    const bar = document.querySelector('#rel-attn');
    out.полоса = txt(bar);
    out.подсказкаСтороны = hover(bar && bar.querySelector('.whytip'));

    // Всё разобрали — знаки уходят со следующим опросом.
    ATT = {supply_min_days: 3, blocked: 0, low_supply: [], blocked_sides: []};
    await attention(true); attnBadges();
    out.знакиУшли = !document.querySelector('nav .attn');
  } catch (e) { out.errs.push('scenario: ' + (e && e.message || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=15000)
    if res.get("error"):
        print("НЕ ЗАПУСТИЛОСЬ:", res["error"])
        return 1
    check("ошибок на странице нет", res.get("errs"), [])

    print("Верхняя панель")
    check("«!» на «Каналах»", res.get("знакКаналы"), "!")
    check("«!» на «Выпусках»", res.get("знакВыпуски"), "!")
    check("на прочих вкладках знака нет", res.get("знакаНаПроектахНет"), True)
    t = res.get("подсказкаКаналы") or ""
    check("наведение: какие каналы", "Nocne Biografie: ближайший выход пустой" in t, True)
    check("   и кто чинит", "Кто чинит: оператор канала" in t, True)
    t = res.get("подсказкаВыпуски") or ""
    check("наведение на «Выпуски»: по сторонам и кто чинит",
          "Вход и профиль: 4 — чинит кто ведёт профили AdsPower" in t, True)

    print("\nКаналы")
    check("плитка с малым запасом — «! мало доноров»", "! мало доноров" in (res.get("плиткаМало") or ""), True)
    check("у нормальной — нет", "мало доноров" in (res.get("плиткаНорма") or ""), False)
    t = res.get("подсказкаПлитки") or ""
    check("наведение на плашку: что делать", "вставить ссылки на доноров" in t, True)
    check("на странице канала — плашка", "Мало доноров: ближайший выход пустой" in (res.get("плашкаКанала") or ""), True)
    check("у канала с запасом плашки нет", res.get("плашкиНетУНормы"), True)

    print("\nВыпуски")
    t = res.get("полоса") or ""
    check("полоса «! В разборе 5» со сторонами", ("В разборе 5" in t, "Вход и профиль" in t, "Студия" in t),
          (True, True, True))
    check("наведение на число — кто чинит",
          (res.get("подсказкаСтороны") or "").startswith("Кто чинит: кто ведёт профили AdsPower"), True)

    print("\nРазобрали — знаки ушли")
    check("знаков нет", res.get("знакиУшли"), True)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
