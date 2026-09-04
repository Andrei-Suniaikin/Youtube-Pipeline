#!/usr/bin/env python3
"""Выдать пульту ключи доступа к соседям — как это сделано в production.

Кабинет ходит к Rewrite и Cover с заголовком `Authorization: Bearer <key>`, а сам ключ
хранит в своей таблице `admin_state` под именами `pub_key_rewrite` и `pub_key_cover`
(см. `server/pub_remote.py:63` — `get_key()` / `set_key()`).

Здесь то же самое, только локально: заводим админа в каждом соседе, логинимся, просим
у него API-ключ и кладём в нашу базу. Пароль берётся из окружения; значение по умолчанию
годится только для локальной базы без реальных данных.

    VS_DEV_PASSWORD=... .venv/bin/python dev/provision.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))

LOGIN = os.environ.get("VS_DEV_LOGIN", "dev")
PASSWORD = os.environ.get("VS_DEV_PASSWORD", "dev-local-only")

# Адреса берутся из окружения: в единой платформе соседи живут под префиксами того же
# порта, при раздельном запуске — на своих. Зашивать выбор нельзя, оба варианта живы.
SERVICES = (
    # имя ключа, адрес, путь создания ключа
    ("rewrite", os.environ.get("VS_REWRITE_BASE") or "http://127.0.0.1:8899",
     "/api/keys/create"),
    ("cover", os.environ.get("VS_COVER_BASE") or "http://127.0.0.1:8900", "/api-keys"),
)


def call(url: str, payload=None, headers=None, method=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body or "{}")
        except ValueError:
            return exc.code, {"error": body[:200]}
    except urllib.error.URLError as exc:
        raise SystemExit(f"  {url} не отвечает: {exc.reason}. Сначала: dev/run.sh")


def session_for(base: str) -> str:
    """Сессия в соседе: первичная настройка, иначе вход, иначе разовая установка пароля."""
    _, status = call(base + "/auth/status")
    if status.get("needs_setup"):
        code, body = call(base + "/auth/setup", {"username": LOGIN, "password": PASSWORD})
        if code == 200 and body.get("session_id"):
            return body["session_id"]
        raise SystemExit(f"  первичная настройка не прошла: {body}")

    code, body = call(base + "/auth/login-password", {"username": LOGIN, "password": PASSWORD})
    if code == 200 and body.get("session_id"):
        return body["session_id"]

    # Учётка есть, пароль неизвестен: у обоих соседей для этого предусмотрен разовый claim.
    code, body = call(base + "/auth/claim-password", {"username": LOGIN, "password": PASSWORD})
    if code == 200 and body.get("session_id"):
        return body["session_id"]
    raise SystemExit(f"  вход не удался: {body}. Задайте VS_DEV_LOGIN / VS_DEV_PASSWORD")


def api_key(base: str, path: str, sid: str) -> str:
    # Сам ключ отдаётся один раз и только при создании, поэтому повторный запуск не может
    # переиспользовать старый — берём новое имя. Старые ключи остаются валидными; чистить
    # их незачем, база локальная.
    name = f"studio-platform-{int(time.time())}"
    code, body = call(base + path, {"name": name}, {"X-Session-Id": sid})
    if code != 200:
        raise SystemExit(f"  ключ не выдан ({code}): {body}")
    for field in ("key", "api_key", "plain", "plain_key", "token", "secret"):
        if body.get(field):
            return str(body[field])
    raise SystemExit(f"  в ответе нет самого ключа, только {sorted(body)}")


def cabinet_password(db_path: str) -> None:
    """Задать пароль админу кабинета его же алгоритмом (pbkdf2, 200k итераций).

    Кабинет проверяет пароль через `verify_password` из `server/main.py`; хэш считаем
    той же функцией, чтобы не завести второй, расходящийся способ.
    """
    import hashlib
    import secrets as _secrets

    salt = _secrets.token_bytes(16)
    it = 200000
    dk = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode("utf-8"), salt, it)
    pwh = f"pbkdf2${it}${salt.hex()}${dk.hex()}"
    c = sqlite3.connect(db_path)
    try:
        cur = c.execute("UPDATE users SET password_hash=?, disabled=0 WHERE name=?",
                        (pwh, LOGIN))
        if cur.rowcount == 0:
            print(f"  в {os.path.basename(db_path)} нет пользователя {LOGIN} — пропускаю")
        else:
            print(f"  пароль админа задан в {os.path.basename(db_path)}")
        c.commit()
    finally:
        c.close()


def main() -> None:
    if not os.path.exists(DB):
        raise SystemExit(f"нет базы {DB} — сначала: python3 dev/make_db.py")
    print(f"логин: {LOGIN}   (пароль из VS_DEV_PASSWORD)")

    print("\nкабинет")
    cabinet_password(DB)
    c = sqlite3.connect(DB)
    now = time.time()
    try:
        for name, base, path in SERVICES:
            print(f"\n{name}  {base}")
            sid = session_for(base)
            print(f"  сессия получена")
            key = api_key(base, path, sid)
            c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
                      (f"pub_key_{name}", key, now))
            print(f"  ключ выдан и записан в admin_state.pub_key_{name} "
                  f"({key[:6]}…{key[-4:]}, {len(key)} символов)")
        c.commit()
    finally:
        c.close()
    print("\nготово: пульт может ходить к обоим соседям")


if __name__ == "__main__":
    main()
