#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""«Не смогли спросить» — не значит «донор удалён» (05.10.2026).

`pub_tags.donor_title` отдавал пусто и на удалённый ролик, и на обрыв сети, и на 429,
а `pub_remote._video_really_gone` читал пусто как «удалён». При сбое сети исправный
донор уходил бы в разбор с приговором «ролика нет». Теперь `donor_lookup` отвечает
тремя словами: alive / gone / unknown, и приговор — только на gone.

14.10.2026: 401/403 у oembed — это и «закрыт», и «встраивание запрещено» у живого ролика.
Их разбирает ключ YouTube Data API (с учётом квоты под «donor»), а без базы — «не знаем».

    .venv/bin/python tests/test_donor_gone_ambiguous.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class _Resp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


ANSWER = {"value": None}


def _get(url, params=None, timeout=None):
    a = ANSWER["value"]
    if isinstance(a, Exception):
        raise a
    return a


import requests                                                     # noqa: E402
import pub_tags                                                     # noqa: E402
import pub_remote                                                   # noqa: E402

requests.get = _get          # в сеть не ходим: ответ oembed подставляет тест

URL = "https://www.youtube.com/watch?v=jl27oYDkhIc"


def main() -> int:
    print("Три ответа oembed")
    cases = (
        ("живой ролик", _Resp(200, {"title": "Kdo byl Tutanchamon"}), ("alive", "Kdo byl Tutanchamon")),
        ("удалён (404)", _Resp(404), ("gone", "")),
        ("удалён (400)", _Resp(400), ("gone", "")),
        ("закрыт или без встраивания (401), без базы — не знаем", _Resp(401), ("unknown", "")),
        ("403, без базы — не знаем", _Resp(403), ("unknown", "")),
        ("429 — не знаем", _Resp(429), ("unknown", "")),
        ("503 — не знаем", _Resp(503), ("unknown", "")),
        ("обрыв сети — не знаем", TimeoutError("timed out"), ("unknown", "")),
        ("битый ответ — не знаем", _Resp(200, ValueError("bad json")), ("unknown", "")),
    )
    for label, answer, want in cases:
        ANSWER["value"] = answer
        check(label, pub_tags.donor_lookup(URL), want)
    check("не YouTube — в сеть не ходим", pub_tags.donor_lookup("https://vimeo.com/1"), ("unknown", ""))

    print("\nНазвание — как раньше")
    ANSWER["value"] = _Resp(200, {"title": "Kdo byl Tutanchamon"})
    check("живой → название", pub_tags.donor_title(URL), "Kdo byl Tutanchamon")
    ANSWER["value"] = TimeoutError("timed out")
    check("обрыв → пусто", pub_tags.donor_title(URL), "")

    print("\nПриговор «ролика нет ни для кого» — только на gone")
    for label, answer, want in (("404", _Resp(404), True), ("429", _Resp(429), False),
                                ("обрыв сети", TimeoutError("x"), False),
                                ("живой", _Resp(200, {"title": "t"}), False)):
        ANSWER["value"] = answer
        check(f"{label} → удалён={want}", pub_remote._video_really_gone("jl27oYDkhIc"), want)

    print("\n401 разбирает ключ API (14.10.2026)")
    import sqlite3
    import pub_calls
    import pub_verify
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    asked = []
    saved = (pub_verify.api_key, pub_verify.check)
    pub_verify.api_key = lambda c_: "k"
    try:
        for label, res, want in (
                ("ключ видит ролик — живой, с названием",
                 {"state": pub_verify.PUBLIC, "title": "Провёл 66 ЛЕТ  в капсуле"},
                 ("alive", "Провёл 66 ЛЕТ в капсуле")),
                ("ключ не видит — закрыт", {"state": pub_verify.NOT_PUBLIC}, ("gone", "")),
                ("квота — не знаем", {"state": pub_verify.QUOTA}, ("unknown", ""))):
            pub_verify.check = lambda vid, key, c=None, consumer="", **kw: (
                asked.append((vid, consumer)) or res)
            ANSWER["value"] = _Resp(401)
            check(label, pub_tags.donor_lookup(URL, c), want)
        check("спрашивали тот ролик и под именем «donor»", asked[0], ("jl27oYDkhIc", "donor"))
        asked.clear()
        ANSWER["value"] = _Resp(404)
        check("404 — ключ не спрашиваем", (pub_tags.donor_lookup(URL, c), asked),
              (("gone", ""), []))
        pub_calls.quota_block(c, "HTTP 403 quotaExceeded", kind="daily")
        pub_verify.check = saved[1]
        ANSWER["value"] = _Resp(401)
        check("квота уже кончилась — в API не идём", pub_tags.donor_lookup(URL, c), ("unknown", ""))
    finally:
        pub_verify.api_key, pub_verify.check = saved

    print(f"\nпровалов: {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
