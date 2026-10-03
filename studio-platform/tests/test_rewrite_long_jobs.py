#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Длинные ролики у рерайта: пауза растёт, лимит Codex откладывает, а не хоронит.

03.10.2026, по журналам боя:
* задача 7712909bb6bb (14/18 готовых кусков): 1015 кругов «Задача принята / Подготовка»
  каждые семь секунд — общий лимит ключа занят, пауза всегда пять секунд — пока рестарт
  не перевёл её в «interrupted»;
* девять выпусков «рерайт не справился: 0 файлов»: окно подписки дома Codex кончилось,
  дом не гасился, три круга авто-повтора за сорок секунд — и задача с 5/6 готовыми
  кусками похоронена, хотя речь шла о паузе до сброса окна.

    .venv/bin/python tests/test_rewrite_long_jobs.py
"""
from __future__ import annotations

import io
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("VIDEO_STUDIO_SHARED_DIR", os.path.join(BASE, "shared"))
sys.path.insert(0, os.path.join(BASE, "rewrite"))
sys.path.insert(0, os.path.join(BASE, "shared"))

import rewrite_core as R                                          # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class FakePool:
    def __init__(self):
        self.limited = []
        self.cleared = []

    def mark_limited(self, value, until_ts):
        self.limited.append((value, until_ts))

    def clear_limit(self, value):
        self.cleared.append(value)


def main() -> int:
    print("Общий лимит ключа занят: пауза удваивается, а не пять секунд вечно")
    check("ряд пауз", [R.host_budget_wait_delay(n) for n in range(1, 9)],
          [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0])
    job = "job-шторм"
    R.forget_host_budget_streak(job)
    now = time.time()
    R.remember_host_budget_wait(job)
    check("первый возврат — пять секунд",
          round(R.host_budget_wait_until(job) - now), 5)
    R.remember_host_budget_wait(job)
    R.remember_host_budget_wait(job)
    check("третий подряд — двадцать", round(R.host_budget_wait_until(job) - now), 20)
    check("серия считается", R.host_budget_wait_streak(job), 3)
    check("pop отдаёт время и снимает отметку, но серию не трогает",
          (round(R.host_budget_wait_until(job, pop=True) - now),
           R.host_budget_wait_until(job), R.host_budget_wait_streak(job)), (20, 0, 3))
    R.forget_host_budget_streak(job)
    R.remember_host_budget_wait(job)
    check("ключ получен — серия сброшена, снова пять",
          round(R.host_budget_wait_until(job) - now), 5)
    R.host_budget_wait_until(job, pop=True)
    R.forget_host_budget_streak(job)

    print("\nОтметку о лимите можно посмотреть, не снимая")
    check("нет отметки — ноль", R.limit_hit_until("нет-такого"), 0.0)
    R.remember_limit_hit("job-лимит", f"{R.SUBSCRIPTION_LIMIT_PREFIX}reset=1800000000|x")
    check("peek видит", R.limit_hit_until("job-лимит"), 1800000000.0)
    check("peek не снимает", R.limit_hit_until("job-лимит"), 1800000000.0)
    check("pop снимает", (R.pop_limit_hit("job-лимит"), R.limit_hit_until("job-лимит")),
          (1800000000.0, 0.0))
    check("не-лимит отметки не оставляет",
          (R.remember_limit_hit("job-x", "CODEX_ERROR: boom"), R.limit_hit_until("job-x")),
          (None, 0.0))

    print("\nCODEX_LIMIT: дом отдыхает, ответ помечен как окно подписки")
    pool = FakePool()
    now = time.time()
    out = R._codex_limit_to_cooldown(pool, "/root/.codex-a",
                                     (False, "CODEX_LIMIT: usage limit reached"))
    check("ответ получил префикс SUBSCRIPTION_LIMIT",
          out[-1].startswith(R.SUBSCRIPTION_LIMIT_PREFIX), True)
    check("исходный текст сохранён", "CODEX_LIMIT: usage limit reached" in out[-1], True)
    check("дом погашен", len(pool.limited), 1)
    check("на умолчание, раз время не названо",
          round((pool.limited[0][1] - now) / 60), round(R._LIMIT_DEFAULT_WAIT_SEC / 60))
    check("время сброса в ответе совпадает с кулдауном",
          R.parse_limit_reset(out[-1]), float(int(pool.limited[0][1])))
    check("и remember_limit_hit его понимает",
          (R.remember_limit_hit("job-c", out[-1]), R.limit_hit_until("job-c") > now), (None, True))
    R.pop_limit_hit("job-c")

    pool = FakePool()
    out = R._codex_limit_to_cooldown(pool, "/root/.codex-a",
                                     (False, [], "CODEX_LIMIT: try again in 2h 15m"))
    check("форма ответа сохранена (тройка)", len(out), 3)
    check("названное время прочитано: ~2ч15м",
          round((pool.limited[0][1] - time.time()) / 60), 135)
    pool = FakePool()
    out = R._codex_limit_to_cooldown(pool, "/root/.codex-a",
                                     (False, "CODEX_LIMIT: resets in 45 minutes"))
    check("«in 45 minutes»", round((pool.limited[0][1] - time.time()) / 60), 45)

    print("\n  не лимит — не трогаем; успех — снимает кулдаун")
    pool = FakePool()
    same = (False, "CODEX_ERROR: exited with code 1")
    check("CODEX_ERROR без изменений",
          R._codex_limit_to_cooldown(pool, "h", same), same)
    check("и дом не гасится", pool.limited, [])
    ok = (True, "готовый текст")
    check("успех без изменений", R._codex_limit_to_cooldown(pool, "h", ok), ok)
    check("успех снимает кулдаун дома", pool.cleared, ["h"])
    check("относительное время без «in» не угадывается (model gpt-5m — не 5 минут)",
          R._codex_limit_reset_from_text("CODEX_LIMIT: model gpt-5m limit"), 0.0)

    print("\nПроводка: оба вызова Codex идут через помощника, авто-повтор видит лимит")
    src = io.open(os.path.join(BASE, "rewrite", "rewrite_core.py"), encoding="utf-8").read()
    check("помощник стоит на обоих вызовах Codex",
          src.count("return _codex_limit_to_cooldown(pool, codex_home, result)"), 2)
    check("голых acquire у Codex не осталось",
          src.count('get_effective_credential_pool("codex").acquire('), 0)
    check("ключ получен — серия возвратов сбрасывается прямо в acquire",
          "forget_host_budget_streak(job_id)" in src.split("def acquire(")[1].split("def ")[0],
          True)
    srv = io.open(os.path.join(BASE, "rewrite", "server.py"), encoding="utf-8").read()
    loop = srv.split("def _run_task_with_resume(")[1].split("\ndef ")[0]
    check("авто-повтор не крутит три круга по исчерпанному окну",
          "limit_hit_until(" in loop, True)
    check("журнал называет, через сколько следующая попытка",
          "следующая попытка через" in srv, True)

    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
