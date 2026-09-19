#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Общий учёт мест под физический ключ: платформа поднимается, потолки настраиваются.

Модуль приехал с ребейзом рерайта на боевой 18.09 (задача B7) и держит два разных
дефекта.

1. **Платформа не поднимается вообще.** Боевой `rewrite_core.py:22` делает
   `import credential_budget` БЕЗ `try`, а ищет его в `VIDEO_STUDIO_SHARED_DIR`
   (в бою — `/opt/video-studio-shared`, такого каталога у нас нет). В бою это три
   отдельных сервиса, и упал бы один; у нас один процесс на три сервиса — падает
   всё: кабинет, пульт, обложки. Поэтому модуль лежит в `studio-platform/shared/`,
   а каталог добавлен в `sys.path` платформы безусловно, а не через переменную.

2. **Потолки строже сегодняшнего.** `CAPACITIES` в бою — `claude_oauth: 1`, и автор
   сам назвал эти цифры «осторожной политикой», а не фактом про тариф. Наш форк
   31.08 разрешал ТРИ одновременных вызова на один oauth-токен Claude
   (`rewrite_core._credential_limit`, `REWRITE_CLAUDE_PER_TOKEN_CONCURRENCY=3`).
   Ребейз обязан сохранить боевое поведение по умолчанию, но не имеет права
   отнять у владельца решение: поэтому умолчания боевые, а потолок настраивается.

Третье, чего тут нет и быть не должно: честного общего учёта физического ключа —
это отдельная задача B8.

    .venv/bin/python tests/test_credential_budget.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
SHARED = os.path.join(BASE, "shared")
sys.path.insert(0, SHARED)

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


PY = os.path.join(BASE, ".venv", "bin", "python")

# Боевые умолчания. Список здесь НЕ для красоты: если кто-то поправит цифры в
# shared/credential_budget.py, разойдясь с боем, это должно стать видно сразу, а не
# в момент, когда стенд начнёт вести себя не так, как VPS.
LIVE_DEFAULTS = {"fastgen": 18, "openai": 12, "claude_oauth": 1,
                 "codex": 1, "veo": 24}


def run(code, env_extra=None, budget_dir=None):
    """Выполнить кусок кода в ОТДЕЛЬНОМ процессе с чистым окружением.

    Отдельный процесс, а не reload: потолки читаются один раз на импорте, и проверять
    их надо так же, как их получит настоящий сервис, — на старте.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = SHARED
    env.pop("VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR", None)
    for name in list(env):
        if name.startswith("VIDEO_STUDIO_CREDENTIAL_BUDGET_"):
            env.pop(name)
    if budget_dir:
        env["VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"] = budget_dir
    env.update(env_extra or {})
    done = subprocess.run([PY, "-c", code], capture_output=True, text=True, env=env)
    return done.returncode, (done.stdout or "").strip(), (done.stderr or "").strip()


def main() -> int:
    print("Модуль на месте и в рабочем состоянии")
    code, out, err = run("import credential_budget as b; print(sorted(b.CAPACITIES))")
    check("импортируется из shared/", code, 0)
    if code:
        print("        " + err.splitlines()[-1] if err else "")

    print("\nУмолчания боевые: ребейз сам по себе ничего не меняет")
    code, out, _ = run("import credential_budget as b; print(b.CAPACITIES)")
    check("потолки как на VPS", eval(out) if code == 0 else out, LIVE_DEFAULTS)

    print("\nПотолок — настройка, а не приговор")
    # Ровно то, ради чего ручка и заводилась: у нас claude_oauth=1 СТРОЖЕ, чем было
    # до ребейза (три вызова на токен). Владелец должен иметь возможность вернуть
    # три, не правя код.
    code, out, _ = run("import credential_budget as b; print(b.CAPACITIES['claude_oauth'])",
                       {"VIDEO_STUDIO_CREDENTIAL_BUDGET_CLAUDE_OAUTH": "3"})
    check("переменная поднимает потолок", out, "3")
    code, out, _ = run("import credential_budget as b; print(b.CAPACITIES['claude_oauth'])",
                       {"VIDEO_STUDIO_CREDENTIAL_BUDGET_CLAUDE_OAUTH": "мусор"})
    check("мусор в переменной оставляет боевое умолчание", out, "1")
    code, out, _ = run("import credential_budget as b; print(b.CAPACITIES['claude_oauth'])",
                       {"VIDEO_STUDIO_CREDENTIAL_BUDGET_CLAUDE_OAUTH": "0"})
    check("ноль не принимается: пул из нуля мест — это остановка, а не политика",
          out, "1")

    print("\nСчёт работает по-настоящему, а не только импортируется")
    root = tempfile.mkdtemp(prefix="budget-test-")
    try:
        # Один токен, потолок 1: второй заказ обязан получить отказ, третий после
        # освобождения — снова место. Это и есть «общий учёт», ради которого модуль.
        code, out, err = run(
            "import credential_budget as b\n"
            "one = b.try_acquire('claude_oauth', 'token-1', 1)\n"
            "two = b.try_acquire('claude_oauth', 'token-1', 1)\n"
            "other = b.try_acquire('claude_oauth', 'token-2', 1)\n"
            "print(one is not None, two is None, other is not None)\n"
            "one.release()\n"
            "three = b.try_acquire('claude_oauth', 'token-1', 1)\n"
            "print(three is not None)\n",
            budget_dir=root)
        check("место занимается, кончается и освобождается", out.split("\n"),
              ["True True True", "True"])

        print("\n...и потолок из переменной на счёт действительно влияет")
        code, out, _ = run(
            "import credential_budget as b\n"
            "got = [b.try_acquire('claude_oauth', 'token-3', 1) for _ in range(4)]\n"
            "print(sum(1 for g in got if g is not None))\n",
            {"VIDEO_STUDIO_CREDENTIAL_BUDGET_CLAUDE_OAUTH": "3"}, budget_dir=root)
        check("с потолком 3 выдано три места", out, "3")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\nКаталог замков: чужой или общедоступный не принимается")
    loose = tempfile.mkdtemp(prefix="budget-loose-")
    try:
        os.chmod(loose, 0o755)
        code, out, err = run(
            "import credential_budget as b\n"
            "try:\n"
            "    b._root(); print('пустил')\n"
            "except b.BudgetConfigurationError: print('отказал')\n",
            budget_dir=loose)
        check("общедоступный каталог отвергнут", out, "отказал")
    finally:
        shutil.rmtree(loose, ignore_errors=True)

    print("\nСтенд настроен: без этих строк платформа не поднимется на Mac")
    run_sh = open(os.path.join(BASE, "dev", "run.sh"), encoding="utf-8").read()
    check("dev/run.sh даёт каталог модуля",
          "VIDEO_STUDIO_SHARED_DIR" in run_sh, True)
    check("dev/run.sh даёт каталог замков (в бою это /run, которого тут нет)",
          "VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR" in run_sh, True)
    check("и делает его приватным — иначе _root() откажет",
          bool(re.search(r"chmod 700 \"\$VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR\"", run_sh)),
          True)
    platform_py = open(os.path.join(BASE, "web", "platform.py"), encoding="utf-8").read()
    check("платформа кладёт shared/ в sys.path сама, не надеясь на переменную",
          'os.path.join(BASE, "shared")' in platform_py, True)

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
