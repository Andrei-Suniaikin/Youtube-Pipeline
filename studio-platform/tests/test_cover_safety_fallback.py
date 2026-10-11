#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обложка: отказ фильтра безопасности — следующие попытки другой моделью (05.10.2026).

Что случилось. Záhady Zvířat, донор «Why Do Sperm Whales TERRIFY Orca?». Пресет канала
делает обложку перерисовкой превью донора (img2img, openai_img). Обе попытки:
«Request blocked by provider safety filters» — фильтр режет само превью (кровь, драка
хищников). Выпуск ушёл в разбор «обложка не нарисовалась дважды». За три дня так
упёрлись 13 обложек из 262.

У моделей одного ключа FastGen фильтры разные. Теперь после первого такого отказа
остальные попытки идут в запасную модель того же ключа (flower; при двух картинках на
входе — flow). Модель берётся умолчанием провайдера. Другие отказы поведение не меняют.

    .venv/bin/python tests/test_cover_safety_fallback.py
"""
from __future__ import annotations

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BASE, "cover"))

import cover_core as cc                                            # noqa: E402
from PIL import Image                                              # noqa: E402

FAILS = []
SAFETY = "Request blocked by provider safety filters"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def preset(provider="openai_img", ref=""):
    return types.SimpleNamespace(
        language="Чешский", generation_provider=provider, reference_image=ref,
        generation_model="gpt-image", max_generation_retries=2, fast_gen_base_url="",
        aspect_ratio="16:9", name="Záhady", analysis_provider="codex", analysis_model="",
        debug_mode=False)


def run(errors, provider="openai_img"):
    """errors: что «провайдер» отвечает на каждую попытку (None — картинка)."""
    calls = []
    answers = list(errors)

    def fake_generate(prompt, key, provider=None, model=None, **kw):
        calls.append((provider, model))
        err = answers.pop(0) if answers else None
        if err:
            cc._last_fastgen_error.value = err
            return None
        return Image.new("RGB", (64, 36), "red")

    saved = (cc._journaled_generate, cc.check_simple_result)
    cc._journaled_generate = fake_generate
    cc.check_simple_result = lambda *a, **k: (True, {"checked": True})
    logs = []
    try:
        r = cc._generate_simple_edit(Image.new("RGB", (64, 36), "blue"), preset(provider),
                                     "fg-key", "", log_fn=logs.append)
    finally:
        cc._journaled_generate, cc.check_simple_result = saved
    return r, calls, logs


def main() -> int:
    print("Фильтр на первой попытке — вторая другой моделью того же ключа")
    r, calls, logs = run([SAFETY, None])
    check("первая — модель пресета, вторая — Nano Banana 2",
          calls, [("openai_img", "gpt-image"), ("flow", "NARWHAL")])
    check("обложка получена", r.image is not None, True)
    check("в журнале сказано, почему сменилась модель", any("🔀" in x for x in logs), True)

    print("\nДругой отказ — модель не меняется")
    r, calls, _ = run(["Generation failed, please try again later", None])
    check("обе попытки — модель пресета", [c[0] for c in calls], ["openai_img", "openai_img"])

    print("\nЗапасная модель")
    check("первая запасная — flow", cc.safety_fallback_provider("openai_img", 1), "flow")
    check("две картинки на входе — flow", cc.safety_fallback_provider("openai_img", 2), "flow")
    check("уже flower — flow", cc.safety_fallback_provider("flower", 1), "flow")

    print("\nЦепочка: Nano Banana 2 → Pro → Flower последним (11.10.2026)")
    ch = cc.safety_fallback_chain
    check("после openai — Nano Banana 2", ch("openai_img", "gpt-image", 1), ("flow", "NARWHAL"))
    check("после Nano Banana 2 — Pro",
          ch("flow", "NARWHAL", 1, [("openai_img", "gpt-image")]), ("flow", "GEM_PIX_2"))
    check("после Pro — Flower",
          ch("flow", "GEM_PIX_2", 1, [("openai_img", ""), ("flow", "NARWHAL")]), ("flower", ""))
    check("пресет flow без модели (= Pro) — сначала Nano Banana 2", ch("flow", "", 1), ("flow", "NARWHAL"))
    check("flow без модели и Nano Banana 2 испробованы — Pro не повторяется, Flower",
          ch("flow", "NARWHAL", 1, [("flow", "")]), ("flower", ""))
    check("две картинки и всё flow испробовано — запасной нет",
          ch("flow", "GEM_PIX_2", 2, [("flow", "NARWHAL")]), ("", ""))
    check("все испробованы — запасной нет",
          ch("flower", "", 1, [("flow", "NARWHAL"), ("flow", "GEM_PIX_2")]), ("", ""))
    check("другая семья ключа (xAI напрямую) — запасной нет", ch("grok_api", "", 1), ("", ""))

    print("\nФильтр дважды — третья попытка уже Pro, оплат не больше попыток")
    r, calls, logs = run([SAFETY, SAFETY, None])
    check("цепочка вызовов", calls,
          [("openai_img", "gpt-image"), ("flow", "NARWHAL"), ("flow", "GEM_PIX_2")])
    check("признак отказа фильтра", (cc._is_safety_block(SAFETY), cc._is_safety_block("rc=1")), (True, False))

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
