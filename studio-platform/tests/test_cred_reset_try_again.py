#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Исчерпанный ключ Codex спит до названного срока, и его отказ не глушит канал при живом втором.

ДЕФЕКТ (ночь 21→22.10.2026). Codex на исчерпанной подписке x5 отвечает «You've hit your
usage limit … try again at Oct 27th, 2026 7:01 AM». Разбор срока знал только форму
«resets …», поэтому ключ парковался на час по умолчанию, раз в час просыпался, падал
снова, и каждый отказ через строку воркера «Канал промптов «codex» недоступен» глушил
ВЕСЬ канал промптов на 600 с — при живом x20. Семь раз за ночь, ≈70 минут без промптов.

Проверяется: срок из «try again at …» разбирается (и старая форма «resets …» не сломана);
при двух живых ключах семейства отказ одного канал не глушит; при одном — глушит, как и
раньше (подменить нечем).

    VS_TEST_NO_NET=1 .venv/bin/python tests/test_cred_reset_try_again.py
"""
from __future__ import annotations

import datetime
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "shared"))
sys.path.insert(0, os.path.join(BASE, "server"))

TMP = tempfile.mkdtemp(prefix="cred-reset-")
LOCKS = os.path.join(TMP, "locks")
os.makedirs(LOCKS, mode=0o700, exist_ok=True)
os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"] = LOCKS
os.environ["HOME"] = os.path.join(BASE, "dev", "home")
os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
os.environ["VS_SERVER_DB"] = os.path.join(TMP, "vsgen.copy.db")
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
DEV_DB = os.path.join(BASE, "dev", "vsgen.dev.db")
if os.path.exists(DEV_DB):
    shutil.copy(DEV_DB, os.environ["VS_SERVER_DB"])

import main                                                          # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


NOW = datetime.datetime(2026, 10, 22, 10, 0)
CODEX_TEXT = ("⚠️ Канал промптов «codex» недоступен (You've hit your usage limit. Visit "
              "https://chatgpt.com/codex/settings/usage to purchase more credits or try again "
              "at Oct 27th, 2026 7:01 AM.)")

print("1. срок из «try again at Oct 27th, 2026 7:01 AM»")
got = main._parse_reset_seconds(CODEX_TEXT, now_dt=NOW)
want = int((datetime.datetime(2026, 10, 27, 7, 1) - NOW).total_seconds()) + 120
check("парковка до 27.10 07:01 (+2 мин запаса)", got, want)

print("2. старая форма «resets …» не сломана")
got = main._parse_reset_seconds("out of extra usage · resets Oct 23, 3pm", now_dt=NOW)
check("resets Oct 23, 3pm → до 23.10 15:00", got,
      int((datetime.datetime(2026, 10, 23, 15, 0) - NOW).total_seconds()) + 120)
check("без срока — None", main._parse_reset_seconds("429 Too Many Requests", now_dt=NOW), None)

print("3. отказ одного ключа при живом втором не глушит канал")
saved = main._prompt_channel_has_spare_credential
main._prompt_channel_has_spare_credential = lambda ch: True
with main._PROMPT_DEAD_LOCK:
    main._PROMPT_DEAD.pop("codex", None)
ret = main._scan_prompt_channel_line(CODEX_TEXT)
check("скан вернул канал (инцидент для парковки ключа), но не глушил", ret, "codex")
check("канал codex жив", main._prompt_channel_dead("codex"), False)

print("4. единственный ключ — глушим, как раньше")
main._prompt_channel_has_spare_credential = lambda ch: False
ret = main._scan_prompt_channel_line(CODEX_TEXT)
check("скан вернул канал", ret, "codex")
check("канал codex помечен нерабочим", bool(main._prompt_channel_dead("codex")), True)
with main._PROMPT_DEAD_LOCK:
    main._PROMPT_DEAD.pop("codex", None)
main._prompt_channel_has_spare_credential = saved

print("5. запас считается по живым ключам семейства")
saved_live, saved_keys = main._live_credential_candidates, main.keys
main.keys = lambda: {}
main._live_credential_candidates = lambda cfg, fam: ["/root/.codex-server", "/root/.codex-vova04"]
check("два живых → есть запас", main._prompt_channel_has_spare_credential("codex"), True)
main._live_credential_candidates = lambda cfg, fam: ["/root/.codex-server"]
check("один живой → запаса нет", main._prompt_channel_has_spare_credential("codex"), False)
check("чужой канал → запаса нет", main._prompt_channel_has_spare_credential("nope"), False)
main._live_credential_candidates, main.keys = saved_live, saved_keys

shutil.rmtree(TMP, ignore_errors=True)
print("\nпровалов:", len(FAILS))
sys.exit(1 if FAILS else 0)
