#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пустой ответ на последней попытке не выбрасывает уже оплаченную обложку (04.10.2026).

Три выпуска стояли «обложка не нарисовалась дважды: 0/1 обложек». В журнале модуля:

    ✅ Проверка текста (попытка 1/2)... ❌ Проблема: EKSKLUZYWNY -> EKSKLUZYWNE; …
    ⚠ Попытка 2/2: Fast Gen не отдал картинку — Generation failed, please try again later
    ❌ [Польский]: Ошибка генерации изображения (все 2 попыток вернули пустой результат)

Первая картинка была нарисована и оплачена, сверку не прошла; вторая попытка вернула
пусто — и переменная с картинкой затёрлась. Итог «все попытки пустые» был неправдой, а
модуль сам велит «текст неидеален, но картинка есть — сохраняем» (анти cost-drain).

    .venv/bin/python tests/test_cover_keeps_paid_image.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BASE, "cover"))

import cover_core as cc                                            # noqa: E402
from PIL import Image                                              # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def img(color):
    return Image.new("RGB", (64, 36), color)


def seq_gen(items):
    items = list(items)
    return lambda *a, **k: items.pop(0) if items else None


def main() -> int:
    saved = (cc._journaled_generate, cc._call_vision, cc.check_result)
    try:
        print("Простая генерация: 1-я картинка не прошла сверку, 2-я попытка пустая")
        paid = img("green")
        cc._journaled_generate = seq_gen([paid, None])
        cc._call_vision = lambda *a, **k: '{"overall_pass": false, "issues": "EKSKLUZYWNY -> EKSKLUZYWNE"}'
        preset = cc.Preset(name="проба", language="Польский", simple_mode=True,
                           max_generation_retries=2)
        lines = []
        r = cc._generate_simple_edit(img("red"), preset, "fg", "", log_fn=lines.append)
        check("обложка есть", r.success, True)
        check("это оплаченная первая картинка", r.image is paid, True)
        check("помечена как неидеальная (fallback)", r.used_fallback, True)
        check("и не как «не сверено»", r.unchecked, False)
        check("в журнале нет неправды «все попытки пустые»",
              any("вернули пустой результат" in x for x in lines), False)

        print("\n  все попытки и правда пустые — честная ошибка, как раньше")
        cc._journaled_generate = seq_gen([None, None])
        r = cc._generate_simple_edit(img("red"), preset, "fg", "", log_fn=lambda m: None)
        check("ошибки не скрываем", (r.success, "пустой результат" in r.error), (False, True))

        print("\nПолная генерация: то же правило")
        cc._journaled_generate = seq_gen([paid, None])          # чистый вариант тоже пуст
        cc.check_result = lambda *a, **k: (False, {"issues": "текст кривой"})
        full = cc.Preset(name="проба", language="Польский", max_generation_retries=2)
        r = cc._generate_for_language(
            None, {"text": "ЗАГОЛОВОК", "video_title": "t"}, None, full, "", "", "fg",
            log_fn=lambda m: None, mode="title", skip_adapt=True)
        check("обложка есть", r.success, True)
        check("это оплаченная картинка с текстом", r.image is paid, True)
        check("помечена как fallback", r.used_fallback, True)
    finally:
        cc._journaled_generate, cc._call_vision, cc.check_result = saved
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
