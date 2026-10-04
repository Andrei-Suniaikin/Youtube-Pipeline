#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Короткий вызов Codex находит место, даже когда идут длинные рерайты (04.10.2026).

Что случилось. Места подписки Codex (4 на учётку) держали куски длинных рерайтов.
Освободившееся место тут же перехватывал соседний кусок, а название ролика и сверка
обложки (chat-once, вызов без задачи) ждали до пяти минут и падали. Готовые ролики
стояли перед заливкой без названия.

Теперь вызов изнутри задачи рерайта занимает место, только если после него останется
свободным ещё одно (REWRITE_SHORT_CALL_RESERVE=1). Короткий вызов без задачи берёт
любое свободное место. Резерв всегда меньше потолка хотя бы на одно место — иначе
рерайт встал бы совсем.

Проверяются сам захват «оставив свободными» (shared/credential_budget.py) и решение
рерайта, сколько оставлять (rewrite_core.short_call_reserve).

    .venv/bin/python tests/test_codex_short_reserve.py
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
LOCKS = tempfile.mkdtemp(prefix="codex-reserve-")
os.chmod(LOCKS, 0o700)
os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"] = LOCKS
os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_CODEX"] = "4"
os.environ.setdefault("VIDEO_STUDIO_SHARED_DIR", os.path.join(BASE, "shared"))
sys.path.insert(0, os.path.join(BASE, "rewrite"))
sys.path.insert(0, os.path.join(BASE, "shared"))

import credential_budget as CB                                      # noqa: E402
import rewrite_core as R                                            # noqa: E402

FAILS = []
ACCOUNT = "{\"tokens\": {\"account_id\": \"acc-reserve-test-0001\"}}"   # как auth.json дома Codex


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def used():
    return (CB.usage("codex", ACCOUNT) or {}).get("used")


def main() -> int:
    print("Захват «оставив одно место свободным», потолок 4")
    long_ = [CB.try_acquire_leaving("codex", ACCOUNT, 1) for _ in range(3)]
    check("три длинных вызова получили места", all(x is not None for x in long_), True)
    check("занято ровно три: лишние места для подсчёта отданы", used(), 3)
    check("четвёртый длинный — нет: последнее место оставлено", CB.try_acquire_leaving("codex", ACCOUNT, 1), None)
    short = CB.try_acquire("codex", ACCOUNT, 1)
    check("короткий вызов берёт оставленное место", short is not None, True)
    check("занято четыре из четырёх", used(), 4)
    short.release()
    long_[0].release()
    again = CB.try_acquire_leaving("codex", ACCOUNT, 1)
    check("освободилось — длинный снова может, если остаётся запас", again is not None, True)
    check("и снова не больше трёх у длинных", CB.try_acquire_leaving("codex", ACCOUNT, 1), None)
    for x in long_[1:] + [again]:
        x.release()
    check("всё отпущено", used(), 0)
    check("резерв не меньше потолка — длинному места нет", CB.try_acquire_leaving("codex", ACCOUNT, 4), None)
    zero = CB.try_acquire_leaving("codex", ACCOUNT, 0)
    check("без резерва — обычный захват", zero is not None and used() == 1, True)
    zero.release()

    print("\nСколько оставляет рерайт")
    check("Codex внутри задачи — одно место", R.short_call_reserve("codex", "job-1"), 1)
    check("Codex без задачи (название, сверка) — ноль", R.short_call_reserve("codex", ""), 0)
    check("другие подписки не трогаем", R.short_call_reserve("claude_oauth", "job-1"), 0)
    saved = CB.CAPACITIES["codex"]
    try:
        CB.CAPACITIES["codex"] = 1
        check("потолок 1 — резерва нет, иначе рерайт встал бы", R.short_call_reserve("codex", "job-1"), 0)
        CB.CAPACITIES["codex"] = 2
        R.SHORT_CALL_RESERVE, was = 5, R.SHORT_CALL_RESERVE
        check("резерв режется до «потолок минус один»", R.short_call_reserve("codex", "job-1"), 1)
        R.SHORT_CALL_RESERVE = was
    finally:
        CB.CAPACITIES["codex"] = saved

    print("\nНастоящий пул ключей рерайта: задача против короткого вызова")
    import threading
    pool = R.ServerCredentialPool("codex", [ACCOUNT], per_credential_limit=10)
    held, outcome = [], {}

    def as_job():
        ev = threading.Event()
        ev._rewrite_job_id = "job-reserve-test"
        R.STOP_FLAG.set_thread_stop_event(ev)
        try:
            for _ in range(3):
                held.append(pool.acquire(timeout=1.0))
            try:
                # Как в бою (timeout=300): чужие места ждут 2 с, потом «ждать в очереди».
                held.append(pool.acquire(timeout=5.0))
                outcome["fourth"] = "выдано"
            except R.CredentialPoolExhausted as exc:
                outcome["fourth"] = str(exc)
        finally:
            R.STOP_FLAG.clear_thread_stop_event()

    t = threading.Thread(target=as_job)
    t.start(); t.join(10)
    check("задача рерайта получила три места", len(held), 3)
    check("четвёртое задаче не дали — «ждать в очереди»",
          str(outcome.get("fourth", "")).startswith(R.HOST_BUDGET_WAIT_PREFIX), True)
    short = pool.acquire(timeout=1.0)
    check("короткий вызов без задачи получил место сразу", short is not None, True)
    check("занято четыре", used(), 4)
    for x in held + [short]:
        x.release()
    check("всё отпущено", used(), 0)

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
