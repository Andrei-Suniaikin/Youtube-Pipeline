#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Гигиена стенда: прогон не пачкает репозиторий и не врёт про свои копии.

Три вещи, которые нашлись при разборе ребейза рерайта на боевой 18.09 (задача B7).
Все три — не про логику конвейера, а про то, что человек видит вокруг него, поэтому
и собраны отдельно: логику ломает баг, а это ломает доверие к тому, что видно.

1. **Прогон стенда правил отслеживаемый файл.** `rewrite/server.py` держит пресеты в
   `REWRITE_PRESETS_DIR`, а по умолчанию — прямо в рабочем дереве
   (`rewrite/presets/<тенант>/generation.json`). `dev/run.sh` переменную не задавал,
   поэтому любая генерация на стенде оставляла в `git diff` чужое «updated_at».

2. **Ручка потолков живёт только в нашей копии общего модуля.** В бою
   `credential_budget.py` лежит в `/opt/video-studio-shared` и про переменные
   `VIDEO_STUDIO_CREDENTIAL_BUDGET_<СЕМЬЯ>` не знает. Переезд на общий каталог тихо
   вернёт боевые потолки, а две разные копии на одной машине разведут общий счёт
   замков. Решение принято такое: правку оставляем, но ограничение написано прямым
   текстом там, где его прочтут, — в `.env.example` и в самом модуле.

3. **Копии баз в `dev/backup/` врали именем.** Файлы назывались
   `*.before-rebase-18-09`, а сняты уже после того, как схема доехала: в них есть
   `create_intents`. Как страховка «вернуться к состоянию до ребейза» они не работают.

    .venv/bin/python tests/test_dev_stand.py
"""
from __future__ import annotations

import glob
import os
import re
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def read(*parts) -> str:
    return open(os.path.join(BASE, *parts), encoding="utf-8").read()


def main() -> int:
    run_sh = read("dev", "run.sh")
    env_example = read(".env.example")

    print("Пресеты рерайта: стенд пишет рядом с dev-базой, а не в рабочее дерево")
    check("REWRITE_PRESETS_DIR задан в dev/run.sh",
          bool(re.search(r"^export REWRITE_PRESETS_DIR=", run_sh, re.M)), True)
    target = re.search(r"^export REWRITE_PRESETS_DIR=\"\$\{REWRITE_PRESETS_DIR:-([^\"}]+)\}\"",
                       run_sh, re.M)
    check("значение по умолчанию есть и читается", bool(target), True)
    if target:
        # Каталог обязан быть ВНЕ отслеживаемого дерева сервисов. Достаточно проверить,
        # что он не внутри rewrite/: именно туда стенд и лез.
        path = target.group(1)
        check("и он не внутри rewrite/", "/rewrite/" not in path.replace("$ROOT", ""), True)
        check("а лежит в состоянии стенда (dev/)", "dev/" in path, True)
    # Стенд без пресетов показывает пустой список, поэтому первый запуск обязан
    # положить туда стартовый набор — иначе «чисто» куплено ценой пустого интерфейса.
    check("первый запуск засевает каталог из репозитория",
          bool(re.search(r'cp -R "\$ROOT/rewrite/presets/\."', run_sh)), True)
    check("и делает это только когда каталог пуст",
          bool(re.search(r'if \[ -z "\$\(ls -A "\$REWRITE_PRESETS_DIR" 2>/dev/null\)" \]',
                         run_sh)), True)

    print("\nОграничение ручки потолков написано прямым текстом")
    # Проверяется не красота формулировки, а наличие ровно тех слов, без которых
    # человек не узнает главного: ручка есть только в нашей копии модуля.
    knob_block = env_example
    check("сказано, что переменные читает только наша копия",
          "ТОЛЬКО наша копия" in knob_block, True)
    check("назван боевой каталог, где правки нет",
          "/opt/video-studio-shared" in knob_block, True)
    check("сказано, чем это кончится — молчаливым возвратом к боевым потолкам",
          "вернутся к боевым" in knob_block, True)
    module = read("shared", "credential_budget.py")
    check("то же предупреждение стоит в самом модуле",
          "живёт ТОЛЬКО в этой копии" in module, True)

    print("\nFASTGEN и VEO помечены как пока не используемые")
    # try_acquire зовётся только для openai / claude_oauth / codex
    # (rewrite/rewrite_core.py, CredentialPool.acquire). Строки про остальные семьи
    # без пометки читаются как работающая настройка — и владелец крутит пустышку.
    core = read("rewrite", "rewrite_core.py")
    # Семьи собираются из САМОГО рерайта: и прямые вызовы, и таблица «провайдер → семья»
    # перед вызовом. Появится четвёртая — проверка упадёт, и пометку в .env.example
    # придётся снять осознанно, а не забыть.
    families = set(re.findall(r'_host_budget\.try_acquire\(\s*"([a-z_]+)"', core))
    mapped = re.search(r"family = \{([^}]*)\}\.get\(self\.provider\)", core)
    if mapped:
        families |= set(re.findall(r':\s*"([a-z_]+)"', mapped.group(1)))
    check("общий учёт зовут только три семьи",
          families, {"claude_oauth", "codex", "openai"})
    tail = env_example.split("VIDEO_STUDIO_CREDENTIAL_BUDGET_OPENAI")[-1]
    check("FASTGEN и VEO стоят после пометки",
          ("FASTGEN" in tail and "VEO" in tail), True)
    check("и пометка сказана словами", "ПОКА НЕ ИСПОЛЬЗУЮТСЯ" in tail, True)

    print("\nОбщая на машину переменная потоков описана как общая")
    # `REWRITE_MAX_THREADS_PER_JOB` читают двое: рерайт (зажимает тело заказа) и
    # кабинет (тем же числом считает ключ повтора). Разные значения в двух процессах —
    # это либо вторая оплаченная задача, либо 409 навсегда, и оба случая молчаливые.
    check("переменная названа в .env.example",
          "REWRITE_MAX_THREADS_PER_JOB" in env_example, True)
    threads_note = env_example.split("REWRITE_MAX_THREADS_PER_JOB", 1)[-1]
    check("и сказано, что читают её двое и значение одно на машину",
          ("ОДНО на машину" in threads_note and "pub_remote" in threads_note), True)

    print("\nКопии баз названы тем, чем они являются")
    backup = os.path.join(BASE, "dev", "backup")
    if not os.path.isdir(backup):
        # Каталог в .gitignore: на чужой машине его просто нет, и это не провал.
        print("  --   dev/backup/ на этой машине нет, проверять нечего")
    else:
        lying = []
        for path in glob.glob(os.path.join(backup, "*before-rebase*")):
            try:
                names = {r[0] for r in sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                         .execute("SELECT name FROM sqlite_master WHERE type='table'")}
            except sqlite3.Error:
                names = set()
            # `create_intents` появилась ВМЕСТЕ с ребейзом. Есть таблица — копия снята
            # после него, и слово «before» в имени врёт.
            if "create_intents" in names:
                lying.append(os.path.basename(path))
        check("нет копий с «before-rebase» в имени и схемой ПОСЛЕ ребейза", lying, [])
        after = glob.glob(os.path.join(backup, "*after-schema-18-09"))
        if after:
            check("рядом лежит файл с одной строкой правды",
                  os.path.isfile(os.path.join(backup, "ПРОЧТИ.txt")), True)

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
