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

Меряется только то, что человек ВИДИТ. Содержимое закрытых `<details>` и то, что
укатилось за край контейнера с собственной прокруткой (`.langs`), пропускается: у
такого содержимого прямоугольники ненулевые, но на экране его нет — см. подробный
разбор в самом замере. Сколько пропущено, печатается рядом с числом измеренного.

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
  const OPEN_SELECTORS = %(open)s;
  const SKIP = 'table,.stage,.ev,.cal,.grid,.chgrid,.day,.sec,.empty,.upsteps,.toast,.inline-ask';
  const LEAF = 'input:not([type=hidden]):not([type=checkbox]),select,textarea,button,label,.note,p,h2,h3,img,.modes,.pill';
  const vis = el => { const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
  // ── Чего человек не видит, то и мерить нельзя ───────────────────────────────
  // Оба правила ниже появились 20.09.2026 после 26 ложных срабатываний на
  // #/projects. Все 26 — с ОТРИЦАТЕЛЬНЫМ зазором, то есть элемент якобы торчал за
  // нижнюю рамку карточки наружу. Вёрстка там целая; ненадёжен был сам замер:
  // `getBoundingClientRect()` честно отвечает и про то, что на экране не показано.
  //
  // 1. Закрытая «гармошка». В HeadlessChrome 153 у содержимого `<details>` при
  //    open=false прямоугольники НЕНУЛЕВЫЕ: замер 20.09.2026 на #/projects показал
  //    саму details высотой 18,6px (одна шапка), а textarea «Или текст — уедет
  //    одному отмеченному» внутри неё — top 1003, bottom 1123. Мерить это значит
  //    мерить то, чего на экране нет. Опираться на `el.checkVisibility()` (он на них
  //    отвечает false) не будем: факт «родительская details закрыта» и есть ответ, и
  //    он читается без знания версии Chrome. Сама шапка `<summary>` видна — и
  //    меряется как обычно.
  const closedUp = el => {
    for (let d = el.closest('details'); d;
         d = d.parentElement && d.parentElement.closest('details')){
      if (d.open) continue;
      const s = d.querySelector(':scope > summary');
      if (!(s && s.contains(el))) return true;
    }
    return false;
  };
  // 2. Своя прокрутка. `.langs` — это `max-height:140px; overflow:auto`: замер нашёл
  //    в коробке 46 чипов языков, scrollHeight 522 при clientHeight 138, и 35 из них
  //    лежат НИЖЕ её рамки — до 373px за пределами карточки. Пока коробку не
  //    прокрутили, этих чипов на экране нет, и «слипнуться» они ни с чем не могут.
  //    Меряется то, что попало в окошко; уехавшее за его край — пропускается.
  const clipped = el => {
    const r = el.getBoundingClientRect();
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement){
      const cs = getComputedStyle(p);
      const scrolls = (/auto|scroll/.test(cs.overflowY) && p.scrollHeight > p.clientHeight + 1)
                   || (/auto|scroll/.test(cs.overflowX) && p.scrollWidth > p.clientWidth + 1);
      if (!scrolls) continue;
      const q = p.getBoundingClientRect();
      if (r.bottom > q.bottom + 1 || r.top < q.top - 1 ||
          r.right > q.right + 1 || r.left < q.left - 1) return true;
    }
    return false;
  };
  // Видно на экране = есть размер, не заперто в закрытой гармошке и не укатилось
  // за край своей прокрутки.
  const seen = el => vis(el) && !closedUp(el) && !clipped(el);
  const name = el => (el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
    (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).join('.') : '') +
    ' «' + (el.textContent || el.placeholder || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 40) + '»');
  // Формы, которые открываются кнопкой, при обычной загрузке не видны — а слипаются
  // именно они (жалоба владельца 18.09.2026: «Добавить ключ» и «Сохранить» вплотную к
  // рамке). Перед замером открываем их: список кнопок приходит в OPEN_SELECTORS.
  const openFirst = async () => {
    for (const sel of OPEN_SELECTORS){
      const el = document.querySelector(sel);
      if (el){ el.click(); await new Promise(r => setTimeout(r, 400)); }
    }
  };
  const run = () => {
    const app = document.getElementById('app');
    // `hidden` — сколько элементов пропущено как невидимые. Число печатается рядом с
    // `measured`: если правила выше однажды проглотят весь экран, это будет видно
    // сразу, а не превратится в тихое «ничего не слиплось».
    const out = {route: location.hash, problems: [], blocks: 0, measured: 0, hidden: 0,
                 text: app.textContent.trim().slice(0, 80)};
    const kids = [...app.children].filter(seen);
    out.blocks = kids.length;
    for (let i = 1; i < kids.length; i++){
      const a = kids[i-1].getBoundingClientRect(), b = kids[i].getBoundingClientRect();
      if (b.top >= a.bottom - 1 && b.top - a.bottom < MIN_GAP)
        out.problems.push(`блоки вплотную (${Math.round(b.top - a.bottom)}px): ${name(kids[i-1])} → ${name(kids[i])}`);
    }
    const cards = [...app.querySelectorAll('.card')].filter(seen);
    const cardsLists = [...app.querySelectorAll('form, div')].filter(seen);
    // соседние карточки внутри любых контейнеров
    for (const c of cards){
      const prev = c.previousElementSibling;
      if (prev && seen(prev) && !prev.closest(SKIP)){
        const a = prev.getBoundingClientRect(), b = c.getBoundingClientRect();
        if (b.top >= a.bottom - 1 && b.top - a.bottom < MIN_GAP)
          out.problems.push(`карточка вплотную к блоку выше (${Math.round(b.top - a.bottom)}px): ${name(prev)} → ${name(c)}`);
      }
    }
    for (const card of cards){
      const r = card.getBoundingClientRect();
      const heads = [...card.querySelectorAll('.sec')].filter(h => seen(h) && h.closest('.card') === card)
        .map(h => h.getBoundingClientRect());
      for (const el of card.querySelectorAll(LEAF)){
        if (el.closest('.card') !== card || el.closest(SKIP)) continue;
        if (el.parentElement && el.parentElement.closest(LEAF)) continue;
        if (!vis(el)) continue;
        // Размер есть, но на экране элемента нет — считаем и идём дальше.
        if (closedUp(el) || clipped(el)){ out.hidden++; continue; }
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
  setTimeout(async () => { await openFirst(); run(); }, 6000);
})();
"""


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# Какие формы открыть перед замером на каком экране. Только то, что открывает форму
# на месте и ничего не отправляет.
OPEN_BUTTONS = {
    "#/keys": ["#addkey"],
    "#/projects": ["#newprj", "#newproj"],
}


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
    # «Админ» и «Оплачено, но не забрано» добавлены 19.09.2026 вместе с плиткой
    # выходов субтитров и экраном подбора: новые блоки ставятся в те же карточки, и
    # отступы у них обязаны быть общие, а не «как получилось».
    out = ["#/channels", "#/projects", "#/queue", "#/paid-texts", "#/presets",
           "#/preset/new", "#/profile", "#/keys", "#/admin"]
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
    measure = MEASURE % {"gap": MIN_GAP, "pad": MIN_PAD,
                         "open": json.dumps(sorted({s for v in OPEN_BUTTONS.values()
                                                    for s in v}))}
    base, stop = live_page.serve(PLATFORM, sid, measure)
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
                  f"элементов в карточках: {res.get('measured', 0)}, "
                  f"пропущено невидимых: {res.get('hidden', 0)})",
                  res.get("blocks", 0) > 1, True)
            # Пропуск невидимого — послабление, и оно обязано быть узким. Если на
            # экране что-то спрятано, а измерено НОЛЬ, значит правила пропуска съели
            # весь экран: тогда «ничего не слиплось» ничего не доказывает.
            if res.get("hidden", 0):
                check("замер не ослеп: видимое в карточках всё ещё меряется",
                      res.get("measured", 0) > 0, True)
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
