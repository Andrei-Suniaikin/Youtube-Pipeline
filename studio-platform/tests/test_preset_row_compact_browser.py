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
        it('4', 'Тёплые', 'cover', [], [], {channels: 0, projects: 0, presets: 0}),
      ]}, {service: 'cover', title: 'Обложки', items: [
        {key: 'Тёплые', name: 'Тёплые', facts: [], channels: []}]}]};
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
  // Одноимённые строки в «Обложке»: ссылка из производства и сам пресет обложки.
  const тёплые = [...document.querySelectorAll('.pitem')].filter(
    r => r.querySelector('.name').textContent.trim() === 'Тёплые');
  out.тёплые = тёплые.map(r => ({тег: (r.querySelector('.ptag') || {}).textContent || '',
    удалить: r.querySelector('.pdel').dataset.kind}));
  // Действия — значками, как IconButton в MUI (владелец 22.09.2026): слова на кнопке
  // нет, но оно есть в подсказке и для читалки — вместе с именем пресета.
  out.значки = [...row('Польский').querySelectorAll('.pacts button')].map(b => ({
    значок: !!b.querySelector('svg') && b.classList.contains('iconbtn'),
    текст: b.textContent.trim(), подсказка: b.title, читалке: b.getAttribute('aria-label')}));
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
    # 22.09.2026 владелец: настройки в карточке не выписывать — «очень мешает».
    check("используемый: только название, ниже каналы и проекты — без повтора числа",
          res.get("польский"), ["Польский", "каналы: pisun · проекты: 3"])
    check("неиспользуемый: без прочерка и без нулей",
          res.get("пустой"), ["Пустой", "ни один канал им не пользуется"])
    check("частичный через целые — так и сказано, с числом один раз",
          res.get("анна"), ["Голос Анна",
                            "напрямую каналы не ссылаются — используется через целые "
                            "пресеты (2)"])
    check("действия — значками, слово в подсказке и для читалки, с именем пресета",
          res.get("значки"),
          [{"значок": True, "текст": "", "подсказка": "Править", "читалке": "Править «Польский»"},
           {"значок": True, "текст": "", "подсказка": "Копия", "читалке": "Копия «Польский»"},
           {"значок": True, "текст": "", "подсказка": "Удалить",
            "читалке": "Удалить «Польский»"}])
    check("одноимённые строки различимы: у ссылки пометка, вопрос называет, что удаляется",
          res.get("тёплые"), [{"тег": "ссылка", "удалить": "ссылку на пресет обложки"},
                              {"тег": "", "удалить": "пресет обложки"}])
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
