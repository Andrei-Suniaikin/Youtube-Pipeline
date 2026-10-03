#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""404 от провайдера — определённый ответ, а не повод крутить ретраи (03.10.2026).

`GET /api/v6/generations/{id}` с ответом 404 значит ровно одно: «такого заказа у меня
нет». Провайдер чистит старые операции, и у заказов 14–17-часовой давности это штатный
исход. До 03.10 этот код уходил в `raise_for_status`, попадал в общий обработчик
исключений и крутился тремя попытками — три одинаковых ответа, потерянное время и
непонятный текст «HTTPError: 404 Client Error» наверху.

Проверяется ровно две вещи, обе про поведение, а не про текст:
  * 404 отвечает СРАЗУ, одним запросом, и код остаётся в причине;
  * транзиентные коды (429, 5xx) по-прежнему повторяются — иначе починка превратила бы
    троттл провайдера в отказ прогона.

    .venv/bin/python tests/test_v6_lost_order_http.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "workers"))

os.environ.setdefault("VS_TEST_NO_NET", "1")

import googler_worker as W                                        # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Ответ:
    def __init__(self, code, text=""):
        self.status_code = code
        self.text = text

    def json(self):
        return {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(
                f"{self.status_code} Client Error: for url: x", response=self)


def спросить(код, text=""):
    """Сколько запросов сделает воркер и что вернёт, если провайдер отвечает так."""
    счёт = {"n": 0}

    def поддельный_get(url, headers=None, timeout=None):
        счёт["n"] += 1
        return Ответ(код, text)

    import requests
    старый, requests.get = requests.get, поддельный_get
    спал = []
    старый_sleep, W.time.sleep = W.time.sleep, lambda s: спал.append(s)
    try:
        out = W.googler_request_v6("generations/gen_61c36", method="GET")
    finally:
        requests.get = старый
        W.time.sleep = старый_sleep
    return счёт["n"], out


def main() -> int:
    print("404 — ответ, а не сбой")
    n, out = спросить(404, '{"detail":"not found"}')
    check("спросили ровно один раз", n, 1)
    check("вернулась причина, а не пустота", bool(out.get("_error")), True)
    check("и код в ней есть", "404" in str(out.get("_error")), True)
    # По этому «404» воркер и решает, что заказ потерян: подстрока — часть контракта.
    check("ровно в том виде, по которому воркер узнаёт потерю",
          "404" in str(out.get("_error")), True)

    print("\nА троттл и серверные сбои по-прежнему повторяются")
    for код in (429, 500, 503):
        n, out = спросить(код, "busy")
        check(f"{код}: попыток больше одной", n > 1, True)
        check(f"{код}: причина названа", str(код) in str(out.get("_error")), True)

    print("\nИ мёртвый ключ отвечает сразу, как и раньше")
    n, out = спросить(401, '{"detail":"bad key"}')
    check("401 без ретраев", n, 1)
    check("и код в причине", "401" in str(out.get("_error")), True)

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
