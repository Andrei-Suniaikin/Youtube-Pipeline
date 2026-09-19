#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Журнал платных вызовов помнит, КАКИМ ключом начат заказ.

ДЕФЕКТ. В `provider_calls` есть провайдер и модель, но нет ничего про ключ. Из-за
этого журнал не может ответить на два вопроса, ради которых его и заводили:
«сколько этот ключ уже съел» и «тем ли ключом мы продолжаем начатый заказ».

Второй вопрос — про деньги. Идентификатор заказа выдан аккаунтом ключа A. Если
через час тот же кадр подхватит ключ B (ключей в пуле несколько, и раздаёт их
round-robin), провайдер ответит «нет такого заказа» — а «нет» неотличимо от «не
создавался». Следующий шаг — второй оплаченный заказ за уже оплаченный кадр.

Что проверяется: колонка заводится и на старых базах; отпечаток закрепляется и
это ХЭШ, а не ключ; чужой ключ не может ни продолжить заказ, ни закрепиться за ним;
после честного отказа (денег не было) новый ключ разрешён; кабинет умеет сказать
журналу, какой ключ он выдал задаче.

    .venv/bin/python tests/test_call_credential_id.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "shared"))
sys.path.insert(0, os.path.join(BASE, "server"))

FAILS = []
TMP = tempfile.mkdtemp(prefix="b8-call-cred-")

KEY_A = "sk-journal-AAAAAAAAAAAAAAAAAAAA"
KEY_B = "sk-journal-BBBBBBBBBBBBBBBBBBBB"

LOCKS = os.path.join(TMP, "locks")
os.makedirs(LOCKS, mode=0o700, exist_ok=True)
os.chmod(LOCKS, 0o700)
os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"] = LOCKS
os.environ["HOME"] = os.path.join(BASE, "dev", "home")
os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
os.environ["VS_SERVER_DB"] = os.path.join(TMP, "vsgen.copy.db")
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
DEV_DB = os.path.join(BASE, "dev", "vsgen.dev.db")
if os.path.exists(DEV_DB):
    shutil.copy(DEV_DB, os.environ["VS_SERVER_DB"])

import credential_budget as budget                                  # noqa: E402
import pub_calls as pc                                              # noqa: E402

ID_A = budget.credential_identity("openai", KEY_A)
ID_B = budget.credential_identity("openai", KEY_B)

# Схема ДО этой правки — ровно как она была в базе стенда.
OLD_SCHEMA = (
    "CREATE TABLE provider_calls("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, "
    "project_id TEXT NOT NULL, release_id TEXT NOT NULL DEFAULT '', "
    "kind TEXT NOT NULL, unit TEXT NOT NULL, fingerprint TEXT NOT NULL, "
    "provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', "
    "remote_order_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, "
    "result_path TEXT NOT NULL DEFAULT '', cost REAL, "
    "attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', "
    "created REAL NOT NULL, updated REAL NOT NULL, "
    "UNIQUE(project_id, kind, unit, fingerprint))"
)


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def reserve(c, unit, credential_id="", fp="fp-1"):
    return pc.reserve(c, tenant_id="t", project_id="p", kind="images", unit=unit,
                      fp=fp, provider="recrafter", credential_id=credential_id)


def main_test() -> int:
    print("Старая база доезжает: колонка добавляется, строки на месте")
    old = fresh_db()
    old.execute(OLD_SCHEMA)
    old.execute("INSERT INTO provider_calls(tenant_id,project_id,kind,unit,fingerprint,"
                "status,created,updated) VALUES('t','p','images','7','fp-old',"
                "'ordered',1.0,1.0)")
    pc.ensure_calls_schema(old)
    cols = [r[1] for r in old.execute("PRAGMA table_info(provider_calls)")]
    check("колонка отпечатка появилась", "credential_id" in cols, True)
    check("старая строка не потерялась",
          old.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0], 1)
    was = old.execute("SELECT credential_id FROM provider_calls").fetchone()[0]
    check("у старой строки ключ честно неизвестен", was, "")
    pc.ensure_calls_schema(old)                    # второй раз — не падает
    check("миграция идемпотентна",
          [r[1] for r in old.execute("PRAGMA table_info(provider_calls)")], cols)
    # На строке без отпечатка правило не применяется: придумывать за неё нечего.
    row = old.execute("SELECT * FROM provider_calls").fetchone()
    check("старая строка не объявляется чужой", pc.credential_conflict(row, ID_A), False)

    print("\nОтпечаток закрепляется за строкой — и это хэш, а не ключ")
    c = fresh_db()
    pc.ensure_calls_schema(c)
    first = reserve(c, "1", ID_A)
    check("первый заказ разрешён", first["action"], "new")
    check("ключ записан", first["row"]["credential_id"], ID_A)
    dump = "".join(str(v) for v in c.execute(
        "SELECT * FROM provider_calls").fetchone())
    check("самого ключа в базе нет", KEY_A in dump, False)

    print("\nПродолжать начатый заказ можно только ключом с тем же отпечатком")
    pc.mark_ordered(c, first["call_id"], "order-777", credential_id=ID_A)
    same = reserve(c, "1", ID_A)
    check("своим ключом — «спроси про заказ»", same["action"], "verify")
    check("и это не конфликт", same["credential_mismatch"], False)
    other = reserve(c, "1", ID_B)
    check("чужим ключом — тоже НИКОГДА не «новый заказ»", other["action"], "verify")
    check("но конфликт назван", other["credential_mismatch"], True)
    check("и отпечаток заказа не перезаписан чужим",
          other["row"]["credential_id"], ID_A)
    try:
        pc.assert_same_credential(other["row"], ID_B)
        got = "пустил"
    except pc.CredentialMismatch as exc:
        got = "остановил" if "…" in str(exc) or "нельзя" in str(exc) else "странно"
    check("жёсткая форма правила останавливает доплату", got, "остановил")
    check("и в тексте отказа нет ключа", KEY_B in str(other), False)

    print("\nЗакрепить за начатым заказом ВТОРОЙ ключ нельзя")
    try:
        pc.mark_ordered(c, first["call_id"], "order-888", credential_id=ID_B)
        got = "пустил"
    except pc.CredentialMismatch:
        got = "остановил"
    check("mark_ordered чужим ключом — ошибка программиста, слышная сразу", got,
          "остановил")
    check("заказ остался прежним",
          c.execute("SELECT remote_order_id FROM provider_calls WHERE id=?",
                    (first["call_id"],)).fetchone()[0], "order-777")

    print("\nОтказ ДО заказа денег не стоил — новая попытка вправе идти другим ключом")
    c2 = fresh_db()
    pc.ensure_calls_schema(c2)
    one = reserve(c2, "2", ID_A)
    pc.mark_failed(c2, one["call_id"], "HTTP 401 — ключ отозван", ordered=False)
    retry = reserve(c2, "2", ID_B)
    check("повтор разрешён", retry["action"], "new")
    check("и ключ перезакреплён на тот, которым пошли", retry["row"]["credential_id"],
          ID_B)
    check("конфликтом это не считается", retry["credential_mismatch"], False)
    # «Не сказали, каким ключом» — не то же самое, что «ключ сменился».
    quiet = reserve(c2, "2", "")
    check("молчание вызывающего старый отпечаток не стирает",
          quiet["row"]["credential_id"], ID_B)

    print("\nКто говорит журналу отпечаток: кабинет, по номеру задачи")
    check("без кабинета журнал честно не знает", pc.job_credential(4242), "")
    import main                                                     # noqa: E402
    identity = main._remember_job_credential(4242, "prompt_openai", KEY_A)
    check("кабинет считает тот же отпечаток, что и общий счёт", identity, ID_A)
    check("и журнал получает его по номеру задачи", pc.job_credential(4242), ID_A)
    check("у семейства вне общего счёта отпечатка нет (и это не ошибка)",
          main._remember_job_credential(4243, "lumean", KEY_A), "")
    main._forget_job_credential(4242)
    check("задача кончилась — отпечаток забыт", pc.job_credential(4242), "")

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_test())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
