#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Отказ лицензии на видео объясняется человеку, а не выдаётся сырым JSON.

21.09.2026 владелец выбрал метод клипов «Flow» и получил в карточке задачи:

    HTTP 403: {"error": "Video generation not allowed for this license",
               "code": "auth.forbidden", ...}

Отказ честный: у лицензии FastGen `video.generate = false`, а квоты `video.*` нулевые —
это видно бесплатно через /api/v6/usage. Но по такому сообщению нельзя догадаться до
главного: рядом есть рабочий метод «Veo 3.1 — по умолчанию», который идёт через ДРУГОГО
провайдера (VeoNonStop) и ДРУГОЙ ключ, уже лежащий в файле. Владелец вместо этого
спросил, не нужно ли доставать новый ключ.

Правило проекта: сообщение об ошибке отвечает на три вопроса — что упало, потрачены ли
деньги, какую кнопку безопасно нажать. Сырой JSON провайдера не отвечает ни на один.

Тест сторожит и обратное: незнакомый отказ НЕ придумывается. Перевод только там, где
причина опознана однозначно, иначе мы бы уверенно советовали лечение не от той болезни.

    .venv/bin/python tests/test_clip_license_refusal.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BASE, "workers"))
sys.path.insert(0, os.path.join(BASE, "shared"))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


import googler_worker as gw                                        # noqa: E402

REAL = ('HTTP 403: {"error": "Video generation not allowed for this license", '
        '"code": "auth.forbidden", "translations": {"en": "Video generation not allowed '
        'for this license", "ru": "Генерация видео недоступна для данной лицензии"}}')


def main() -> int:
    print("Проверка: отказ лицензии на видео")

    print("\nЗнакомый отказ переводится и называет лекарство")
    out = gw._humanize_refusal(REAL)
    check("сказано, что дело в лицензии",
          "лицензи" in out and "FastGen" in out, True)
    check("назван рабочий метод", "Veo 3.1" in out, True)
    check("назван другой провайдер", "VeoNonStop" in out, True)
    check("сказано про деньги", "Денег это не стоило" in out, True)
    check("сказано, что повтор не поможет", "повтор не поможет" in out, True)
    check("сырого JSON в сообщении нет", "auth.forbidden" in out, False)

    print("\nСообщение доезжает до карточки задачи через общий канал отказов")
    gw._set_provider_refusal(REAL)
    check("в отказе провайдера тот же человеческий текст",
          "Veo 3.1" in gw._provider_refusal(), True)
    check("и он обрезан до разумной длины", len(gw._provider_refusal()) <= 300, True)

    print("\nНезнакомый отказ не выдумывается")
    other = 'HTTP 500: {"error": "internal", "code": "server.boom"}'
    check("остаётся как есть", gw._humanize_refusal(other), other)
    # Половины признака мало: «auth.forbidden» бывает и не про видео, и советовать
    # там смену метода клипов значило бы лечить не ту болезнь.
    half = 'HTTP 403: {"error": "Token expired", "code": "auth.forbidden"}'
    check("одного совпадения из двух недостаточно", gw._humanize_refusal(half), half)

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
