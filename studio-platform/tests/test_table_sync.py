#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Единая таблица: приём листов из Google-таблицы и отчёт ноды (pub_table_sync).

Проверяется без сети и без FastAPI: чистые функции модуля.
  1. листы узнаются по шапке, а не по имени; второй push того же текста — «без изменений»;
  2. пустой лист каналов и отсутствующий лист аккаунтов отклоняются (книги не трогаем);
  3. отчёт ноды: текст в Telegram один раз на (снимок, исход), «без изменений» не шумит,
     отчёт о старом снимке не перетирает текущий.

    VS_TEST_NO_NET=1 .venv/bin/python tests/test_table_sync.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_table_sync as ts                                           # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


TMP = tempfile.mkdtemp(prefix="table-sync-")
D = ts.sync_dir(TMP)

CH = ("﻿Канал;UC;Профиль AdsPower;Почта владельца;Почта заливки;Роль почты заливки;Состояние\n"
      "Tichá Obloha;UCAvlAWXAk7yitpFLLIwBlgg;10;o@gmail.com;m@gmail.com;менеджер;работает\n"
      "Noční Portréty;UCeHqQgsz9UIxqCeZZRLR9cA;13;o2@gmail.com;m2@gmail.com;менеджер;работает\n")
ACC = "Почта;Профиль AdsPower;Состояние почты;Чем заменена;Есть 2FA\nm@gmail.com;10;вошла;;да\nm2@gmail.com;13;;;да\n"

print("1. листы по шапке, повтор без изменений")
got = ts.classify({"Каналы (единая)": CH, "Аккаунты (единая)": ACC})
check("лист с UC → channels, лист «Почта…» → accounts", sorted(got), ["accounts", "channels"])
r1 = ts.ingest_push(D, {"Лист1": CH, "Лист2": ACC}, source="тест", now=1_800_000_000)
check("первый push принят", (r1["changed"], r1["rows"]), (True, {"channels": 2, "accounts": 2}))
r2 = ts.ingest_push(D, {"channels": CH, "accounts": ACC}, source="тест", now=1_800_000_300)
check("тот же текст второй раз — без изменений", (r2["changed"], r2["sha"] == r1["sha"]), (False, True))
cur = ts.current(D, with_text=True)
check("current отдаёт текст листов ноде", (cur["channels"] == CH, cur["accounts"] == ACC, cur["report"]), (True, True, None))
check("снимок лежит в incoming", os.path.isdir(cur["dir"]) and os.path.exists(os.path.join(cur["dir"], "meta.json")), True)
r3 = ts.ingest_push(D, {"channels": CH.replace("работает", "ждёт прав"), "accounts": ACC}, now=1_800_000_600)
check("изменённый лист — новый снимок", (r3["changed"], r3["sha"] != r1["sha"]), (True, True))

print("2. отказы: пустые каналы, нет аккаунтов")
try:
    ts.ingest_push(D, {"channels": "Канал;UC\n", "accounts": ACC}); check("пустой лист каналов отклонён", False, True)
except ValueError as exc:
    check("пустой лист каналов отклонён", "пустой" in str(exc), True)
try:
    ts.ingest_push(D, {"channels": CH}); check("без аккаунтов отклонено", False, True)
except ValueError as exc:
    check("без аккаунтов отклонено", "аккаунты" in str(exc), True)
check("отказ не сдвинул текущий снимок", ts.current(D)["sha"], r3["sha"])

print("3. отчёты ноды и уведомления")
rep = ts.record_report(D, {"sha": r3["sha"], "status": "plan", "summary": "+1 привязка, −0",
                           "lines": ["книга входов: + привязка профиль 13 UCeHqQ… → m2@gmail.com"]}, now=1_800_000_900)
check("план → уведомление", rep["notify"], True)
check("текст называет исход и строки", ("запись выключена" in rep["text"], "книга входов" in rep["text"]), (True, True))
rep2 = ts.record_report(D, {"sha": r3["sha"], "status": "plan", "summary": "+1 привязка, −0", "lines": []}, now=1_800_001_200)
check("тот же исход о том же снимке — молчим", rep2["notify"], False)
rep3 = ts.record_report(D, {"sha": r3["sha"], "status": "applied", "summary": "+1 привязка", "lines": []}, now=1_800_001_500)
check("записано → уведомление", rep3["notify"], True)
check("current помнит последний отчёт", ts.current(D)["report"]["status"], "applied")
rep4 = ts.record_report(D, {"sha": r3["sha"], "status": "same", "summary": "", "lines": []}, now=1_800_001_800)
check("«без изменений» не шумит", rep4["notify"], False)
rep5 = ts.record_report(D, {"sha": r1["sha"], "status": "blocked", "summary": "старый снимок", "lines": []}, now=1_800_002_100)
check("отчёт о старом снимке не перетирает текущий", (rep5["notify"], ts.current(D)["report"]["status"]), (False, "same"))
try:
    ts.record_report(D, {"sha": r3["sha"], "status": "weird"}); check("неизвестный статус отклонён", False, True)
except ValueError:
    check("неизвестный статус отклонён", True, True)

shutil.rmtree(TMP, ignore_errors=True)
print("\nпровалов:", len(FAILS))
sys.exit(1 if FAILS else 0)
