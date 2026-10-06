#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Три обложки; не прошла ни одна — ИИ ставит 0–10, берётся лучшая (владелец 06.10.2026).

06.10 в разборе стояли 40 выпусков «обложка деградировала дважды». Модуль делал две
попытки, обе не проходили сверку текста, и сохранялась ПОСЛЕДНЯЯ картинка с пометкой
брака — студия её не брала. Правило владельца: «делаем три обложки, если не проходят
проверку, каждую оценивает ИИ от 0 до 10, выигрывает лучшая или первая с одинаковой
наивысшей оценкой».

Проверяется:
  * попыток три, даже если в пресете две;
  * прошла сверку — берётся сразу, оценок нет;
  * не прошла ни одна — побеждает высшая оценка, при равенстве — первая из лучших;
  * выбранная по оценке обложка не помечается браком (иначе студия опять в разбор);
  * контролёр не дал ни одной оценки — поведение прежнее (последняя, с пометкой);
  * то же в полном режиме (по заголовку).

    .venv/bin/python tests/test_cover_best_of_three.py
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


def seq_gen(items, calls):
    items = list(items)

    def gen(*a, **k):
        calls.append(1)
        return items.pop(0) if items else None
    return gen


def vision(scores):
    """Сверка всегда «не прошла»; оценка — по цвету кандидата (None — молчит)."""
    def call(images, prompt, *a, **k):
        if "Rate" not in prompt:
            return '{"overall_pass": false, "issues": "текст кривой"}'
        cand = images[-1] if isinstance(images, (list, tuple)) else images
        s = scores.get(cand.getpixel((0, 0)))
        return "нет ответа" if s is None else '{"score": %s, "why": "ok"}' % s
    return call


RED, GREEN, BLUE = (255, 0, 0), (0, 128, 0), (0, 0, 255)


def simple(scores, gens=None):
    calls, lines = [], []
    a, b, c = img(RED), img(GREEN), img(BLUE)
    cc._journaled_generate = seq_gen(gens if gens is not None else [a, b, c], calls)
    cc._call_vision = vision(scores)
    preset = cc.Preset(name="проба", language="Чешский", simple_mode=True,
                       max_generation_retries=2)
    r = cc._generate_simple_edit(img("white"), preset, "fg", "", log_fn=lines.append)
    return r, (a, b, c), calls, lines


def main() -> int:
    saved = (cc._journaled_generate, cc._call_vision, cc.check_result, cc.COVER_CANDIDATES)
    cc.COVER_CANDIDATES = 3
    try:
        print("Простой режим: три попытки, ни одна не прошла, лучшая — вторая")
        r, (a, b, c), calls, lines = simple({RED: 4, GREEN: 8, BLUE: 7})
        check("попыток три, хотя в пресете две", len(calls), 3)
        check("взята лучшая (8/10)", r.image is b, True)
        check("обложка есть", r.success, True)
        check("не брак — студия её примет", r.used_fallback, False)
        check("помечена как выбранная по оценке", r.scored, True)
        check("оценки названы в журнале",
              any("Оценки ИИ:" in x and "беру №2" in x for x in lines), True)

        print("\nРавные высшие оценки — первая из них")
        r, (a, b, c), _, _ = simple({RED: 6, GREEN: 9, BLUE: 9})
        check("взята №2, а не №3", r.image is b, True)
        r, (a, b, c), _, _ = simple({RED: 7, GREEN: 7, BLUE: 7})
        check("все равны — взята №1", r.image is a, True)

        print("\nКандидат без оценки не участвует")
        r, (a, b, c), _, _ = simple({RED: None, GREEN: 3, BLUE: None})
        check("взята единственная оценённая", r.image is b, True)

        print("\nКонтролёр не дал ни одной оценки — как раньше")
        r, (a, b, c), _, lines = simple({})
        check("последняя картинка", r.image is c, True)
        check("с пометкой брака", r.used_fallback, True)
        check("и не «выбрана по оценке»", r.scored, False)

        print("\nПустая попытка — не кандидат")
        r, (a, b, c), _, _ = simple({RED: 5, GREEN: 9}, gens=[img(RED), None, img(BLUE)])
        check("из двух нарисованных лучшая по оценке",
              r.image.getpixel((0, 0)), RED)  # у синей оценки нет — остаётся красная 5/10

        print("\nПрошла сверку — берётся сразу, без оценок")
        calls = []
        good = img(GREEN)
        cc._journaled_generate = seq_gen([img(RED), good, img(BLUE)], calls)
        n = {"i": 0}

        def pass_second(images, prompt, *a, **k):
            if "Rate" in prompt:
                FAILS.append("оценка при прошедшей сверке")
                return '{"score": 1}'
            n["i"] += 1
            return '{"overall_pass": %s}' % ("true" if n["i"] == 2 else "false")
        cc._call_vision = pass_second
        preset = cc.Preset(name="проба", language="Чешский", simple_mode=True)
        r = cc._generate_simple_edit(img("white"), preset, "fg", "", log_fn=lambda m: None)
        check("взята прошедшая", r.image is good, True)
        check("третья не рисовалась", len(calls), 2)

        print("\nПолный режим: то же правило вместо картинки без текста")
        calls = []
        a, b, c = img(RED), img(GREEN), img(BLUE)
        cc._journaled_generate = seq_gen([a, b, c, img("black")], calls)
        cc.check_result = lambda *x, **k: (False, {"issues": "текст кривой"})
        cc._call_vision = vision({RED: 2, GREEN: 5, BLUE: 9})
        full = cc.Preset(name="проба", language="Чешский", max_generation_retries=2)
        r = cc._generate_for_language(
            None, {"text": "ЗАГОЛОВОК", "video_title": "t"}, None, full, "", "", "fg",
            log_fn=lambda m: None, mode="title", skip_adapt=True)
        check("взята лучшая (9/10)", r.image is c, True)
        check("вариант без текста не заказывался", len(calls), 3)
        check("не брак", r.used_fallback, False)
    finally:
        (cc._journaled_generate, cc._call_vision, cc.check_result,
         cc.COVER_CANDIDATES) = saved
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
