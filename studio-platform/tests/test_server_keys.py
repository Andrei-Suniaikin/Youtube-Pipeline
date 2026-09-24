#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Серверные ключи владельца правятся со страницы, а не по SSH (24.09.2026).

Главное, что проверяется, — не «записалось», а чего записываться НЕ должно: чужие поля
файла (заметки, лимиты, правила выбора канала промптов) обязаны пережить правку,
значение ключа не должно возвращаться наружу, файл обязан остаться закрытым (0600), а
испорченный файл — не перезаписываться вслепую.

    .venv/bin/python tests/test_server_keys.py
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="server-keys-")
os.environ["VS_SERVER_KEYS_PATH"] = os.path.join(TMP, "video_studio_api_keys.json")
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

import server_keys as SK                                          # noqa: E402

FAILS = []
OLD = "fg_live_СТАРЫЙ_ключ_1234"
NEW = "fg_live_НОВЫЙ_ключ_9876"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def refused(label, fn, expect):
    try:
        fn()
        check(label, "прошло без отказа", f"отказ: {expect}")
    except SK.KeyError_ as exc:
        check(label, expect in str(exc), True)


def seed(data=None):
    payload = data if data is not None else {
        "googler": OLD,
        "elevenlabs": "el_ключ_5555",
        "googler_pool": [],
        # Чужое: заметки, лимиты и правила выбора канала промптов.
        "_googler_pool_note": "пул пуст с 05.09",
        "_concurrency": {"recrafter": 4, "openai": 2},
        "_prompt_policy": {"channel": "altclaude", "why": "счёт OpenAI пуст"},
        "codex_homes": ["/root/.codex-a", "/root/.codex-b"],
    }
    with open(SK.path(), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.chmod(SK.path(), 0o600)
    return payload


def file_now():
    with open(SK.path(), encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    print("Что видно на экране")
    seed()
    items = {i["family"]: i for i in SK.listing()}
    check("семейства перечислены", len(items) >= 8, True)
    check("заданный помечен", items["googler"]["set"], True)
    check("виден только хвост", items["googler"]["tail"], "…1234")
    check("незаданный так и сказан", items["veo"]["set"], False)
    listing_text = json.dumps(SK.listing(), ensure_ascii=False)
    check("значения ключа в списке нет", OLD in listing_text, False)

    print("Замена ключа")
    out = SK.set_value("googler", NEW)
    check("ответ без значения", NEW in json.dumps(out, ensure_ascii=False), False)
    check("хвост нового", out["tail"], "…9876")
    data = file_now()
    check("новый ключ на месте", data["googler"], NEW)
    check("старый сохранён рядом",
          [v for k, v in data.items() if k.startswith("_googler_prev_")], [OLD])

    print("Чужое в файле не тронуто")
    check("заметка цела", data["_googler_pool_note"], "пул пуст с 05.09")
    check("лимиты целы", data["_concurrency"], {"recrafter": 4, "openai": 2})
    check("правила промптов целы", data["_prompt_policy"]["channel"], "altclaude")
    check("чужие ключи целы", data["elevenlabs"], "el_ключ_5555")
    check("список папок цел", data["codex_homes"], ["/root/.codex-a", "/root/.codex-b"])

    print("Файл остаётся закрытым")
    check("права 0600", stat.S_IMODE(os.stat(SK.path()).st_mode), 0o600)
    check("временных файлов не осталось",
          [f for f in os.listdir(TMP) if f.startswith(".keys-")], [])

    print("Отказы")
    refused("пустой ключ", lambda: SK.set_value("googler", "  "), "пустой")
    refused("слишком короткий", lambda: SK.set_value("googler", "abc"), "короче")
    refused("тот же самый", lambda: SK.set_value("googler", NEW), "уже задан")
    refused("чужое поле файла", lambda: SK.set_value("_prompt_policy", "x" * 20),
            "отсюда не меняется")
    refused("выдуманное семейство", lambda: SK.set_value("gemini", "x" * 20),
            "отсюда не меняется")
    refused("нечего убирать", lambda: SK.drop("veo"), "и так нет")
    refused("чужое поле не убрать", lambda: SK.drop("_concurrency"), "не меняется")

    print("Испорченный файл не затираем вслепую")
    with open(SK.path(), "w", encoding="utf-8") as fh:
        fh.write("{это не json")
    refused("правка отказывает", lambda: SK.set_value("googler", "x" * 20), "испорчен")
    check("файл не переписан", open(SK.path(), encoding="utf-8").read(), "{это не json")

    print("Снятие ключа")
    seed()
    SK.drop("googler")
    data = file_now()
    check("ключа нет", "googler" in data, False)
    check("снятый сохранён рядом",
          [v for k, v in data.items() if k.startswith("_googler_removed_")], [OLD])
    check("соседний ключ цел", data["elevenlabs"], "el_ключ_5555")
    check("в списке помечен незаданным",
          {i["family"]: i["set"] for i in SK.listing()}["googler"], False)

    print("Пул: ключей несколько")
    seed({"googler": OLD, "googler_pool": ["fg_второй_ключ_2222", "fg_третий_3333"]})
    g = {i["family"]: i for i in SK.listing()}["googler"]
    check("сказано, что их несколько", (g["pooled"], g["count"]), (True, 3))

    print("Пульт: только администратор, значение не возвращается")
    import app as W
    from fastapi import HTTPException

    class Req:
        def __init__(self, role):
            class U(dict):
                def keys(self):
                    return super().keys()
            self.state = type("St", (), {"user": U(id=1, name="boss", role=role)})()

    seed()
    for fn, args in ((W.ui_server_keys, ()),
                     (W.ui_server_key_set, (W.ServerKeyIn(family="googler", value=NEW),)),
                     (W.ui_server_key_drop, ("googler",))):
        try:
            fn(*args, Req("user")) if args else fn(Req("user"))
            check(f"{fn.__name__} без прав", "прошло", 403)
        except HTTPException as exc:
            check(f"{fn.__name__} без прав — 403", exc.status_code, 403)

    got = W.ui_server_keys(Req("admin"))
    check("админ видит список", len(got["items"]) >= 8, True)
    check("в ответе нет значения", OLD in json.dumps(got, ensure_ascii=False), False)
    out = W.ui_server_key_set(W.ServerKeyIn(family="googler", value=NEW), Req("admin"))
    check("замена прошла", out["item"]["tail"], "…9876")
    check("значение не вернулось", NEW in json.dumps(out, ensure_ascii=False), False)
    try:
        W.ui_server_key_set(W.ServerKeyIn(family="googler", value="abc"), Req("admin"))
        check("короткий ключ", "прошло", 400)
    except HTTPException as exc:
        check("короткий ключ — 400 с причиной",
              (exc.status_code, "короче" in exc.detail), (400, True))

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
