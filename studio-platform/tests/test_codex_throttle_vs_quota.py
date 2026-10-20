#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Частотный отказ ≠ кончившаяся подписка (20.10.2026, вечер).

Что случилось. Утренняя правка научила студию ждать вместо того, чтобы хоронить задачу
при лимите — и это сработало. Но под одно слово «лимит» попали две разные вещи:

* кончилось окно подписки — ждать до сброса (часы), дом выдавать нельзя;
* частотное ограничение `429 Too Many Requests` — провайдер придержал на секунды.

Обе гасили дом на пятнадцать минут. При двух домах хватало пары частотных отказов, чтобы
пул целиком «ушёл отдыхать»: владелец смотрел на подписку (свободно 70%) и видел в
студии «все ключи отдыхают до сброса окна», а 24 выпуска стояли в разборе.

Вторая половина правки: лимит на ПРОВЕРКЕ КАЧЕСТВА тоже откладывает задачу, а не роняет
её. Утром научили ждать сам рерайт, а проверка осталась со старым поведением — и именно
на ней вставали все 24 выпуска, с готовыми кусками текста на диске.

    .venv/bin/python tests/test_codex_throttle_vs_quota.py
"""
from __future__ import annotations

import os
import queue
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
LOCKS = tempfile.mkdtemp(prefix="codex-throttle-")
os.chmod(LOCKS, 0o700)
os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"] = LOCKS
os.environ.setdefault("VIDEO_STUDIO_SHARED_DIR", os.path.join(BASE, "shared"))
sys.path.insert(0, os.path.join(BASE, "rewrite"))
sys.path.insert(0, os.path.join(BASE, "shared"))

import rewrite_core as R                                            # noqa: E402

FAILS = []

# Ровно то, что лежало в причине у выпусков 20.10 вечером.
THROTTLE = ("stream error: Too many requests\n"
            "ERROR: exceeded retry limit, last status: 429 Too Many Requests")
# Так выглядит настоящий конец окна подписки.
QUOTA = "You've hit your usage limit. Try again in 2h 30m."


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ок   {name}")
    else:
        FAILS.append(f"{name}: получили {got!r}, ждали {want!r}")
        print(f"  ПЛОХО {name}: получили {got!r}, ждали {want!r}")


def near(name: str, got: float, want: float, slack: float = 20.0) -> None:
    if abs(got - want) <= slack:
        print(f"  ок   {name} (≈{got:.0f} с)")
    else:
        FAILS.append(f"{name}: получили {got:.0f} с, ждали около {want:.0f}")
        print(f"  ПЛОХО {name}: получили {got:.0f} с, ждали около {want:.0f}")


class FakePool:
    def __init__(self):
        self.limited = {}
        self.cleared = []

    def mark_limited(self, home, until):
        self.limited[home] = until

    def clear_limit(self, home):
        self.cleared.append(home)


def main() -> int:
    print("1. Отличаем частоту от конца окна")
    check("боевой 429 — частота", R._codex_text_is_throttle(THROTTLE), True)
    check("«usage limit» — не частота", R._codex_text_is_throttle(QUOTA), False)
    check("«quota exceeded» — не частота",
          R._codex_text_is_throttle("quota exceeded for this account"), False)
    # Оба слова разом: верим худшему, иначе вернёмся через минуту в ту же стену.
    check("частота И окно вместе — считаем окном",
          R._codex_text_is_throttle("usage limit reached; too many requests"), False)
    check("обе разновидности остаются лимитом (задача ждёт, а не падает)",
          (R._codex_text_is_limit(THROTTLE), R._codex_text_is_limit(QUOTA)), (True, True))
    check("обычная поломка — не лимит",
          R._codex_text_is_limit("ERROR: model stream closed unexpectedly"), False)

    print("\n2. Частота гасит дом на минуту, окно — на пятнадцать")
    now = time.time()
    pool = FakePool()
    out = R._codex_limit_to_cooldown(pool, "/root/.codex-server",
                                     (False, f"CODEX_LIMIT: {THROTTLE}"))
    near("частотный отказ — кулдаун минута",
         pool.limited["/root/.codex-server"] - now, R._THROTTLE_WAIT_SEC)
    check("в причине сказано, что это частота", "|частота|" in out[-1], True)
    check("префикс для отложки на месте",
          out[-1].startswith(R.SUBSCRIPTION_LIMIT_PREFIX), True)

    pool2 = FakePool()
    R._codex_limit_to_cooldown(pool2, "/root/.codex-vova04", (False, "CODEX_LIMIT: quota exceeded"))
    near("конец окна без названного срока — кулдаун 15 минут",
         pool2.limited["/root/.codex-vova04"] - now, R._LIMIT_DEFAULT_WAIT_SEC)

    pool3 = FakePool()
    out3 = R._codex_limit_to_cooldown(pool3, "/root/.codex-server", (False, f"CODEX_LIMIT: {QUOTA}"))
    near("названный провайдером срок уважаем (2ч30м)",
         pool3.limited["/root/.codex-server"] - now, 2 * 3600 + 30 * 60, slack=60)
    check("в причине сказано, что это окно", "|окно|" in out3[-1], True)

    print("\n3. Успех снимает кулдаун досрочно")
    pool4 = FakePool()
    R._codex_limit_to_cooldown(pool4, "/root/.codex-server", (True, "готово"))
    check("после успеха дом отпущен", pool4.cleared, ["/root/.codex-server"])

    print("\n4. Лимит на проверке качества откладывает задачу, а не хоронит")
    job = "job-quality-limit"
    R.pop_limit_hit(job)                                    # на всякий случай чисто
    saved = {name: getattr(R, name) for name in
             ("cleanup_enabled", "cleanup_model_name", "classify_provider",
              "get_allowed_codex_models", "cleanup_protected_terms", "cleanup_split",
              "cleanup_one_piece", "preclean_rewrite_source")}
    try:
        R.cleanup_enabled = lambda *a, **k: True
        R.cleanup_model_name = lambda *a, **k: "gpt-5.6-sol"
        R.classify_provider = lambda *a, **k: "codex"
        R.get_allowed_codex_models = lambda *a, **k: {"gpt-5.6-sol"}
        R.cleanup_protected_terms = lambda *a, **k: []
        R.cleanup_split = lambda text: [text]
        R.preclean_rewrite_source = lambda text: (text, 0)
        # Проверка упирается ровно в ту причину, что лежала у 24 выпусков.
        reason = f"{R.SUBSCRIPTION_LIMIT_PREFIX}reset={int(time.time() + 300)}|частота|{THROTTLE}"
        R.cleanup_one_piece = lambda *a, **k: ("", True, reason)
        try:
            R._cleanup_strict_rewrite_results(
                ["текст ролика"], "Polish", "Polish", queue.Queue(),
                "канал", "ведущий", False, job, "base", "cleanup_pl", True)
            check("проверка качества отказала", False, True)
        except R.RewriteQualityError as exc:
            check("отказ назван QUALITY_REVIEW_FAILED", "QUALITY_REVIEW_FAILED" in str(exc), True)
            check("готовые куски сохранены для повтора",
                  "chunks retained" in str(exc), True)
    finally:
        for name, value in saved.items():
            setattr(R, name, value)
    until = R.pop_limit_hit(job)
    check("отметка о лимите поставлена — финализация отложит джоб", until > time.time(), True)
    near("отложка до названного срока", until - time.time(), 300, slack=30)

    print("\nитог:", "всё сошлось" if not FAILS else f"ПРОВАЛОВ {len(FAILS)}")
    for line in FAILS:
        print("  -", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
