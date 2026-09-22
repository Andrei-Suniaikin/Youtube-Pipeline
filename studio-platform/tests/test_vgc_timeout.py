#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Оплаченный заказ озвучки не отменяется по НАШЕМУ таймауту.

Локальный срок ожидания — это наш таймер, а не доказательство того, что у провайдера
ничего не происходит. Раньше по его истечении воркер звал `cancel_task`: заказ уже
оплачен, мог быть почти готов — и выбрасывался. Та же грабля в бою 03.09 заставила
поднять таймауты картинок в десять раз (см. CLAUDE.md, «Ловушки»).

Сеть здесь не трогается: подменяются create/poll/cancel, а сам цикл ожидания — настоящий.

    .venv/bin/python tests/test_vgc_timeout.py
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "workers"))

import voicegencore_worker as W                                   # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def main() -> int:
    cancels, said = [], []
    saved = (W.create_task, W.check_task_status, W.cancel_task, W.download_audio,
             W.print_error_user_friendly, W.print_user, W.print_debug,
             W.MAX_WAIT_TIME_MINUTES, W.POLL_INTERVAL)

    W.create_task = lambda *a, **k: "task-777"
    # Провайдер работает, но не успевает: всегда «в процессе».
    W.check_task_status = lambda token, tid: ("processing", 40, {})
    W.cancel_task = lambda token, tid: cancels.append(tid)
    W.download_audio = lambda *a, **k: None
    W.print_error_user_friendly = lambda msg, *a, **k: said.append(str(msg))
    W.print_user = lambda msg, *a, **k: None
    W.print_debug = lambda msg, *a, **k: None
    W.MAX_WAIT_TIME_MINUTES = 0.001          # ~0,06 с ожидания вместо 450 минут
    W.POLL_INTERVAL = 0.01

    out = tempfile.mkdtemp(prefix="vgc-")
    try:
        ok = W.process_text("token", "текст", "voice-1", "engine", 0.5, out, "001.mp3", 1)
    finally:
        (W.create_task, W.check_task_status, W.cancel_task, W.download_audio,
         W.print_error_user_friendly, W.print_user, W.print_debug,
         W.MAX_WAIT_TIME_MINUTES, W.POLL_INTERVAL) = saved

    print("Локальный срок вышел, задача у провайдера ещё идёт")
    check("работа считается незавершённой", ok, False)
    check("оплаченный заказ НЕ отменён", cancels, [])

    joined = "\n".join(said)
    check("напечатан машинный маркер с номером заказа",
          "__VGC_REMOTE_PENDING__|task-777" in joined, True)
    check("и человеку сказано, что заказ жив",
          "не отменён" in joined or "остался жив" in joined, True)
    check("номер заказа виден в тексте", "task-777" in joined, True)

    # Сама отмена никуда не делась: она остаётся для явного решения человека.
    check("функция отмены по-прежнему существует", callable(W.cancel_task), True)

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
