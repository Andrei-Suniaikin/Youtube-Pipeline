#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выбор языков — одной строкой, а не стеной из сорока галочек (владелец 21.09.2026).

На «Сценарии», «Обложке» и в пакетной карточке текста список языков висел раскрытым
всегда, и сам заказ уезжал под него. Теперь:

* видна кнопка с итогом выбора («выбрать языки», «Русский, Польский», «… +2»);
* список с поиском раскрывается по ней, поиск прячет неподходящие;
* галочки и их обработчики — прежние: выбор по-прежнему уходит в заказ.

    .venv/bin/python tests/test_lang_picker_browser.py
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
  const pause = ms => new Promise(r => setTimeout(r, ms));
  try {
  const box = document.createElement('div');
  box.className = 'langs';
  box.innerHTML = ['Русский', 'Польский', 'Чешский', 'Немецкий', 'Английский']
    .map(n => `<label><input type="checkbox">${n}</label>`).join('');
  const host = document.createElement('div');
  host.append(box);
  document.getElementById('app').append(host);
  let ушло = 0;
  box.querySelectorAll('input').forEach(i => i.onchange = () => ушло++);
  langPicker(box);
  const btn = host.querySelector('.lpick-btn');
  const видно = el => el.getClientRects().length > 0;
  out.сначала = btn.textContent;
  out.список_скрыт = !видно(box);
  btn.click();
  out.раскрылся = видно(box);
  out.раскрыт_для_читалки = btn.getAttribute('aria-expanded');
  const pick = n => { const l = [...box.querySelectorAll('label')].find(
    x => x.textContent.trim() === n); const i = l.querySelector('input');
    i.checked = !i.checked; i.dispatchEvent(new Event('change', {bubbles: true})); };
  pick('Польский'); pick('Русский');
  out.два = btn.textContent;
  pick('Чешский'); pick('Немецкий'); pick('Английский');
  out.пять = btn.textContent;
  out.обработчики_целы = ушло;
  const q = host.querySelector('.lpick-q');
  q.value = 'чеш'; q.dispatchEvent(new Event('input'));
  out.поиск = [...box.querySelectorAll('label')].filter(видно).map(l => l.textContent.trim());
  btn.click();
  out.свернулся = !видно(box);
  } catch (e) { out.errs.push(String(e && e.stack || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=4000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2
    check("ошибок в браузере нет", res.get("errs"), [])
    check("сначала — приглашение выбрать", res.get("сначала"), "выбрать языки")
    check("стены галочек не видно", res.get("список_скрыт"), True)
    check("кнопка раскрывает список", res.get("раскрылся"), True)
    check("и говорит читалке, что раскрыто", res.get("раскрыт_для_читалки"), "true")
    check("итог — выбранные по порядку списка", res.get("два"), "Русский, Польский")
    check("длинный выбор — коротко", res.get("пять"), "Русский, Польский, Чешский +2")
    check("галочки по-прежнему доходят до своих обработчиков",
          res.get("обработчики_целы"), 5)
    check("поиск оставляет подходящие", res.get("поиск"), ["Чешский"])
    check("повторное нажатие сворачивает", res.get("свернулся"), True)
    print("\nПодключено там, где язык выбирают для заказа")
    html = open(os.path.join(HERE, "..", "web", "index.html"), encoding="utf-8").read()
    check("«Сценарий»", "langPicker(card.querySelector('#tx-langs'));" in html, True)
    check("«Обложка»", "langPicker(card.querySelector('#cv-langs'));" in html, True)
    check("пакетные карточки текста и обложки",
          "      cb.onchange = () => l.classList.toggle('on', cb.checked);\n    });\n"
          "    langPicker(box);" in html, True)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
