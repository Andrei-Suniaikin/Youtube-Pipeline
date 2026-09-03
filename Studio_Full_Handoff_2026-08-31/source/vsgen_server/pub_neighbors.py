#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Списки пресетов соседних сервисов (Rewrite и Cover) для карточки канала.

Все три сервиса живут на одной машине под одним владельцем, поэтому списки читаются
прямо из их хранилищ — без ключей, токенов и сетевых вызовов. Только чтение.

Пользователи у сервисов свои, общего идентификатора нет; связка идёт по ИМЕНИ входа
(`example_user` в кабинете = `example_user` в рерайте = `example_user` в обложках).

Любая ошибка — пустой список, а не исключение: карточка канала не должна падать из-за
того, что сосед переехал или остановлен. Пустой список интерфейс показывает как
«список недоступен» и оставляет ручной ввод.
"""

from __future__ import annotations

import json
import os
import sqlite3

REWRITE_DIR = os.environ.get("VS_REWRITE_DIR", "/root/rewrite-server")
COVER_DIR = os.environ.get("VS_COVER_DIR", "/root/cover_tool")

REWRITE_URL = os.environ.get("VS_REWRITE_URL", "https://rewrite.example.invalid")
COVER_URL = os.environ.get("VS_COVER_URL", "https://cover.example.invalid")


def _user_id(db_path: str, username: str) -> str:
    """Найти идентификатор пользователя соседа по имени входа."""
    if not username or not os.path.exists(db_path):
        return ""
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = c.execute(
                "SELECT user_id FROM users WHERE username=? COLLATE NOCASE LIMIT 1",
                (username,)).fetchone()
        finally:
            c.close()
        return row[0] if row else ""
    except Exception:
        return ""


def _names_from_json(path: str, key: str = "") -> list:
    """Достать имена пресетов из файла соседа. Формат у сервисов разный."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if key:
        data = data.get(key) or {}
    if isinstance(data, dict):
        names = list(data.keys())
    elif isinstance(data, list):
        names = [x.get("name") for x in data if isinstance(x, dict) and x.get("name")]
    else:
        return []
    return sorted((n for n in names if isinstance(n, str) and n.strip()), key=str.lower)


def rewrite_presets(username: str) -> list:
    """Пресеты РЕРАЙТА (не генерации!).

    Живут в sessions.db → user_keys.rew_presets, а не в presets/<uid>/generation.json:
    там лежат брифы для создания текста с нуля, к рерайту по ссылке они отношения не имеют.
    """
    return sorted(_rewrite_preset_map(username).keys(), key=str.lower)


def rewrite_preset_config(username: str, name: str) -> dict:
    """Содержимое пресета рерайта: модель, потоки, размер куска, стиль обращения."""
    got = _rewrite_preset_map(username).get(name)
    return got if isinstance(got, dict) else {}


def _rewrite_preset_map(username: str) -> dict:
    db_path = os.path.join(REWRITE_DIR, "sessions.db")
    uid = _user_id(db_path, username)
    if not uid or not os.path.exists(db_path):
        return {}
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = c.execute("SELECT rew_presets FROM user_keys WHERE user_id=?",
                            (uid,)).fetchone()
        finally:
            c.close()
    except Exception:
        return {}
    if not row or not row[0]:
        return {}
    try:
        data = json.loads(row[0])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def cover_presets(username: str) -> list:
    """Пресеты Cover Generator: presets/<uid>/presets.json — словарь имя → настройки."""
    uid = _user_id(os.path.join(COVER_DIR, "cover_sessions.db"), username)
    if not uid:
        return []
    return _names_from_json(os.path.join(COVER_DIR, "presets", uid, "presets.json"))


def vsgen_presets(db_conn, tenant_id: str) -> list:
    """Пресеты самого кабинета: (id, название). Здесь ключ — число, а не имя."""
    try:
        return [(r[0], r[1] or f"пресет {r[0]}") for r in db_conn.execute(
            "SELECT id, name FROM presets WHERE tenant_id=? ORDER BY name COLLATE NOCASE",
            (tenant_id,)).fetchall()]
    except Exception:
        return []
