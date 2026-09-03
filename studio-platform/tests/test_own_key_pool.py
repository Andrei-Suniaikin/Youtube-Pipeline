#!/usr/bin/env python3
"""Свои ключи: раздаются ли задачи по нескольким ключам или садятся на первый.

Фиксирует ФАКТИЧЕСКОЕ поведение до правки, чтобы после неё было с чем сравнить.
Ни сети, ни провайдеров: проверяется только выбор credential.

    .venv/bin/python tests/test_own_key_pool.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

# HOME задаём ЯВНО и до импорта: main.py на строке 121 делает
# sys.path.insert(0, os.path.expanduser("~/VideoStudio/src")) прямо при импорте, и
# setdefault здесь бесполезен — HOME уже пришёл из оболочки.
os.environ["HOME"] = os.path.join(BASE, "dev", "home")
os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
os.environ.setdefault("VS_SERVER_DB", os.path.join(BASE, "dev", "cabinet.db"))
os.environ.setdefault("VS_TENANTS_DIR", os.path.join(BASE, "dev", "data", "tenants"))

import main  # noqa: E402


class FakeUser(dict):
    """users-строка в том виде, в каком её читает кабинет: sqlite3.Row-подобный доступ."""

    def keys(self):                      # noqa: A003 — имя как у sqlite3.Row
        return list(super().keys())


KEY_A = "own-key-AAAAAAAAAAAAAAAAAAAA"
KEY_B = "own-key-BBBBBBBBBBBBBBBBBBBB"
FAMILY = "googler"                       # семейство провайдера recrafter (картинки)
PROVIDER = "recrafter"


def user_with(*own_keys) -> FakeUser:
    return FakeUser(id=1, name="dev", role="user", tenant_id="dev", daily_quota=500,
                    keys=json.dumps({FAMILY: list(own_keys)}), key_opts="{}",
                    disabled=0, token="t", email="", password_hash="")


def check(label, got, want):
    ok = got == want
    print(f"  {'OK  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"        ожидалось: {want}")
        print(f"        получено:  {got}")
    return ok


def main_test() -> int:
    ok = True
    u2 = user_with(KEY_A, KEY_B)

    # 1. Оба ключа вообще видны системе.
    pool = main._own_pool_values(u2, FAMILY)
    ok &= check("_own_pool_values видит оба своих ключа", pool, [KEY_A, KEY_B])

    # 2. А вот ёмкость второго ключа не видит: _own_credentials_from_keys схлопывает
    #    список в первый ключ (_first_credential), и дальше по коду его уже не вернуть.
    ok &= check("_user_own_credentials отдаёт ОДНО значение, а не список",
                main._user_own_credentials(u2).get(FAMILY), KEY_A)

    # 3. Что реально выдаётся задаче. Slot каждый раз отпускаем: имитируем задачи,
    #    идущие одна за другой, а не десять одновременных.
    issued = []
    for _ in range(10):
        cred, lease = main._own_credential_with_pool(u2, PROVIDER, "images", {})
        issued.append(cred)
        if lease is not None:
            lease.release()
    distinct = sorted(set(issued))
    print(f"\n  выдано за 10 задач: {len(distinct)} различных ключ(а/ей)")
    for k in distinct:
        print(f"    {k[:12]}… × {issued.count(k)}")

    balanced = len(distinct) == 2 and min(issued.count(k) for k in distinct) >= 4
    if balanced:
        print("\n  ИТОГ: нагрузка делится между ключами — пул подключён")
    else:
        print("\n  ИТОГ: все задачи ушли на ОДИН ключ, второй простаивает.")
        print("        Второй ключ невидим и для ёмкости: список схлопнут в первый")
        print("        значением ещё до всех расчётов.")

    # 3b. Один ключ — поведение прежнее: slot не берётся вообще, выдаётся тот же ключ.
    #     Правка не должна ничего менять там, где раздавать нечего.
    u1 = user_with(KEY_A)
    cred1, lease1 = main._own_credential_with_pool(u1, PROVIDER, "images", {})
    ok &= check("один ключ: выдан он же", cred1, KEY_A)
    ok &= check("один ключ: slot не берётся", lease1, None)

    # 3c. Своих ключей нет — своя дорожка не включается, задача уйдёт на серверную.
    u0 = user_with()
    cred0, lease0 = main._own_credential_with_pool(u0, PROVIDER, "images", {})
    ok &= check("без своих ключей: credential пуст", cred0, "")
    ok &= check("без своих ключей: slot не берётся", lease0, None)

    # 4. Машинерия пула написана, но не подключена: считаем НАСТОЯЩИЕ вызовы,
    #    не считая объявление параметра в самой сигнатуре try_acquire.
    src = open(os.path.join(BASE, "server", "main.py"), encoding="utf-8").read()
    body = "\n".join(l for l in src.split("\n") if "def try_acquire" not in l)
    calls = body.count("own_values=")
    pool_defs = src.count("def _own_pool_size")
    print(f"\n  вызовов try_acquire(own_values=…) вне сигнатуры: {calls}")
    print(f"  определений _own_pool_size: {pool_defs}"
          f" (обе не вызываются ниоткуда — мёртвый код)")

    print()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main_test())
