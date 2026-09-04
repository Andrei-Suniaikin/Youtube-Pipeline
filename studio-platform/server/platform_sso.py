#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Единый вход: соседи узнают человека по сессии кабинета.

До этого у Rewrite и Cover был свой экран входа, свой список пользователей и свой
пароль — три учётки на одного человека, склеенные только совпадением логина.

Здесь личность приходит из одного места. Это **не** доверенный заголовок: снаружи
приходит только кука `vs_session`, а проверяет её сам кабинет своей же функцией
`validate_session` — с тем же сроком, той же блокировкой и той же базой. Подделать
её нельзя ровно в той же мере, в какой нельзя подделать вход в сам кабинет.

Работает, только когда кабинет загружен в том же процессе, то есть в единой платформе
(`web/platform.py`). При раздельном запуске соседи ведут себя как раньше — со своим
входом; так и задумано, пока переезд не закончен.

**Требует выполненной миграции личности** (`dev/migrate_identity.py`). До неё данные
человека у соседа лежат под их собственным случайным `user_id`, и вход по сессии
кабинета показал бы пустой профиль вместо чужого — но всё же пустой. Поэтому миграция
и единый вход выкатываются одной выкаткой, а не по очереди.
"""
from __future__ import annotations

import sys

COOKIE = "vs_session"


def cabinet_user(request):
    """{user_id, user_name} по куке кабинета либо None.

    `user_id` — это `tenant_id`: тот самый общий идентификатор, к которому миграция
    приводит данные во всех трёх сервисах.
    """
    mod = sys.modules.get("main")
    if mod is None or not hasattr(mod, "validate_session"):
        return None                      # запущены отдельно — единого входа нет
    try:
        sid = request.cookies.get(COOKIE) or ""
    except Exception:                                              # noqa: BLE001
        return None
    if not sid:
        return None
    try:
        u = mod.validate_session(sid)
    except Exception:                                              # noqa: BLE001
        return None
    if u is None:
        return None
    return {"user_id": str(u["tenant_id"] or u["id"]),
            "user_name": u["name"],
            "role": (u["role"] if "role" in u.keys() else "") or ""}
