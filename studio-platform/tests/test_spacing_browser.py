#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Отступы на всех экранах пульта — замер в настоящем браузере на данных стенда.

Жалоба владельца 17.09.2026: блоки «слипаются» — кнопка вплотную к карточке под ней,
поле вплотную к линии заголовка, текст вплотную к рамке. Проверки строк в CSS этого не
ловят, поэтому здесь каждый экран открывается в headless Chrome и меряются зазоры:

  * соседние блоки страницы — не ближе MIN_GAP;
  * содержимое карточки (поля, кнопки, подписи, заметки) — не ближе MIN_PAD к рамке
    и к линии заголовка `.sec`. Строчные компоненты «от края до края» по замыслу
    (таблицы, строки `.stage`, календарь) не меряются.

Требует стенд (как `tests/test_web_pult.py`) и Chrome:

    VS_PLATFORM_BASE=http://127.0.0.1:8082 VS_DEV_PASSWORD=… \\
    .venv/bin/python tests/test_spacing_browser.py

`SPACING_SHOTS=<каталог>` — дополнительно сохранить скриншот каждого экрана.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "helpers"))

import live_page                                                  # noqa: E402

PLATFORM = os.environ.get("VS_PLATFORM_BASE", "http://127.0.0.1:8082")
LOGIN = os.environ.get("VS_DEV_LOGIN", "dev")
PASSWORD = os.environ.get("VS_DEV_PASSWORD", "dev-local-only")
DB = os.environ.get("VS_DEV_DB", os.path.join(live_page.BASE, "dev", "vsgen.dev.db"))
TENANT = os.environ.get("VS_DEV_TENANT", "dev")
MIN_GAP = 12
MIN_PAD = 12
FAILS = []

MEASURE = r"""
(() => {
  const MIN_GAP = %(gap)d, MIN_PAD = %(pad)d;
  const SKIP = 'table,.stage,.ev,.cal,.grid,.chgrid,.day,.sec,.empty,.upsteps,.toast,.inline-ask';
  const LEAF = 'input:not([type=hidden]):not([type=checkbox]),select,textarea,button,label,.note,p,h2,h3,img,.modes,.pill';
  const vis = el => { const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
  const name = el => (el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
    (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).join('.') : '') +
    ' «' + (el.textContent || el.placeholder || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 40) + '»');
  const run = () => {
    const app = document.getElementById('app');
    const out = {route: location.hash, problems: [], blocks: 0, measured: 0,
                 text: app.textContent.trim().slice(0, 80)};
    const kids = [...app.children].filter(vis);
    out.blocks = kids.length;
    for (let i = 1; i < kids.length; i++){
      const a = kids[i-1].getBoundingClientRect(), b = kids[i].getBoundingClientRect();
      if (b.top >= a.bottom - 1 && b.top - a.bottom < MIN_GAP)
        out.problems.push(`блоки вплотную (${Math.round(b.top - a.bottom)}px): ${name(kids[i-1])} → ${name(kids[i])}`);
    }
    const cards = [...app.querySelectorAll('.card')].filter(vis);
    const cardsLists = [...app.querySelectorAll('form, div')].filter(vis);
    // соседние карточки внутри любых контейнеров
    for (const c of cards){
      const prev = c.previousElementSibling;
      if (prev && vis(prev) && !prev.closest(SKIP)){
        const a = prev.getBoundingClientRect(), b = c.getBoundingClientRect();
        if (b.top >= a.bottom - 1 && b.top - a.bottom < MIN_GAP)
          out.problems.push(`карточка вплотную к блоку выше (${Math.round(b.top - a.bottom)}px): ${name(prev)} → ${name(c)}`);
      }
    }
    for (const card of cards){
      const r = card.getBoundingClientRect();
      const heads = [...card.querySelectorAll('.sec')].filter(h => vis(h) && h.closest('.card') === card)
        .map(h => h.getBoundingClientRect());
      for (const el of card.querySelectorAll(LEAF)){
        if (!vis(el) || el.closest('.card') !== card || el.closest(SKIP)) continue;
        if (el.parentElement && el.parentElement.closest(LEAF)) continue;
        // У текстовых блоков меряется текст (content box): заметка с собственным
        // отступом `.secbody` не слипается, хотя её рамка и совпадает с карточкой.
        // У полей и кнопок видна их рамка — они меряются целиком.
        const b = el.getBoundingClientRect(), cs = getComputedStyle(el);
        const ctl = el.matches('input,select,textarea,button,img,.modes,.pill');
        const px = v => ctl ? 0 : parseFloat(v) || 0;
        const e = {left: b.left + px(cs.paddingLeft), right: b.right - px(cs.paddingRight),
                   top: b.top + px(cs.paddingTop), bottom: b.bottom - px(cs.paddingBottom),
                   width: b.width - px(cs.paddingLeft) - px(cs.paddingRight)};
        out.measured++;
        if (e.left - r.left < MIN_PAD) out.problems.push(`вплотную к левой рамке (${Math.round(e.left - r.left)}px): ${name(el)}`);
        if (r.right - e.right < MIN_PAD - 1 && e.width < r.width - 2*MIN_PAD)
          out.problems.push(`вплотную к правой рамке (${Math.round(r.right - e.right)}px): ${name(el)}`);
        const above = heads.filter(h => h.bottom <= e.top + 1).map(h => h.bottom);
        const ceiling = above.length ? Math.max(...above) : r.top;
        if (e.top - ceiling < MIN_PAD - 4)
          out.problems.push(`вплотную к верху/линии заголовка (${Math.round(e.top - ceiling)}px): ${name(el)}`);
        const below = heads.filter(h => h.top >= e.bottom - 1).map(h => h.top);
        const floor = below.length ? Math.min(...below) : r.bottom;
        if (floor - e.bottom < MIN_PAD - 4)
          out.problems.push(`вплотную к низу карточки (${Math.round(floor - e.bottom)}px): ${name(el)}`);
      }
    }
    document.documentElement.setAttribute('data-result', JSON.stringify(out));
  };
  setTimeout(run, 6000);
})();
"""


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def routes() -> list:
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        ch = c.execute("SELECT id FROM pub_channels WHERE tenant_id=? ORDER BY id LIMIT 1",
                       (TENANT,)).fetchone()
        pr = c.execute("SELECT id FROM projects WHERE tenant_id=? ORDER BY updated DESC LIMIT 1",
                       (TENANT,)).fetchone()
        ps = c.execute("SELECT id FROM presets WHERE tenant_id=? ORDER BY id LIMIT 1",
                       (TENANT,)).fetchone()
    finally:
        c.close()
    out = ["#/channels", "#/projects", "#/queue", "#/presets", "#/preset/new", "#/profile"]
    if ch:
        out += [f"#/channel/{ch["id"]}/roliki", f"#/channel/{ch["id"]}/nastroyki"]
    # Два проекта: свежий (часто пустой) и тот, где больше всего задач — у него есть
    # файлы, лог и результаты, то есть все блоки вкладок.
    rich = None
    c = sqlite3.connect(DB, timeout=5)
    try:
        rich = c.execute("SELECT project_id FROM queue_tasks WHERE tenant_id=? AND project_id IN "
                         "(SELECT id FROM projects) GROUP BY project_id ORDER BY COUNT(*) DESC "
                         "LIMIT 1", (TENANT,)).fetchone()
    finally:
        c.close()
    for pid in dict.fromkeys([x for x in (pr and pr["id"], rich and rich[0]) if x]):
        out += [f"#/project/{pid}/{t}"
                for t in ("scenario", "tts", "images", "video", "render", "cover")]
    if ps:
        out.append(f"#/preset/{ps["id"]}")
    return out


def main() -> int:
    sid = live_page.login(PLATFORM, LOGIN, PASSWORD)
    base, stop = live_page.serve(PLATFORM, sid, MEASURE % {"gap": MIN_GAP, "pad": MIN_PAD})
    shots = os.environ.get("SPACING_SHOTS", "")
    try:
        for route in routes():
            res = live_page.chrome_dump(f"{base}/studio{route}")
            print(f"\n{route}")
            if "error" in res:
                check("экран отрисован", res["error"], None)
                continue
            # Экран из одних таблиц и строк «от края до края» (каналы, очередь) мерить
            # внутри нечего — тогда проверяются только зазоры между его блоками.
            check(f"экран отрисован (блоков: {res.get('blocks', 0)}, "
                  f"элементов в карточках: {res.get('measured', 0)})",
                  res.get("blocks", 0) > 1, True)
            check("ничего не слиплось", res.get("problems"), [])
            if shots:
                os.makedirs(shots, exist_ok=True)
                fname = route.strip("#/").replace("/", "_").replace(":", "-") or "root"
                live_page.chrome_dump(f"{base}/studio{route}",
                                      screenshot=os.path.join(shots, fname + ".png"))
    finally:
        stop()
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
