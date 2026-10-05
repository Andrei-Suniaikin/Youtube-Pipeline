#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Промпты: три задачи Codex разом и проба модели на отдельных проектах (05.10.2026).

Решение владельца по роадмапе недели 4: промпты — главное узкое место (35 задач в очереди,
идут по одной-две), поэтому три задачи разом вместо двух и проба sol вместо terra на
двух-трёх проектах.

Проверяется: с боевыми настройками (8 задач в админ-таблице, 4 потока) дорожка даёт ровно
три задачи — не две и не восемь; память при этом в бюджете; модель проекта берётся из его
настроек, а чужая/битая — нет; подмена модели стоит только у промптов на серверной подписке.

    .venv/bin/python tests/test_prompt_codex_trial.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "shared"))
sys.path.insert(0, os.path.join(BASE, "server"))

FAILS = []
TMP = tempfile.mkdtemp(prefix="prompt-trial-")
LOCKS = os.path.join(TMP, "locks")
os.makedirs(LOCKS, mode=0o700, exist_ok=True)
os.chmod(LOCKS, 0o700)
os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"] = LOCKS
KEYS_PATH = os.path.join(TMP, "video_studio_api_keys.json")
# Как на бою 05.10: админ-таблица разрешает 8 задач, потоков 4.
json.dump({"codex_model": "gpt-5.6-terra",
           "_concurrency": {"prompt_codex": {"global": 8, "per_tenant": 6, "threads": 4}}},
          open(KEYS_PATH, "w"))
DEV_DB = os.path.join(BASE, "dev", "vsgen.dev.db")
DB_COPY = os.path.join(TMP, "vsgen.copy.db")
if os.path.exists(DEV_DB):
    shutil.copy(DEV_DB, DB_COPY)
os.environ["HOME"] = os.path.join(BASE, "dev", "home")
os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
os.environ["VS_SERVER_KEYS_PATH"] = KEYS_PATH
os.environ["VS_SERVER_DB"] = DB_COPY
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["VS_KEY_BUDGET_MODE"] = "off"
os.environ.pop("PROMPT_CODEX_G", None)
os.environ.pop("PROMPT_CODEX_K", None)

import main                                                         # noqa: E402


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


print("\nТри задачи разом")
check("потолок в коде — 3", main._PROVIDER_HARD_JOB_CAPS["prompt_codex"], 3)
check("админ-таблица 8 → дорожка даёт 3, не больше",
      main._provider_caps("prompt_codex")[0], 3)
# На бою пул из двух домов Codex, а все проекты в одном тенанте: держит потолок на
# тенант (6 в таблице → 3 из кода). Он и давал «по одному-два» до правки.
check("на один тенант — 3 задачи", main._provider_caps("prompt_codex")[1], 3)
if main._provider_pool_size("prompt_codex") == 1:
    check("слотов задач на один дом Codex — 3", main._provider_job_slots("prompt_codex"), 3)
check("потоков на задачу — по-прежнему 4", main._prompt_threads_setting("codex"), 4)
calls = main._prompt_channel_lane_jobs("prompt_codex") * main._prompt_threads_setting("codex")
check("вызовов Codex разом не больше потолка канала",
      calls <= main._prompt_channel_max_calls("codex"), True)
check("память каналов промптов в бюджете",
      main._prompt_memory_estimate_mb() <= main.PROMPT_MEMORY_BUDGET_MB, True)

print("\nМодель проекта")
projects = {
    "p_sol": {"settings": json.dumps({"prompt_codex_model": "gpt-5.6-sol"})},
    "p_none": {"settings": json.dumps({})},
    "p_typo": {"settings": json.dumps({"prompt_codex_model": "gpt-5.6-soul"})},
    "p_broken": {"settings": "{не json"},
}


class _Row(dict):
    def keys(self):
        return super().keys()


_orig = main._get_project
main._get_project = lambda tid, pid: _Row(projects[pid]) if pid in projects else None
try:
    check("sol в настройках — sol", main._project_codex_model("3", "p_sol"), "gpt-5.6-sol")
    check("пусто — модель сервера", main._project_codex_model("3", "p_none"), "")
    check("опечатка — не берётся", main._project_codex_model("3", "p_typo"), "")
    check("битые настройки — не роняют запуск", main._project_codex_model("3", "p_broken"), "")
    check("проекта нет — пусто", main._project_codex_model("3", "p_gone"), "")
finally:
    main._get_project = _orig

print("\nГде стоит подмена")
src = open(os.path.join(BASE, "server", "main.py"), encoding="utf-8").read()
at = src.index("_trial_model = _project_codex_model(")
guard = src[src.rindex("\n", 0, at - 20):at]
check("только задача промптов", 't["kind"] == "prompts"' in guard, True)
check("только канал codex", 'prompt_provider == "codex"' in guard, True)
check("не на своей подписке партнёра", "not own_credential" in guard, True)
check("подмена после сборки окружения воркера",
      src.index("job_env = _key_env(") < at, True)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nпровалов: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
