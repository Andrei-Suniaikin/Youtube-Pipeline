#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Длинный донор на экране — жёлтым, не красным (пункт 3, 22.09.2026, ревью-итерация 2).

ВАЖНО с 30.09.2026: потолка длины по умолчанию БОЛЬШЕ НЕТ — владелец его убрал
(«убрать потолок совсем»), и на умолчаниях длинный донор не даёт ни отказа, ни
оговорки. Но механизм оговорки остался и включается числом в настройке, поэтому тест
задаёт потолок явно: он проверяет ПОКАЗ оговорки, а не то, при какой длине она
появляется. Умолчание проверяет `test_donor_precheck.py`.

FR-4: измеренный (по субтитрам) донор длиннее потолка — это `warn`, не `reject`, и
`donorsHtml` обязан нарисовать такую строку пилюлей «с оговоркой», а не «не годится»,
и не добавить её ни в счётчик отказов в шапке, ни в блок «Проверка советует, а не
запрещает» (этот блок — для НАСТОЯЩИХ отказов, к длинному донору отношения не имеет).

Серверный тест (`test_donor_precheck.py`) уже проверяет `donor_verdict()` саму по себе.
Здесь — другое звено: то, что настоящий вывод `donor_verdict()` для 120-минутного
измеренного донора, пройдя всю сборку строки так же, как это делает `/ui/links/check`
(`web/app.py`: `donor_facts_blank` → факты → `donor_verdict` → сборка `donor_summary`),
доезжает до `donorsHtml` и рисуется правильно. Фикстура строится из НАСТОЯЩЕГО вызова
`PT.donor_verdict()`, а не набирается вручную — иначе тест ловил бы только то, что сам
и придумал.

    .venv/bin/python tests/test_donor_length_warn_browser.py
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(HERE, "helpers"))

import browser_harness                                            # noqa: E402
import pub_tags as PT                                              # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# ───────── строим строку ТАК ЖЕ, как это делает /ui/links/check (web/app.py) ─────────
# `donor_facts_blank` → факты о ролике → `donor_verdict` дописывает вердикт поверх тех
# же фактов (`facts.update(donor_verdict(facts, limits))`, см. `web/app.py:2857`).
# Здесь ровно та же сборка, а не вручную набранный JSON: если вывод `donor_verdict`
# для измеренной длины когда-нибудь снова станет `reject`, эта строка сама принесёт
# отказ на экран, и тест это увидит без переписывания.
# Потолок задаём явно: по умолчанию его нет, и без этой строки фикстура собиралась бы
# из вердикта «годится», то есть тест проверял бы показ того, чего не показывают.
LIM = dict(PT.donor_limits(), max_minutes=90.0)
URL = "https://youtu.be/LONG120min"
_facts = PT.donor_facts_blank(URL, time.time())
_facts.update({
    "video_id": "LONG120min",
    "title": "Длинный донор",
    "title_from": "сеть",
    "has_subs": True,
    "chars": 90000,             # заведомо больше порога «дорожка неполная» на 120 мин
    "lang": "ru",
    "lang_ru": "Русский",
    "seconds": 120 * 60,
    "minutes": 120.0,
    "length_from": "субтитры",  # ИЗМЕРЕННАЯ длина — не оценка
    "cached": False,
})
_facts.update(PT.donor_verdict(_facts, LIM))

# Не `assert`, намеренно: если `donor_verdict()` для измеренных 120 минут снова начнёт
# отдавать `reject`, тест обязан не остановиться здесь молча, а довести настоящий
# (уже сломанный) факт до `donorsHtml` в браузере и упасть НА РЕНДЕРЕ — ровно там, где
# его увидел бы владелец. Так проверяется вся цепочка «сервер → экран», а не только
# то, что сборка фикстуры в этом файле не разъехалась с сервером.
if _facts["verdict"] != "warn":
    print(f"  FAIL сервер вернул verdict={_facts['verdict']!r} для измеренных 120 минут "
          "(ожидался 'warn') — фикстура берётся как есть, дальше падает рендер")
    FAILS.append("donor_verdict() для измеренных 120 минут вернул не 'warn'")

_summary = {"ok": 0, "warn": 0, "reject": 0, "unknown": 0}
_summary[_facts["verdict"]] += 1

CHECK = {
    "donors": [_facts],
    "donor_summary": _summary,
    "timed_out": False,
    "wall_sec": 30,
    "duplicates": 0,
    "donor_note": "Проверка бесплатна: квоту YouTube Data API не тратит.",
}


SCENARIO = r"""
(async () => {
  const out = {};
  try {
    const CHECK = __CHECK_JSON__;
    const box = document.createElement('div');
    document.body.append(box);
    box.innerHTML = donorsHtml(CHECK);
    const rows = [...box.querySelectorAll('.stage')];
    out.rows = rows.length;
    const pill = rows[0] ? rows[0].querySelector('.pill') : null;
    out.pillClass = pill ? pill.className.replace('pill', '').trim() : '';
    out.pillLabel = pill ? pill.textContent.trim() : '';
    const notes = rows[0] ? [...rows[0].querySelectorAll('.note')] : [];
    const whyNote = notes.find(n => n.textContent.includes('1,3'));
    out.whyNoteFound = !!whyNote;
    out.whyNoteBad = whyNote ? whyNote.classList.contains('bad') : null;
    out.whyText = whyNote ? whyNote.textContent.trim() : '';
    const secEl = box.querySelector('.sec');
    out.head = secEl ? secEl.textContent.trim() : '';
    out.hasRejectBlock = box.textContent.includes('Проверка советует, а не запрещает');
    out.hasNeGodyatsya = box.textContent.includes('не годятся');
  } catch (e) { out.error = String(e && e.stack || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""
SCENARIO = SCENARIO.replace("__CHECK_JSON__", json.dumps(CHECK, ensure_ascii=False))


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=10000)
    if res.get("error") and "Chrome не найден" in str(res.get("error")):
        print("  ПРОПУЩЕНО: " + res["error"])
        FAILS.append("браузерная часть не выполнена")
        print()
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1

    print("Длинный (120 мин, измерено) донор на экране предпросмотра")
    check("сценарий выполнен без ошибок", res.get("error"), None)
    check("ровно одна строка донора", res.get("rows"), 1)
    # Мутация-проверка: верни в `donor_verdict()` (server/pub_tags.py) старое
    # `return out("reject", "too_long", why)` для измеренной длины — пилюля станет
    # `s-error`, подпись «не годится», в шапке появится «не годятся: 1», и заодно
    # вылезет блок «Проверка советует, а не запрещает» — все три проверки ниже упадут.
    check("пилюля строки — «с оговоркой», не «отказ»", res.get("pillClass"), "s-queued")
    check("подпись пилюли", res.get("pillLabel"), "с оговоркой")
    check("причина найдена на строке", res.get("whyNoteFound"), True)
    check("причина НЕ покрашена как отказ (класс bad)", res.get("whyNoteBad"), False)
    check("в тексте есть честный множитель цены 1,3×",
          "1,3×" in (res.get("whyText") or ""), True)
    check("слова «потолок» (в любом падеже) в тексте нет",
          "потолк" in (res.get("whyText") or ""), False)
    check("в шапке — «с оговоркой: 1»", "с оговоркой: 1" in (res.get("head") or ""), True)
    check("в шапке НЕТ «не годятся»", res.get("hasNeGodyatsya"), False)
    check("блока «советует, а не запрещает» на экране нет",
          res.get("hasRejectBlock"), False)

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
