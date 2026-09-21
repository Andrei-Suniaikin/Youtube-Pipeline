#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Промпты к кадрам пишутся FastGen'ом, когда на счету OpenAI ноль.

21.09.2026 весь конвейер встал на промптах: OpenAI отвечал 429 insufficient_quota
(«You have no credits remaining»), а повтор при исчерпанном балансе не помогает никогда.

Оказалось, что у FastGen — провайдера, за которого уже заплачено и который рисует нам
кадры, — есть полноценный OpenAI-совместимый `/v1/chat/completions`: 28 моделей, наш
заголовок `Authorization: Bearer` принимается, ответ приходит штатной `chat.completion`.
Расход идёт из ОТДЕЛЬНОЙ квоты `prompt.tokens.hour` (200 000 токенов/час) и кредиты
картинок не трогает. Подключается существующим каналом `altclaude`, без правок движка.

Три вещи проверены живыми вызовами, а не выведены из названий, и каждая из них ломала
канал молча:

1. **База обязана включать `/v1`.** Адрес склеивается как `base + "/chat/completions"`
   без нормализации; без префикса выходит 404, который считается фатальным.
2. **Модель обязана быть в стиле OpenRouter** (`openai/...`). Дефолт канала —
   `claude-sonnet-5`, а моделей Anthropic на этом эндпоинте нет вовсе: HTTP 400 на каждом
   запросе. Предполётная проверка этого не ловит — она смотрит только, что адрес и ключ
   непусты.
3. **Шестидесяти секунд на чтение не хватает.** Бэкенд — сохранённые веб-аккаунты: даже
   однословный ответ идёт 10-12 с, а на плане сцен приходил `ReadTimeout`. Это ровно та
   болезнь, из-за которой 03.09 подняли таймауты картинок: наш собственный таймер снимает
   вызов, который провайдер ЕЩЁ ВЫПОЛНЯЕТ, и оплаченный ответ выбрасывается.

Сети тут нет: проверяется срок чтения и то, что стенд задаёт канал правильно.

    .venv/bin/python tests/test_alt_prompt_channel.py
"""
from __future__ import annotations

import importlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BASE, "workers"))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def test_timeout() -> None:
    print("\nСрок чтения: поднимается для альт-канала и только вверх")
    os.environ.pop("VS_ALT_CHAT_READ_TIMEOUT", None)
    import googler_worker as gw                                    # noqa: PLC0415
    gw = importlib.reload(gw)

    check("по умолчанию заметно больше шестидесяти", gw.ALT_CHAT_READ_TIMEOUT >= 180, True)
    check("короткий срок вызывающего поднимается", gw._alt_timeout((5, 60)),
          (5, gw.ALT_CHAT_READ_TIMEOUT))
    check("ничего не задано — тоже поднимается", gw._alt_timeout(None),
          (5, gw.ALT_CHAT_READ_TIMEOUT))
    # Урезать чужой запас нельзя: у длинных задач он выставлен осознанно.
    check("больший срок вызывающего НЕ урезается", gw._alt_timeout((5, 600)), (5, 600))
    check("срок числом тоже понимается", gw._alt_timeout(30), (5, gw.ALT_CHAT_READ_TIMEOUT))

    check("общий срок чат-вызовов не тронут", gw.GPT_CHAT_TIMEOUT, (5, 60))

    os.environ["VS_ALT_CHAT_READ_TIMEOUT"] = "333"
    gw = importlib.reload(gw)
    check("значение берётся из переменной", gw._alt_timeout(None), (5, 333))
    os.environ.pop("VS_ALT_CHAT_READ_TIMEOUT", None)
    importlib.reload(gw)


def test_stand_wires_it() -> None:
    print("\nСтенд задаёт канал, и задаёт его правильно")
    sh = open(os.path.join(BASE, "dev", "run.sh"), encoding="utf-8").read()

    check("канал включается", "VS_PROMPT_PROVIDER" in sh and "altclaude" in sh, True)
    # 21.09, вторая итерация: адрес и модель альт-канала пускач больше НЕ задаёт.
    # Их источник — файл ключей (alt_llm_base/alt_llm_model), откуда кабинет собирает
    # окружение подпроцесса. Два источника расходились молча: ключ доезжал отсюда, а
    # адрес — оттуда, и они указывали на разных провайдеров.
    check("пускач не задаёт адрес альт-канала — его источник один",
          "VS_ALT_LLM_BASE=" in sh, False)
    check("и не задаёт модель",  "VS_ALT_LLM_MODEL=" in sh, False)
    # Две переменные рядом устроены ПО-РАЗНОМУ, и перепутать их — прямой путь к 404:
    #   VS_ALT_LLM_BASE      — БАЗА, движок сам добавляет "/chat/completions";
    #   REWRITE_GPT_CHAT_URL — ПОЛНЫЙ адрес, к нему не добавляется ничего.
    # Поэтому запрет на "/chat/completions" относится только к первой, и проверять его
    # надо в её строке, а не подстрокой по всему файлу.
    _base_line = next((l for l in sh.splitlines() if "VS_ALT_LLM_BASE=" in l), "")
    check("у базы канала промптов нет лишнего /chat/completions",
          "/chat/completions" in _base_line, False)
    _rw_line = next((l for l in sh.splitlines() if "REWRITE_GPT_CHAT_URL=" in l), "")
    check("а у адреса рерайта он, наоборот, есть",
          "/chat/completions" in _rw_line, True)
    check("ключ рерайта задан вместе с адресом",
          bool(re.search(r"REWRITE_GPT_CHAT_KEY=", sh)), True)

    # ── Ключ принадлежит хосту ────────────────────────────────────────────────
    # 21.09 это стрельнуло: ключ для FastGen брался как `alt_llm or googler`. Пока
    # alt_llm в файле не было, подставлялся googler и всё работало. Как только alt_llm
    # появился (он нужен обложкам, у которых ДРУГОЙ хост), он молча победил — и на
    # FastGen поехал ключ чужого провайдера. Наружу это вышло как «Неверный API ключ»,
    # то есть как сломанный аккаунт, хотя сломалось сопоставление ключа и адреса.
    # Проверяем по блокам: у каждого маршрута своё семейство и никаких «или».
    # Граница — присваивание _coverkey: блок ключа обложки начинается именно там и
    # стоит ВЫШЕ строки COVER_ALT_BASE_URL (резать по ней значило бы отдать этот блок
    # чужой половине — на этом тест и споткнулся, когда писался).
    _cover_at = sh.find("_coverkey=")
    check("блок ключа обложки найден", _cover_at > 0, True)
    _fastgen_block = sh[:_cover_at]
    _cover_block = sh[_cover_at:]
    check("маршрут текста берёт ключ googler",
          'data.get("googler")' in _fastgen_block, True)
    check("...и НЕ берут ключ чужого провайдера",
          'data.get("alt_llm")' in _fastgen_block, False)
    check("маршрут обложки берёт свой ключ alt_llm",
          'data.get("alt_llm")' in _cover_block, True)
    check("...и не подставляет им ключ FastGen",
          'data.get("googler")' in _cover_block, False)

    check("ключ берётся из общего файла, а не вписан в скрипт",
          "alt_llm" in sh and "googler" in sh, True)

    # Все четыре — через ${VAR:-...}: заданное снаружи значение обязано побеждать,
    # иначе нельзя ни вернуть openai, ни подставить другого провайдера.
    for var in ("REWRITE_GPT_CHAT_URL", "COVER_ALT_BASE_URL", "COVER_ALT_MODEL"):
        check(f"{var} не перетирает заданное снаружи",
              bool(re.search(rf'{var}="\$\{{{var}:-', sh)), True)
    check("VS_ALT_LLM_KEY ставится только когда пуст",
          bool(re.search(r'-z "\$VS_ALT_LLM_KEY"', sh)), True)


def main() -> int:
    print("Проверка: канал промптов FastGen подключён и не режется таймером")
    test_timeout()
    test_stand_wires_it()
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
