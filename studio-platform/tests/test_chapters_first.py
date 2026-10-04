#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Таймкоды готового ролика не стоят за промптами новых проектов (04.10.2026).

Что случилось. Таймкоды (задача chapters) — последний шаг уже собранного ролика: пока их
нет, выпуск на заливку не уходит. Они идут в той же дорожке, что промпты новых проектов,
а промпты занимают её по 15–40 минут. 04.10 восемь готовых роликов ждали таймкоды с 09:25
до ночи за десятками промптов и опоздали к своим слотам.

Теперь у каждого юзера таймкоды идут головой очереди. Порядок внутри таймкодов и внутри
остального прежний, задачи других видов друг друга не обгоняют.

Шедулер настоящий (main._claim_fair_tasks), база — копия стенда, дорожка промптов
сужена до одной задачи, чтобы было видно, кто проходит первым.

    .venv/bin/python tests/test_chapters_first.py
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
TMP = tempfile.mkdtemp(prefix="chapters-first-")
LOCKS = os.path.join(TMP, "locks")
os.makedirs(LOCKS, mode=0o700, exist_ok=True)
os.chmod(LOCKS, 0o700)
os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"] = LOCKS
KEYS_PATH = os.path.join(TMP, "video_studio_api_keys.json")
json.dump({"openai": "sk-chapters-first-0000000001",
           "_concurrency": {"prompt_openai": {"threads": 1}}}, open(KEYS_PATH, "w"))
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
os.environ["PROMPT_OPENAI_G"] = "1"          # одна задача в дорожке разом
os.environ["PROMPT_OPENAI_K"] = "1"

import main                                                         # noqa: E402

PROVIDER = "prompt_openai"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def seed(rows):
    """rows: (job_id, kind, возраст в секундах). Очередь чистим: чужие строки стенда мешали бы."""
    c = main.db()
    try:
        u = c.execute("SELECT id, tenant_id FROM users ORDER BY id").fetchone()
        c.execute("DELETE FROM queue_tasks")
        c.execute("DELETE FROM admin_state WHERE k='queue_pause'")
        for jid, kind, age in rows:
            c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,created,"
                      "uid,provider,params) VALUES(?,?,?,?,'queued',?,?,?,'{}')",
                      (jid, u["tenant_id"] or str(u["id"]), f"p-ch-{jid}", kind,
                       main._now() - age, u["id"], PROVIDER))
        c.commit()
    finally:
        c.close()
    main._queue_pause_forget_cache()


LEASES = {}


def claim():
    """Выдача шедулера. Место ключа, которое задача держит, отпускаем в finish — как
    это делает настоящий исполнитель по окончании задачи."""
    c = main.db()
    try:
        out = []
        for t in main._claim_fair_tasks(c):
            LEASES[t["job_id"]] = t.get("_credential_lease")
            out.append((t["job_id"], t["kind"]))
        return out
    finally:
        c.close()


def finish(jid):
    lease = LEASES.pop(jid, None)
    if lease is not None:
        lease.release()
    c = main.db()
    try:
        c.execute("UPDATE queue_tasks SET status='done', finished=? WHERE job_id=?",
                  (main._now(), jid))
        c.commit()
    finally:
        c.close()


def main_() -> int:
    if not os.path.exists(DB_COPY):
        print("ПРОПУЩЕНО: нет базы стенда dev/vsgen.dev.db")
        return 0
    print("Две старые задачи промптов и свежие таймкоды, место в дорожке одно")
    seed([(980001, "prompts", 3600), (980002, "prompts", 1800),
          (980003, "chapters", 60), (980004, "chapters", 30)])
    check("первыми идут таймкоды, старшие из них", claim(), [(980003, "chapters")])
    finish(980003)
    check("затем вторые таймкоды", claim(), [(980004, "chapters")])
    finish(980004)
    check("затем самый старый промпт", claim(), [(980001, "prompts")])
    finish(980001)
    check("и следующий промпт по старшинству", claim(), [(980002, "prompts")])
    finish(980002)

    print("\nБез таймкодов порядок прежний")
    seed([(980011, "prompts", 900), (980012, "prompts", 300)])
    check("старший промпт первым", claim(), [(980011, "prompts")])

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_())
