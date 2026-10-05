#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Три поля Google не принимают значения друг друга (05.10.2026).

Client secret попал в поле ключа YouTube Data API: YouTube ответил «API key not valid»,
а проверка выхода роликов, увидев «ключ есть», перестала ходить по публичным страницам.
Теперь форма узнаёт чужое значение по виду и говорит, в какую строку его нести:
Client ID кончается на .apps.googleusercontent.com, Client secret начинается с GOCSPX-,
ключ API — с AIza.

    .venv/bin/python tests/test_google_key_shapes.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

TMP = tempfile.mkdtemp(prefix="google-keys-")
KEYS = os.path.join(TMP, "keys.json")
json.dump({"googler": "fastgen-test-0000000000"}, open(KEYS, "w"))
os.environ["VS_SERVER_KEYS_PATH"] = KEYS

import server_keys as sk                                            # noqa: E402

FAILS = []
CID = "123456789012-abcdefghijklmnopqrstuvwxyz012345.apps.googleusercontent.com"
SECRET = "GOCSPX-abcdefghijklmnopqrstuvwxyz1"
APIKEY = "AIzaSyA-abcdefghijklmnopqrstuvwxyz0123"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def refused(family, value):
    try:
        sk.set_value(family, value)
    except sk.KeyError_ as exc:
        return str(exc)
    return ""


print("Свои значения принимаются")
check("Client ID", sk.set_value("yt_oauth_client_id", CID)["set"], True)
check("Client secret", sk.set_value("yt_oauth_client_secret", SECRET)["set"], True)
check("ключ API", sk.set_value("youtube_api_key", APIKEY)["set"], True)
check("старый секрет без GOCSPX- тоже принимается",
      sk.set_value("yt_oauth_client_secret", "legacy-secret-abcdefghijklmnop")["set"], True)

print("\nЧужие — отказ с подсказкой, куда нести")
why = refused("youtube_api_key", SECRET)
check("секрет в поле ключа API — отказ (случай 05.10)", bool(why), True)
check("подсказка: в строку Client secret", "«Google — Client secret приложения»" in why, True)
why = refused("youtube_api_key", CID)
check("Client ID в поле ключа API — отказ", "«Google — Client ID приложения»" in why, True)
check("секрет в поле Client ID — отказ", bool(refused("yt_oauth_client_id", SECRET)), True)
check("ключ API в поле Client ID — отказ",
      "«YouTube Data API — ключ API»" in refused("yt_oauth_client_id", APIKEY), True)
check("Client ID в поле секрета — отказ", bool(refused("yt_oauth_client_secret", CID)), True)
check("ключ API в поле секрета — отказ", bool(refused("yt_oauth_client_secret", APIKEY)), True)
check("ключ API без AIza — отказ с объяснением, где взять",
      "Credentials" in refused("youtube_api_key", "abcdefghijklmnopqrstuvwxyz"), True)
check("Client ID без хвоста — отказ",
      "apps.googleusercontent.com" in refused("yt_oauth_client_id", "123456789012-abcdef"), True)

print("\nОтказ ничего не записал")
data = json.load(open(KEYS))
check("в поле ключа API — прежний ключ", data.get("youtube_api_key"), APIKEY)
check("в Client ID — прежний", data.get("yt_oauth_client_id"), CID)

print("\nДругие провайдеры не затронуты")
check("FastGen принимает любой вид", sk.set_value("googler", "GOCSPX-fastgen-0000000000")["set"], True)

print(f"\nпровалов: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
