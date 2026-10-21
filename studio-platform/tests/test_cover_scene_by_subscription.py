#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сцена по названию (title-mode) генерируется той же подпиской, что разбирает превью (21.10.2026).

Что случилось. Все 53 пресета ВВ2 стоят на разборе подпиской Codex (analysis_provider=codex),
подгонка текста идёт ею же (11.10), а сцена по названию по-прежнему уходила в OpenAI/прокси
ключом `openai_api_key`. У прокси кончился лимит (402 «token allowance exhausted») — и 24–29
обложек ВВ2 три дня стояли в «ждём: сервис обложек не сделал сцену», хотя подписка жива.

    .venv/bin/python tests/test_cover_scene_by_subscription.py
"""
from __future__ import annotations

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BASE, "cover"))

import cover_core as cc                                            # noqa: E402

FAILS = []
REPLY = ('{"scene_description": "a bright tank on a sunlit hill", "mood": "bright", '
         '"dominant_colors": ["#ffcc00", "#3366ff", "#ffffff"], "characters": [], '
         '"visual_tropes": "sunlight rays"}')


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def main() -> int:
    saved = (cc._subscription_call, cc._chat_route)
    sub_calls, oa_calls = [], []

    def fake_sub(provider, messages, model="", images=None):
        sub_calls.append((provider, model, len(messages)))
        return REPLY

    class _Msg:
        content = REPLY

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kw):
            oa_calls.append(kw.get("model"))
            return _Resp()

    class _Client:
        chat = types.SimpleNamespace(completions=_Completions())

    cc._subscription_call = fake_sub
    cc._chat_route = lambda key, model: (_Client(), model, key)
    try:
        print("1. Пресет на подписке codex — сцена подпиской, OpenAI не трогаем, ключ не нужен")
        out = cc.generate_scene_from_title("Jak więźniowie brutalnie…", "", "", "gpt-5.6-sol", provider="codex")
        check("подписку звали один раз с моделью пресета", sub_calls, [("codex", "gpt-5.6-sol", 2)])
        check("OpenAI не звали", oa_calls, [])
        check("сцена разобрана", out.get("scene_description"), "a bright tank on a sunlit hill")
        check("композиционные поля подставлены", out.get("subject_position"), "left-third")

        print("\n2. Пресет на ключе OpenAI — как раньше")
        sub_calls.clear()
        out = cc.generate_scene_from_title("Title", "extra", "sk-key", "gpt-4o", provider="openai")
        check("подписку не звали", sub_calls, [])
        check("OpenAI звали с моделью пресета", oa_calls, ["gpt-4o"])
        check("сцена разобрана", out.get("mood"), "bright")

        print("\n3. Ни ключа, ни подписки — честная ошибка, а не 402")
        try:
            cc.generate_scene_from_title("Title", "", "", "gpt-4o", provider="openai")
            check("ValueError", False, True)
        except ValueError as exc:
            check("ValueError", "openai_api_key required" in str(exc), True)
    finally:
        cc._subscription_call, cc._chat_route = saved
    print("\nитог:", "все проверки прошли" if not FAILS else f"ПРОВАЛОВ {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
