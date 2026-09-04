#!/usr/bin/env python3
"""Выдать пульту ключи доступа к соседям — как это сделано в production.

Кабинет ходит к Rewrite и Cover с заголовком `Authorization: Bearer <key>`, а сам ключ
хранит в своей таблице `admin_state` под именами `pub_key_rewrite` и `pub_key_cover`
(см. `server/pub_remote.py` — `get_key()` / `set_key()`).

ПЕРЕПИСАНО 04.09.2026. Раньше скрипт заводил у каждого соседа отдельную учётку и
логинился в неё своим паролем — то есть держал три пароля на одного человека. После
слияния личности этого не нужно: сессия кабинета открывает всех соседей (единый вход),
и ключ создаётся ею. Один пароль, один вход, три ключа.

    VS_DEV_PASSWORD=... .venv/bin/python dev/provision.py

Требует поднятого стенда: ключи выдают сами соседи, а не мы за них.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import http.cookies
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))

LOGIN = os.environ.get("VS_DEV_LOGIN", "dev")
PASSWORD = os.environ.get("VS_DEV_PASSWORD", "dev-local-only")

PLATFORM = os.environ.get("VS_PLATFORM_BASE") or "http://127.0.0.1:8080"

# Где искать соседа. Порядок: что сказали в окружении → единая платформа под префиксом
# → старый отдельный порт. Пробуем по очереди, а не гадаем: оба способа запуска живы,
# и требовать от человека помнить, каким он поднял стенд, — лишняя работа.
def _base(name: str, own_port: int) -> str:
    said = os.environ.get(f"VS_{name.upper()}_BASE")
    if said:
        return said.rstrip("/")
    for candidate in (f"{PLATFORM.rstrip('/')}/{name}", f"http://127.0.0.1:{own_port}"):
        try:
            req = urllib.request.Request(candidate + "/auth/status")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    return candidate
        except Exception:                                          # noqa: BLE001
            continue
    return f"http://127.0.0.1:{own_port}"      # ничего не ответило — скажем об этом внятно


SERVICES = (
    # имя ключа, адрес, путь создания ключа
    ("rewrite", _base("rewrite", 8899), "/api/keys/create"),
    ("cover", _base("cover", 8900), "/api-keys"),
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
        raise SystemExit(f"  {url} не отвечает: {exc.reason}.\n"
                         f"  Сначала поднимите стенд: dev/run.sh\n"
                         f"  Если он на другом адресе: VS_PLATFORM_BASE=http://…:ПОРТ")


def cabinet_session() -> str:
    """Войти в кабинет и получить его сессию. Ею же открываются соседи.

    Кабинет на успешный вход отвечает 302 с Set-Cookie, на неуспешный — 200 со
    страницей входа. Различаем по этому, а не по тексту.
    """
    data = urllib.parse.urlencode({"login": LOGIN, "password": PASSWORD}).encode("utf-8")
    req = urllib.request.Request(PLATFORM.rstrip("/") + "/login", data=data,
                                 method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_a, **_k):
            return None

    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=15) as resp:
            headers = resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as exc:
        headers = exc.headers.get_all("Set-Cookie") or []
    except urllib.error.URLError as exc:
        raise SystemExit(f"  кабинет {PLATFORM} не отвечает: {exc.reason}\n"
                         f"  Сначала поднимите стенд: dev/run.sh")
    for raw in headers:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            continue
        if "vs_session" in jar:
            return jar["vs_session"].value
    raise SystemExit(f"  вход в кабинет не удался. Пароль задаётся тем же запуском: "
                     f"VS_DEV_PASSWORD=... dev/provision.py")


def api_key(base: str, path: str, sid: str) -> str:
    # Сам ключ отдаётся один раз и только при создании, поэтому повторный запуск не может
    # переиспользовать старый — берём новое имя. Старые ключи остаются валидными; чистить
    # их незачем, база локальная.
    name = f"studio-platform-{int(time.time())}"
    # Соседа открывает кука кабинета: после слияния личности он узнаёт человека
    # по ней, а своей сессии у него для нас больше нет.
    code, body = call(base + path, {"name": name}, {"Cookie": f"vs_session={sid}"})
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
    sid = cabinet_session()
    print(f"  вход выполнен, сессия получена")

    c = sqlite3.connect(DB)
    now = time.time()
    try:
        for name, base, path in SERVICES:
            print(f"\n{name}  {base}")
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
