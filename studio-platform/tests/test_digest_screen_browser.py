#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экран «Сводка» в настоящем браузере: числа, «не знаем» и выключенная рассылка.

Почему этого мало проверять по исходнику. Сводку читают глазами и копируют в отчёт
заказчику, поэтому опасны здесь не формулы, а то, что видно:

  * «подтверждено» — самое дорогое число экрана. Если подтверждения нет, потому что
    нет ключа, на экране обязано стоять «не знаем», а не ноль: ноль читается как
    «сегодня ничего не вышло», то есть как авария, которой нет;
  * канал без тега обязан быть виден в группе «прочие» — сводка, из которой канал
    тихо выпал, хуже отсутствия сводки;
  * кнопка копирования обязана отдавать ТОТ ЖЕ текст, что уходит в телеграм, а не
    разметку страницы: отчёт вставляют в переписку;
  * галочка рассылки обязана быть снята при открытии. Владелец просил уменьшить поток
    сообщений, и экран, открывающийся с включённой галочкой, включит рассылку первым
    же случайным сохранением.

Настоящий `web/index.html` в headless Chrome, `api()`/`post()` подменены — ни стенда,
ни сети, ни базы.

    .venv/bin/python tests/test_digest_screen_browser.py
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
    text: 'Сводка за 07.10.2026 (UTC)\n\nСегодня: ожидалось 2 выхода, ' +
          'подтверждено: не знаем, задержано 0.',
    settings: {telegram: false, hour: 9, tz: 'UTC'},
    digest: {
      day: '07.10.2026', tz: 'UTC',
      groups: [{tag: 'ww2', channels: 2, plan: 3, accepted: 1, blocked: 0},
               {tag: 'прочие', channels: 1, plan: 0, accepted: 0, blocked: 2}],
      today: {expected: 2, ahead: 1, confirmed: 0, delayed: 0, unknown: 2,
              why: ['нет ключа YouTube Data API — подтвердить выход нечем']},
      production: {accepted: 1, no_supply: 2, supply_unknown: 1, threshold_days: 3},
      problems: [{channel: 'Третий', channel_id: 77, why: 'ключ FastGen кончился',
                  next: 'не назначен — материала на ближайший выход нет'}],
      alerts: [{key: 'a', title: 'Каналам не хватает материала'}], alerts_why: ''
    }
  };
  window.api = async (p) => {
    if (p === '/ui/me') return {name: 'Андрей', role: 'admin'};
    if (p === '/ui/digest') return DIGEST;
    return {};
  };
  out.posted = [];
  window.post = async (p, body) => {
    out.posted.push([p, body]);
    return {telegram: body.telegram, hour: body.hour, tz: 'UTC'};
  };
  // Буфер обмена в headless недоступен — подменяем, чтобы увидеть, ЧТО кладут.
  out.copied = '';
  // Присваивание `navigator.clipboard = …` Chrome молча игнорирует (свойство только
  // для чтения), поэтому подменяем его через defineProperty — иначе проверка copy
  // молча ничего не проверяла бы.
  Object.defineProperty(navigator, 'clipboard', {configurable: true,
    value: {writeText: async (t) => { out.copied = t; }}});

  addEventListener('hashchange', render);
  location.hash = '#/digest';
  await tick(500);

  out.экран = document.querySelector('#app').textContent.replace(/\s+/g, ' ');
  out.вкладкаПодсвечена = document.getElementById('tab-digest')
                            .classList.contains('on');
  out.чужаяВкладкаПогасла = !document.getElementById('tab-channels')
                              .classList.contains('on');

  const tg = document.getElementById('digest-tg');
  out.галочкаСнята = tg && !tg.checked;
  out.галочкаДоступна = tg && !tg.disabled;

  document.getElementById('digest-copy').click();
  await tick(120);
  out.скопированоРавноТексту = out.copied === DIGEST.text;

  tg.checked = true;
  tg.dispatchEvent(new Event('change', {bubbles: true}));
  await tick(200);

  // Клик по строке проблемы ведёт на её канал.
  document.querySelector('#app tr[data-cid]').click();
  await tick(200);
  out.ушлиНаКанал = location.hash;

  } catch(e) { out.errs.push('исключение: ' + (e && e.message)); }
  finish();
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=20000)
    check("сценарий выполнен без ошибок", res.get("error"), None)
    if res.get("error"):
        print("  (Chrome не найден или страница не отчиталась — проверки не выполнены)")
        return 1
    check("страница не бросала исключений", res.get("errs"), [])

    text = res.get("экран") or ""
    print("\nЧисла сводки на экране")
    check("вкладка «Сводка» подсвечена", res.get("вкладкаПодсвечена"), True)
    check("чужая вкладка при этом погасла", res.get("чужаяВкладкаПогасла"), True)
    check("день отчёта в заголовке", "Сводка за 07.10.2026" in text, True)
    check("подтверждение названо «не знаем»", "не знаем" in text, True)
    check("нуля вместо «не знаем» на экране нет",
          "Подтверждён выход на YouTube 0" in text, False)
    check("причина незнания написана",
          "нет ключа YouTube Data API" in text, True)
    check("группа «прочие» видна", "прочие" in text, True)
    check("её блокеры видны", "Блокеры" in text, True)
    check("каналов с неизвестным запасом — отдельной строкой",
          "запас неизвестен" in text, True)
    check("проблема названа каналом, причиной и следующим результатом",
          all(p in text for p in ("Третий", "ключ FastGen кончился",
                                  "не назначен")), True)
    check("действующие тревоги перечислены",
          "Каналам не хватает материала" in text, True)

    print("\nОтчёт и рассылка")
    check("копируется текст сервера, а не разметка",
          res.get("скопированоРавноТексту"), True)
    check("галочка рассылки снята при открытии", res.get("галочкаСнята"), True)
    check("администратору она доступна", res.get("галочкаДоступна"), True)
    posted = res.get("posted") or []
    check("включение ушло на сервер одним запросом", len(posted), 1)
    check("и это адрес настройки сводки", posted and posted[0][0],
          "/ui/digest/settings")
    check("включение передано явно", posted and posted[0][1].get("telegram"), True)
    check("час отправки передан", posted and posted[0][1].get("hour"), 9)

    print("\nПереходы")
    check("клик по проблеме ведёт на её канал", res.get("ушлиНаКанал"), "#/channel/77")

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
