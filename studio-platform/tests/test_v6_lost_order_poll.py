#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Опрос v6 перестаёт ждать заказ, которого у провайдера нет (04.10.2026).

Что случилось. С 12:25 до вечера ни одной картинки: оба места очереди картинок держали
два прогона, каждый опрашивал заказы, на которые провайдер отвечал
`404 resource.operation_not_found_or_expired`. Опрос считал это «статус неизвестен» и
ждал полный таймаут кадра — 10 000 с, а перезапуски студии начинали отсчёт заново.
37 выпусков стояли в очереди на картинки, 25 из них уже опоздали к своему слоту.

Развилка «провайдер потерял заказ → кадр можно заказать заново» уже была (03.10,
tests/test_lost_order.py), но включалась только ПОСЛЕ таймаута опроса. Теперь опрос сам
возвращает «нет кадра», когда 404 идёт подряд дольше `V6_LOST_GRACE_SEC`.

Проверяется и обратное: короткий 404 (только что созданный заказ ещё не виден), 404,
перемежающийся живым статусом, и любой другой отказ — опрос НЕ бросают раньше времени.

Провайдера нет, часы подменены — тест идёт секунду.

    .venv/bin/python tests/test_v6_lost_order_poll.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "workers"))

import googler_worker as G                                        # noqa: E402

FAILS = []
LOST = {"_error": "HTTP 404: заказ или адрес не найден: {\"code\":"
                  "\"resource.operation_not_found_or_expired\"}"}
RUNNING = {"status": "running"}
DONE = {"status": "succeeded", "results": [{"data": "data:image/png;base64,QUJD"}]}


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Clock:
    """Часы воркера: sleep двигает время, а не ждёт его."""

    def __init__(self):
        self.now = 1_800_000_000.0

    def time(self):
        return self.now

    def sleep(self, sec):
        self.now += float(sec)

    def __getattr__(self, name):                 # strftime и прочее — настоящие
        import time as _t
        return getattr(_t, name)


def run(answers, timeout=10_000):
    """answers(elapsed) → ответ провайдера на опрос. Возвращает (результат, секунд, опросов)."""
    clock = Clock()
    start = clock.now
    calls = []
    saved_req, saved_time = G.googler_request_v6, G.time

    def fake_request(endpoint, method="POST", data=None):
        calls.append(endpoint)
        return dict(answers(clock.now - start))

    G.googler_request_v6, G.time = fake_request, clock
    try:
        out = G.poll_v6_generation("gen_x", index=7, timeout=timeout)
    finally:
        G.googler_request_v6, G.time = saved_req, saved_time
    return out, clock.now - start, len(calls)


def main() -> int:
    grace = G.V6_LOST_GRACE_SEC
    print(f"Пауза до вывода «заказа нет»: {grace} с, таймаут кадра {G.IMAGE_POLL_TIMEOUT} с")
    check("пауза много короче таймаута кадра", grace * 10 < G.IMAGE_POLL_TIMEOUT, True)

    print("\nЗаказ потерян: 404 на каждый опрос")
    G._set_provider_refusal("")
    out, took, n = run(lambda t: LOST)
    check("кадра нет", out, None)
    check("бросили за паузу, а не за таймаут кадра", grace <= took <= grace + 2 * G.VIDEO_POLL_INTERVAL, True)
    check("причина — слова провайдера", "operation_not_found" in G._provider_refusal(), True)

    print("\nТолько что созданный заказ: 404 короче паузы, потом готов")
    out, took, n = run(lambda t: LOST if t < grace - 2 * G.VIDEO_POLL_INTERVAL else DONE)
    check("кадр забран", out, "QUJD")

    print("\n404, перемежающийся живым статусом, — отсчёт начинается заново")
    def flapping(t):
        if t >= 3 * grace:
            return DONE
        return RUNNING if int(t // (grace * 0.75)) % 2 else LOST
    out, took, n = run(flapping)
    check("не брошен: каждая серия 404 короче паузы", out, "QUJD")

    print("\nДругой отказ (мёртвый ключ) — поведение прежнее: ждём до таймаута")
    dead_key = {"_error": "HTTP 401: invalid api key"}
    out, took, n = run(lambda t: dead_key, timeout=grace * 5)
    check("кадра нет", out, None)
    check("ждали весь таймаут, а не паузу 404", took >= grace * 5, True)

    print("\nЖивой заказ без ошибок — ждём сколько нужно")
    out, took, n = run(lambda t: RUNNING if t < grace * 4 else DONE)
    check("кадр забран после долгого ожидания", out, "QUJD")

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
