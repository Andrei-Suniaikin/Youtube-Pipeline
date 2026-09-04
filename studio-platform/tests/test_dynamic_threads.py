#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Потоки делятся по фактической нагрузке, а не по настроенным слотам.

Раньше при четырёх настроенных слотах одинокая задача получала четверть бюджета,
и ключ простаивал на три четверти. Проверяется и обратное: перебрать бюджет
одновременными задачами нельзя.

Кабинет здесь настоящий — модуль импортируется целиком, подменяются только те три
функции, что описывают лимиты провайдера.

    .venv/bin/python tests/test_dynamic_threads.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("VS_SERVER_DISABLE_BACKGROUND", "1")
os.environ.setdefault("VS_SERVER_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
os.environ.setdefault("VS_TENANTS_DIR", os.path.join(BASE, "dev", "data", "tenants"))
# Кабинет тянет движки по зашитому пути ~/VideoStudio/src — тот же изолированный HOME,
# что и у dev/run.sh. Без него импорт падает на googler_worker.
os.environ.setdefault("HOME", os.path.join(BASE, "dev", "home"))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

import main as M                                                  # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def setup(active: int, budget: int = 24, slots: int = 4, capacity: int = 24):
    """Провайдер с известными лимитами и заданным числом идущих задач."""
    M._ACTIVE_JOBS_CACHE.clear()
    M._provider_active_jobs = lambda prov: active
    M._provider_call_budget = lambda prov: budget
    M._provider_job_slots = lambda prov: slots
    M._provider_slot_capacity_units = lambda prov: capacity
    M._provider_limit_opt = lambda prov, name: None


def threads(active, hard=24, default=24, params=None):
    setup(active)
    return M._server_threads_setting("recrafter", default, hard, params)


def main() -> int:
    saved = (M._provider_active_jobs, M._provider_call_budget, M._provider_job_slots,
             M._provider_slot_capacity_units, M._provider_limit_opt)
    try:
        print("Одинокая задача берёт весь бюджет ключа")
        check("ничего не идёт — 24 из 24", threads(active=0), 24)

        print("\nЗадачи делят бюджет между собой")
        check("одна уже идёт, нас двое — по 12", threads(active=1), 12)
        check("две идут, нас трое — по 8", threads(active=2), 8)
        check("три идут, нас четверо — по 6", threads(active=3), 6)

        print("\nБольше настроенных слотов делителем не станет")
        check("девять идут, но слотов четыре — делим на четыре",
              threads(active=9), 6)

        print("\nПотолок на задачу выше бюджета не поднимает")
        setup(active=0, budget=8)
        check("бюджет 8 при потолке 24 — восемь",
              M._server_threads_setting("recrafter", 24, 24), 8)

        print("\nБюджет не делится в ноль")
        setup(active=3, budget=2, slots=4)
        check("два вызова на четверых — по одному, а не ноль",
              M._server_threads_setting("recrafter", 24, 24), 1)

        print("\nРешение принимается один раз и не меняется")
        params = {}
        setup(active=0)
        first = M._server_threads_setting("recrafter", 24, 24, params)
        check("первый расчёт", first, 24)
        check("записан в снимок задачи",
              params[M._GRANTED_THREADS_KEY]["recrafter"], 24)
        # Между выдачей слота и сборкой команды другая задача успела начаться.
        setup(active=3)
        second = M._server_threads_setting("recrafter", 24, 24, params)
        check("второй вызов вернул ТО ЖЕ, а не пересчитал", second, 24)
        check("иначе воркер получил бы больше, чем зарезервировано", second, first)

        print("\n  без снимка задачи расчёт свежий каждый раз")
        setup(active=0)
        check("одна", M._server_threads_setting("recrafter", 24, 24), 24)
        setup(active=3)
        check("четверо", M._server_threads_setting("recrafter", 24, 24), 6)
    finally:
        (M._provider_active_jobs, M._provider_call_budget, M._provider_job_slots,
         M._provider_slot_capacity_units, M._provider_limit_opt) = saved

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
