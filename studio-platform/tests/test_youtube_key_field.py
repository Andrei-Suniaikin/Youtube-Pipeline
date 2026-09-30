#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ключ YouTube Data API вставляется с экрана, а не по SSH (29.09.2026).

Владелец: «есть ли у нас поле для вставки серверного ключа такого типа у админа?» — не
было: ключ читался только из окружения или прямой записи в базу, то есть заводился с
боевой машины. У заказчика SSH нет и быть не должно.

Проверяется старшинство, и оно не произвольное:

  окружение → файл ключей (его правит форма) → admin_state

Файл ключей ВЫШЕ базы именно потому, что в него пишет форма. Будь наоборот, владелец
вставил бы новый ключ, увидел «сохранено» и не понял, почему ничего не изменилось:
старое значение из базы продолжало бы побеждать. А `admin_state` остаётся последним и
поддерживается — на установках, где ключ уже лежит там, молча перестать его видеть
значило бы погасить проверку публикаций.

    .venv/bin/python tests/test_youtube_key_field.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="yt-key-")
DB = os.path.join(TMP, "vsgen.db")
KEYS = os.path.join(TMP, "keys", "video_studio_api_keys.json")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_SERVER_KEYS_PATH"] = KEYS
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ.pop("VS_YT_API_KEY", None)
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

_c = sqlite3.connect(DB)
_c.execute("CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, "
           "updated REAL)")
_c.commit()
_c.close()

import server_keys                                                 # noqa: E402
import pub_verify                                                  # noqa: E402

FAILS = []
FROM_FILE = "AIza-из-файла-ключей-0000"
FROM_DB = "AIza-из-базы-1111"
FROM_ENV = "AIza-из-окружения-2222"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def main() -> int:
    print("Поле заведено на экране «Ключи»")
    names = [n for n, _t in server_keys.EDITABLE]
    check("семейство есть", "youtube_api_key" in names, True)
    title = dict(server_keys.EDITABLE)["youtube_api_key"]
    check("названо по-человечески", "YouTube" in title, True)
    check("и объяснено, зачем", "проверка выхода" in title, True)

    print("Ничего не задано — ключа нет")
    check("пусто", pub_verify.api_key(conn()), "")

    print("Только база — берётся база (как было до правки)")
    c = conn()
    c.execute("INSERT OR REPLACE INTO admin_state VALUES('youtube_api_key',?,0)",
              (FROM_DB,))
    c.commit()
    c.close()
    check("ключ из базы", pub_verify.api_key(conn()), FROM_DB)

    print("Вставили с экрана — побеждает вставленное")
    out = server_keys.set_value("youtube_api_key", FROM_FILE)
    check("форма приняла", out["set"], True)
    check("значение наружу не вернулось",
          FROM_FILE in json.dumps(out, ensure_ascii=False), False)
    check("виден только хвост", out["tail"].endswith(FROM_FILE[-4:]), True)
    check("ключ теперь из файла", pub_verify.api_key(conn()), FROM_FILE)

    print("Окружение старше всего — им чинят машину")
    os.environ["VS_YT_API_KEY"] = FROM_ENV
    try:
        check("ключ из окружения", pub_verify.api_key(conn()), FROM_ENV)
    finally:
        os.environ.pop("VS_YT_API_KEY", None)
    check("без окружения снова файл", pub_verify.api_key(conn()), FROM_FILE)

    print("Сняли с экрана — возвращается то, что в базе")
    server_keys.drop("youtube_api_key")
    check("ключ снова из базы", pub_verify.api_key(conn()), FROM_DB)

    print("Файл ключей испорчен — старый ключ из базы не теряется")
    # Испорченный файл не должен гасить проверку публикаций: это разные хранилища, и
    # поломка одного не повод ослепнуть целиком.
    with open(KEYS, "w", encoding="utf-8") as fh:
        fh.write("{это не json")
    check("база спасает", pub_verify.api_key(conn()), FROM_DB)

    print("Списком ключей значение не показывается")
    with open(KEYS, "w", encoding="utf-8") as fh:
        json.dump({"youtube_api_key": FROM_FILE}, fh, ensure_ascii=False)
    shown = json.dumps(server_keys.listing(), ensure_ascii=False)
    check("значения в списке нет", FROM_FILE in shown, False)
    check("а хвост есть", FROM_FILE[-4:] in shown, True)

    print("Тот же ключ второй раз не принимается")
    # Не придирчивость: «сохранено» на неизменённом значении выглядит как успешная
    # замена просроченного ключа, которой не было.
    try:
        server_keys.set_value("youtube_api_key", FROM_FILE)
        check("отказ на повторе", "прошло без отказа", "отказ")
    except server_keys.KeyError_ as exc:
        check("отказ на повторе", "уже задан" in str(exc), True)

    print("Файл остаётся закрытым")
    server_keys.set_value("youtube_api_key", FROM_FILE + "-новый")
    check("права 0600", oct(os.stat(KEYS).st_mode)[-3:], "600")

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
