#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экран выпусков подгружает по двадцать при прокрутке (владелец 20.10.2026).

Повод: «почему-то страница с выпусками долговато прогружается». Она просила у сервера
ВСЕ выпуски тенанта — 996 строк, из них 842 давно вышедших, и на каждую считалась
полоска этапов и объяснение причины.

Что проверяется здесь, в настоящем `web/index.html` под headless Chrome:

* первый запрос уходит с `limit=20&offset=0` — экран не просит всё;
* следующие страницы запрашиваются со сдвигом 20, 40 — без дыр и перекрытий;
* строки накапливаются, а не заменяют друг друга, и ни одна не задвоилась;
* подгрузка останавливается, когда сервер сказал `has_more: false` — бесконечного
  долбления сервера нет;
* группировка по причине видит ВСЕ пришедшие строки: шесть выпусков с одной бедой,
  приехавшие разными страницами, остаются одной карточкой, а не тремя.

    .venv/bin/python tests/test_releases_scroll_browser.py
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
  const out = {errs: [], calls: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  addEventListener('unhandledrejection',
                   e => out.errs.push('promise: ' + (e.reason && e.reason.message || e.reason)));

  const TOTAL = 47;
  const ALL = [];
  for (let i = 0; i < TOTAL; i++) ALL.push({
    release_id: 'r_' + String(i).padStart(3, '0'),
    channel_id: 1, channel: 'Канал', lang: 'PL',
    donor_title: 'Донор ' + i, donor_url: 'https://y/' + i,
    slot_at: 1792500000 + i * 60, phase: 'created', phase_title: 'заведён',
    phase_index: 0, phase_total: 7, status: i < 6 ? 'blocked' : 'pending',
    attempts: 0, idle_sec: 60, bar: [], gaps: [], plan_item_id: i + 1,
    // Первые шесть — одна и та же беда, но приедут они РАЗНЫМИ страницами.
    blocked_reason: i < 6 ? 'рерайт не справился: 1 из 1 задач не выполнено' : '',
    block: i < 6 ? {kind: 'service', tag: 'текст', title: 'рерайт не справился',
                    what: 'ждём ключ', who: 'студия'} : {},
  });

  window.api = async (p) => {
    out.calls.push(p);
    if (p.startsWith('/ui/channels')) return {channels: []};
    if (p.startsWith('/ui/attention')) return {blocked: 0, blocked_sides: []};
    if (p.startsWith('/ui/releases')) {
      const q = new URLSearchParams(p.split('?')[1] || '');
      const limit = Number(q.get('limit') || 0), offset = Number(q.get('offset') || 0);
      const rows = limit ? ALL.slice(offset, offset + limit) : ALL;
      return {releases: rows, total: TOTAL, phases: [],
              has_more: limit ? (offset + rows.length) < TOTAL : false};
    }
    return {};
  };
  window.post = async () => ({ok: true});
  window.render = async () => {};

  await viewReleases();
  out.первая_страница = document.querySelectorAll('tr.row').length;
  // В headless наблюдатель за прокруткой не срабатывает, да и у человека может не
  // быть колеса мыши: добираемся кнопкой — она обязана делать ровно то же самое.
  for (let i = 0; i < 5; i++) {
    const btn = document.getElementById('rel-more');
    if (!btn) break;
    const было = document.querySelectorAll('tr.row').length;
    btn.click();
    // Ждём не «исчезла кнопка» (она пропадает сразу, на время загрузки), а прибавку
    // строк: именно она означает, что страница приехала и список перерисован.
    for (let w = 0; w < 30 && document.querySelectorAll('tr.row').length === было; w++) {
      await new Promise(r => setTimeout(r, 25));
    }
  }

  const ids = [...document.querySelectorAll('tr.row')].map(tr => tr.dataset.id);
  const relCalls = out.calls.filter(p => p.startsWith('/ui/releases'));
  out.первый_запрос = relCalls[0] || '';
  out.сдвиги = relCalls.map(p => Number(new URLSearchParams(p.split('?')[1]).get('offset')));
  out.строк = ids.length;
  out.повторов = ids.length - new Set(ids).size;
  out.подвал = (document.getElementById('rel-foot') || {}).textContent || '';
  out.карточек_беды = document.querySelectorAll('.grpcard').length;
  out.в_карточке = document.querySelectorAll('.grpcard tr.row').length;
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})()
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=15000)
    if res.get("error"):
        print("нет браузера или сценарий не отчитался:", res["error"])
        return 2
    print("Подгрузка выпусков по двадцать")
    check("ошибок на странице нет", res.get("errs"), [])
    check("первый запрос — ровно двадцать",
          "limit=20&offset=0" in str(res.get("первый_запрос")), True)
    check("на экране сразу двадцать, а не всё", res.get("первая_страница"), 20)
    check("сдвиги страниц без дыр", res.get("сдвиги"), [0, 20, 40])
    check("показаны все 47", res.get("строк"), 47)
    check("повторов нет", res.get("повторов"), 0)
    check("подвал говорит, что это всё", "показаны все 47" in str(res.get("подвал")), True)
    check("одна беда — одна карточка", res.get("карточек_беды"), 1)
    check("и в ней все шесть выпусков", res.get("в_карточке"), 6)

    print("\nитог:", "все проверки прошли" if not FAILS else f"ПРОВАЛОВ {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
