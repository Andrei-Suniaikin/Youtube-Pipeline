#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Списки пресетов соседних сервисов (Rewrite и Cover) для карточки канала.

ПЕРЕПИСАНО 04.09.2026. Раньше этот модуль открывал чужие SQLite и чужие JSON прямо
с диска и сопоставлял людей сравнением логинов. Это был анти-контракт: он держался
на том, что соседи никогда не поменяют схему, и на том, что никто не заведёт двух
разных людей с одинаковым логином.

Теперь всё иначе по двум причинам. Личность общая — `tenant_id` кабинета одинаков во
всех трёх сервисах после `dev/migrate_identity.py`. И сервисы живут в одном процессе
(`web/platform.py`), поэтому у соседа можно спросить его же функцией, а не читать его
хранилище. Схема соседа снова принадлежит соседу.

Ключ доступа для этого не нужен и сознательно не используется: ключ принадлежит одному
человеку и отдал бы ЕГО пресеты. Карточка канала партнёра показывала бы пресеты
владельца — молча и правдоподобно, худший вид ошибки.

Если сосед не в этом процессе (сервисы подняты по старинке, порознь), список пуст.
Интерфейс показывает это как «список недоступен» и оставляет ручной ввод — так же,
как раньше вёл себя остановленный сосед.
"""

from __future__ import annotations

import os
import sys

REWRITE_URL = os.environ.get("VS_REWRITE_URL", "https://rewrite.13.140.129.66.sslip.io")
COVER_URL = os.environ.get("VS_COVER_URL", "https://cover.13.140.129.66.sslip.io")


def _neighbour(module: str):
    """Модуль соседа, если он поднят в этом же процессе. Иначе None."""
    return sys.modules.get(module)


def rewrite_presets(user_id: str) -> list:
    """Пресеты РЕРАЙТА (не генерации!).

    Разница существенная: пресеты рерайта по ссылке и брифы создания текста с нуля
    живут в разных хранилищах и означают разное. Канал ссылается именно на первые.
    """
    return sorted(_rewrite_preset_map(user_id).keys(), key=str.lower)


def rewrite_preset_config(user_id: str, name: str) -> dict:
    """Содержимое пресета рерайта: модель, потоки, размер куска, стиль обращения."""
    got = _rewrite_preset_map(user_id).get(name)
    return got if isinstance(got, dict) else {}


def _rewrite_preset_map(user_id: str) -> dict:
    mod = _neighbour("server")
    if not user_id or mod is None or not hasattr(mod, "_api_presets"):
        return {}
    try:
        data = (mod._api_presets(str(user_id)) or {}).get("rewrite") or {}
    except Exception:                                              # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def cover_presets(user_id: str) -> list:
    """Пресеты Cover Generator — именами, как их видит сам Cover."""
    mod = _neighbour("cover_server")
    if not user_id or mod is None or not hasattr(mod, "_get_preset_manager"):
        return []
    try:
        names = list((mod._get_preset_manager(str(user_id))._presets or {}).keys())
    except Exception:                                              # noqa: BLE001
        return []
    return sorted((n for n in names if isinstance(n, str) and n.strip()), key=str.lower)


def vsgen_presets(db_conn, tenant_id: str) -> list:
    """Пресеты самого кабинета: (id, название). Здесь ключ — число, а не имя."""
    try:
        return [(r[0], r[1] or f"пресет {r[0]}") for r in db_conn.execute(
            "SELECT id, name FROM presets WHERE tenant_id=? ORDER BY name COLLATE NOCASE",
            (tenant_id,)).fetchall()]
    except Exception:
        return []
