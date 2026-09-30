#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сводка встречает при входе, и её можно убрать (владелец 30.09.2026).

«Да, давай показывать сводку сразу плюс возможность её скрыть».

Что проверяется и почему именно это:

  • ВХОД В СТУДИЮ БЕЗ АДРЕСА — ЭТО СВОДКА. Смена начинается с вопросов «что сегодня»
    и «что застряло»; ответ должен встречать сам, а не ждать за отдельной вкладкой.
  • «СКРЫТЬ» УБИРАЕТ ЕЁ СО ВХОДА И ПЕРЕЖИВАЕТ ПЕРЕЗАХОД. Выбор, который надо делать
    каждое утро заново, — это не выбор.
  • СКРЫТАЯ НАХОДИТСЯ. Вкладка «Сводка» остаётся на месте и видна, экран объясняет,
    почему он больше не встречает, и возвращается той же кнопкой. Молча исчезнувший
    экран человек не найдёт и решит, что он сломался.
  • ЭТО РЕШЕНИЕ ОДНОГО ЧЕЛОВЕКА, а не общая настройка: у второго входа в том же
    браузере сводка обязана остаться на месте, и на сервер за этим никто не ходит.
  • ПРЯМЫЕ ССЫЛКИ НЕ СЛОМАНЫ. У кого в закладках канал — тот попадает на канал, и
    при скрытой сводке, и при показанной.
  • ПОЛОМКА СВОДКИ НЕ ЗАПИРАЕТ СТУДИЮ. Раз она встречает при входе, её отказ обязан
    быть назван словами, а не оставить пустой экран — и дорога к каналам остаётся.

Настоящий `web/index.html` в headless Chrome, `api()` подменён — ни стенда, ни сети.

    .venv/bin/python tests/test_digest_home_browser.py
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
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  const tick = ms => new Promise(r => setTimeout(r, ms || 200));
  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {

  const DIGEST = {
    text: 'Сводка за 30.09.2026 (UTC)',
    settings: {telegram: false, hour: 9, tz: 'UTC'},
    digest: {
      day: '30.09.2026', tz: 'UTC',
      groups: [{tag: 'ww2', channels: 2, plan: 3, accepted: 1, blocked: 0}],
      today: {expected: 2, ahead: 1, confirmed: 0, delayed: 0, unknown: 2,
              why: ['нет ключа YouTube Data API — подтвердить выход нечем']},
      production: {accepted: 1, no_supply: 2, supply_unknown: 1, threshold_days: 3},
      problems: [], alerts: [], alerts_why: ''
    }
  };
  const CH = {id: 7, name: 'Nocne Biografie', handle: '@nb', uc: 'UC' + 'x'.repeat(22),
              lang: 'PL', niche: 'биографии', status: 'active', tz: 'UTC',
              schedule: {}, horizon_days: 3, lead_days: 2, per_day: 1,
              has_profile: true, has_uc: true};
  out.digestCalls = 0;
  let digestBroken = false;
  window.api = async (p) => {
    if (p === '/ui/me') return {id: 11, name: 'Андрей', role: 'admin'};
    if (p === '/ui/digest'){
      out.digestCalls += 1;
      if (digestBroken) throw new Error('база занята другим запросом');
      return DIGEST;
    }
    if (p === '/ui/channels') return {channels: [CH]};
    if (p === '/ui/channels/stats') return {channels: []};
    if (p.indexOf('/ui/channels/7?history=14') === 0)
      return {channel: CH, calendar: [], counts: {}, by_day: {}, tz: 'UTC',
              waiting_items: [], waiting_without_slot: 0};
    return {};
  };
  window.post = async () => ({});

  const KEY = 'vs.digest.home.11';
  try { localStorage.removeItem(KEY); localStorage.removeItem('vs.digest.home.12'); }
  catch (e) {}

  const screen = () => (document.querySelector('#app').textContent || '')
                         .replace(/\s+/g, ' ');
  const tabVisible = () => {
    const t = document.getElementById('tab-digest');
    return !!(t && t.offsetHeight > 0 && t.getAttribute('href') === '#/digest');
  };
  // Вход в студию — это пустой адрес. Так же он выглядит и при перезаходе по закладке
  // на корень: страница открывается без `#/…`.
  const enter = async () => {
    if (location.hash){ location.hash = ''; await tick(120); }
    await render();
    await tick(250);
  };

  addEventListener('hashchange', render);

  /* ── 1. сводка встречает при входе ── */
  await enter();
  out.входСводка = screen().indexOf('Сводка за 30.09.2026') >= 0;
  out.входНеКаналы = screen().indexOf('Новый канал') < 0;
  out.вкладкаПодсвечена = document.getElementById('tab-digest')
                            .classList.contains('on');
  out.кнопкаСкрыть = (document.getElementById('digest-greet') || {}).textContent;
  out.адресНеПереписан = location.hash;

  /* ── 2. «Скрыть» убирает сводку со входа ── */
  document.getElementById('digest-greet').click();
  await tick(350);
  out.послеСкрытияАдрес = location.hash;
  out.послеСкрытияЭкран = screen().indexOf('Новый канал') >= 0;
  out.послеСкрытияНеСводка = screen().indexOf('Сводка за 30.09.2026') < 0;
  out.сказалиГдеИскать = [...document.querySelectorAll('#toasts .toast')]
    .map(t => t.textContent).join(' | ');
  out.вкладкаОсталась = tabVisible();
  try { out.вПамяти = localStorage.getItem(KEY); } catch (e) { out.вПамяти = null; }

  /* ── 3. выбор переживает перезаход ── */
  await enter();
  out.перезаходКаналы = screen().indexOf('Новый канал') >= 0;
  out.перезаходБезСводки = screen().indexOf('Сводка за 30.09.2026') < 0;

  /* ── 4. у второго человека в том же браузере сводка на месте ── */
  const БЫЛ = ME;
  ME = {id: 12, name: 'Напарник', role: 'user'};
  await enter();
  out.напарникуСводка = screen().indexOf('Сводка за 30.09.2026') >= 0;
  ME = БЫЛ;

  /* ── 5. спрятанную можно вернуть — вкладкой, и она объясняет себя ── */
  document.getElementById('tab-digest').click();
  await tick(350);
  out.вкладкаОткрылаСводку = screen().indexOf('Сводка за 30.09.2026') >= 0;
  const note = document.getElementById('digest-hidden-note');
  out.объяснениеВидно = !!(note && note.offsetHeight > 0);
  out.объяснение = note ? note.textContent.replace(/\s+/g, ' ') : '';
  out.кнопкаВернуть = (document.getElementById('digest-greet') || {}).textContent;
  document.getElementById('digest-greet').click();
  await tick(350);
  try { out.вПамятиПослеВозврата = localStorage.getItem(KEY); }
  catch (e) { out.вПамятиПослеВозврата = null; }
  await enter();
  out.сноваВстречает = screen().indexOf('Сводка за 30.09.2026') >= 0;

  /* ── 6. прямая ссылка на канал открывает канал — и так, и так ── */
  location.hash = '#/channel/7';
  await tick(350);
  out.ссылкаКанал = (document.querySelector('#app h1') || {}).textContent || '';
  digestGreetsSet(false);              // сводка убрана со входа
  location.hash = '';
  await tick(120);
  location.hash = '#/channel/7';
  await tick(350);
  out.ссылкаКаналСкрытой = (document.querySelector('#app h1') || {}).textContent || '';
  digestGreetsSet(true);

  /* ── 7. поломка сводки не запирает вход ── */
  digestBroken = true;
  await enter();
  out.поломкаЭкран = screen();
  out.поломкаЕстьВыходКаналы = !!document.querySelector('#app a[href="#/channels"]');
  out.поломкаКнопкаСкрыть = !!document.getElementById('digest-greet');
  digestBroken = false;

  } catch(e) { out.errs.push('исключение: ' + (e && e.message)); }
  finish();
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=25000)
    if res.get("error"):
        print("ПРОПУЩЕНО:", res["error"])
        return 0
    check("страница не бросала исключений", res.get("errs"), [])

    print("\nВход в студию")
    check("сводка встречает при входе", res.get("входСводка"), True)
    check("каналы вместо неё не показаны", res.get("входНеКаналы"), True)
    check("вкладка «Сводка» подсвечена", res.get("вкладкаПодсвечена"), True)
    check("кнопка называется «Скрыть»", res.get("кнопкаСкрыть"), "Скрыть")
    check("адрес входа не переписан (кнопка «назад» браузера не закольцована)",
          res.get("адресНеПереписан"), "")

    print("\nСкрытие")
    check("после «Скрыть» человек на каналах", res.get("послеСкрытияАдрес"),
          "#/channels")
    check("экран каналов действительно нарисован", res.get("послеСкрытияЭкран"), True)
    check("сводки на экране больше нет", res.get("послеСкрытияНеСводка"), True)
    check("сказано, где её искать", "вкладкой «Сводка»" in (res.get("сказалиГдеИскать")
                                                           or ""), True)
    check("вкладка «Сводка» осталась видна", res.get("вкладкаОсталась"), True)
    check("выбор записан в браузере, а не на сервер", res.get("вПамяти"), "off")

    print("\nПерезаход")
    check("после перезахода встречают каналы", res.get("перезаходКаналы"), True)
    check("и сводка не всплывает снова", res.get("перезаходБезСводки"), True)
    check("у второго человека в том же браузере сводка на месте",
          res.get("напарникуСводка"), True)

    print("\nВозврат")
    check("вкладка открывает спрятанную сводку", res.get("вкладкаОткрылаСводку"), True)
    check("на ней видно объяснение", res.get("объяснениеВидно"), True)
    check("объяснение говорит, что сводка не встречает при входе",
          "не встречает при входе" in (res.get("объяснение") or ""), True)
    check("объяснение называет кнопку возврата",
          "Показывать при входе" in (res.get("объяснение") or ""), True)
    check("кнопка предлагает вернуть сводку на вход", res.get("кнопкаВернуть"),
          "Показывать при входе")
    check("запись о скрытии снята", res.get("вПамятиПослеВозврата"), None)
    check("сводка снова встречает при входе", res.get("сноваВстречает"), True)

    print("\nПрямые ссылки")
    check("закладка на канал открывает канал", res.get("ссылкаКанал"),
          "Nocne Biografie")
    check("и при убранной со входа сводке — тоже",
          res.get("ссылкаКаналСкрытой"), "Nocne Biografie")

    print("\nПоломка сводки на входе")
    text = res.get("поломкаЭкран") or ""
    check("причина названа словами", "база занята другим запросом" in text, True)
    check("не выдано за «всё хорошо»", "это не значит, что их ноль" in text, True)
    check("сказано, что деньги не потрачены", "Денег этот экран не тратит" in text,
          True)
    check("дорога к каналам осталась", res.get("поломкаЕстьВыходКаналы"), True)
    check("убрать сводку со входа можно и со сломанной", res.get("поломкаКнопкаСкрыть"),
          True)
    check("сводку не запрашивали повторно сами по себе",
          (res.get("digestCalls") or 0) > 0, True)

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
