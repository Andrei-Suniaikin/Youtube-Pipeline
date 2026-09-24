#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вход ноды заливки: чужой токен — отказ, мятый токен — тоже отказ, а не пятисотка.

Найдено 23.09.2026 на боевой выкатке: `secrets.compare_digest` на строках с не-ASCII
бросает TypeError, и нода, приславшая заголовок с чем угодно в нём, получала 500
«внутренняя ошибка». Пятисотка тут вреднее отказа — выглядит как поломка сервера и
уводит разбор не туда, а на машине заливки в этот момент ролик ждёт отправки.

Стенда не нужно: маршруты регистрируются на пустом приложении со своей базой в памяти.

    .venv/bin/python tests/test_upload_auth.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

from fastapi import FastAPI                                       # noqa: E402
from fastapi.testclient import TestClient                         # noqa: E402

from pub_schema import ensure_pub_schema                           # noqa: E402
from pub_upload_api import register_upload_routes                 # noqa: E402

FAILS = []
TOKEN = "upl_Zx9-boevoy-token"             # такой выдаёт кабинет: только ASCII


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def main() -> int:
    # База файлом, а не в памяти: обработчик закрывает соединение за собой, как в бою,
    # и каждое обращение просит новое — у памяти оно было бы пустым.
    path = os.path.join(tempfile.mkdtemp(prefix="upload-auth-"), "vsgen.db")

    def db():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    conn = db()
    # Схему берём НАСТОЯЩУЮ, а не выдуманную: выдача задания читает несколько таблиц,
    # и на самодельной заглушке отказ пришёл бы из sqlite, а не из проверки токена.
    ensure_pub_schema(conn)
    conn.commit()
    conn.close()

    app = FastAPI()
    register_upload_routes(app, {"db": db, "_now": lambda: 1790000000.0,
                                 "upload_token": lambda: TOKEN})
    client = TestClient(app, raise_server_exceptions=False)

    def claim(token):
        # Заголовок шлём БАЙТАМИ: по сети он так и приходит, а тестовый клиент строки
        # с не-ASCII отправлять отказывается — сеть же отказать не может.
        headers = {} if token is None else {"X-Upload-Token": (
            token.encode("utf-8") if isinstance(token, str) else token)}
        return client.post("/api/upload/claim", json={"host": "нода", "worker": "1"},
                           headers=headers)

    print("Кого не пускаем")
    check("без токена — 401", claim(None).status_code, 401)
    check("пустой токен — 401", claim("").status_code, 401)
    check("чужой токен — 401", claim("upl_чужой").status_code, 401)
    # Ровно тот случай с боевой выкатки: не-ASCII в заголовке.
    r = claim("проверка")   # ровно то, что прилетело на боевой выкатке
    check("мятый токен — 401, а не 500", r.status_code, 401)
    check("…и сказано, чего не хватает", "токен" in r.json().get("detail", ""), True)
    check("очень длинный мусор — 401", claim("ы" * 5000).status_code, 401)

    print("Кого пускаем")
    r = claim(TOKEN)
    check("верный токен — 200", r.status_code, 200)
    check("работы нет — так и сказано", r.json(), {"task": None})

    print("Клейм задания — та же строгость")
    conn = db()
    conn.execute(
        "INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,channel_url,"
        "title,visibility,video_path,slot_at,status,created,updated,claim_token) "
        "VALUES('rel-1','t1',1,'UC1','https://studio.youtube.com/channel/UC1',"
        "'ролик','public','/tmp/v.mp4',0,'running',1,1,'clm-live-9x')")
    conn.commit()
    def progress(claim_token):
        return client.post("/api/upload/rel-1/progress", json={"phase": "preflight"},
                           headers={"X-Upload-Token": TOKEN.encode("utf-8"),
                                    "X-Upload-Claim": claim_token.encode("utf-8")})
    check("чужой клейм — 403", progress("clm-other-7y").status_code, 403)
    check("мятый клейм — 403, а не 500", progress("клейм").status_code, 403)
    check("свой клейм — не отказ",
          progress("clm-live-9x").status_code not in (403, 500), True)

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
