#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""429 «слишком часто» и 429 «деньги кончились» — разные вещи, и ждать надо только первую.

20.09.2026, ночь перед показом. В логе заказа текста шло:

    ⏳ Rate limit [text_analysis], жду 7с… 8с… 18с… 35с… 66с…

Рерайт считал лимитом ЛЮБОЙ 429 и уходил в откат с растущей паузой: десять повторов,
полчаса ожидания и отказ. Между тем OpenAI отвечает 429 на две противоположные вещи:
«слишком часто» (пройдёт само через секунды) и «кончилась квота или деньги» (не пройдёт
НИКОГДА). Настоящий текст ответа при этом уходил в debug_log, который по умолчанию выключен,
и человек полчаса смотрел на слово «Rate limit», не зная, чего он ждёт.

Тот же класс поломки, что и утром с сертификатом: код знает причину и молчит о ней.

    .venv/bin/python tests/test_rewrite_quota_vs_rate.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
# rewrite_core тянет общий счётчик ключа: без каталога он не импортируется вовсе.
os.environ.setdefault("VIDEO_STUDIO_SHARED_DIR", os.path.join(BASE, "shared"))
sys.path.insert(0, os.path.join(BASE, "rewrite"))
sys.path.insert(0, os.path.join(BASE, "shared"))

import rewrite_core as R                                          # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# Тела взяты с настоящих ответов OpenAI: машинные признаки (type/code) провайдер держит
# стабильными, а тексты меняет — поэтому распознаём по ним, а не по вольной фразе.
QUOTA = ('{"error":{"message":"You exceeded your current quota, please check your plan '
         'and billing details.","type":"insufficient_quota","code":"insufficient_quota"}}')
RATE = ('{"error":{"message":"Rate limit reached for gpt-4.1-mini in organization org-x '
        'on requests per min (RPM): Limit 3, Used 3.","type":"requests",'
        '"code":"rate_limit_exceeded"}}')
BILLING = '{"error":{"message":"Billing hard limit has been reached","code":"billing_hard_limit_reached"}}'


def main() -> int:
    print("Что считается исчерпанной квотой")
    check("кончились деньги — да", R._quota_exhausted(QUOTA, ""), True)
    check("упёрлись в потолок счёта — да", R._quota_exhausted(BILLING, ""), True)
    check("слишком часто — НЕТ, это ожидание", R._quota_exhausted(RATE, ""), False)
    check("пустой ответ — не выдумываем", R._quota_exhausted("", ""), False)
    check("чужие слова про лимит не обманывают",
          R._quota_exhausted('{"error":{"message":"limit of characters in prompt"}}', ""), False)

    print("\nПризнак ищется и в теле, и в разобранном сообщении")
    check("только сообщение, без тела",
          R._quota_exhausted("", "You exceeded your current quota"), True)
    check("регистр не важен", R._quota_exhausted("INSUFFICIENT_QUOTA", ""), True)

    print("\nОбе ветки разбора 429 знают про это")
    src = open(os.path.join(BASE, "rewrite", "rewrite_core.py"), encoding="utf-8").read()
    check("ветка прямого HTTPError проверяет квоту",
          src.count("_quota_exhausted(") >= 3, True)   # определение + два места разбора
    # Ради этого всё и делалось: в видимой строке ожидания должны стоять слова провайдера,
    # а не одно слово «Rate limit». Иначе человек снова не узнает, чего он ждёт.
    check("в строку ожидания попадают слова провайдера",
          "жду {wait_time:.0f}с — {(error_msg" in src, True)
    check("отказ по лимиту тоже несёт причину",
          'f"Rate limit after {max_rate_limit_retries} retries: "' in src, True)

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
