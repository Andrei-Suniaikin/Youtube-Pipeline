#!/usr/bin/env python3
"""Сквозная проверка: движок ↔ кабинет ↔ журнал платных вызовов.

Требует поднятого кабинета (dev/run.sh). Провайдеров не трогает: движок здесь
изображается клиентом call_journal, а «вызов к провайдеру» — заглушкой.

    .venv/bin/python tests/test_calls_api.py
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "workers"))

from call_journal import CallJournal  # noqa: E402

DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
URL = os.environ.get("VS_CALLS_URL", "http://127.0.0.1:8080")
PROJECT = "proj-dev-0001"

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def db():
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def make_running_job(kind="images") -> tuple:
    """Живая задача в очереди с токеном — от её имени и пишет движок."""
    token = secrets.token_hex(16)
    c = db()
    try:
        cur = c.execute(
            "INSERT INTO queue_tasks(tenant_id,project_id,kind,status,created,params,uid,"
            "claim_token,provider) VALUES('dev',?,?,'running',?,'{}',1,?,'recrafter')",
            (PROJECT, kind, time.time(), token))
        c.commit()
        return cur.lastrowid, token
    finally:
        c.close()


def cleanup(jid):
    c = db()
    try:
        c.execute("DELETE FROM queue_tasks WHERE job_id=?", (jid,))
        c.execute("DELETE FROM provider_calls WHERE project_id=? AND unit LIKE 'test-%'",
                  (PROJECT,))
        c.commit()
    finally:
        c.close()


def main() -> int:
    jid, token = make_running_job()
    j = CallJournal(base=URL, job_id=jid, job_token=token, log=lambda m: None)
    check("журнал включён", j.enabled, True)

    unit = "test-001"
    fp = j.fingerprint(model="NARWHAL", prompt="маяк в тумане", seed=11)

    print("\nПервый прогон кадра")
    d = j.reserve(unit=unit, fingerprint=fp, kind="images",
                  provider="recrafter", model="NARWHAL")
    check("вызов разрешён", d.action, "new")
    check("можно обращаться к провайдеру", d.may_call, True)
    check("отчёт о заказе принят", j.ordered(d.call_id, "op-test-1"), True)
    check("отчёт о результате принят",
          j.delivered(d.call_id, "googler/test-001.jpg", cost=4.0), True)

    print("\nПовтор того же кадра — платить нельзя")
    again = j.reserve(unit=unit, fingerprint=fp, kind="images")
    check("решение skip", again.action, "skip")
    check("обращаться к провайдеру нельзя", again.may_call, False)
    check("отдан готовый файл", again.result_path, "googler/test-001.jpg")

    print("\nОбрыв после создания заказа")
    unit2, fp2 = "test-002", j.fingerprint(model="NARWHAL", prompt="лодка", seed=12)
    d2 = j.reserve(unit=unit2, fingerprint=fp2, kind="images")
    j.ordered(d2.call_id, "op-test-2")
    j.failed(d2.call_id, "read timeout", ordered=True)
    after = j.reserve(unit=unit2, fingerprint=fp2, kind="images")
    check("решение verify", after.action, "verify")
    check("повторно платить нельзя", after.may_call, False)
    check("идентификатор заказа вернулся", after.remote_order_id, "op-test-2")

    print("\nОтказ 429 до создания заказа")
    unit3, fp3 = "test-003", j.fingerprint(model="NARWHAL", prompt="дом", seed=13)
    d3 = j.reserve(unit=unit3, fingerprint=fp3, kind="images")
    j.failed(d3.call_id, "429 too many requests", ordered=False)
    retry = j.reserve(unit=unit3, fingerprint=fp3, kind="images")
    check("повтор разрешён", retry.action, "new")
    check("попытка посчитана", retry.attempts, 2)

    print("\nЗащита от чужого доступа")
    bad = CallJournal(base=URL, job_id=jid, job_token="не тот токен", log=lambda m: None)
    check("чужой токен не пускает", bad.reserve(unit="test-x", fingerprint=fp).action,
          "unknown")
    ghost = CallJournal(base=URL, job_id=999999, job_token=token, log=lambda m: None)
    check("несуществующая задача не пускает",
          ghost.reserve(unit="test-x", fingerprint=fp).action, "unknown")

    print("\nЖурнал не настроен — движок работает как раньше")
    off = CallJournal(base="", job_id="", job_token="")
    d_off = off.reserve(unit="test-y", fingerprint=fp)
    check("решение off", d_off.action, "off")
    check("обращаться к провайдеру можно", d_off.may_call, True)

    print("\nКабинет недоступен — платить не разрешаем")
    down = CallJournal(base="http://127.0.0.1:1", job_id=jid, job_token=token,
                       log=lambda m: None)
    d_down = down.reserve(unit="test-z", fingerprint=fp)
    check("решение unknown", d_down.action, "unknown")
    check("обращаться к провайдеру НЕЛЬЗЯ", d_down.may_call, False)

    cleanup(jid)
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
