#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кавычки в visual_prompt: повтор говорит модели, ЧТО именно убрать (11.10.2026).

Что случилось. Задача 2095 («Wojna, Której Nie Znacie»): часть 156 плана сцен трижды
вернулась с текстом в кавычках внутри visual_prompt, и весь план отклонён — «схема не
исправлена после 3 точечных повторов». Точечный повтор пересказывал модели только
«literal wording запрещён», не называя кавычек, и не объяснял, что запрещены и кавычки
вокруг имён и прозвищ.

Правило приёма не меняется: кавычки в visual_prompt по-прежнему отказ. Меняется текст
отказа (какие кавычки) и подсказка в точечном повторе.

    .venv/bin/python tests/test_scene_literal_wording_retry.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "workers"))
os.environ.setdefault("VS_TEST_NO_NET", "1")

import googler_worker as G                                        # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def scene(prompt, visible=None):
    return {"subject": "bomber", "visual_prompt": prompt,
            "camera_angle": sorted(G._SCENE_CAMERA_ANGLES)[0],
            "subject_type": sorted(G._SCENE_SUBJECT_TYPES)[0],
            "visible_text": visible if visible is not None else []}


def reject(prompt):
    try:
        G._validate_scene_object(scene(prompt), 155, "tekst", require_visible_text=True)
    except G.ScenePlanSchemaError as exc:
        return str(exc)
    return ""


def main() -> int:
    print("Отказ называет кавычки")
    why = reject('A B-17 nicknamed "Memphis Belle" over «Schweinfurt» at dawn')
    check("отказ есть", why.startswith("часть 156: literal wording запрещён"), True)
    check("названы обе пары кавычек",
          ('"Memphis Belle"' in why, "«Schweinfurt»" in why), (True, True))
    check("без кавычек — принято",
          reject("A B-17 bomber with a painted name on its nose over a burning city"), "")
    check("без require_visible_text (старые сцены) — как раньше, принято",
          bool(G._validate_scene_object(scene('a sign "Achtung"'), 0, "t")), True)

    print("\nТочечный повтор объясняет, что делать")
    base = {"model": "x", "messages": [{"role": "user", "content": "plan"}]}
    p = G._scene_corrective_payload(base, "часть 156", why)
    last = p["messages"][-1]["content"]
    check("подсказка про любые кавычки", "NO quotation marks of any kind" in last, True)
    check("подсказка про visible_text", "verbatim into visible_text" in last, True)
    check("сами кавычки в причине", '"Memphis Belle"' in last, True)
    check("исходная переписка не тронута", len(base["messages"]), 1)
    other = G._scene_corrective_payload(base, "часть 1", "часть 1: недопустимый camera_angle")
    check("на других отказах подсказки нет",
          "quotation marks" in other["messages"][-1]["content"], False)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
