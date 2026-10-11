#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Текст обложки подгоняется под язык той же подпиской, что разбирает превью (11.10.2026).

Что случилось. 53 пресета ВВ2 разбирают превью ключом OpenAI, а у ключа кончился лимит
(402 «token allowance exhausted»): 107 обложек за три дня упали. Пресеты с разбором
подпиской Codex за то же время сделали 70 из 70. Но при переводе пресета на Codex
подгонка текста на язык канала всё равно шла в OpenAI: без ключа текст молча оставался
на языке донора, с пустым ключом — снова 402. Теперь подгонка идёт той же подпиской.

    .venv/bin/python tests/test_cover_adapt_by_subscription.py
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


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def preset(analysis_provider):
    return types.SimpleNamespace(
        language="Чешский", generation_provider="grok_api", generation_model="",
        reference_image="", simple_mode=False, analysis_provider=analysis_provider,
        analysis_model="", name="ВВ2", aspect_ratio="16:9", fast_gen_base_url="",
        max_generation_retries=1, debug_mode=False)


def run(analysis_provider, openai_key):
    sub_calls, openai_calls = [], []
    saved = (cc._subscription_call, cc._chat_route, cc.build_generation_prompt)
    cc._subscription_call = lambda prov, msgs, *a, **k: (sub_calls.append(prov), "PŘELOŽENO")[1]

    def no_openai(*a, **k):
        openai_calls.append(1)
        raise RuntimeError("Error code: 402 - token allowance exhausted")
    cc._chat_route = no_openai
    cc.build_generation_prompt = lambda *a, **k: "prompt"
    try:
        r = cc._generate_for_language(Image.new("RGB", (64, 36)), {"text": "LAST LETTER"}, None,
                                      preset(analysis_provider), "", openai_key, "")
    finally:
        cc._subscription_call, cc._chat_route, cc.build_generation_prompt = saved
    return r, sub_calls, openai_calls


def main() -> int:
    print("Разбор подпиской Codex, ключа OpenAI нет — текст всё равно переведён")
    r, sub, oa = run("codex", "")
    check("подгонка — подпиской codex", sub, ["codex"])
    check("текст переведён", r.adapted_text, "PŘELOŽENO")
    check("в OpenAI не ходили", oa, [])

    print("\nРазбор подпиской, ключ OpenAI есть, но пуст — в OpenAI всё равно не ходим")
    r, sub, oa = run("codex", "sk-пустой")
    check("подгонка — подпиской", (sub, oa), (["codex"], []))

    print("\nРазбор ключом OpenAI — как раньше, через OpenAI")
    r, sub, oa = run("openai", "sk-ключ")
    check("подписку не звали, OpenAI звали", (sub, len(oa)), ([], 1))

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
