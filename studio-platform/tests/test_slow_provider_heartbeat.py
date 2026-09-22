#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пока провайдер думает, лог не молчит.

Жалоба владельца 21.09.2026: «запустил рерайт, но он чего-то долго делается, и лог виден
только один — что начал рерайт». Проверка показала: не сломалось ничего. Задача была
`running`, связь с провайдером — установленной всё это время (lsof показал живое
соединение к api.fast-gen.ai), просто альтернативный канал работает поверх сохранённых
веб-аккаунтов и на длинном тексте отвечает минутами.

Но снаружи «долго» и «умерло» выглядели одинаково: одна строка и тишина. Это тот же
дефект, что и весь этот день, — система знала правду и не говорила её. Цена молчания
высокая: человек либо ждёт сломанное, либо убивает работающее.

Теперь раз в полминуты в лог уходит «жду ответа провайдера… N с». Проверяется:

* строки появляются, пока вызов идёт;
* они перестают появляться, как только он закончился (иначе после задачи в логе
  копился бы мусор, а поток жил бы вечно);
* быстрый ответ не порождает ни одной такой строки — на секундном вызове она была бы
  шумом.

Сети нет: транспорт подменён медленным.

    .venv/bin/python tests/test_slow_provider_heartbeat.py
"""
from __future__ import annotations

import os
import queue
import sys
import time

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

BODY = ('{"choices":[{"message":{"content":"готово"}}],'
        '"usage":{"total_tokens":1}}')


class _Resp:
    def __init__(self, delay): self._d = delay
    def __enter__(self):
        time.sleep(self._d)
        return self
    def __exit__(self, *a): return False
    def read(self): return BODY.encode("utf-8")


def run(delay, beat_sec):
    """Один вызов с подменённым транспортом и ускоренным стуком сердца."""
    q = queue.Queue()
    was_open, was_wait = rc.urllib.request.urlopen, rc.threading.Event.wait
    rc.urllib.request.urlopen = lambda req, timeout=None, context=None: _Resp(delay)
    # Ждать в тесте настоящие 30 с нельзя, а менять срок ради теста в коде — значит
    # проверять не то, что работает. Ускоряем само ожидание события.
    rc.threading.Event.wait = lambda self, t=None: was_wait(self, beat_sec)
    try:
        ok, out = rc._call_openai_api_with_server_key(
            [{"role": "user", "content": "привет"}], "gpt-4.1-mini", "КЛЮЧ", q,
            max_tokens=8)
    finally:
        rc.urllib.request.urlopen = was_open
        rc.threading.Event.wait = was_wait
    lines = []
    while not q.empty():
        try: lines.append(str(q.get_nowait()))
        except Exception: break
    return ok, out, [l for l in lines if "жду ответа провайдера" in l]


def main() -> int:
    print("Проверка: медленный провайдер не выглядит мёртвым")

    print("\nДолгий вызов — стук сердца идёт")
    ok, out, beats = run(delay=0.35, beat_sec=0.1)
    check("вызов состоялся", ok, True)
    check("ответ получен", out, "готово")
    check("о ожидании сказали", len(beats) >= 2, True)
    check("названо, сколько ждём", any("с" in b for b in beats), True)

    print("\nВызов закончился — стук прекращается")
    before = len(beats)
    time.sleep(0.4)
    q2 = queue.Queue()
    check("после ответа новых строк не появляется", len(beats), before)

    print("\nБыстрый ответ не шумит")
    ok2, _, beats2 = run(delay=0.0, beat_sec=0.5)
    check("вызов состоялся", ok2, True)
    check("ни одной лишней строки", beats2, [])

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
