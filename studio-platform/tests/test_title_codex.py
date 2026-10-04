#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Название ролика — подпиской Codex через сервис текста, а не OpenAI (04.10.2026).

33 готовых ролика стояли перед заливкой с «модель временно недоступна: 429»: названия писал
OpenAI напрямую, а счёт OpenAI пуст с 21.09 («нет денег» — тоже 429), и выпуск ждал вечно.
Владелец: «промпты должны улетать в подписку Codex». Названия идут тем же входом, что
обложки: `chat-once` сервиса текста с ключом рерайта.

    .venv/bin/python tests/test_title_codex.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_publish as P                                           # noqa: E402
import pub_services as S                                          # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body, self.text = code, body, str(body)

    def json(self):
        return self._body


def main() -> int:
    seen = {}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers)
        return seen["answer"]

    saved = P.requests.post
    P.requests.post = post
    try:
        print("Название пишет подписка Codex")
        seen["answer"] = Resp(200, {"ok": True, "text": "Название: «Тайна последнего письма с фронта»"})
        t = P.make_title_codex("The Last Letter From the Front", "ru", "rws_key", "http://x/rewrite")
        check("ответ очищен от кавычек и пометки", t, "Тайна последнего письма с фронта")
        check("идёт в chat-once сервиса текста", seen["url"], "http://x/rewrite/api/v1/chat-once")
        check("провайдер — Codex", seen["json"]["provider"], "codex")
        check("модель — как у текстов рерайта", seen["json"]["model"], P.TITLE_CODEX_MODEL)
        check("ключ — сервиса текста", seen["headers"]["Authorization"], "Bearer rws_key")
        check("язык канала в задании", "русский" in seen["json"]["messages"][1]["content"], True)
        check("а не в OpenAI", "openai.com" in seen["url"], False)

        print("\\nОтказ подписки — ролик ждёт, а не умирает")
        for code in (429, 502, 503):
            seen["answer"] = Resp(code, {"detail": "CODEX_LIMIT: usage limit"})
            try:
                P.make_title_codex("Donor", "pl", "k", "http://x")
                check(f"{code}: ждём", "не упало", "TitleUnavailable")
            except P.TitleUnavailable as exc:
                check(f"{code}: ждём, причина словами", "Codex" in str(exc), True)
        seen["answer"] = Resp(200, {"text": "Ок"})
        try:
            P.make_title_codex("Donor", "pl", "k", "http://x")
            check("слишком короткий ответ — ждём", "не упало", "TitleUnavailable")
        except P.TitleUnavailable:
            check("слишком короткий ответ — ждём", True, True)
        try:
            P.make_title_codex("Donor", "pl", "", "http://x")
            check("нет ключа — ждём настройки", "не упало", "TitleUnavailable")
        except P.TitleUnavailable:
            check("нет ключа — ждём настройки", True, True)
        try:
            P.make_title_codex("   ", "pl", "k", "http://x")
            check("нет названия донора — это беда строки", "не упало", "TitleError")
        except P.TitleError:
            check("нет названия донора — это беда строки", True, True)
    finally:
        P.requests.post = saved

    print("\\nВыпуск по умолчанию идёт через Codex, OpenAI — только по явной настройке")
    calls = []
    saved_c, saved_o, saved_k = S.make_title_codex, S.make_title, S.get_key
    S.make_title_codex = lambda *a, **k: calls.append("codex") or "Название через Codex"
    S.make_title = lambda *a, **k: calls.append("openai") or "Название через OpenAI"
    S.get_key = lambda c, name: "rws_key"
    try:
        svc = S.PubServices({"create_project": lambda *a, **k: "", "enqueue": lambda *a, **k: None,
                             "project_dir": lambda *a, **k: ""})
        svc._openai_key = lambda c: "sk-x"
        rel = {"donor_title": "Donor", "lang": "pl"}
        os.environ.pop("VS_TITLE_PROVIDER", None)
        check("по умолчанию — Codex", (svc._make_title(None, rel), calls[-1]),
              ("Название через Codex", "codex"))
        os.environ["VS_TITLE_PROVIDER"] = "openai"
        check("VS_TITLE_PROVIDER=openai — прежний путь", svc._make_title(None, rel),
              "Название через OpenAI")
    finally:
        os.environ.pop("VS_TITLE_PROVIDER", None)
        S.make_title_codex, S.make_title, S.get_key = saved_c, saved_o, saved_k

    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
