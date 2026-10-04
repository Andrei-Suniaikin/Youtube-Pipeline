#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Отказ модели не уходит на YouTube названием ролика (04.10.2026).

Что случилось. На канале ВВ2 «Ημερολόγια του Μετώπου» вышел публичный ролик fMYByvfhsQU
с названием «I can't help rewrite titles that use sexual violence or assault as intrigue,
even for». Модель отказалась переписывать название, а единственная проверка ответа —
«не короче 10 знаков» — отказ пропустила. В новой студии было то же самое.

Теперь ответ, который НАЧИНАЕТСЯ как отказ (английский или русский), — это TitleError:
выпуск останавливается до заливки с причиной словами. Именно TitleError, а не
TitleUnavailable: отказ по теме донора повтором не лечится, а «ждать» значило бы вечно
дёргать Codex. Проверяются и обычные названия, похожие на отказ только по началу
(«I Can Hear the Front», «Я не могу забыть её глаза», «Icanthus»), — их пропускаем.

    .venv/bin/python tests/test_title_refusal.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import block_reasons as B                                          # noqa: E402
import pub_publish as P                                            # noqa: E402

FAILS = []

REFUSALS = [
    "I can't help rewrite titles that use sexual violence or assault as intrigue, even for",
    "I’m sorry, but I can’t help with that", "Sorry, I can't do that", "I cannot rewrite this title",
    "I won't write that", "I am unable to help with this request", "Unfortunately, I can't",
    "As an AI, I must decline", "Извините, но я не могу", "К сожалению, я не могу помочь",
    "Я не могу переписать это название", "Не могу помочь с этим",
]
TITLES = [
    "Ce i-a uimit pe soldații germani în Ucraina - Memorii de război", "Sorryn Tarina: Kadonnut Kylä",
    "I Can Hear the Front: Letters 1943", "Icanthus — tajemná rostlina", "I Cannonball the Line",
    "I Am Sorrow: Poems from the Trench", "Я не верил в это: письма с фронта",
    "Я не могу забыть её глаза", "Не могу забыть: дневник 1942 года", "Извилистый путь домой",
    "Извинись перед мёртвыми", "The Day the General Said Sorry",
    "Письмо матери: «Извините, я не могу вернуться»", "Γερμανός στρατιώτης σοκαρισμένος από τις Ουκρανές",
]


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Resp:
    def __init__(self, payload, status=200):
        self.status_code, self._p, self.text = status, payload, str(payload)

    def json(self):
        return self._p


def via_codex(answer):
    saved = P.requests.post
    P.requests.post = lambda *a, **k: Resp({"text": answer})
    try:
        return ("ok", P.make_title_codex("Donor title about the war", "el", "key", "http://x"))
    except P.TitleError as exc:
        return ("error", str(exc))
    except P.TitleUnavailable as exc:
        return ("wait", str(exc))
    finally:
        P.requests.post = saved


def via_openai(answer):
    saved = P.requests.post
    P.requests.post = lambda *a, **k: Resp({"choices": [{"message": {"content": answer}}]})
    try:
        return ("ok", P.make_title("Donor title about the war", "el", "key"))
    except P.TitleError as exc:
        return ("error", str(exc))
    except P.TitleUnavailable as exc:
        return ("wait", str(exc))
    finally:
        P.requests.post = saved


def main() -> int:
    print("Отказы узнаются")
    for t in REFUSALS:
        check(t[:60], P.refusal_in(t), True)
    print("\nОбычные названия не трогаем")
    for t in TITLES:
        check(t[:60], P.refusal_in(t), False)

    print("\nНазвание подпиской Codex")
    kind, text = via_codex(REFUSALS[0])
    check("отказ — это ошибка строки, а не «подождать»", kind, "error")
    check("в причине слова модели", "I can't help rewrite" in text, True)
    kind, text = via_codex("  «" + REFUSALS[1] + "»  ")
    check("отказ в кавычках тоже пойман", kind, "error")
    check("обычное название проходит", via_codex(TITLES[0]), ("ok", TITLES[0]))

    print("\nНазвание через OpenAI (старый путь)")
    check("отказ — ошибка", via_openai(REFUSALS[0])[0], "error")
    check("обычное название проходит", via_openai(TITLES[2]), ("ok", TITLES[2]))

    print("\nЭкран выпуска объясняет причину")
    _kind, text = via_codex(REFUSALS[0])
    e = B.explain(text)
    check("это проблема донора", e.get("kind"), B.DONOR)
    check("«вернуть в работу» не предлагается", e.get("revive"), False)
    check("заголовок понятный", "отказалась писать название" in (e.get("title") or ""), True)

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
