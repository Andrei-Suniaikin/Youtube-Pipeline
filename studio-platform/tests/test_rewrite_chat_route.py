#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Текст можно писать не только в OpenAI — но ключ едет только вместе с адресом.

21.09.2026 у счёта OpenAI ноль (429 insufficient_quota), и сценарий вставал целиком,
хотя рядом лежал оплаченный провайдер с OpenAI-совместимым `/v1/chat/completions`.
Рерайт ходил в чат одним-единственным местом с зашитым адресом.

Адрес и ключ вынесены в переменные. Два свойства, ради которых написан этот тест:

**По умолчанию не меняется ничего.** Пустые переменные — прежний `api.openai.com` и
прежний серверный ключ. Смена провайдера текста — это смена качества выдачи, и
случаться она должна только когда её попросили.

**Адрес без ключа — отказ, а не тихий откат.** Задать чужой адрес и оставить ключ
OpenAI значит отправить секрет туда, где он не предназначен, и никогда об этом не
узнать. Поэтому вызов отказывается и говорит, что денег это не стоило. Откат на
OpenAI молча был бы хуже: человек увидел бы обычную ошибку кредитов и не понял, что
его ключ уже улетел на сторону.

Сети здесь нет: транспорт подменён, проверяется адрес и заголовок.

    .venv/bin/python tests/test_rewrite_chat_route.py
"""
from __future__ import annotations

import os
import queue
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
for _p in ("shared", "server", "workers", "rewrite"):
    sys.path.insert(0, os.path.join(BASE, _p))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


import rewrite_core as rc                                          # noqa: E402

SENT = {}


class _Resp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self):
        return b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":1}}'


def _fake_urlopen(req, timeout=None, context=None):
    SENT["url"] = req.full_url
    SENT["auth"] = req.get_header("Authorization") or ""
    SENT["timeout"] = timeout
    return _Resp()


def _call():
    SENT.clear()
    return rc._call_openai_api_with_server_key(
        [{"role": "user", "content": "привет"}], "gpt-4.1-mini",
        "КЛЮЧ-OPENAI", queue.Queue(), max_tokens=8)


def main() -> int:
    print("Проверка: маршрут чат-вызова рерайта")
    was = rc.urllib.request.urlopen
    rc.urllib.request.urlopen = _fake_urlopen
    for v in ("REWRITE_GPT_CHAT_URL", "REWRITE_GPT_CHAT_KEY"):
        os.environ.pop(v, None)
    try:
        print("\nБез переменных — ровно прежнее поведение")
        ok, _ = _call()
        check("вызов состоялся", ok, True)
        check("адрес прежний", SENT.get("url"), "https://api.openai.com/v1/chat/completions")
        check("ключ прежний", SENT.get("auth"), "Bearer КЛЮЧ-OPENAI")

        print("\nАдрес без ключа — отказ, и в сеть никто не пошёл")
        os.environ["REWRITE_GPT_CHAT_URL"] = "https://чужой.example/v1/chat/completions"
        ok, msg = _call()
        check("отказано", ok, False)
        check("в сеть не ходили", "url" in SENT, False)
        check("названа причина", "без REWRITE_GPT_CHAT_KEY" in msg, True)
        check("сказано, что денег не стоило", "Денег это не стоило" in msg, True)
        check("ключ OpenAI в сообщение не попал", "КЛЮЧ-OPENAI" in msg, False)

        print("\nАдрес вместе с ключом — едут оба")
        os.environ["REWRITE_GPT_CHAT_KEY"] = "КЛЮЧ-ПРОВАЙДЕРА"
        ok, _ = _call()
        check("вызов состоялся", ok, True)
        check("адрес чужой", SENT.get("url"), "https://чужой.example/v1/chat/completions")
        check("ключ тоже чужой, а не OpenAI", SENT.get("auth"), "Bearer КЛЮЧ-ПРОВАЙДЕРА")

        print("\nТолько ключ, без адреса — по-прежнему OpenAI")
        os.environ.pop("REWRITE_GPT_CHAT_URL")
        ok, _ = _call()
        check("адрес прежний", SENT.get("url"), "https://api.openai.com/v1/chat/completions")
    finally:
        rc.urllib.request.urlopen = was
        for v in ("REWRITE_GPT_CHAT_URL", "REWRITE_GPT_CHAT_KEY"):
            os.environ.pop(v, None)

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
