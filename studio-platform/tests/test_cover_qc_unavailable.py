#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Неработающий контролёр не уносит уже оплаченную обложку.

21.09.2026, при попытке сделать обложку через FastGen на пустом счету OpenAI:

    openai.RateLimitError: 429 insufficient_quota · credit_balance_exhausted

Падало НЕ на генерации. Режим «дорисовать превью донора» (`simple_mode`) vision-анализ
пропускает и рисует целиком в FastGen — там всё прошло и деньги списались. А следом
`check_simple_result` сверяет оригинал с результатом двумя картинками через OpenAI, и
это исключение улетало наверх, унося с собой оплаченную картинку.

Ровно против этого ниже в `_generate_simple_edit` стоит подпись «анти cost-drain»:
если проверка НЕ ПРОШЛА, картинку всё равно сохраняют — повтор дороже одного неидеального
текста. И та же функция уже умеет «QC не распарсился → не блокируем сохранение». То есть
правило было, но исключение его обходило: «проверка не прошла» и «проверку не смогли
выполнить» обрабатывались по-разному, хотя деньги в обоих случаях уже потрачены.

Повтор тут бессмыслен вдвойне: от второй попытки контролёр не починится, а каждая
попытка — новая оплаченная генерация.

Отдельно проверяется, что правда не подменяется зелёной галочкой: `checked=False`
отличает «проверили и всё хорошо» от «проверить не смогли», и человек это видит.

    .venv/bin/python tests/test_cover_qc_unavailable.py
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


def _img(color="red"):
    return Image.new("RGB", (64, 36), color)


def test_qc_cannot_run() -> None:
    print("\nКонтролёр недоступен — вердикт не выдумывается, сохранение не блокируется")
    was = cc._call_vision

    def boom(*a, **kw):
        raise RuntimeError("429 insufficient_quota")
    try:
        cc._call_vision = boom
        passed, details = check_no_raise()
    finally:
        cc._call_vision = was

    check("исключение наружу не выпускается", passed is not None, True)
    check("сохранению не мешаем", passed, True)
    check("но честно помечено, что не проверяли", details.get("checked"), False)
    check("и сказано почему",
          "проверка текста не выполнена" in (details.get("issues") or ""), True)


def check_no_raise():
    try:
        return cc.check_simple_result(_img("red"), _img("blue"), "Русский",
                                      "openai", "gpt-4.1-mini", "")
    except Exception as exc:                                       # noqa: BLE001
        FAILS.append("check_simple_result бросил исключение")
        print(f"  FAIL исключение вылетело наружу: {type(exc).__name__}: {exc}")
        return None, {}


def test_verdict_still_honest() -> None:
    print("\nРаботающий контролёр по-прежнему судит сам")
    was = cc._call_vision
    try:
        cc._call_vision = lambda *a, **kw: '{"overall_pass": false, "issues": "текст кривой"}'
        passed, details = cc.check_simple_result(_img(), _img("blue"), "Русский",
                                                 "openai", "gpt-4.1-mini", "ключ")
        check("отказ контролёра остаётся отказом", passed, False)
        check("...и не выдаёт себя за «не смогли проверить»",
              details.get("checked"), None)

        cc._call_vision = lambda *a, **kw: '{"overall_pass": true, "issues": ""}'
        passed, details = cc.check_simple_result(_img(), _img("blue"), "Русский",
                                                 "openai", "gpt-4.1-mini", "ключ")
        check("успех остаётся успехом", passed, True)
        check("...и помечен как настоящая проверка", details.get("checked"), None)
    finally:
        cc._call_vision = was


def test_paid_image_survives() -> None:
    print("\nОплаченная картинка доходит до человека, а не теряется")
    was_vision, was_gen = cc._call_vision, cc._journaled_generate
    made = _img("green")
    try:
        cc._journaled_generate = lambda *a, **kw: made

        def boom(*a, **kw):
            raise RuntimeError("429 insufficient_quota")
        cc._call_vision = boom

        preset = cc.Preset(name="проба", language="Русский", simple_mode=True)
        lines = []
        r = cc._generate_simple_edit(_img("red"), preset, "fastgen-ключ", "",
                                     log_fn=lines.append)
        check("задача считается выполненной", r.success, True)
        check("картинка отдана, а не выброшена", r.image is made, True)
        check("помечена как несверенная", r.used_fallback, True)
        joined = " ".join(lines)
        check("в логе нет зелёной галочки проверки", "Проверка пройдена" in joined, False)
        check("...а сказано, что проверить не смогли",
              "проверка текста не выполнена" in joined, True)
        check("...и объяснено, почему картинку всё равно оставили",
              "уже оплачена" in joined, True)
    finally:
        cc._call_vision, cc._journaled_generate = was_vision, was_gen


def main() -> int:
    print("Проверка: сломанный контролёр не уничтожает оплаченный результат")
    test_qc_cannot_run()
    test_verdict_still_honest()
    test_paid_image_survives()
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
