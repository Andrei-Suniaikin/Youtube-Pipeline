#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Числа в заголовке главы, которые в тексте сказаны словами (11.10.2026).

Что случилось. Текст для озвучки пишет числа словами («тринадцять кулеметів»), и
расшифровка повторяет их словами же, а модель в заголовке ставит цифры («13 кулеметів
B-17»). Проверка от выдуманных дат искала в главе только цифры — и меняла верный
заголовок на обрывок фразы. На Сталевой Грозе (UA) так ушли в запасные 4 из 12
заголовков, задача упала с текстом «канал промптов почти не отвечал», хотя модель
ответила на все окна. Всего по бою — 480 таких отказов.

Теперь число, сказанное словами в любом падеже, засчитывается; выдуманное — нет; а
текст провала называет настоящую причину.

    .venv/bin/python tests/test_chapters_numbers_in_words.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "workers"))

import chapters as C                                               # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def main() -> int:
    print("Число словами в тексте главы — заголовок принят")
    check("uk: 13 ↔ «тринадцять»",
          C._numbers_ok("13 кулеметів B-17 проти атак",
                        "у B-17 було тринадцять кулеметів", "uk"), True)
    check("uk: 60 ↔ «шістдесяти» (падеж)",
          C._numbers_ok("Чорний четвер: 60 B-17 збили",
                        "з шістдесяти B-17 не повернувся жоден", "uk"), True)
    check("pl: 13 ↔ «trzynastu»",
          C._numbers_ok("13 karabinów na pokładzie",
                        "miał aż trzynastu strzelców i karabiny", "pl"), True)
    check("cs: 1943 ↔ «tisíc devět set čtyřicet tři» раздельно",
          C._numbers_ok("Nálet roku 1943",
                        "v roce tisíc devět set čtyřicet tři přišel nálet", "cs"), True)
    check("hr: 13 ↔ «trinaest» (через сербский)",
          C._numbers_ok("13 strojnica", "imao je trinaest strojnica", "hr"), True)
    check("цифрами, как раньше", C._numbers_ok("60 B-17", "збили 60 літаків B-17", "uk"), True)

    print("\nЧисла в главе нет ни так, ни так — отказ, как раньше")
    check("uk: 13, а в тексте «шістдесят»",
          C._numbers_ok("13 кулеметів", "шістдесят літаків", "uk"), False)
    check("pl: выдуманный год",
          C._numbers_ok("Bitwa z 1944", "w roku tysiąc dziewięćset czterdzieści trzy", "pl"),
          False)
    check("«три» не засчитывается по «тридцять»",
          C._numbers_ok("3 літаки", "тридцять літаків", "uk"), False)
    check("cs: «tři» не засчитывается по «třicet»",
          C._numbers_ok("3 letadla", "třicet letadel", "cs"), False)
    check("pl: 13 не засчитывается по «trzydziestu» (30)",
          C._numbers_ok("13 karabinów", "trzydziestu strzelców", "pl"), False)
    check("uk: 12 не засчитывается по «двадцять»",
          C._numbers_ok("12 літаків", "двадцять літаків", "uk"), False)
    check("греческий — только цифрами (библиотека не знает языка)",
          C._numbers_ok("13 πολυβόλα", "δεκατρία πολυβόλα", "el"), False)
    check("язык неизвестен — только цифрами",
          C._numbers_ok("13 кулеметів", "тринадцять кулеметів", ""), False)

    print("\nТекст провала называет настоящую причину")
    segs = [{"start": i * 10.0, "end": i * 10.0 + 9.5,
             "text": f"Речення номер {i}. Літаки летіли далі."} for i in range(220)]
    real_ask = C._ask_llm

    def answer_all_with_fake_years(prompt, model):
        import re
        wins = [int(w) for w in re.findall(r"ОКНО (\d+)", prompt)]
        return {"picks": [{"window": w, "cut": "", "title": f"Наліт {1900 + w} року над містом"}
                          for w in wins] + [{"window": 0, "cut": "",
                                             "title": f"Наліт 1899 року над містом"}]}
    C._ask_llm = answer_all_with_fake_years
    try:
        res = C.build_chapters(segs, language="uk", model="x", log=lambda *_: None)
    finally:
        C._ask_llm = real_ask
    joined = " | ".join(res.get("warnings") or [])
    check("провал", res.get("status"), "failed")
    check("не «канал почти не отвечал»", "почти не отвечал" in joined, False)
    check("а «не прошли проверку»", "не прошли проверку" in joined, True)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
