#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Соединения к SQLite закрываются явно, а не «когда-нибудь соберёт сборщик».

21.09.2026 стенд владельца умер за ночь на ровном месте:

    OSError: [Errno 24] Too many open files
    sqlite3.OperationalError: unable to open database file

`lsof` показал 12 599 открытых файлов у одного процесса, из них **5 536 копий
`rewrite/sessions.db`** и 729 копий `cover/cover_job_queue.db`. Причина — идиома,
встречавшаяся в коде соседей 58 раз:

    with sqlite3.connect(path) as conn:      # ← НЕ закрывает соединение
        ...

Контекст у `sqlite3.Connection` делает commit/rollback и на этом заканчивается: закрыть
соединение он не обязан. Обычно спасает подсчёт ссылок, но connection попадает в
циклическую ссылку (кадр стека ↔ исключение ↔ traceback), а циклы разбирает только полная
сборка мусора — в долгоживущем сервере она случается редко. Замер это подтвердил: 300
вызовов оставляли 167 ручек, а если исключение сохранялось в переменную — все 300.
`gc.collect()` освобождал их до нуля, то есть это не «течёт навсегда», а «копится
быстрее, чем убирается».

Ловушка коварна тем, что симптом врёт дважды: «unable to open database file» выглядит как
пропавшая база, а не как кончившиеся ручки; и чем хуже дела, тем быстрее деградация —
каждая неудачная попытка открыть базу сама оставляет ещё одну ручку.

Тест сторожит две вещи:
  1. поведение — ручки не копятся при отключённом сборщике мусора;
  2. структуру — идиома не вернулась ни в одном файле соседей.

    .venv/bin/python tests/test_sqlite_fd_leak.py
"""
from __future__ import annotations

import gc
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILS: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'ПРОВАЛ'} {name}" + ("" if ok else f"  получено {got!r}, ждали {want!r}"))
    if not ok:
        FAILS.append(name)


def open_fds() -> int:
    """Сколько файловых ручек держит процесс. /dev/fd есть и на macOS, и на Linux."""
    return len(os.listdir("/dev/fd"))


def test_handles_do_not_pile_up() -> None:
    """Главная проверка: без сборщика мусора ручки не должны копиться.

    Сборщик выключен намеренно. С ним тест зелёный и при сломанном коде — он-то и
    маскировал дефект в бою, пока нагрузка была маленькой.
    """
    print("\nРучки не копятся (сборщик мусора выключен)")
    from rewrite import auth as rewrite_auth

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "sessions.db")
        mgr = rewrite_auth.LicenseManager(db)

        gc.disable()
        try:
            mgr.has_users()                      # прогрев: ленивые импорты и WAL-файлы
            before = open_fds()
            for _ in range(300):
                mgr.has_users()                  # обычное чтение
                mgr.validate("нет-такой-сессии") # и чтение, не нашедшее строку
            grown = open_fds() - before
        finally:
            gc.enable()

        # 600 вызовов на сломанном коде дают сотни ручек; на починенном — ноль.
        # Порог 20 оставлен на служебные файлы самого интерпретатора.
        check("прирост ручек за 600 обращений к базе < 20", grown < 20, True)
        print(f"        (фактический прирост: {grown})")


def test_idiom_is_gone() -> None:
    """Структурная проверка: идиома не вернулась ни в одном файле соседей."""
    print("\nИдиома `with sqlite3.connect(...) as conn:` нигде не осталась")
    bad: list[str] = []
    pat = re.compile(r'^\s*with\s+(sqlite3\.connect|_conn)\([^)]*\)\s+as\s+\w+\s*:')
    for service in ("rewrite", "cover", "web", "server"):
        folder = os.path.join(ROOT, service)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8") as fh:
                for num, line in enumerate(fh, 1):
                    if pat.match(line):
                        bad.append(f"{service}/{name}:{num}")
    check("мест без closing()", bad, [])
    if bad:
        for b in bad:
            print("      -", b)


def main() -> int:
    print("Проверка: соединения к SQLite закрываются явно")
    test_handles_do_not_pile_up()
    test_idiom_is_gone()

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
