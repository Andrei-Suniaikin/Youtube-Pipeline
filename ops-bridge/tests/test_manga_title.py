#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Заголовки роликов Кодексом подписки, а не ключом прокси (02.10.2026).

Повод. Ключ прокси названий встал на лимите («API key token allowance exhausted»), и
девяносто девять готовых роликов остановились на шаге, который стоит копейки. Заказчик
разрешил пользоваться Кодексом, и заголовки переведены на него.

Проверяется то, из-за чего смена провайдера может выйти боком:

1. **Разбор машинного потока.** Печатный вывод Кодекса повторяет ответ дважды и
   обрамляет своими строками; берём `agent_message` из `--json`, а не «последнюю
   строку».
2. **Классы отказа остались кабинетными.** `TitleUnavailable` — ролик ЖДЁТ,
   `TitleError` — виновата строка и она уходит в карантин. Мост на них уже умеет
   отвечать правильно, и смена провайдера не имеет права это поменять.
3. **Запасной путь.** Кодекс не настроен — идём прокси, как раньше.
4. **Кодекс не зовётся вхолостую:** без исходного названия отказ выносится до запуска.

В Кодекс проверка не ходит: запуск подменён.

    .venv/bin/python ops-bridge/tests/test_manga_title.py
"""
from __future__ import annotations

import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.dirname(HERE)
sys.path.insert(0, BRIDGE)

# Кабинетный `pub_publish` живёт на боевой машине. Здесь — его двойник ровно в том
# объёме, который использует модуль заголовков: промпты, чистка, классы отказа.
pub = types.ModuleType("pub_publish")


class TitleError(Exception):
    pass


class TitleUnavailable(Exception):
    pass


import re                                                          # noqa: E402

pub.TitleError = TitleError
pub.TitleUnavailable = TitleUnavailable
pub.SYSTEM = "Ты редактор заголовков YouTube."
pub.RETRY_HINT = "Переформулируй нейтрально."
pub.LANG_NAMES = {"pl": "польский", "es": "испанский"}
pub._REFUSAL = re.compile(r"^(извин|sorry|i can'?t|не могу)", re.I)
pub.clean_title = lambda t: " ".join(str(t or "").split()).strip('"«» ')
pub.make_title = lambda d, l, llm: "ЗАГОЛОВОК ОТ ПРОКСИ"
sys.modules["pub_publish"] = pub

import manga_title as mt                                           # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def поток(*события) -> str:
    """Машинный поток Кодекса: события построчно, как он их и печатает."""
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in события)


ОТВЕТ = {"type": "item.completed",
         "item": {"id": "item_0", "type": "agent_message",
                  "text": "Czytelnik, który wiedział wszystko, odkrył prawdę"}}
НАЧАЛО = {"type": "thread.started", "thread_id": "x"}
КОНЕЦ = {"type": "turn.completed", "usage": {"output_tokens": 97}}


def настроен():
    """Кодекс как будто настроен: бинарь, дом с доступом и модель на месте."""
    mt.codex_setup = lambda: {"bin": "/codex", "home": "/home", "model": "gpt-5.6-terra"}


def main() -> int:
    print("Ответ берётся из машинного потока, а не из печатного вывода")
    # Между событиями Кодекс печатает и свои строки — они не должны попасть в заголовок.
    смесь = "Reading additional input from stdin...\n" + поток(НАЧАЛО, ОТВЕТ, КОНЕЦ) + "\ncodex\n"
    check("взят текст agent_message", mt._answer(смесь),
          "Czytelnik, który wiedział wszystko, odkrył prawdę")
    check("без событий ответа нет", mt._answer("codex\nчто-то\n"), "")
    check("битые строки не ломают разбор", mt._answer("{не json\n" + поток(ОТВЕТ)),
          ОТВЕТ["item"]["text"])
    # В настоящем потоке `item.completed` приходит не только на ответ: Кодекс так же
    # закрывает свои рассуждения и прочие шаги. Брать «любой завершённый item» значит
    # однажды поставить на канал кусок размышления вместо заголовка.
    размышление = {"type": "item.completed",
                   "item": {"type": "reasoning", "text": "Надо перевести на польский…"}}
    check("чужой тип события в заголовок не попадает",
          mt._answer(поток(НАЧАЛО, размышление, ОТВЕТ, КОНЕЦ)), ОТВЕТ["item"]["text"])
    check("и если ответа нет, рассуждение не подменяет его",
          mt._answer(поток(НАЧАЛО, размышление, КОНЕЦ)), "")
    # Берётся ПОСЛЕДНИЙ ответ: при повторной попытке их в потоке два.
    второй = {"type": "item.completed",
              "item": {"type": "agent_message", "text": "Второй заголовок, нейтральный"}}
    check("берётся последний", mt._answer(поток(ОТВЕТ, второй)), "Второй заголовок, нейтральный")

    print("\nЗаголовок делается Кодексом")
    настроен()
    звонки = []

    def бегун(cmd, env, timeout):
        звонки.append({"cmd": cmd, "home": env.get("CODEX_HOME"), "timeout": timeout})
        return 0, поток(НАЧАЛО, ОТВЕТ, КОНЕЦ)

    из_кодекса = mt.make_title("The Reader Who Knew Everything", "pl", {}, runner=бегун)
    check("заголовок получен", из_кодекса, "Czytelnik, który wiedział wszystko, odkrył prawdę")
    check("звонок один", len(звонки), 1)
    cmd = звонки[0]["cmd"]
    check("машинный вывод запрошен", "--json" in cmd, True)
    check("песочница только на чтение", 'sandbox_mode="read-only"' in cmd, True)
    check("сеть запрещена", 'web_search="disabled"' in cmd, True)
    check("оболочка запрещена", "shell_tool" in cmd, True)
    check("модель передана", "gpt-5.6-terra" in cmd, True)
    check("дом подставлен", звонки[0]["home"], "/home")
    промпт = cmd[-1]
    check("в промпте язык канала", "польский" in промпт, True)
    check("и название оригинала", "The Reader Who Knew Everything" in промпт, True)

    print("\nОтказ модели: одна попытка нейтрально, потом карантин строки")
    состояние = {"n": 0}

    def отказ(cmd, env, timeout):
        состояние["n"] += 1
        return 0, поток({"type": "item.completed",
                         "item": {"type": "agent_message", "text": "Извините, не могу"}})

    try:
        mt.make_title("Страшное название", "pl", {}, runner=отказ)
        check("отказ доведён до карантина", "не отказал", "TitleError")
    except TitleError as exc:
        check("отказ доведён до карантина", "TitleError", "TitleError")
        check("и попыток было две", состояние["n"], 2)
        check("в тексте виден ответ модели", "не могу" in str(exc).lower(), True)

    print("\nПустой ответ и короткий — разные беды")
    try:
        mt.make_title("Название", "pl", {}, runner=lambda c, e, t: (0, поток(НАЧАЛО, КОНЕЦ)))
        check("пустой ответ", "не отказал", "TitleUnavailable")
    except TitleUnavailable:
        # Пусто — это ресурс не ответил: ролик ЖДЁТ, а не отправляется в карантин.
        check("пустой ответ — ролик ждёт", "TitleUnavailable", "TitleUnavailable")
    try:
        mt.make_title("Название", "pl", {},
                      runner=lambda c, e, t: (0, поток({"type": "item.completed",
                                                        "item": {"type": "agent_message",
                                                                 "text": "Три"}})))
        check("короткий ответ", "не отказал", "TitleError")
    except TitleError:
        # Короткий ответ — вина строки: по ней заголовка не выходит.
        check("короткий ответ — карантин строки", "TitleError", "TitleError")

    print("\nКодекс не ответил — ролик ждёт, а не падает в карантин")
    try:
        mt.make_title("Название", "pl", {}, runner=lambda c, e, t: (1, "codex: boom"))
        check("ненулевой код", "не отказал", "TitleUnavailable")
    except TitleUnavailable as exc:
        check("ненулевой код — ролик ждёт", "TitleUnavailable", "TitleUnavailable")
        check("и сказано, что ответил Кодекс", "boom" in str(exc), True)

    print("\nБез исходного названия Кодекс не зовётся вовсе")
    было = []
    try:
        mt.make_title("   ", "pl", {}, runner=lambda c, e, t: было.append(1) or (0, ""))
        check("отказ", "не отказал", "TitleError")
    except TitleError:
        check("отказ до запуска", "TitleError", "TitleError")
        check("Кодекс не звали", было, [])

    print("\nКодекс не настроен — идём прокси, как раньше")
    mt.codex_setup = lambda: {}
    check("ответ от прокси", mt.make_title("Название", "pl", {"base": "b", "key": "k"}),
          "ЗАГОЛОВОК ОТ ПРОКСИ")
    check("готовность честная", mt.ready(), False)

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
