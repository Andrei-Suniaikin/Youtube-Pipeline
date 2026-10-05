#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Отклонённый провайдером заказ кадра перезаказывается, а не держит выпуск (05.10.2026).

Что случилось. Ночью пять выпусков встали в разбор «этап images завершился со статусом
error». В каждом не хватало 5 кадров из 1184: провайдер отклонил эти заказы фильтром
безопасности («Request blocked by provider safety filters»). На повторном прогоне журнал
видел «заказ уже создан», спрашивал провайдера и получал status=failed — но потерянным
считал только 404. Итог: «оплаченный заказ результата не дал — кадр пропущен без нового
платного запроса» на каждом прогоне, кадр не перезаказывался никогда, хотя обычный путь
генерации умеет смягчать промпт именно на этот отказ.

Теперь оба определённых ответа — 404 (заказа нет) и status=failed (отклонён) — значат
«результата не будет»: строка журнала помечается потерянной, и следующий прогон заказывает
кадр обычным путём. Всё прочее (сеть, 5xx, «ещё в работе») выводом не считается.

    .venv/bin/python tests/test_order_never_delivers.py
"""
from __future__ import annotations

import os
import re
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


def main() -> int:
    f = W.order_never_delivers
    print("Результата не будет")
    lost = f({"_error": "HTTP 404: заказ или адрес не найден: operation_not_found_or_expired"})
    check("404 — заказ потерян", lost.startswith("провайдер потерял заказ"), True)
    why = f({"status": "failed", "error": "Request blocked by provider safety filters"})
    check("failed — заказ отклонён", why.startswith("провайдер отклонил заказ"), True)
    check("и причина провайдера в тексте", "safety filters" in why, True)
    check("failed в другом регистре", bool(f({"status": "FAILED"})), True)
    check("ошибка словарём", "blocked" in f({"status": "failed", "error": {"message": "blocked"}}), True)

    print("\nВывода нет — ждать или спросить позже")
    for label, probe in (("ещё в работе", {"status": "running"}),
                         ("в очереди", {"status": "queued"}),
                         ("готов", {"status": "succeeded"}),
                         ("5xx", {"_error": "HTTP 503: unavailable"}),
                         ("мёртвый ключ", {"_error": "HTTP 401: invalid api key"}),
                         ("сеть", None), ("пусто", {})):
        check(label, f(probe), "")

    print("\nВетка журнала кадра пользуется этим выводом и разрешает перезаказ")
    src = open(os.path.join(BASE, "workers", "googler_worker.py"), encoding="utf-8").read()
    m = re.search(r"_jrn_final = order_never_delivers\(_jrn_probe\)\n(.{0,8000}?)# v2 endpoint dead",
                  src, re.S)
    check("решение в ветке — по order_never_delivers", m is not None, True)
    check("и строка журнала помечается потерянной", bool(m and "lost=True" in m.group(1)), True)

    print("\nНедоставленный кадр заказывается заново в ЭТОМ ЖЕ прогоне (владелец 05.10)")
    tail = src[src.find("_jrn_final = order_never_delivers(_jrn_probe)"):]
    tail = tail[:tail.find("# v2 endpoint dead")]
    i_lost = tail.find("lost=True")
    i_rereserve = tail.find("_jrn = _journal_reserve_frame(")
    check("после пометки «потерян» — новая заявка в журнал", 0 < i_lost < i_rereserve, True)
    check("и между ними выхода нет — заявка действительно делается",
          "return (index, None)" in tail[i_lost:i_rereserve], False)
    seg = tail[i_rereserve:tail.find("else:", i_rereserve)]
    check("если журнал разрешил — дальше обычная генерация, без выхода",
          "if not _jrn.may_call:" in seg and seg.count("return (index, None)") == 1, True)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for x in FAILS:
            print("  -", x)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
