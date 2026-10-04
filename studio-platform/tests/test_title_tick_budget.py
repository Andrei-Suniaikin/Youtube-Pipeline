#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Названия не растягивают круг движка: на них отведён бюджет времени (04.10.2026).

После перевода названий на подписку Codex ответ шёл 1–3 минуты, а движок проходит выпуски
по одному. 28 выпусков ждали название — круг тянулся бы час. Четыре выпуска на фазе
«проект готов» (срок 15 минут) в 10:00 и 10:16 получили «шаг не двигался дольше срока —
снимаю и считаю попытку», так и не дойдя до своего обработчика; третья попытка — разбор.

Что проверяется:
  • пока бюджет круга не исчерпан, названия пишутся;
  • исчерпан — выпуск ждёт («названия делаются по очереди»), подписку не дёргаем;
  • новый круг движка бюджет обнуляет — движок сам вызывает begin_tick;
  • упавший вызов тоже тратит бюджет (иначе медленные отказы опять растянут круг);
  • путь через OpenAI (VS_TITLE_PROVIDER=openai) бюджетом не ограничен — он быстрый.

    .venv/bin/python tests/test_title_tick_budget.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_engine as E                                            # noqa: E402
import pub_schema                                                 # noqa: E402
import pub_services as S                                          # noqa: E402
from pub_publish import TitleUnavailable                          # noqa: E402

FAILS = []
REL = {"release_id": "rel-1", "donor_title": "Donor", "lang": "pl"}


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Clock:
    t = 1000.0


def svc():
    s = S.PubServices({"create_project": lambda *a, **k: "", "enqueue": lambda *a, **k: None,
                       "project_dir": lambda *a, **k: "/нет"})
    s.TITLE_TICK_BUDGET_SEC = 120
    return s


def main() -> int:
    calls = []
    saved = (S.make_title_codex, S.make_title, S.get_key, time.monotonic)
    durations = []

    def codex(*a, **k):
        calls.append("codex")
        Clock.t += durations.pop(0) if durations else 60
        if k.get("fail") or (durations and durations[0] == -1):
            raise TitleUnavailable("502")
        return "Название"

    S.make_title_codex = codex
    S.make_title = lambda *a, **k: calls.append("openai") or "Название OpenAI"
    S.get_key = lambda c, name: "k"
    time.monotonic = lambda: Clock.t
    os.environ.pop("VS_TITLE_PROVIDER", None)
    try:
        print("Бюджет названий на круг движка")
        s = svc()
        durations[:] = [100, 50]
        check("первое название пишется", s._make_title(None, REL), "Название")
        check("второе тоже — потрачено 100 с из 120", s._make_title(None, REL), "Название")
        try:
            s._make_title(None, REL)
            got = "написано"
        except TitleUnavailable as exc:
            got = str(exc)
        check("третье ждёт следующего круга", "по очереди" in got and "следующих кругов" in got, True)
        check("подписку третий раз не дёргали", calls, ["codex", "codex"])

        print("\n  новый круг — бюджет заново")
        s.begin_tick(0)
        durations[:] = [30]
        check("снова пишется", s._make_title(None, REL), "Название")

        print("\n  медленный отказ тоже тратит бюджет")
        s = svc()
        calls.clear()

        def slow_fail(*a, **k):
            calls.append("codex")
            Clock.t += 130
            raise TitleUnavailable("подписка Codex не дала название (502)")
        S.make_title_codex = slow_fail
        try:
            s._make_title(None, REL)
        except TitleUnavailable:
            pass
        try:
            s._make_title(None, REL)
            got = "дёрнули"
        except TitleUnavailable as exc:
            got = "по очереди" in str(exc)
        check("после отказа в 130 с — ждём круга", got, True)
        check("второй раз не звали", calls, ["codex"])
        S.make_title_codex = codex

        print("\n  путь OpenAI бюджетом не ограничен")
        os.environ["VS_TITLE_PROVIDER"] = "openai"
        s = svc()
        s._title_spent = 999
        s._openai_key = lambda c: "sk"
        check("пишется", s._make_title(None, REL), "Название OpenAI")
        os.environ.pop("VS_TITLE_PROVIDER", None)

        print("\nДвижок сам начинает круг")
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        pub_schema.ensure_pub_schema(c)
        seen = []

        class Stub:
            def begin_tick(self, now):
                seen.append(now)
        E.tick(c, Stub(), now=1_800_000_000.0)
        check("begin_tick вызван с моментом круга", seen, [1_800_000_000.0])
        out = E.tick(c, object(), now=1_800_000_000.0)
        check("без begin_tick движок работает как раньше", isinstance(out, dict), True)

        print("\nПо умолчанию — 120 с")
        check("бюджет", S.PubServices.TITLE_TICK_BUDGET_SEC, 120.0)
    finally:
        S.make_title_codex, S.make_title, S.get_key, time.monotonic = saved
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
