#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Карточка «Telegram-отчёт» в профиле — в настоящем браузере (14.10.2026).

Серверная часть проверена `test_tg_personal_report.py`; здесь — то, что видит человек:
не подключён → кнопка → одноразовая ссылка; подключён → галочка, час, выбор каналов;
каждая правка уходит на сервер, отказ сервера возвращает галочку как была.

Настоящий `web/index.html` в headless Chrome, `api()`/`post()` подменены.

    .venv/bin/python tests/test_tg_report_card_browser.py
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
  const out = {errs: [], posted: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  const tick = ms => new Promise(r => setTimeout(r, ms || 150));
  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {
  let D = {linked: false, bot_ready: true, enabled: false, hour: 9, tz: 'Europe/Warsaw',
           filter: {mode: 'all', tags: [], channels: []},
           tags: [{id: 1, name: 'ww2'}], channels: [{id: 7, name: 'Stal i Popiół'}]};
  let refuse = false;
  window.api = async (p) => p === '/ui/tg-report' ? D : {};
  window.post = async (p, body) => {
    out.posted.push([p, body || null]);
    if (refuse) throw new Error('сервер отказал');
    if (p === '/ui/tg-report/link')
      D = {...D, code: 'abc', link: 'https://t.me/studio_bot?start=abc',
           code_expires: Date.now() / 1000 + 900};
    if (p === '/ui/tg-report/settings') D = {...D, ...body};
    return D;
  };
  REF_FOLD_OPEN['profile:tgreport'] = true;
  const host = document.querySelector('#app');
  host.innerHTML = '';
  host.append(await tgReportCard());
  await tick();
  const text = () => host.textContent.replace(/\s+/g, ' ');
  out.первый = text();
  out.кнопкаЕсть = !!host.querySelector('#tgr-link');
  host.querySelector('#tgr-link').click();
  await tick();
  out.ссылка = (host.querySelector('a[href^="https://t.me/"]') || {}).href || '';

  D = {...D, linked: true, chat_title: 'Андрей', code: null, link: null};
  host.innerHTML = '';
  host.append(await tgReportCard());
  await tick();
  out.подключён = text();
  const on = host.querySelector('#tgr-on');
  on.checked = true; on.dispatchEvent(new Event('change'));
  await tick();
  out.включено = D.enabled;

  refuse = true;
  const on2 = host.querySelector('#tgr-on');
  on2.checked = false; on2.dispatchEvent(new Event('change'));
  await tick();
  out.послеОтказаГалочка = host.querySelector('#tgr-on').checked;
  out.ошибкаВидна = text().includes('сервер отказал');
  refuse = false;

  const pick = host.querySelector('input[name="tgr-mode"][value="pick"]');
  pick.checked = true; pick.dispatchEvent(new Event('change'));
  await tick();
  out.выборВиден = !host.querySelector('#tgr-pick').hidden;
  const chip = host.querySelector('.langs[data-kind="channels"] label input');
  chip.checked = true; chip.dispatchEvent(new Event('change'));
  await tick();
  out.последний = out.posted[out.posted.length - 1];
  } catch(e) { out.errs.push('исключение: ' + (e && e.message)); }
  finish();
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=15000)
    check("сценарий выполнен", res.get("error"), None)
    if res.get("error"):
        return 1
    check("исключений нет", res.get("errs"), [])
    check("не подключён — так и написано", "не подключён" in (res.get("первый") or ""), True)
    check("кнопка «Подключить Telegram» есть", res.get("кнопкаЕсть"), True)
    check("после нажатия — ссылка на бота", res.get("ссылка"),
          "https://t.me/studio_bot?start=abc")
    check("подключён — видно чат", "Андрей" in (res.get("подключён") or ""), True)
    check("галочка включила отчёт", res.get("включено"), True)
    check("отказ сервера — галочка вернулась", res.get("послеОтказаГалочка"), True)
    check("отказ сервера — причина видна", res.get("ошибкаВидна"), True)
    check("«по выбору» показывает теги и каналы", res.get("выборВиден"), True)
    check("выбранный канал ушёл на сервер", res.get("последний"),
          ["/ui/tg-report/settings", {"filter": {"mode": "pick", "tags": [], "channels": [7]}}])
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
