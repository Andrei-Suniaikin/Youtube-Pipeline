#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Анализ обложки умеет ходить не только в OpenAI — но ключ едет только с адресом.

21.09.2026: счёт OpenAI пуст, и обложка «новой сценой» стала невозможна В ПРИНЦИПЕ.
В этом форке весь vision-анализ идёт ТОЛЬКО в OpenAI: Gemini-SDK удалён в v1.0.3, а
`provider="google"` молча падает обратно на OpenAI (`_call_vision`). То есть ключ Gemini
не помог бы — его некому отдать.

В бою эта развилка уже решена тремя переменными `COVER_ALT_*`: анализ уходит на сторонний
OpenAI-совместимый прокси, который умеет зрение. Взят тот же путь, а не изобретён свой.

Что закрепляет тест:

* **По умолчанию не меняется ничего** — пустые переменные означают прежний OpenAI,
  прежний ключ и прежнюю модель.
* **Модель берётся чужая.** У стороннего провайдера свой список, и модели OpenAI там
  может не быть вовсе — наша `gpt-4.1-mini` вернула бы 400 на каждом вызове.
* **Адрес без ключа — отказ.** Отправить ключ OpenAI чужому провайдеру значит засветить
  секрет там, где он не предназначен, и никогда об этом не узнать.
* **Клиенты кэшируются по паре ключ+адрес.** Кэш только по ключу вернул бы клиента,
  настроенного на старый адрес, и правка молча не применилась бы.

Сети здесь нет: клиент подменён, проверяется адрес, ключ и модель.

    .venv/bin/python tests/test_cover_alt_vision.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BASE, "cover"))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


import cover_core as cc                                            # noqa: E402

try:
    from PIL import Image                                          # noqa: E402
except ImportError:                                                # pragma: no cover
    print("ПРОПУЩЕНО: нет PIL")
    raise SystemExit(2)

SENT = {}


class _Client:
    def __init__(self, key, base):
        self.chat = self
        self.completions = self
        self._key, self._base = key, base

    def create(self, model=None, messages=None, max_tokens=None):
        SENT.update(key=self._key, base=self._base, model=model)

        class _M:
            content = "ответ"

        class _C:
            message = _M()
        return type("R", (), {"choices": [_C()]})()


def _fake_client(api_key, base_url=""):
    return _Client(api_key, base_url)


def _call():
    SENT.clear()
    return cc._call_openai_vision(Image.new("RGB", (8, 8)), "промпт",
                                  "КЛЮЧ-OPENAI", "gpt-4.1-mini")


def main() -> int:
    print("Проверка: маршрут анализа обложки")
    was_client = cc._openai_client
    cc._openai_client = _fake_client
    for v in ("COVER_ALT_BASE_URL", "COVER_ALT_API_KEY", "COVER_ALT_MODEL"):
        os.environ.pop(v, None)
    try:
        print("\nБез переменных — прежний OpenAI")
        _call()
        check("адрес не подменён", SENT.get("base"), "")
        check("ключ прежний", SENT.get("key"), "КЛЮЧ-OPENAI")
        check("модель прежняя", SENT.get("model"), "gpt-4.1-mini")

        print("\nАдрес без ключа — отказ, в сеть не идём")
        os.environ["COVER_ALT_BASE_URL"] = "https://чужой.example/v1"
        try:
            _call()
            check("отказано", False, True)
        except RuntimeError as exc:
            check("отказано", True, True)
            check("названа причина", "COVER_ALT_API_KEY" in str(exc), True)
            check("сказано, что денег не стоило", "Денег это не стоило" in str(exc), True)
            check("ключ OpenAI в сообщение не попал", "КЛЮЧ-OPENAI" in str(exc), False)
        check("в сеть не ходили", "key" in SENT, False)

        print("\nАдрес с ключом — едут оба, и модель чужая")
        os.environ["COVER_ALT_API_KEY"] = "КЛЮЧ-ПРОВАЙДЕРА"
        os.environ["COVER_ALT_MODEL"] = "модель-провайдера"
        _call()
        check("адрес чужой", SENT.get("base"), "https://чужой.example/v1")
        check("ключ чужой, а не OpenAI", SENT.get("key"), "КЛЮЧ-ПРОВАЙДЕРА")
        check("модель тоже его", SENT.get("model"), "модель-провайдера")

        print("\nБез своей модели — остаётся наша")
        os.environ.pop("COVER_ALT_MODEL")
        _call()
        check("модель прежняя", SENT.get("model"), "gpt-4.1-mini")
        check("адрес всё равно чужой", SENT.get("base"), "https://чужой.example/v1")
    finally:
        cc._openai_client = was_client
        for v in ("COVER_ALT_BASE_URL", "COVER_ALT_API_KEY", "COVER_ALT_MODEL"):
            os.environ.pop(v, None)

    print("\nКлиенты кэшируются по паре ключ+адрес, а не по одному ключу")
    cc._openai_clients.clear()
    was_avail, cc.OPENAI_AVAILABLE = cc.OPENAI_AVAILABLE, True
    import types                                                   # noqa: PLC0415
    was_openai = getattr(cc, "openai", None)
    cc.openai = types.SimpleNamespace(OpenAI=lambda **kw: dict(kw))
    try:
        a = cc._openai_client("К", "")
        b = cc._openai_client("К", "https://другой.example/v1")
        check("разные адреса — разные клиенты", a is b, False)
        check("адрес доехал до клиента", b.get("base_url"), "https://другой.example/v1")
        check("тот же ключ и адрес — тот же клиент",
              cc._openai_client("К", "") is a, True)
    finally:
        cc.OPENAI_AVAILABLE = was_avail
        if was_openai is not None:
            cc.openai = was_openai
        cc._openai_clients.clear()

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
