#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кабинет не предлагает снятых моделей Codex и не ходит в разлогиненный дом (02.10.2026).

Две мины, найденные при разборе вопроса владельца «мы сейчас используем x5 или x20».

ПЕРВАЯ. `CODEX_MODELS` предлагал `gpt-5.4-mini` и `gpt-5.4`, а боевой каталог Codex их
не отдаёт (`models_cache.json` обоих живых домов, снято 02.10: gpt-reserve, gpt-5.6-sol,
gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, codex-auto-review). Хуже того, `gpt-5.4-mini`
стояла УМОЛЧАНИЕМ, а проверка «cm in CODEX_MODELS» её же и принимала: список показывал
модель, которой невозможно воспользоваться, и позволял её выбрать. В бою не кусало лишь
потому, что перебито полем `codex_model` в файле ключей, — то есть мина ждала первого
кабинета без этого поля.

ВТОРАЯ, дороже. Дом Codex проверялся через `os.path.isdir`. Разлогиненный
`/root/.codex-rewrite` каталогом быть не перестал — внутри лежат `auth.json.expired-*`
и `auth.json.bak.revoked-*`, — поэтому проверка отвечала «да», путь уходил воркеру, и
отказ наступал уже у провайдера, на платном шаге. Он же стоял умолчанием, так что
пропажа REWRITE_CODEX_HOME из окружения уводила кабинет в этот дом молча.

Проверяется и то, что чинить НЕ собирались: имена живых моделей и согласие умолчания
со списком — иначе в выпадающем списке не окажется выбранного пункта.

    .venv/bin/python tests/test_codex_home_and_models.py
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
MAIN = os.path.join(BASE, "server", "main.py")

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def строка_списка() -> str:
    """Строка `CODEX_MODELS = [...]` прямо из исходника.

    Читаем исходник, а не разрешённое значение, нарочно: проверка «снятого имени нет
    в списке» должна падать и на закомментированном возврате, и на любой записи, где
    имя снова появилось. Для одной строки это надёжно; у многострочных констант так
    делать НЕЛЬЗЯ — по первой строке проверка молча проходит всегда (поймано мутацией
    02.10: умолчание дома так и ускользнуло).
    """
    src = io.open(MAIN, encoding="utf-8").read()
    m = re.search(r"^CODEX_MODELS = (.+?)$", src, re.M)
    return m.group(1).strip() if m else ""


# Боевой каталог моделей, снятый 02.10.2026 с обоих живых домов Codex. Это снимок
# факта, а не пожелание: тест сравнивает наш список с ним, а не с догадкой.
КАТАЛОГ_02_10 = {"gpt-reserve", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
                 "gpt-5.5", "codex-auto-review"}
СНЯТЫЕ = ("gpt-5.4-mini", "gpt-5.4")

_ПРОБА = r'''
import json, os, sys
sys.path.insert(0, os.path.join(%(base)r, "server"))
import main
tmp = os.environ["VS_PROBE_TMP"]

пустой = os.path.join(tmp, "пустой-дом")
os.makedirs(пустой, exist_ok=True)
# Ровно то, что лежит в /root/.codex-rewrite: следы прошлых входов, живого нет.
open(os.path.join(пустой, "auth.json.expired-20260901T124600Z"), "w").write("{}")
живой = os.path.join(tmp, "живой-дом")
os.makedirs(живой, exist_ok=True)
open(os.path.join(живой, "auth.json"), "w").write("{}")
файл = os.path.join(tmp, "не-каталог")
open(файл, "w").write("x")

R = {}
# СНАЧАЛА умолчание, и только потом подмены: ниже тест двигает main.CODEX_HOME.
# Обе переменные окружения подпроцессу сняты, значит это именно умолчание кода.
R["дом_по_умолчанию"] = main.CODEX_HOME
R["пустой_дом_годен"] = main._codex_home_ready(пустой)
R["живой_дом_годен"] = main._codex_home_ready(живой)
R["нет_такого_пути"] = main._codex_home_ready(os.path.join(tmp, "нету"))
R["файл_вместо_каталога"] = main._codex_home_ready(файл)
R["пусто_годно"] = main._codex_home_ready("")
R["none_годно"] = main._codex_home_ready(None)

# Потребитель №1: сбор серверных credentials. Разлогиненный дом не должен попасть
# в пул — иначе очередь получит ёмкость, которой нет.
main.CODEX_HOME = пустой
R["пул_с_пустым_домом"] = main._credential_candidates({}, "codex_home")
main.CODEX_HOME = живой
R["пул_с_живым_домом"] = main._credential_candidates({}, "codex_home")

R["модели"] = list(main.CODEX_MODELS)
R["умолчание"] = main.DEFAULT_CODEX_MODEL
print(json.dumps(R, ensure_ascii=False))
'''


def проба() -> dict:
    """main.py на импорте трогает файлы — гоняем отдельным процессом со своим HOME."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["HOME"] = os.path.join(BASE, "dev", "home")
        env["VS_SERVER_DISABLE_BACKGROUND"] = "1"
        env["VS_SERVER_DB"] = os.path.join(tmp, "t.db")
        env["VS_TENANTS_DIR"] = os.path.join(tmp, "tenants")
        env["VS_SERVER_KEYS_PATH"] = os.path.join(tmp, "keys.json")
        env["VS_PROBE_TMP"] = tmp
        # Переменные окружения дома гасим: тест про УМОЛЧАНИЕ и про проверку, а не
        # про то, что задано на этой машине.
        for имя in ("VS_CODEX_HOME", "REWRITE_CODEX_HOME"):
            env.pop(имя, None)
        out = subprocess.run([sys.executable, "-c", _ПРОБА % {"base": BASE}],
                             capture_output=True, text=True, env=env, timeout=180)
    if out.returncode != 0:
        raise SystemExit("подпроцесс упал:\n" + out.stderr[-4000:])
    return json.loads((out.stdout.strip().splitlines() or ["{}"])[-1])


def main() -> int:
    print("Список моделей не предлагает того, чего у провайдера нет")
    строка = строка_списка()
    for имя in СНЯТЫЕ:
        # Подстрока: «gpt-5.4» внутри «gpt-5.4-mini» тоже считается попаданием, и это
        # правильно — ни одной из двух в каталоге нет.
        check(f"снятая {имя} убрана", имя in строка, False)

    print("\nПоведение кабинета")
    R = проба()
    модели = R["модели"]
    check("каждая предложенная модель есть в боевом каталоге",
          [m for m in модели if m not in КАТАЛОГ_02_10], [])
    check("умолчание выбрано из этого же списка", R["умолчание"] in модели, True)
    check("умолчание — боевое gpt-5.6-terra", R["умолчание"], "gpt-5.6-terra")

    print("\nРазлогиненный дом Codex не считается годным")
    check("каталог без auth.json — не дом", R["пустой_дом_годен"], False)
    check("каталог с auth.json — дом", R["живой_дом_годен"], True)
    check("нет такого пути — не дом", R["нет_такого_пути"], False)
    check("файл вместо каталога — не дом", R["файл_вместо_каталога"], False)
    check("пустая строка — не дом", R["пусто_годно"], False)
    check("None — не дом", R["none_годно"], False)

    print("\nИ в пул серверных подписок он не попадает")
    check("пустой дом в пул не пошёл", R["пул_с_пустым_домом"], [])
    check("живой дом в пул пошёл", len(R["пул_с_живым_домом"]), 1)

    print("\nУмолчание дома больше не указывает на разлогиненный каталог")
    # Спрашиваем разрешённое значение у модуля, а не строку исходника: константа
    # многострочная, и чтение по первой строке давало ложное «зелено».
    check("прежнее /root/.codex-rewrite в умолчании не стоит",
          "/root/.codex-rewrite" in str(R["дом_по_умолчанию"]), False)
    check("умолчание — тот дом, которым ходит бой",
          R["дом_по_умолчанию"], "/root/.codex-server")

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
