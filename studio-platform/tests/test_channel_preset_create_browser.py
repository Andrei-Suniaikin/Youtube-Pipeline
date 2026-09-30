#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пресет канала: окно с полями и быстрое удаление (30.09.2026).

Две жалобы владельца, обе про один экран.

ПЕРВАЯ: «когда создаю пресет для канала, у меня почему-то не появляется попап для
параметров этого пресета, а пустой пресет просто в список скидывается».

Причина найдена: поля для НОВОГО пресета брались у уже существующего
(`/ui/channel-presets` → первый из списка → его поля). Когда ни одного пресета нет —
первый в студии или все удалили — брать было не у кого, и окно выходило без единого
поля. Человек сохранял одно название, и в списке появлялся пустой пресет. Поля при этом
не зависят ни от какого пресета: это константа на сервере.

ВТОРАЯ: «когда удаляю пресет, это почему-то долго происходит, это из-за SQLite?». Нет:
сервер тратит на удаление около трёх миллисекунд. Долгим было то, что ПОСЛЕ удаления
экран перерисовывался целиком, а список пресетов производства ходит по сети к соседним
сервисам с шестисекундным ожиданием на каждого.

    .venv/bin/python tests/test_channel_preset_create_browser.py
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
    out = str(text or "")
    for sp in (" ", " ", " ", "\n", "\t"):
        out = out.replace(sp, " ")
    return " ".join(out.split())


SCENARIO = r"""
(async () => {
  const out = {errs: []};
  addEventListener('error', e => out.errs.push(e.message));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    ME = {name: 'boss', role: 'admin'};
    const ПОЛЯ = [
      {key: 'schedule_days', label: 'Дни', kind: 'days', value: []},
      {key: 'schedule_times', label: 'Время выходов', kind: 'text', value: ''},
      {key: 'tz', label: 'Часовой пояс', kind: 'text', value: ''},
      {key: 'horizon_days', label: 'Горизонт', kind: 'text', value: ''},
      {key: 'host_id', label: 'Машина заливки', kind: 'select', value: '', options: []},
      {key: 'niche', label: 'Ниша', kind: 'text', value: ''}];

    // Пресетов НЕТ ни одного — тот самый случай, в котором окно выходило пустым.
    let ПРЕСЕТЫ = [];
    let ПУСТЫЕ_ПОЛЯ = false;
    let спрошено = [];
    let медленных = 0;

    window.api = async (p, opt) => {
      спрошено.push(((opt || {}).method || 'GET') + ' ' + p);
      if (p === '/ui/me') return ME;
      if (p === '/ui/channels') return {channels: []};
      if (p === '/ui/channels/stats') return {channels: []};
      if (p === '/ui/channel-presets') return {presets: ПРЕСЕТЫ, note: ''};
      if (p === '/ui/channel-presets/new')
        return {id: null, name: '', note: '', fields: ПУСТЫЕ_ПОЛЯ ? [] : ПОЛЯ,
                channels: [], days: DAY_ORDER, any_day: '*'};
      if (p.startsWith('/ui/channel-presets/') && (opt || {}).method === 'DELETE'){
        ПРЕСЕТЫ = ПРЕСЕТЫ.filter(x => String(x.id) !== p.split('/').pop());
        return {ok: true};
      }
      if (p === '/ui/presets'){
        // Так ведёт себя настоящий экран: список пресетов производства ходит по сети к
        // соседям. Здесь это медленный ответ — если удаление его дожидается, разница
        // видна сразу.
        медленных++;
        await sleep(700);
        return {groups: [], broken: [], note: '', services: []};
      }
      if (p === '/ui/text') return {models: [], presets: []};
      if (p === '/ui/covers') return {options: [], presets: []};
      return {};
    };
    window.post = async () => ({ok: true});
    addEventListener('hashchange', render);

    location.hash = '#/presets';
    await sleep(900);

    // ── 1. Окно заведения при ПУСТОМ списке пресетов ──
    спрошено = [];
    document.querySelector('#chp-new').click();
    await sleep(300);
    const окно = document.querySelector('dialog.modal');
    out.окноОткрылось = !!окно;
    out.полейВОкне = окно ? окно.querySelectorAll('.mbody .fld').length : 0;
    out.спросилиПустой = спрошено.some(x => x.includes('/ui/channel-presets/new'));
    out.неБралиУСоседа = !спрошено.some(x => x === 'GET /ui/channel-presets');
    if (окно) окно.remove();
    await sleep(60);

    // ── 1б. Сервер не дал полей — окна быть не должно ──
    // Пустая форма и есть та беда, из-за которой заводился пустой пресет. Лучше честно
    // отказать, чем принять одно название в никуда.
    ПУСТЫЕ_ПОЛЯ = true;
    document.querySelector('#chp-new').click();
    await sleep(300);
    out.окноБезПолей = !!document.querySelector('dialog.modal');
    out.сказалиПроОтказ = (document.querySelector('#toasts') || {}).textContent || '';
    document.querySelectorAll('dialog.modal').forEach(d => d.remove());
    ПУСТЫЕ_ПОЛЯ = false;
    await sleep(60);

    // ── 2. Удаление не ждёт перерисовки всего экрана ──
    ПРЕСЕТЫ = [{id: 7, name: 'Ночные', note: '', channels: [], given: []},
               {id: 8, name: 'Дневные', note: '', channels: [], given: []}];
    location.hash = '#/channels';
    await sleep(200);
    location.hash = '#/presets';
    await sleep(900);
    out.строкДоУдаления = document.querySelectorAll('[data-chp]').length;

    медленных = 0;
    const t0 = performance.now();
    document.querySelector('.chp-del').click();
    await sleep(80);
    const да = [...document.querySelectorAll('dialog.modal .m-ok')].pop();
    да.click();
    await sleep(250);
    out.мс = Math.round(performance.now() - t0);
    out.строкПослеУдаления = document.querySelectorAll('[data-chp]').length;
    out.ходилиЗаПресетамиПроизводства = медленных;
    out.сказалиЧтоУдалили = (document.querySelector('#toasts') || {}).textContent || '';

    // ── 3. Удалили последний — список объясняет пустоту ──
    document.querySelector('.chp-del').click();
    await sleep(80);
    [...document.querySelectorAll('dialog.modal .m-ok')].pop().click();
    await sleep(250);
    const карточка = [...document.querySelectorAll('.card')]
      .find(el => (el.textContent || '').includes('Пресеты каналов'));
    out.пустойСписок = карточка ? карточка.textContent : '';
  } catch (e) { out.errs.push(String(e && e.message || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=12000)
    if res.get("error"):
        print("ПРОПУЩЕНО:", res["error"])
        return 0
    check("ошибок на странице нет", res.get("errs"), [])

    print("Окно заведения открывается с полями, даже когда пресетов ещё нет")
    check("окно открылось", res.get("окноОткрылось"), True)
    check("и в нём есть поля, а не одно название",
          (res.get("полейВОкне") or 0) >= 5, True)
    check("поля спрошены отдельным маршрутом", res.get("спросилиПустой"), True)
    check("а не взяты у соседа по списку", res.get("неБралиУСоседа"), True)

    print("Полей нет — окна нет")
    check("пустая форма не открывается", res.get("окноБезПолей"), False)
    check("и сказано, что не вышло",
          "поля" in flat(res.get("сказалиПроОтказ")).lower(), True)

    print("Удаление не тянет за собой перерисовку всего экрана")
    check("до удаления строк две", res.get("строкДоУдаления"), 2)
    check("после удаления одна", res.get("строкПослеУдаления"), 1)
    # Главное в этой проверке: за пресетами производства (это сеть к соседям) после
    # удаления не ходили ВОВСЕ. Раньше ходили, и человек ждал их ответа.
    check("за пресетами производства не ходили",
          res.get("ходилиЗаПресетамиПроизводства"), 0)
    check("удаление уложилось быстрее медленного ответа соседей",
          (res.get("мс") or 9999) < 700, True)
    check("и человеку сказали, что удалили",
          "удал" in flat(res.get("сказалиЧтоУдалили")).lower(), True)

    print("Список опустел — это объяснено, а не оставлено пустым местом")
    пусто = flat(res.get("пустойСписок"))
    check("сказано, что пресетов нет", "Пресетов каналов нет" in пусто, True)
    check("и зачем они нужны", "расписание" in пусто, True)

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
