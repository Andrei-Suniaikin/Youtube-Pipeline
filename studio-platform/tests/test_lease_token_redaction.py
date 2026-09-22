#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Токен лизы профиля не попадает в текст ошибки и в трассировку.

Лиза профиля берётся отдельной командой координатора, и токен владельца уезжает в неё
аргументом `--owner-token`. Текст исключения `subprocess` содержит ВЕСЬ argv — значит, и
токен. Эта строка шла в лог задачи и в сообщение пользователю. Перенос правки production
(6fcb8b5b, срез live_2026-09-18): оставляем только класс ошибки, а `from None` обрывает
цепочку, где тот же argv лежит в `__cause__`.

Порт production-теста `live_2026-09-18/код/заливка/tests/test_upload_profile_lease_redaction.py`:
подменяется только `subprocess.run`, сам обработчик — настоящий.

    .venv/bin/python tests/test_lease_token_redaction.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "upload"))

import upload_coordination as UC                                  # noqa: E402

SECRET = "SYNTHETIC_OWNER_TOKEN_MUST_NOT_APPEAR"
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def lease():
    obj = UC.ProfileLease("54", {
        "schema_version": 1, "coordinator_command": ["never-executed-coordinator"],
        "holder": "minsk", "ttl_seconds": 120,
        "renew_interval_seconds": 10, "command_timeout_seconds": 1,
    })
    obj.owner_token = SECRET
    return obj


def run_case(kind):
    obj = lease()
    for action in ("acquire", "renew", "release"):
        cmd = ["never-executed-coordinator", action, "--owner-token", SECRET]
        original = (kind(cmd, 1, output=SECRET, stderr=SECRET)
                    if kind is subprocess.TimeoutExpired
                    else kind(1, cmd, output=SECRET, stderr=SECRET))
        # Исходная ошибка ДЕЙСТВИТЕЛЬНО течёт — иначе тест проверял бы пустоту.
        check(f"{kind.__name__}/{action}: исходная ошибка содержит токен",
              SECRET in str(original), True)
        with mock.patch.object(UC.subprocess, "run", side_effect=original) as transport:
            try:
                obj._invoke(action)
                check(f"{kind.__name__}/{action}: ошибка проброшена", "не проброшена",
                      "ProfileLeaseError")
            except UC.ProfileLeaseError as error:
                rendered = "".join(traceback.format_exception(
                    type(error), error, error.__traceback__))
                check(f"{kind.__name__}/{action}: сообщение без токена",
                      str(error),
                      f"profile lease {action} transport failed ({kind.__name__})")
                check(f"{kind.__name__}/{action}: трассировка без токена",
                      SECRET in rendered, False)
                check(f"{kind.__name__}/{action}: и без имени аргумента",
                      "--owner-token" in rendered, False)
                check(f"{kind.__name__}/{action}: цепочка исключений оборвана",
                      (error.__cause__, error.__suppress_context__), (None, True))
        # Сам вызов при этом токен получить обязан: мы прячем его в тексте ошибки,
        # а не ломаем работу лизы.
        check(f"{kind.__name__}/{action}: команде токен всё-таки передан",
              SECRET in transport.call_args.args[0], True)


def main() -> int:
    print("Обрыв связи с координатором")
    run_case(subprocess.TimeoutExpired)
    print("\nКоординатор ответил ошибкой")
    run_case(subprocess.CalledProcessError)

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
