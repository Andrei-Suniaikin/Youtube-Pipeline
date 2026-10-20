#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ответ «429 Too Many Requests» — это лимит, а не поломка вызова (20.10.2026).

Что случилось. Восемь выпусков встали в разбор с «рерайт не справился: 1 из 1 задач не
выполнено | no output files saved (0/1)», притом что ключ был живой, а в логе задачи
честно значилось «Romanian: 4/4» — текст сгенерирован. В stderr codex лежало

    ERROR: exceeded retry limit, last status: 429 Too Many Requests

и ни одно слово из прежнего списка к этому не подошло: «retry limit» — не «rate limit».
Отказ уезжал как CODEX_ERROR, то есть «вызов сломался», а значит:

* дом Codex не гасился — пул выдавал его снова, и следующий кусок падал там же;
* ответ не получал префикс SUBSCRIPTION_LIMIT, `remember_limit_hit` молчал;
* `pop_limit_hit` при финализации возвращал 0 — и джоб ХОРОНИЛСЯ вместо того, чтобы
  дождаться открытия окна.

Механизм ожидания был на месте с 03.10 и просто не включался. Здесь проверяется, что
теперь включается, и что ровно те формулировки, из-за которых всё затевалось, узнаются.

    .venv/bin/python tests/test_codex_429_limit.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
LOCKS = tempfile.mkdtemp(prefix="codex-429-")
os.chmod(LOCKS, 0o700)
os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"] = LOCKS
os.environ.setdefault("VIDEO_STUDIO_SHARED_DIR", os.path.join(BASE, "shared"))
sys.path.insert(0, os.path.join(BASE, "rewrite"))
sys.path.insert(0, os.path.join(BASE, "shared"))

import rewrite_core as R                                            # noqa: E402

FAILS = []

# Ровно то, что было в stderr у задачи 9596aa5f6a2b (Romanian, 20.10 10:07).
BOEVOY_429 = ("stream error: Too many requests\n"
              "ERROR: exceeded retry limit, last status: 429 Too Many Requests")


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ок   {name}")
    else:
        FAILS.append(f"{name}: получили {got!r}, ждали {want!r}")
        print(f"  ПЛОХО {name}: получили {got!r}, ждали {want!r}")


class FakePool:
    """Пул, который только запоминает, кого погасили и кого отпустили."""

    def __init__(self):
        self.limited = {}
        self.cleared = []

    def mark_limited(self, home, until):
        self.limited[home] = until

    def clear_limit(self, home):
        self.cleared.append(home)


def main() -> int:
    print("1. Узнаём лимит по формулировке")
    check("боевой 429 из лога — лимит", R._codex_text_is_limit(BOEVOY_429), True)
    check("«exceeded retry limit» — лимит",
          R._codex_text_is_limit("ERROR: exceeded retry limit"), True)
    check("прежние слова не потеряны: usage limit",
          R._codex_text_is_limit("You've hit your usage limit"), True)
    check("прежние слова не потеряны: rate limit",
          R._codex_text_is_limit("rate limit exceeded"), True)
    check("прежние слова не потеряны: quota",
          R._codex_text_is_limit("insufficient quota"), True)
    check("прежние слова не потеряны: limit reached",
          R._codex_text_is_limit("limit reached|1792000000"), True)

    print("\n2. Чужое за лимит не принимаем")
    # В хвост stderr попадают последние 4000 символов, и там бывает кусок промпта:
    # по числу «429» в тексте о войне гасить ключ нельзя.
    check("число 429 в тексте — не лимит",
          R._codex_text_is_limit("в 429 году до н.э. началась осада"), False)
    check("поломка разбора — не лимит",
          R._codex_text_is_limit("CODEX_ERROR: JSONDecodeError: line 1"), False)
    check("нет авторизации — не лимит",
          R._codex_text_is_limit("stream error: unauthorized (401)"), False)
    check("пустой ответ — не лимит", R._codex_text_is_limit(""), False)
    check("None — не лимит", R._codex_text_is_limit(None), False)

    print("\n3. Дом гасится, ответ помечается — джоб будет отложен, а не похоронен")
    pool = FakePool()
    home = "/root/.codex-server"
    before = time.time()
    ok, text = R._codex_limit_to_cooldown(pool, home, (False, f"CODEX_LIMIT: {BOEVOY_429}"))
    check("вызов остался неуспешным", ok, False)
    check("дом погашен", home in pool.limited, True)
    rest = pool.limited.get(home, 0) - before
    check("отдых назначен в разумных пределах (до часа)", 0 < rest <= 3600, True)
    check("ответ помечен префиксом ожидания",
          str(text).startswith(R.SUBSCRIPTION_LIMIT_PREFIX), True)

    print("\n4. По этой метке джоб откладывается до сброса")
    job = "job-429-test"
    check("до отказа отметки нет", R.limit_hit_until(job), 0.0)
    R.remember_limit_hit(job, text)
    until = R.limit_hit_until(job)
    check("отметка появилась", until > before, True)
    check("финализация её забирает", R.pop_limit_hit(job), until)
    check("забрали один раз — больше нет", R.pop_limit_hit(job), 0.0)

    print("\n5. Успех снимает отдых досрочно")
    R._codex_limit_to_cooldown(pool, home, (True, "готовый текст"))
    check("дом отпущен по первому успеху", pool.cleared, [home])

    print("\n6. Поломка вызова по-прежнему не считается лимитом")
    pool2 = FakePool()
    R._codex_limit_to_cooldown(pool2, home, (False, "CODEX_ERROR: codex exited with code 1"))
    check("дом не погашен", pool2.limited, {})

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("всё сошлось")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
