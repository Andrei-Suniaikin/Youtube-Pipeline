#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Карта голосов переключает подпись сразу при смене «Движка» (решение владельца 17.09.2026).

Поведенческая проверка в настоящем браузере, а не поиск строк в исходнике: берётся
`web/index.html`, запуск приложения (`render()`) заменяется сценарием, который строит
форму `fieldForm()` из описания группы «Озвучка», меняет движок и читает DOM. Chrome
запускается headless без сети и без стенда.

    .venv/bin/python tests/test_langmap_engine_switch.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import preset_fields as PF                                         # noqa: E402
sys.path.insert(0, os.path.join(HERE, "helpers"))
import browser_harness                                            # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Cabinet:
    """Минимальный кабинет: движки и языки — ровно то, что нужно группе «Озвучка»."""
    _LANG_NAMES = {"ru": "Русский", "pl": "Polski"}
    TTS_ENGINES = {"edge": ("Edge", ""), "lumean": ("Lumean", "ключ")}


SCENARIO = r"""
(async () => {
  const out = {};
  try {
    const groups = GROUPS_JSON;
    const form = fieldForm(groups);
    document.body.innerHTML = '';
    document.body.append(form);
    const fld = form.querySelector('#f-voice_by_lang-box').closest('.fld');
    const read = () => ({
      label: fld.querySelector('label').textContent,
      hint: (fld.querySelector('.note.muted') || {}).textContent || '',
      placeholder: form.querySelector('.lm-voice') ? form.querySelector('.lm-voice').placeholder : '',
      value: form.read().voice_by_lang,
    });
    out.before = read();
    const eng = form.querySelector('#f-engine');
    eng.value = 'lumean';
    eng.dispatchEvent(new Event('change'));
    out.lumean = read();
    form.querySelector('.langmap-add').click();
    out.newRowPlaceholder = [...form.querySelectorAll('.lm-voice')].pop().placeholder;
    eng.value = 'edge';
    eng.dispatchEvent(new Event('change'));
    out.back = read();
  } catch (e) { out.error = String(e && e.stack || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    groups = [g for g in PF.describe(Cabinet, {"engine": "edge", "voice_by_lang": "ru: v-1"})
              if any(f["key"] == "voice_by_lang" for f in g["fields"])]
    check("группа с картой голосов описана", len(groups), 1)
    if not groups:
        return 1
    res = browser_harness.run_scenario(
        SCENARIO.replace("GROUPS_JSON", json.dumps(groups, ensure_ascii=False)))
    if "before" not in res:
        check("браузер выполнил сценарий", res.get("error"), None)
    else:
        check("сценарий без ошибок", res.get("error"), None)
        b, l, back = res.get("before", {}), res.get("lumean", {}), res.get("back", {})
        check("исходно — «Голоса по языкам»", b.get("label"), "Голоса по языкам")
        check("исходная подсказка строки — голос", b.get("placeholder"), "id голоса")
        check("после смены на Lumean подпись сразу другая", l.get("label"),
              "Пресеты Lumean по языкам")
        check("подсказка под полем — про template_id", "template_id" in l.get("hint", ""), True)
        check("подсказка в уже нарисованной строке сменилась", l.get("placeholder"),
              "id пресета Lumean (template_id)")
        check("новая строка после смены — тоже про Lumean", res.get("newRowPlaceholder"),
              "id пресета Lumean (template_id)")
        check("значение карты при переключении не потерялось", l.get("value"), "ru: v-1")
        check("обратно на Edge — подпись вернулась", back.get("label"), "Голоса по языкам")
        check("обратно на Edge — подсказка строки вернулась", back.get("placeholder"), "id голоса")

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
