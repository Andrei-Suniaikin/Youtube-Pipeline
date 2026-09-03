#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вход в пульт — это вход в кабинет.

Своего списка пользователей у пульта нет и не будет. Три сервиса сегодня опознают
человека по-разному, и главный нерешённый вопрос переписывания — кто из них хозяин
личности (`docs/01_TASKS.md`, C1–C3). Завести здесь четвёртую учётную запись значило
бы решить этот вопрос молча и неправильно.

Поэтому пароль проверяет кабинет: пульт пересылает его на `POST /login` и забирает
выданную сессию. Ни хэшей, ни паролей, ни своих ролей.

Одно исключение сделано осознанно: проверка уже выданной сессии читается из той же
базы тем же запросом, что и `main.validate_session`. Импортировать ради этого
`main.py` нельзя — это 17 000 строк и запуск всего кабинета внутри пульта; ходить
по HTTP на каждый чих читающего экрана — лишний круг. Дублируются десять строк,
и они помечены: при слиянии в кабинет этот файл исчезает целиком.
"""
from __future__ import annotations

import datetime
import http.cookies
import os
import urllib.error
import urllib.parse
import urllib.request

COOKIE = "vs_session"                    # то же имя, что у кабинета: значение то же самое
TIMEOUT = 10

# secure=True по умолчанию: снимать защиту должен тот, кто знает, что он в песочнице,
# а не тот, кто забыл её включить в production.
INSECURE = (os.environ.get("VS_DEV_INSECURE_COOKIE") or "").strip() in ("1", "true", "yes")


class LoginFailed(Exception):
    pass


def login(base: str, name: str, password: str) -> str:
    """Спросить кабинет. Возвращает id сессии либо бросает LoginFailed.

    Кабинет на успешный вход отвечает 302 с Set-Cookie, на неуспешный — 200 со
    страницей входа. Различаем именно по этому, а не по тексту ответа.
    """
    data = urllib.parse.urlencode({"login": name, "password": password}).encode("utf-8")
    req = urllib.request.Request(f"{base.rstrip('/')}/login", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_a, **_k):
            return None                  # 302 — это и есть успех, идти по нему незачем

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return _sid(resp.headers.get_all("Set-Cookie") or []) or _fail(resp)
    except urllib.error.HTTPError as exc:
        sid = _sid(exc.headers.get_all("Set-Cookie") or [])
        if sid:
            return sid
        raise LoginFailed("неверный логин или пароль")
    except urllib.error.URLError as exc:
        raise LoginFailed(f"кабинет не отвечает: {exc.reason}")


def _fail(_resp):
    raise LoginFailed("неверный логин или пароль")


def _sid(headers) -> str:
    for raw in headers:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            continue
        if COOKIE in jar:
            return jar[COOKIE].value
    return ""


def who(c, sid: str):
    """Кто это. None — сессии нет, истекла или учётка заблокирована.

    ДУБЛИРУЕТ main.validate_session сознательно — см. шапку модуля. Три условия
    выхода те же, и это не случайность: блокировка из админ-панели должна действовать
    на уже выданные сессии, иначе заблокированный партнёр продолжает работать
    в открытой вкладке до истечения куки.
    """
    if not sid:
        return None
    row = c.execute(
        "SELECT s.expires AS _exp, u.* FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.id=?", (sid,)).fetchone()
    if not row:
        return None
    if row["_exp"] and row["_exp"] < datetime.datetime.now().timestamp():
        return None
    if _disabled(row):
        return None
    return row


def _disabled(row) -> bool:
    try:
        return bool(row["disabled"])
    except (IndexError, KeyError):
        return False


def logout(base: str, sid: str) -> None:
    """Погасить сессию там же, где она заведена. Молча: выход не должен падать."""
    if not sid:
        return
    req = urllib.request.Request(f"{base.rstrip('/')}/logout", data=b"", method="POST")
    req.add_header("Cookie", f"{COOKIE}={sid}")
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT).close()
    except Exception:                                              # noqa: BLE001
        pass
