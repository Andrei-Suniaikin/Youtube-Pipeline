#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Скачивание субтитров не вешает движок (18.09.2026).

`youtube-transcript-api` зовёт сессию без таймаута, а выход бывает полумёртвым: порт
слушает, туннель молчит — ровно это на бою 18.09 (срез live_2026-09-18, глава
«субтитры», раздел 5). Движок выпусков крутится одним циклом, поэтому повисший запрос
останавливает всё, включая снятие зависших заливок. Проверяем, что таймаут проставляется
сессией и что зависший выход считается виной выхода, а не приговором ролику.

    .venv/bin/python tests/test_subs_timeout.py
"""
from __future__ import annotations

import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_remote as PR                                            # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def main() -> int:
    print("Сессия субтитров подставляет таймаут сама")
    check("таймаут задан и разумный", 0 < PR.SUBS_TIMEOUT <= 60, True)

    seen = {}
    saved = requests.Session.request

    def spy(self, method, url, **kw):
        seen["timeout"] = kw.get("timeout")
        seen["url"] = url
        raise requests.exceptions.Timeout("выход не отвечает")

    try:
        requests.Session.request = spy
        sess = PR._subs_session("")
        check("это наша сессия, а не голая requests.Session",
              isinstance(sess, PR._SubsSession), True)
        try:
            sess.get("https://www.youtube.com/watch?v=x")
        except requests.exceptions.Timeout:
            pass
        check("вызов без таймаута получает умолчание", seen.get("timeout"), PR.SUBS_TIMEOUT)
        seen.clear()
        try:
            sess.get("https://www.youtube.com/watch?v=x", timeout=3)
        except requests.exceptions.Timeout:
            pass
        check("явный таймаут вызывающего сильнее", seen.get("timeout"), 3)
    finally:
        requests.Session.request = saved

    print("\nЗависший выход — вина выхода, а не ролика")
    check("таймаут считается отказом выхода (выход уходит отдыхать, ролик живёт)",
          PR._is_exit_fault(requests.exceptions.Timeout("повис")), True)
    check("и не путается с «у ролика нет субтитров»",
          PR._no_subtitles(requests.exceptions.Timeout("повис")), False)

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
