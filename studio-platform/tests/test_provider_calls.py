#!/usr/bin/env python3
"""Журнал платных вызовов: проверяем, что дубль оплатить нельзя.

Без сети и без кабинета — только таблица и правила поверх неё.

    .venv/bin/python tests/test_provider_calls.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_calls as pc  # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pc.ensure_calls_schema(c)
    return c


ARGS = dict(tenant_id="dev", project_id="p1", kind="images", unit="007")


def test_happy_path():
    print("\nОбычный путь: заявка → заказ → результат")
    c = fresh()
    fp = pc.fingerprint(model="NARWHAL", prompt="кот на крыше", seed=42)
    r = pc.reserve(c, fp=fp, provider="recrafter", model="NARWHAL", **ARGS)
    check("первая заявка разрешает вызов", r["action"], "new")
    pc.mark_ordered(c, r["call_id"], "order-777")
    pc.mark_delivered(c, r["call_id"], "googler/007.jpg", cost=4.0)

    again = pc.reserve(c, fp=fp, **ARGS)
    check("повтор того же входа НЕ разрешает вызов", again["action"], pc.SKIP)
    check("отдаёт готовый файл", again["row"]["result_path"], "googler/007.jpg")
    check("денег списано один раз", pc.project_summary(c, "p1")["cost"], 4.0)


def test_crash_between_reserve_and_order():
    print("\nСмерть процесса между заявкой и заказом — судьба заказа неизвестна")
    c = fresh()
    fp = pc.fingerprint(model="NARWHAL", prompt="кот", seed=1)
    pc.reserve(c, fp=fp, **ARGS)                      # процесс умер здесь
    after = pc.reserve(c, fp=fp, **ARGS)
    check("вторая попытка требует ПРОВЕРИТЬ, а не заказывать", after["action"], pc.VERIFY)
    check("строка осталась в reserved", after["status"], pc.RESERVED)


def test_crash_after_order():
    print("\nОбрыв после создания заказа — деньги, возможно, потрачены")
    c = fresh()
    fp = pc.fingerprint(model="NARWHAL", prompt="кот", seed=2)
    r = pc.reserve(c, fp=fp, **ARGS)
    pc.mark_ordered(c, r["call_id"], "order-42")
    pc.mark_failed(c, r["call_id"], "read timeout", ordered=True)

    after = pc.reserve(c, fp=fp, **ARGS)
    check("повтор запрещён, нужно проверить заказ", after["action"], pc.VERIFY)
    check("идентификатор заказа сохранён", after["row"]["remote_order_id"], "order-42")


def test_pre_order_refusal_allows_retry():
    print("\nОтказ ДО создания заказа (документированный 429) — повтор безопасен")
    c = fresh()
    fp = pc.fingerprint(model="NARWHAL", prompt="кот", seed=3)
    r = pc.reserve(c, fp=fp, **ARGS)
    pc.mark_failed(c, r["call_id"], "429 too many requests", ordered=False)

    after = pc.reserve(c, fp=fp, **ARGS)
    check("повтор разрешён", after["action"], "new")
    check("попытка посчитана", after["row"]["attempts"], 2)


def test_different_input_is_different_call():
    print("\nДругой вход — другой вызов, а не «уже оплачено»")
    c = fresh()
    a = pc.fingerprint(model="NARWHAL", prompt="кот", seed=1)
    b = pc.fingerprint(model="NARWHAL", prompt="кот", seed=2)
    check("разный seed даёт разный отпечаток", a != b, True)
    pc.reserve(c, fp=a, **ARGS)
    check("второй вход разрешён", pc.reserve(c, fp=b, **ARGS)["action"], "new")

    same = pc.fingerprint(seed=1, prompt="кот", model="NARWHAL")
    check("порядок полей не влияет на отпечаток", same, a)
    empty = pc.fingerprint(model="NARWHAL", prompt="кот", seed=1, refs=[])
    check("пустое поле не меняет отпечаток", empty, a)


def test_race_between_two_workers():
    print("\nГонка: два воркера заявляют один кадр одновременно")
    c = fresh()
    fp = pc.fingerprint(model="NARWHAL", prompt="кот", seed=9)
    first = pc.reserve(c, fp=fp, **ARGS)
    second = pc.reserve(c, fp=fp, **ARGS)
    check("первый получает разрешение", first["action"], "new")
    check("второй — требование проверить", second["action"], pc.VERIFY)
    check("строка одна", c.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0], 1)


def test_delivered_requires_file():
    print("\n«Готово» доказывается файлом, а не статусом")
    c = fresh()
    r = pc.reserve(c, fp=pc.fingerprint(model="X"), **ARGS)
    try:
        pc.mark_delivered(c, r["call_id"], "")
        check("пустой result_path отвергнут", "прошло", "ValueError")
    except ValueError:
        check("пустой result_path отвергнут", "ValueError", "ValueError")


def test_open_calls_after_restart():
    print("\nПосле рестарта видно, что осталось выяснить")
    c = fresh()
    a = pc.reserve(c, fp=pc.fingerprint(u=1), **ARGS)
    b = pc.reserve(c, fp=pc.fingerprint(u=2), **ARGS)
    d = pc.reserve(c, fp=pc.fingerprint(u=3), **ARGS)
    pc.mark_ordered(c, b["call_id"], "order-b")
    pc.mark_delivered(c, d["call_id"], "googler/003.jpg", cost=1.0)
    open_ids = [r["id"] for r in pc.open_calls(c, "p1")]
    check("незакрытых два: заявленный и заказанный",
          open_ids, [a["call_id"], b["call_id"]])


if __name__ == "__main__":
    for fn in (test_happy_path, test_crash_between_reserve_and_order,
               test_crash_after_order, test_pre_order_refusal_allows_retry,
               test_different_input_is_different_call, test_race_between_two_workers,
               test_delivered_requires_file, test_open_calls_after_restart):
        fn()
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        raise SystemExit(1)
    print("все проверки прошли")
