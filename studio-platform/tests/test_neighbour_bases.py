#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Студия по умолчанию ходит к СВОИМ сервисам, а не к старым (30.09.2026).

Владелец: «конечно ходим к своим, старых студий для новой как будто нет, она живёт свою
жизнь».

Почему на это нужен тест, а не только правка. Раньше умолчанием были адреса СТАРЫХ
сервисов (8899 и 8900) — верно, пока шёл переезд. Верным это перестало быть молча:
адреса своих сервисов выставляет `deploy/start-studio.sh`, и только он. Любой запуск
мимо него — правка юнита, разовый прогон, новая установка — отправил бы новую студию
работать в ЧУЖИЕ базы: чужие пресеты, чужая очередь, чужие файлы. Такая беда не падает с
ошибкой, она тихо делает работу не там, и замечают её по странным данным недели спустя.

Проверяется ровно это: без настроек — свои; порт один и тот же у кода и у скрипта
запуска; явно названный чужой адрес по-прежнему слушается (старый сервис не запрещён,
он просто не подразумевается).

    .venv/bin/python tests/test_neighbour_bases.py
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def bases(env: dict) -> tuple:
    """Перечитать модуль с заданным окружением: адреса считаются при импорте."""
    for name in ("VS_REWRITE_BASE", "VS_COVER_BASE", "VS_PORT"):
        os.environ.pop(name, None)
    os.environ.update(env)
    for mod in [m for m in list(sys.modules) if m.startswith("pub_remote")]:
        del sys.modules[mod]
    import pub_remote                                             # noqa: PLC0415
    return pub_remote.REWRITE_BASE, pub_remote.COVER_BASE


def main() -> int:
    print("Без настроек — свои сервисы того же процесса")
    rew, cov = bases({})
    check("текст — свой", rew, "http://127.0.0.1:8090/rewrite")
    check("обложки — свои", cov, "http://127.0.0.1:8090/cover")
    check("старого текста в умолчании нет", "8899" in rew, False)
    check("старых обложек в умолчании нет", "8900" in cov, False)

    print("Порт берётся из VS_PORT — как у скрипта запуска")
    rew, cov = bases({"VS_PORT": "8084"})
    check("текст на своём порту", rew, "http://127.0.0.1:8084/rewrite")
    check("обложки на своём порту", cov, "http://127.0.0.1:8084/cover")

    print("Пустой VS_PORT не ломает адрес")
    # Пустая переменная в окружении встречается чаще, чем кажется: её оставляет
    # незаполненная строка в .env. Адрес без порта не открылся бы вовсе.
    rew, cov = bases({"VS_PORT": "   "})
    check("текст остался рабочим", rew, "http://127.0.0.1:8090/rewrite")
    check("обложки тоже", cov, "http://127.0.0.1:8090/cover")

    print("Явно названный адрес сильнее умолчания")
    rew, cov = bases({"VS_REWRITE_BASE": "http://127.0.0.1:8899",
                      "VS_COVER_BASE": "http://127.0.0.1:8900"})
    check("текст — куда сказали", rew, "http://127.0.0.1:8899")
    check("обложки — куда сказали", cov, "http://127.0.0.1:8900")

    print("Скрипт запуска и код считают порт одинаково")
    # Разъехавшись, они дадут самую неприятную из поломок: работает на боевой машине и
    # не работает у того, кто поднял студию иначе.
    start = open(os.path.join(BASE, "deploy", "start-studio.sh"), encoding="utf-8").read()
    m = re.search(r'PORT="\$\{VS_PORT:-(\d+)\}"', start)
    check("в скрипте запуска порт найден", bool(m), True)
    if m:
        rew, cov = bases({})
        check("и он тот же, что в коде", f":{m.group(1)}/" in rew, True)
        check("для обложек тоже", f":{m.group(1)}/" in cov, True)
    check("скрипт выставляет адрес текста", "VS_REWRITE_BASE" in start, True)
    check("и адрес обложек", "VS_COVER_BASE" in start, True)

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
