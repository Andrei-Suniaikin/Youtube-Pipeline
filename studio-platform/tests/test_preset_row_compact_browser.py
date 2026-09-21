#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Строка пресета — две строки текста, а не четыре (жалоба владельца 21.09.2026).

Было: название; факты или «—»; «каналы: 0, проекты: 0»; «ни один канал им не
пользуется» — и три крупные кнопки. Одно и то же говорилось дважды, а пустое место
заполнялось прочерком. Теперь:

* название и факты — одной строкой; пустых фактов нет — нет и прочерка;
* кто пользуется — второй строкой; нули не печатаются, число каналов рядом с их
  списком не повторяется;
* правда при этом не теряется: неиспользуемый так и назван, используемый через целые
  пресеты — тоже, а проекты считаются, когда они есть.

    .venv/bin/python tests/test_preset_row_compact_browser.py
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
  try {
  const it = (key, name, scope, facts, channels, users) =>
    ({key, name, scope, facts, channels, users});
  window.api = async p => {
    if (p === '/ui/presets') return {broken: [], groups: [{service: 'vsgen',
      title: 'Производство', items: [
        it('1', 'Польский', 'full', ['engine: lumean'], [{id: 5, name: 'pisun'}],
           {channels: 1, projects: 3, presets: 0}),
        it('2', 'Пустой', 'full', [], [], {channels: 0, projects: 0, presets: 0}),
        it('3', 'Голос Анна', 'voice', [], [], {channels: 0, projects: 0, presets: 2}),
      ]}]};
    if (p === '/ui/channel-presets') return {presets: [], note: ''};
    if (p === '/ui/lumean/templates') return {ok: true, templates: [], fields: []};
    return {};
  };
  try { localStorage.removeItem('presetFilter'); } catch (e) {}
  await viewPresets();
  const row = name => [...document.querySelectorAll('.pitem')].find(
    r => r.querySelector('.name').textContent.trim() === name);
  const lines = r => [...r.querySelectorAll('.body > div')].map(
    d => d.textContent.replace(/\s+/g, ' ').trim());
  out.польский = lines(row('Польский'));
  out.пустой = lines(row('Пустой'));
  out.анна = lines(row('Голос Анна'));
  out.кнопки_маленькие = [...row('Польский').querySelectorAll('.pacts button')]
    .every(b => b.classList.contains('small'));
  } catch (e) { out.errs.push(String(e && e.stack || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=6000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2
    check("ошибок в браузере нет", res.get("errs"), [])
    check("используемый: название с фактами, ниже каналы и проекты — без повтора числа",
          res.get("польский"), ["Польскийengine: lumean", "каналы: pisun · проекты: 3"])
    check("неиспользуемый: без прочерка и без нулей",
          res.get("пустой"), ["Пустой", "ни один канал им не пользуется"])
    check("частичный через целые — так и сказано, с числом один раз",
          res.get("анна"), ["Голос Анна",
                            "напрямую каналы не ссылаются — используется через целые "
                            "пресеты (2)"])
    check("кнопки строки — маленькие", res.get("кнопки_маленькие"), True)
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
