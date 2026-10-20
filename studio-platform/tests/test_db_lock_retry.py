#!/usr/bin/env python3
"""Запись в журнал платных вызовов не теряется, когда база занята.

19.10.2026 в бою пропали 23 отметки «результат получен» и 3 «заказ создан»: SQLite
ответил `database is locked`, движок получил 500, деньги остались потраченными, а строка
журнала — в `ordered`. Здесь проверяем починку на НАСТОЯЩЕМ замке, а не на подделанном
исключении: второе соединение берёт BEGIN IMMEDIATE и отпускает его из таймера.

    .venv/bin/python tests/test_db_lock_retry.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time

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


def fresh_file():
    path = os.path.join(tempfile.mkdtemp(prefix="calls-lock-"), "vsgen.db")
    c = sqlite3.connect(path)
    pc.ensure_calls_schema(c)
    c.commit()
    c.close()
    return path


def opener(path, *, timeout: float = 0.2):
    """Соединение с КОРОТКИМ ожиданием: иначе замок съест сам sqlite и повтор не проверится."""
    def _open():
        c = sqlite3.connect(path, timeout=timeout)
        c.row_factory = sqlite3.Row
        return c
    return _open


def hold_write_lock(path, seconds: float):
    """Держать замок записи `seconds`, потом отпустить. Возвращает поток."""
    ready = threading.Event()

    def _hold():
        c = sqlite3.connect(path, timeout=30)
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE provider_calls SET unit=unit WHERE 0")
        ready.set()
        time.sleep(seconds)
        c.rollback()
        c.close()

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    ready.wait(5)
    return t


ARGS = dict(tenant_id="dev", project_id="p1", kind="text", unit="ru")


def test_lock_is_recognised():
    print("\nОтличаем занятую базу от всего остального")
    check("database is locked", pc.is_locked(sqlite3.OperationalError("database is locked")), True)
    check("ЗАГЛАВНЫМИ тоже", pc.is_locked(sqlite3.OperationalError("DATABASE IS LOCKED")), True)
    check("нет такой колонки — не замок",
          pc.is_locked(sqlite3.OperationalError("no such column: cost")), False)
    check("чужой тип исключения — не замок", pc.is_locked(ValueError("database is locked")), False)


def test_delivered_survives_lock():
    print("\nОтметка «результат получен» ложится, хотя база была занята")
    path = fresh_file()
    db = opener(path)
    c = db()
    row = pc.reserve(c, fp="fp-1", provider="lumean", **ARGS)
    c.commit()
    call_id = row["call_id"]
    c.execute("UPDATE provider_calls SET status=? WHERE id=?", (pc.ORDERED, call_id))
    c.commit()
    c.close()

    hold_write_lock(path, 1.2)           # замок отпустят на второй-третьей попытке
    said = []
    pc.write_retry(db, lambda conn: pc.mark_delivered(conn, call_id, "/out/text.txt"),
                   label="результат", log=said.append)

    c = db()
    got = c.execute("SELECT status, result_path FROM provider_calls WHERE id=?",
                    (call_id,)).fetchone()
    c.close()
    check("статус", got["status"], pc.DELIVERED)
    check("путь к результату", got["result_path"], "/out/text.txt")
    check("повтор был виден в журнале", bool(said), True)


def test_real_error_is_not_retried():
    print("\nНе-замок повторять нельзя: отказ должен прийти сразу")
    path = fresh_file()
    db = opener(path)
    started = time.time()
    try:
        pc.write_retry(db, lambda conn: conn.execute("SELECT нет_такой_таблицы"),
                       label="кривой запрос")
        got = "без исключения"
    except sqlite3.OperationalError as exc:
        got = "ошибка" if "нет_такой_таблицы" in str(exc) or "no such" in str(exc) else str(exc)
    check("ошибка отдана как есть", got, "ошибка")
    check("без пауз на повторы", time.time() - started < 1.0, True)


def test_retry_here_keeps_work():
    print("\nНа чужом соединении запись тоже дожимается (так пишет кабинет)")
    path = fresh_file()
    db = opener(path)
    c = db()
    row = pc.reserve(c, fp="fp-2", provider="lumean", **ARGS)
    c.commit()
    call_id = row["call_id"]
    c.execute("UPDATE provider_calls SET status=? WHERE id=?", (pc.ORDERED, call_id))
    c.commit()

    hold_write_lock(path, 1.2)
    said = []
    got_id = pc.retry_here(c, lambda conn: (
        pc.mark_delivered(conn, call_id, "/out/again.txt") or call_id),
        label="текст ru", log=said.append)
    c.close()

    c = db()
    got = c.execute("SELECT status, result_path FROM provider_calls WHERE id=?",
                    (call_id,)).fetchone()
    c.close()
    check("вернулся id вызова", got_id, call_id)
    check("статус", got["status"], pc.DELIVERED)
    check("путь к результату", got["result_path"], "/out/again.txt")
    check("повтор был виден в журнале", bool(said), True)


def main():
    test_lock_is_recognised()
    test_delivered_survives_lock()
    test_real_error_is_not_retried()
    test_retry_here_keeps_work()
    print()
    if FAILS:
        print(f"ПРОВАЛОВ: {len(FAILS)} — {', '.join(FAILS)}")
        return 1
    print("всё сошлось")
    return 0


if __name__ == "__main__":
    sys.exit(main())
