#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Чтение `.env` — маленькое и своё, чтобы не тащить зависимость ради пяти строк.

`.env` лежит в корне проекта и **не** в git (см. `.gitignore`). Значения, уже заданные
в окружении, не перезаписываются: systemd и docker должны выигрывать у файла, иначе
забытый локальный `.env` однажды подменит боевой токен.
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
_LOADED = set()


def load_env(path: str = None, override: bool = False) -> dict:
    """Прочитать `.env` в окружение. Возвращает то, что реально положил."""
    path = os.path.abspath(path or os.environ.get("VS_ENV_FILE")
                           or os.path.join(ROOT, ".env"))
    if path in _LOADED and not override:
        return {}
    _LOADED.add(path)
    if not os.path.exists(path):
        return {}
    put = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            v = v.strip()
            # Кавычки снимаем только парные — иначе пароль, начинающийся с кавычки,
            # молча потеряет символ.
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if not k or (k in os.environ and not override):
                continue
            os.environ[k] = v
            put[k] = v
    return put


def mask(value: str, keep: int = 4) -> str:
    """Показать, что ключ есть, не показав ключа."""
    v = str(value or "")
    if not v:
        return "—"
    if len(v) <= keep * 2:
        return "…" * 3
    return f"{v[:keep]}…{v[-keep:]}"
