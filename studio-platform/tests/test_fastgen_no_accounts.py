#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastGen «No accounts available» — не израсходованный заказ, а простой провайдера.

03.10.2026, бой: FastGen часами отвечал на генерации «No accounts available, please try
again later». Студия узнавала простой и повторяла задачу через 10 минут, но воркер на
повторе отказывался заказывать: «блок заказан уже 4 раза, годного звука нет — новый
заказ не создаю». Предел MAX_ORDERS_PER_BLOCK рассчитан на брак звука («проверьте текст
блока»), а провайдер-без-аккаунтов шёл в тот же счёт — и автоповтор был пустым.

Проверяется на поддельном FastGen: отказ «нет аккаунтов» счёт не трогает, новые заказы в
этом запуске не шлёт и выходит словами FastGen (по ним студия повторяет); сколько угодно
повторов той же задачи доходят до заказа; а настоящий брак звука по-прежнему упирается в
предел.

    .venv/bin/python tests/test_fastgen_no_accounts.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
os.environ["VS_JOB_ID"] = "777"            # одна и та же задача студии на всех повторах

spec = importlib.util.spec_from_file_location(
    "fastgen_tts_worker", os.path.join(BASE, "workers", "fastgen_tts_worker.py"))
W = importlib.util.module_from_spec(spec)
sys.modules["fastgen_tts_worker"] = W        # dataclass ищет свой модуль в sys.modules
spec.loader.exec_module(W)

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class FakeFastGen:
    def __init__(self, fail_error):
        self.fail_error = fail_error
        self.posts = 0

    def __call__(self, key, method, path, body=None, timeout=60):
        if method == "POST" and path == "/generations":
            self.posts += 1
            return {"id": f"gen_{self.posts}", "status": "queued"}
        if method == "GET" and path.startswith("/generations/"):
            return {"id": path.rsplit("/", 1)[1], "status": "failed", "error": self.fail_error}
        raise AssertionError(f"неожиданный вызов {method} {path}")


W.check_key = lambda key, model, voice: "ключ проверен"
W.say = lambda msg, kind="INFO": None
TMP = tempfile.mkdtemp(prefix="fastgen-")
NO_ACCOUNTS = "No accounts available, please try again later"


def block():
    text = "Это текст блока для озвучки. " * 10
    b = W.Block(idx=1, text=text, path=os.path.join(TMP, "b1.mp3"),
                ident=W.identity(text, "algieba", "m", ""))
    b.orders = W.orders_so_far(b)
    return b


def run_once(fake):
    """Один запуск той же задачи студии (повтор после простоя)."""
    W._api = fake
    job = W.Job("k", "m", "algieba", "")
    b = block()
    try:
        W.process_block(job, b, TMP)
        return job, b, None
    except Exception as e:                                         # noqa: BLE001
        return job, b, e


def main() -> int:
    print("Нет свободных аккаунтов — простой провайдера, а не брак звука")
    fake = FakeFastGen(NO_ACCOUNTS)
    job, b, err = run_once(fake)
    check("заказ ушёл один раз — второй в этом же запуске не шлём", fake.posts, 1)
    check("счёт заказов блока не вырос", b.orders, 0)
    check("и в квитанции тоже 0", W.read_receipt(b.path).get("orders"), 0)
    check("рубильник запуска опущен — соседние блоки не заказываются", job.service_down.is_set(), True)
    check("задача выходит словами FastGen — по ним студия узнаёт простой",
          NO_ACCOUNTS in str(err), True)

    print("\n  студия повторяет ту же задачу снова и снова — каждый раз доходит до заказа")
    for i in range(6):
        _job, b, err = run_once(fake)
    check("шесть повторов — шесть заказов, «заказан уже 4 раза» не наступило",
          fake.posts, 7)
    check("и это был именно отказ провайдера, а не предел",
          isinstance(err, W.OrderCap), False)

    print("\nНастоящий брак звука по-прежнему упирается в предел")
    os.remove(W.receipt_path(os.path.join(TMP, "b1.mp3")))
    bad = FakeFastGen("content policy violation")
    errs = []
    for i in range(4):
        _job, b, err = run_once(bad)
        errs.append(err)
    check("предел MAX_ORDERS_PER_BLOCK на браке соблюдён",
          bad.posts, W.MAX_ORDERS_PER_BLOCK)
    check("и дальше — «заказан уже N раз», без нового заказа",
          any(isinstance(e, W.OrderCap) or "заказан уже" in str(e) for e in errs), True)

    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
