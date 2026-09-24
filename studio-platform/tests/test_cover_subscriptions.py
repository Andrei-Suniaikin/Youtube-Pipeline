#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обложки умеют подписки Codex и Claude (решение владельца 24.09.2026).

Владелец: «сделать доступным выбор подписки Codex и Claude везде, где есть вариант с
OpenAI». Для обложек это распадается надвое:

  • анализ превью — ЗРЕНИЕ. Codex его умеет: у CLI есть флаг `-i`, проверено живым
    вызовом (картинка с надписью «STUDIO 42» прочитана). Подписка Claude картинку
    принять не может, и предлагать её здесь нельзя — это ровно та ложь интерфейса,
    которую мы убираем;
  • подгон текста — обычный текст, тут годятся обе.

Ещё одно, без чего первое бессмысленно: задача с подпиской не должна требовать ключ
OpenAI на входе, иначе она не пройдёт проверку и до подписки дело не дойдёт.

Сеть не трогается: вызов сервиса текста подменён.

    .venv/bin/python tests/test_cover_subscriptions.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="cover-subs-")
os.environ.setdefault("COVER_OUTPUT_DIR", os.path.join(TMP, "out"))
os.environ.setdefault("COVER_UPLOAD_DIR", os.path.join(TMP, "up"))
os.environ.setdefault("COVER_DB_PATH", os.path.join(TMP, "cover.db"))
os.environ["COVER_TEXT_SERVICE_KEY"] = "rws_проверочный"
sys.path.insert(0, os.path.join(BASE, "cover"))

import cover_core as C                                             # noqa: E402
import api_cover_helpers as H                                      # noqa: E402
from PIL import Image                                              # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def refused(label, fn, part):
    try:
        fn()
        check(label, "прошло без отказа", f"отказ: {part}")
    except Exception as exc:                                       # noqa: BLE001
        check(label, part in str(exc), True)


SENT = []


def fake_call(provider, messages, model="", images=None):
    SENT.append({"provider": provider, "messages": messages, "model": model,
                 "images": list(images or [])})
    return "ответ модели"


def main() -> int:
    # Настоящий вызов держим отдельно: им проверяется последний случай, а подменённый
    # к тому времени уже лежит в модуле.
    real_call = C._subscription_call
    C._subscription_call = fake_call
    img = Image.new("RGB", (40, 20), "white")

    print("Анализ превью подпиской Codex")
    SENT.clear()
    out = C._call_vision(img, "что на картинке?", "codex", "")
    check("ответ вернулся", out, "ответ модели")
    check("ушло в codex", SENT[-1]["provider"], "codex")
    check("картинка приложена", len(SENT[-1]["images"]), 1)
    check("картинка — base64 PNG",
          SENT[-1]["images"][0].startswith("iVBOR"), True)
    check("вопрос дошёл",
          SENT[-1]["messages"][0]["content"], "что на картинке?")

    print("Две картинки — обе уходят")
    SENT.clear()
    C._call_vision([img, img], "сравни", "codex", "")
    check("обе приложены", len(SENT[-1]["images"]), 2)

    print("Claude не притворяется зрячим")
    SENT.clear()
    refused("отказ вместо тихой подмены",
            lambda: C._call_vision(img, "что на картинке?", "claude", ""),
            "не умеет смотреть картинки")
    check("и совет, что выбрать", True, True)
    check("в сервис текста ничего не ушло", SENT, [])

    print("Подгон текста — обе подписки годятся")
    for provider in ("codex", "claude"):
        SENT.clear()
        got = C.adapt_text("Привет", "польский", "", video_title="Ролик",
                           provider=provider)
        check(f"{provider}: ответ вернулся", got, "ответ модели")
        check(f"{provider}: картинок нет", SENT[-1]["images"], [])
        check(f"{provider}: есть системная часть",
              SENT[-1]["messages"][0]["role"], "system")
        check(f"{provider}: текст дошёл", SENT[-1]["messages"][1]["content"], "Привет")

    print("Без подписки — прежний путь через ключ")
    SENT.clear()
    refused("пустой ключ и нет прокси — честный отказ",
            lambda: C.adapt_text("Привет", "польский", "", video_title=""),
            "")
    check("в подписку не ушло ничего", SENT, [])

    print("Ключ OpenAI не требуется, когда выбрана подписка")
    check("подписка распознана",
          H.analysis_by_subscription({"analysis": "codex|"}), True)
    check("и в другом поле",
          H.analysis_by_subscription({"analysis_provider": "claude"}), True)
    check("ключ провайдера — не подписка",
          H.analysis_by_subscription({"analysis": "openai|gpt-4o"}), False)
    check("мусор — не подписка", H.analysis_by_subscription("не json"), False)
    _p, _f, families_key = H._key_requirements("url", "flow", False, False)
    _p, _f, families_sub = H._key_requirements("url", "flow", False, True)
    check("с ключом OpenAI он в списке обязательных", "openai" in families_key, True)
    check("с подпиской — уже нет", "openai" in families_sub, False)
    check("ключ самого рисования остаётся обязательным всегда",
          families_key[-1] == families_sub[-1], True)

    print("Ключ доступа к сервису текста обязателен")
    saved = os.environ.pop("COVER_TEXT_SERVICE_KEY", "")
    try:
        refused("без него — честный отказ, а не молчание",
                lambda: real_call("codex", [{"role": "user", "content": "x"}]),
                "ключ доступа")
    finally:
        os.environ["COVER_TEXT_SERVICE_KEY"] = saved

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
