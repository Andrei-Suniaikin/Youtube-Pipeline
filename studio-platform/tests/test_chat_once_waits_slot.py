#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Одноразовый вызов подписки ждёт свободного места, а не сдаётся за 2 секунды (04.10.2026).

После исправления номера профиля за 20 минут не появилось ни одного задания заливки:
ни одно название не написалось. Проба на сервере:

    статус 502 за 2.1 с | {"detail":"HOST_BUDGET_WAIT:общий лимит ключа занят; …"}

Общий предел подписки почти всегда занят шестью задачами рерайта; аренда ключа ждёт
места 2 с. Задачу рерайта такой отказ не роняет — её повторяет своя очередь. У `chat-once`
(названия роликов, сверка обложек) очереди нет: 502 сразу, и рерайт выигрывал почти
каждый раз.

Что проверяется:
  • «общий лимит занят» — ждём и повторяем, паузы растут, до срока;
  • любой другой отказ (лимит окна, модель) — сразу, без ожидания;
  • срок не превышен, после него — честный отказ;
  • ручка `/api/v1/chat-once` пользуется ожиданием и для Codex, и для Claude;
  • срок по умолчанию оставляет запас под сам вызов внутри таймаута названия (240 с).

    .venv/bin/python tests/test_chat_once_waits_slot.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "rewrite"))
sys.path.insert(0, os.path.join(BASE, "shared"))
sys.path.insert(0, os.path.join(BASE, "server"))
os.environ.pop("REWRITE_CHAT_ONCE_WAIT_SEC", None)

import api as A                                                   # noqa: E402

FAILS = []
BUSY = (False, A.HOST_BUDGET_WAIT_PREFIX + "общий лимит ключа занят; ожидание в очереди")


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def seq(items):
    items = list(items)
    calls = []

    def call():
        calls.append(1)
        return items.pop(0) if items else items_last
    items_last = items[-1]
    return call, calls


def main() -> int:
    print("Общий лимит занят — ждём места")
    clk = Clock()
    call, calls = seq([BUSY, BUSY, (True, "Tichá obloha")])
    got = A.wait_for_slot(call, wait_sec=150, sleep=clk.sleep, clock=clk.now)
    check("дождались ответа", got, (True, "Tichá obloha"))
    check("три попытки", len(calls), 3)
    check("паузы растут", clk.slept, [1.0, 2.0])

    print("\n  другой отказ — сразу")
    clk = Clock()
    call, calls = seq([(False, "SUBSCRIPTION_LIMIT: окно подписки исчерпано")])
    got = A.wait_for_slot(call, wait_sec=150, sleep=clk.sleep, clock=clk.now)
    check("отказ возвращён как есть", got[0], False)
    check("одна попытка, без пауз", (len(calls), clk.slept), (1, []))

    print("\n  место так и не освободилось — отказ после срока")
    clk = Clock()
    call, calls = seq([BUSY])
    got = A.wait_for_slot(call, wait_sec=30, sleep=clk.sleep, clock=clk.now)
    check("честный отказ «лимит занят»", got, BUSY)
    check("ждали не дольше срока", clk.t <= 30, True)
    check("и ждали, а не сдались сразу", clk.t >= 15, True)
    check("паузы не больше 8 с", max(clk.slept) <= 8.0, True)

    print("\nРучка chat-once ждёт места")
    router = A.build_api_router(queue=None, upload_dir=Path("/tmp"), output_dir=Path("/tmp"),
                                job_stop_events={}, job_stop_events_lock=threading.Lock())
    found = [r for r in router.routes if getattr(r, "path", "") == "/api/v1/chat-once"]
    check("маршрут есть", len(found), 1)
    handler = found[0].endpoint
    saved = (A.call_codex_subscription, A.call_claude_subscription, A.credential_source,
             time.sleep)
    try:
        time.sleep = lambda s: None
        A.credential_source = lambda provider, uid: "server"
        codex_calls = []
        answers = [BUSY, BUSY, (True, "Ticha Obloha: zvuky noci")]

        def fake_codex(**kw):
            codex_calls.append(kw.get("task_info"))
            return answers.pop(0)
        A.call_codex_subscription = fake_codex
        body = A.ApiChatOnce(provider="codex", model="gpt-5.6-sol",
                             messages=[{"role": "user", "content": "x"}])
        out = handler(body=body, user={"user_id": "u1"})
        check("Codex: ответ получен после двух «занято»", out, {"ok": True, "text": "Ticha Obloha: zvuky noci"})
        check("вызывали трижды", len(codex_calls), 3)

        claude_answers = [BUSY, (True, "ok")]
        A.call_claude_subscription = lambda *a, **k: claude_answers.pop(0)
        out = handler(body=A.ApiChatOnce(provider="claude", messages=[{"role": "user", "content": "x"}]),
                      user={"user_id": "u1"})
        check("Claude: тоже ждёт", out, {"ok": True, "text": "ok"})
    finally:
        (A.call_codex_subscription, A.call_claude_subscription, A.credential_source,
         time.sleep) = saved

    print("\nСрок по умолчанию")
    check("150 с", A.CHAT_ONCE_WAIT_SEC, 150.0)
    check("остаётся минимум минута на сам вызов внутри 240 с названия",
          A.CHAT_ONCE_WAIT_SEC <= 240 - 60, True)
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
