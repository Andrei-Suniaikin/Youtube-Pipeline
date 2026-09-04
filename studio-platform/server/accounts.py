#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Учётные записи: завести, заблокировать, сменить пароль и роль.

Заводить людей раньше было можно тремя разными способами, и все три жили порознь:
скрипт `create_partner.py` из командной строки, роут админ-панели `/admin/api/create_partner`
по статичному токену и ручная правка базы. Каждый делал то же самое своим кодом, и
расхождение было вопросом времени: партнёр, заведённый одним способом, отличался бы от
заведённого другим — например, без каталога тенанта или без записи в `tenants`.

Здесь это в одном месте. Правила, которые нельзя нарушать и поэтому они проверяются
здесь, а не в интерфейсе:

* **Тенант равен идентификатору пользователя.** По нему раскладываются проекты
  (`/data/tenants/<tid>/projects`) и по нему же после слияния личности человека узнают
  Rewrite и Cover. Разъедься они — и партнёр увидит чужие пресеты.
* **Последнего администратора нельзя ни заблокировать, ни разжаловать.** Иначе системой
  перестаёт управлять кто-либо, и починить это можно только правкой базы.
* **Блокировка гасит живые сессии.** Иначе заблокированный работает в открытой вкладке
  до истечения куки.
* **Пароль наружу не отдаётся никогда** — ни хэш, ни соль.
"""
from __future__ import annotations

import datetime
import os
import re
import secrets

LOGIN_RE = re.compile(r"^[A-Za-z0-9._-]{2,32}$")
ROLES = ("admin", "user")
MIN_PASSWORD = 6


class AccountError(ValueError):
    """Отказ, который надо показать человеку словами."""


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def check_login(login: str) -> str:
    login = (login or "").strip()
    if not LOGIN_RE.match(login):
        raise AccountError("логин: латиница, цифры, точка, дефис или подчёркивание, "
                           "от 2 до 32 символов")
    return login


def check_password(password: str) -> str:
    password = password or ""
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"пароль короче {MIN_PASSWORD} символов")
    return password


def admin_count(c, exclude_id=None) -> int:
    sql = "SELECT COUNT(*) FROM users WHERE role='admin' AND COALESCE(disabled,0)=0"
    args = ()
    if exclude_id is not None:
        sql += " AND id<>?"
        args = (int(exclude_id),)
    return c.execute(sql, args).fetchone()[0]


def list_users(c) -> list:
    """Все учётные записи. Ни хэша, ни соли, ни API-токена здесь нет."""
    out = []
    for r in c.execute("SELECT id, name, role, tenant_id, daily_quota, created, "
                       "       COALESCE(disabled,0) disabled, "
                       "       (password_hash IS NOT NULL AND password_hash!='') has_pw "
                       "FROM users ORDER BY id"):
        out.append({"id": r["id"], "name": r["name"], "role": r["role"] or "user",
                    "tenant_id": r["tenant_id"] or str(r["id"]),
                    "quota": r["daily_quota"], "created": r["created"],
                    "disabled": bool(r["disabled"]), "has_password": bool(r["has_pw"])})
    return out


def create_user(c, login: str, password: str, hash_password, role: str = "user",
                quota=None, tenants_dir: str = "") -> dict:
    """Завести человека: учётка, тенант, каталог. Всё три или ничего.

    `hash_password` приходит извне — это функция кабинета, и второго алгоритма хэширования
    в системе быть не должно.
    """
    login = check_login(login)
    check_password(password)
    if role not in ROLES:
        raise AccountError(f"роль бывает только {' или '.join(ROLES)}")
    if c.execute("SELECT 1 FROM users WHERE name=?", (login,)).fetchone():
        raise AccountError(f"логин «{login}» уже занят")
    try:
        quota = int(quota) if str(quota).strip() not in ("", "None") else None
    except (TypeError, ValueError):
        raise AccountError("квота: целое число или пусто")

    now = _now_iso()
    cur = c.execute(
        "INSERT INTO users(name,token,role,created,password_hash,daily_quota) "
        "VALUES(?,?,?,?,?,?)",
        (login, "usr_" + secrets.token_urlsafe(24), role, now,
         hash_password(password), quota))
    uid = cur.lastrowid
    tid = str(uid)
    c.execute("UPDATE users SET tenant_id=? WHERE id=?", (tid, uid))
    c.execute("INSERT OR REPLACE INTO tenants(id,name,created,is_active,daily_quota) "
              "VALUES(?,?,?,1,?)", (tid, login, now, quota))
    c.commit()
    made_dir = ""
    if tenants_dir:
        try:
            made_dir = os.path.join(tenants_dir, tid, "projects")
            os.makedirs(made_dir, exist_ok=True)
        except OSError:
            made_dir = ""      # каталог создастся при первом проекте — не повод падать
    return {"id": uid, "tenant_id": tid, "login": login, "dir": made_dir}


def set_disabled(c, uid: int, disabled: bool, actor_id=None) -> dict:
    """Заблокировать или вернуть доступ. Блокировка гасит живые сессии."""
    r = c.execute("SELECT id, name, role, COALESCE(disabled,0) disabled FROM users "
                  "WHERE id=?", (int(uid),)).fetchone()
    if not r:
        raise AccountError("нет такой учётной записи")
    disabled = bool(disabled)
    if disabled and actor_id is not None and int(uid) == int(actor_id):
        raise AccountError("нельзя заблокировать самого себя")
    if disabled and (r["role"] or "") == "admin" and admin_count(c, exclude_id=uid) == 0:
        raise AccountError("это последний администратор — заблокировать его значит "
                           "оставить систему без управления")
    c.execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, int(uid)))
    if disabled:
        c.execute("DELETE FROM sessions WHERE user_id=?", (int(uid),))
    c.commit()
    return {"id": r["id"], "name": r["name"], "disabled": disabled}


def set_role(c, uid: int, role: str, actor_id=None) -> dict:
    if role not in ROLES:
        raise AccountError(f"роль бывает только {' или '.join(ROLES)}")
    r = c.execute("SELECT id, name, role FROM users WHERE id=?", (int(uid),)).fetchone()
    if not r:
        raise AccountError("нет такой учётной записи")
    if role != "admin" and (r["role"] or "") == "admin" \
            and admin_count(c, exclude_id=uid) == 0:
        raise AccountError("это последний администратор — снять с него роль значит "
                           "оставить систему без управления")
    c.execute("UPDATE users SET role=? WHERE id=?", (role, int(uid)))
    c.commit()
    return {"id": r["id"], "name": r["name"], "role": role}


def set_password(c, uid: int, password: str, hash_password, drop_sessions=True) -> dict:
    """Задать пароль. Живые сессии гасим: смена пароля должна закрывать чужой вход."""
    check_password(password)
    r = c.execute("SELECT id, name FROM users WHERE id=?", (int(uid),)).fetchone()
    if not r:
        raise AccountError("нет такой учётной записи")
    c.execute("UPDATE users SET password_hash=? WHERE id=?",
              (hash_password(password), int(uid)))
    if drop_sessions:
        c.execute("DELETE FROM sessions WHERE user_id=?", (int(uid),))
    c.commit()
    return {"id": r["id"], "name": r["name"]}


def set_quota(c, uid: int, quota) -> dict:
    try:
        value = int(quota) if str(quota).strip() not in ("", "None") else None
    except (TypeError, ValueError):
        raise AccountError("квота: целое число или пусто")
    r = c.execute("SELECT id, name, tenant_id FROM users WHERE id=?",
                  (int(uid),)).fetchone()
    if not r:
        raise AccountError("нет такой учётной записи")
    c.execute("UPDATE users SET daily_quota=? WHERE id=?", (value, int(uid)))
    c.execute("UPDATE tenants SET daily_quota=? WHERE id=?",
              (value, r["tenant_id"] or str(r["id"])))
    c.commit()
    return {"id": r["id"], "name": r["name"], "quota": value}
