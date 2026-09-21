#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка готовности не пугает на исправной системе и ловит подмену ключа.

`dev/preflight.py` отвечает на один вопрос до запуска ролика: пойдёт или нет. Цена
ошибки у неё несимметричная и обе стороны плохи:

* **Ложная тревога хуже отсутствия проверки.** Первая версия смотрела на переменную
  SSL_CERT_FILE в ТЕКУЩЕЙ оболочке и кричала «сертификаты НЕ ПОЙДЁТ» сразу после того,
  как сама успешно сходила по https к трём провайдерам (requests носит certifi с собой
  и переменной не требует). Такую проверку перестают читать, и она бесполезна ровно
  тогда, когда впервые окажется права.
* **Молчание про настоящую поломку** стоило 21.09 целого дня: у стенда на адрес FastGen
  уехал ключ другого провайдера, и наружу это вышло как «Неверный API ключ», то есть как
  сломанный аккаунт.

Отсюда два правила, которые закрепляет тест:

1. Сертификаты меряются по тому, сможет ли СТЕНД их выставить (есть связка certifi и
   пускач её экспортирует), а не по текущему окружению.
2. Ключ каждого маршрута сверяется с адресом этого маршрута — по окружению работающего
   стенда, а не своему. Значения ключей при этом не печатаются никогда.

    .venv/bin/python tests/test_preflight.py
"""
from __future__ import annotations

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)

_spec = importlib.util.spec_from_file_location(
    "preflight", os.path.join(BASE, "dev", "preflight.py"))
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def last(stage):
    for s, state, note in reversed(pf.ROWS):
        if s == stage:
            return state, note
    return None, None


def test_certs() -> None:
    print("\nСертификаты: меряем стенд, а не эту оболочку")
    pf.ROWS.clear()
    was = os.environ.pop("SSL_CERT_FILE", None)
    try:
        pf.check_certs()
        state, note = last("сертификаты")
        # Связка есть, пускач её экспортирует — значит стенду будет чем проверять,
        # даже если в этой оболочке переменной нет.
        check("на исправной системе не пугает", state, pf.OK)
        check("и объясняет, почему переменной тут нет",
              "стенд выставит её сам" in (note or ""), True)
    finally:
        if was is not None:
            os.environ["SSL_CERT_FILE"] = was


def test_key_routes() -> None:
    print("\nКлюч сверяется с адресом своего маршрута")
    k = {"googler": "КЛЮЧ-FASTGEN", "alt_llm": "КЛЮЧ-ПРОКСИ"}
    # Распределение после 21.09: текст рерайта идёт в FastGen, промпты и анализ обложки —
    # на сторонний прокси. Адрес промптам задаёт кабинет из файла ключей, поэтому
    # VS_ALT_LLM_BASE в окружении стенда может отсутствовать — ключ проверяется всё равно.
    good = {"REWRITE_GPT_CHAT_KEY": "КЛЮЧ-FASTGEN",
            "REWRITE_GPT_CHAT_URL": "https://api.fast-gen.ai/v1/chat/completions",
            "VS_ALT_LLM_KEY": "КЛЮЧ-ПРОКСИ",
            "COVER_ALT_API_KEY": "КЛЮЧ-ПРОКСИ", "COVER_ALT_BASE_URL": "https://proxy/v1"}

    pf.ROWS.clear(); pf.check_key_routes(k, good, "123")
    check("верные ключи — тихо", last("Ключ ↔ адрес")[0], pf.OK)

    # Ровно сегодняшний дефект: на адрес FastGen уехал ключ прокси.
    bad = dict(good, REWRITE_GPT_CHAT_KEY="КЛЮЧ-ПРОКСИ")
    pf.ROWS.clear(); pf.check_key_routes(k, bad, "123")
    state, note = last("Ключ ↔ адрес")
    check("чужой ключ на адресе FastGen — отказ", state, pf.BAD)
    check("названы и переменная, и оба семейства",
          "REWRITE_GPT_CHAT_KEY" in note and "alt_llm" in note and "googler" in note, True)
    check("предсказан симптом, по которому его будут искать",
          "Неверный API ключ" in note, True)
    check("значение ключа не напечатано",
          "КЛЮЧ-ПРОКСИ" in note or "КЛЮЧ-FASTGEN" in note, False)

    # Ровно то, что стояло у владельца: ключ FastGen на канале промптов, который ходит
    # на прокси. Симптом тот же — 401 и «Неверный API ключ».
    pf.ROWS.clear(); pf.check_key_routes(k, dict(good, VS_ALT_LLM_KEY="КЛЮЧ-FASTGEN"), "1")
    check("ключ FastGen на канале промптов — отказ", last("Ключ ↔ адрес")[0], pf.BAD)

    # Незнакомый ключ — тоже повод сказать: молчать про «не знаю чей» опаснее.
    pf.ROWS.clear(); pf.check_key_routes(k, dict(good, VS_ALT_LLM_KEY="ЧУЖОЙ"), "123")
    check("неизвестный ключ тоже замечен", last("Ключ ↔ адрес")[0], pf.BAD)

    pf.ROWS.clear(); pf.check_key_routes(k, {}, "")
    check("стенда нет — не выдумываем вердикт", last("Ключ ↔ адрес")[0], pf.WARN)


def test_scheduler() -> None:
    print("\nИсполнитель задач проверяется у стенда, а не у себя")
    pf.ROWS.clear(); pf.check_scheduler({"VS_SERVER_DISABLE_BACKGROUND": "1"}, "77")
    state, note = last("Исполнитель задач")
    check("выключен — отказ", state, pf.BAD)
    check("назван pid, чтобы было что смотреть", "77" in (note or ""), True)
    check("сказано, чем поднимать", "run-live" in (note or ""), True)

    pf.ROWS.clear(); pf.check_scheduler({"VS_SERVER_DISABLE_BACKGROUND": ""}, "77")
    check("включён — готово", last("Исполнитель задач")[0], pf.OK)

    pf.ROWS.clear(); pf.check_scheduler({}, "")
    check("стенда нет — оговорка, а не вердикт", last("Исполнитель задач")[0], pf.WARN)


def main() -> int:
    print("Проверка: dev/preflight.py")
    test_certs()
    test_key_routes()
    test_scheduler()
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
