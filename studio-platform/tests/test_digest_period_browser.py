#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сводка: календарь периода и блокеры «чья беда» в настоящем браузере (04.10.2026).

Владелец: «на сводке показывается, что залито 0 роликов, а ты говоришь 5; не совсем
понятно, за какой период — давай приятный календарь для выбора в нашем стиле, а так
будет показывать за текущий месяц»; «24 блокера — что за блокеры, давай разбираться».

Что проверяется:
  • по умолчанию экран спрашивает сервер без дат (сервер отдаёт текущий месяц) и
    пишет период словами в шапке;
  • «Залито в расписание» — отдельная первая строка, «вышло» — отдельная;
  • календарь: первый щелчок — начало, второй — конец (в любом порядке), «Показать»
    до второго щелчка закрыт; выбранный период уходит на сервер датами;
  • быстрые периоды («Вчера» и т.п.) применяются одним щелчком;
  • стрелки листают месяцы; Esc закрывает без запроса;
  • выбор помнится при возврате на сводку в той же вкладке;
  • блокеры: метка «чья беда», что случилось, сколько, какие каналы, что делать.

    .venv/bin/python tests/test_digest_period_browser.py
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
  const tick = ms => new Promise(r => setTimeout(r, ms || 200));
  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {
  try { sessionStorage.clear(); } catch (_e) {}
  const pad = n => String(n).padStart(2, '0');
  const iso = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const mFirst = new Date(today.getFullYear(), today.getMonth(), 1);
  const mLast = new Date(today.getFullYear(), today.getMonth() + 1, 0);
  out.ждём = {from: iso(mFirst), to: iso(mLast)};
  const digest = per => ({
    text: 'Сводка', settings: {telegram: false, hour: 9, tz: 'Europe/Warsaw'},
    digest: {
      day: '04.10.2026', tz: 'Europe/Warsaw', period: per,
      groups: [{tag: 'прочие', channels: 30, plan: 384, accepted: 6, blocked: 24}],
      today: {expected: 14, ahead: 8, confirmed: 0, by_hand: 0, delayed: 14, unknown: 0, why: []},
      production: {accepted: 6, no_supply: 0, supply_unknown: 0, threshold_days: 3},
      problems: [], uploads: {'всего': 0}, alerts: [], alerts_why: '',
      blockers: [
        {kind: 'studio', tag: 'Сбой студии', title: 'рерайт прерывался перезапусками студии',
         fix: 'Это наша сторона: нажмите «Вернуть в работу».', revive: true, count: 2,
         channels: ['Noční Portréty', 'Dawne Noce']},
        {kind: 'channel', tag: 'Настройка канала',
         title: 'у аккаунта профиля AdsPower нет доступа к каналу', fix: 'Выдайте доступ.',
         revive: true, count: 1, channels: ['Zaginiona Cywilizacja']},
        {kind: 'donor', tag: 'Проблема в доноре', title: 'автор скрыл видео', fix: '',
         revive: false, count: 8, channels: ['A', 'B', 'C', 'D', 'E', 'F']}]
    }});
  window.api = async (p) => {
    if (p === '/ui/me') return {name: 'Андрей', role: 'admin'};
    if (p.startsWith('/ui/digest')) {
      out.calls.push(p);
      const q = new URLSearchParams(p.split('?')[1] || '');
      return digest(q.get('from') ? {from: q.get('from'), to: q.get('to')} : out.ждём);
    }
    return {};
  };
  window.post = async () => ({});

  addEventListener('hashchange', render);
  location.hash = '#/digest';
  await tick(500);
  const txt = () => document.querySelector('#app').textContent.replace(/\s+/g, ' ');
  out.первыйЗапрос = out.calls[0];
  out.шапка = (document.querySelector('.head .sub') || {}).textContent || '';
  const rows = [...document.querySelectorAll('#app .card table tr')].map(tr =>
    tr.textContent.replace(/\s+/g, ' ').trim());
  out.первыеСтроки = rows.slice(0, 3);
  out.блокеры = txt().includes('Блокеры сейчас — 11');
  out.метки = [...document.querySelectorAll('#digest-blockers .blktag')].map(x => x.textContent);
  out.естьЧтоДелать = txt().includes('Это наша сторона: нажмите «Вернуть в работу».');
  out.длинныйСписокКаналов = txt().includes('A, B, C, D и ещё 2');

  // ── календарь ──
  const btn = document.querySelector('.pp-btn');
  out.кнопкаЕсть = !!btn;
  out.надписьКнопки = btn ? btn.textContent.replace(/\s+/g, ' ').trim() : '';
  btn.click(); await tick(100);
  let pop = document.querySelector('.pp-pop');
  out.календарьОткрыт = !!pop;
  out.месяц = pop.querySelector('.pp-month').textContent;
  // Окно — под кнопкой, а не внизу страницы (`$` отдаёт обёртку: координаты, заданные
  // обёртке, окно не двигали).
  const rb = btn.getBoundingClientRect(), rp = pop.getBoundingClientRect();
  out.подКнопкой = Math.abs(rp.top - rb.bottom - 6) < 2 && rp.right <= innerWidth;
  out.пресеты = [...pop.querySelectorAll('.pp-preset')].map(x => x.textContent);
  out.пресетОтмечен = (pop.querySelector('.pp-preset.on') || {}).textContent || '';
  // Esc закрывает без запроса
  const before = out.calls.length;
  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  await tick(100);
  out.escЗакрыл = !document.querySelector('.pp-pop') && out.calls.length === before;

  // щелчок мимо окна закрывает его без запроса
  btn.click(); await tick(100);
  document.querySelector('#app h1').dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
  await tick(100);
  out.мимоЗакрыл = !document.querySelector('.pp-pop') && out.calls.length === before;

  // выбор отрезка щелчками: сначала поздний день, потом ранний — порядок не важен
  btn.click(); await tick(100);
  pop = document.querySelector('.pp-pop');
  const dayBtn = n => [...pop.querySelectorAll('.pp-day:not(.out)')]
    .find(b => b.textContent === String(n));
  dayBtn(12).click(); await tick(50);
  out.показатьЗакрытДоКонца = pop.querySelector('.pp-ok').disabled;
  out.подсказкаКонца = pop.querySelector('.pp-sel').textContent;
  dayBtn(3).click(); await tick(50);
  out.показатьОткрыт = !pop.querySelector('.pp-ok').disabled;
  out.отмеченоДней = pop.querySelectorAll('.pp-day.in').length;
  pop.querySelector('.pp-ok').click(); await tick(400);
  const m = pad(mFirst.getMonth() + 1), y = mFirst.getFullYear();
  out.ждёмЗапрос = `/ui/digest?from=${y}-${m}-03&to=${y}-${m}-12`;
  out.запросОтрезка = out.calls[out.calls.length - 1];
  out.шапкаПосле = (document.querySelector('.head .sub') || {}).textContent || '';

  // выбор помнится в той же вкладке
  location.hash = '#/channels'; await tick(150);
  location.hash = '#/digest'; await tick(400);
  out.запомнил = out.calls[out.calls.length - 1];

  // стрелки листают месяц; быстрый период — одним щелчком
  document.querySelector('.pp-btn').click(); await tick(100);
  pop = document.querySelector('.pp-pop');
  const was = pop.querySelector('.pp-month').textContent;
  pop.querySelector('.pp-prev').click(); await tick(50);
  out.листает = pop.querySelector('.pp-month').textContent !== was;
  const y1 = new Date(today); y1.setDate(y1.getDate() - 1);
  [...pop.querySelectorAll('.pp-preset')].find(x => x.textContent === 'Вчера').click();
  await tick(400);
  out.ждёмВчера = `/ui/digest?from=${iso(y1)}&to=${iso(y1)}`;
  out.запросВчера = out.calls[out.calls.length - 1];
  out.попапЗакрыт = !document.querySelector('.pp-pop');
  } catch (e) { out.errs.push('сценарий: ' + e.message); }
  finish();
})();
"""


def main() -> int:
    print("Сводка: период и блокеры в настоящем браузере")
    r = browser_harness.run_scenario(SCENARIO, budget_ms=20000)
    if "error" in r:
        print("ПРОПУЩЕНО:", r["error"])
        return 2
    check("ошибок JS нет", r.get("errs"), [])
    check("по умолчанию — без дат: сервер отдаёт текущий месяц", r.get("первыйЗапрос"), "/ui/digest")
    check("период словами в шапке", r.get("шапка", "").startswith("За 1–"), True)
    rows = r.get("первыеСтроки") or []
    check("первая строка — «Залито в расписание»",
          bool(rows) and rows[0].startswith("Залито в расписание") and rows[0].endswith("6"), True)
    check("«вышло» — отдельной строкой ниже",
          any(x.startswith("Вышло на YouTube, подтверждено") for x in rows), True)
    check("блокеры посчитаны", r.get("блокеры"), True)
    check("у каждой группы — чья беда",
          r.get("метки"), ["Сбой студии", "Настройка канала", "Проблема в доноре"])
    check("сказано, что делать", r.get("естьЧтоДелать"), True)
    check("длинный список каналов свёрнут", r.get("длинныйСписокКаналов"), True)
    check("кнопка периода есть", r.get("кнопкаЕсть"), True)
    check("на кнопке — период", r.get("надписьКнопки", "").startswith("▦ 1–"), True)
    check("календарь открывается", r.get("календарьОткрыт"), True)
    check("и открывается под кнопкой", r.get("подКнопкой"), True)
    check("быстрые периоды на месте", r.get("пресеты"),
          ["Сегодня", "Вчера", "7 дней", "30 дней", "Этот месяц", "Прошлый месяц"])
    check("текущий выбор отмечен", r.get("пресетОтмечен"), "Этот месяц")
    check("Esc закрывает без запроса", r.get("escЗакрыл"), True)
    check("щелчок мимо закрывает без запроса", r.get("мимоЗакрыл"), True)
    check("«Показать» закрыт до второго щелчка", r.get("показатьЗакрытДоКонца"), True)
    check("подсказка «выберите конец»", "выберите конец" in (r.get("подсказкаКонца") or ""), True)
    check("после второго щелчка — открыт", r.get("показатьОткрыт"), True)
    check("отрезок подсвечен по дням", r.get("отмеченоДней"), 10)
    check("на сервер ушёл отрезок, раньше — позже", r.get("запросОтрезка"), r.get("ждёмЗапрос"))
    check("шапка назвала новый период", (r.get("шапкаПосле") or "").startswith("За 3–12 "), True)
    check("выбор помнится в той же вкладке", r.get("запомнил"), r.get("ждёмЗапрос"))
    check("стрелки листают месяц", r.get("листает"), True)
    check("«Вчера» — одним щелчком", r.get("запросВчера"), r.get("ждёмВчера"))
    check("календарь закрылся", r.get("попапЗакрыт"), True)
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
