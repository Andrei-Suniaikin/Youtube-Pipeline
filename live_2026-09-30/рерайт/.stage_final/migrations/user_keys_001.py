# -*- coding: utf-8 -*-
"""
Миграция user_keys_001 — per-user keys storage.

Цель: убрать global /root/.rewrite_keys_config.json (один файл на всех users).
Партнёры через UI затирали друг другу ключи.

Idempotent:
  1. Создаёт таблицу user_keys в sessions.db (CREATE TABLE IF NOT EXISTS).
  2. Если admin row отсутствует — INSERT из существующего legacy config
     (или из default констант, если файла нет).

Безопасно вызывать повторно: admin row создаётся только если его нет.
"""
import json
import os
import sqlite3
import time
from typing import Tuple

# Должен совпадать с rewrite_core.ADMIN_USER_ID и server.py / api.py / index.html.
ADMIN_USER_ID = "292d249f2de0"


def _load_legacy_config(legacy_path: str) -> dict:
    """Читает старый /root/.rewrite_keys_config.json, нормализует под schema БД."""
    blank = {
        "anthropic_api_key": "",
        "openai_api_key": "",
        "oauth_tokens": "[]",
        "gpt_service_tier": "default",
        "gpt_use_cache": 1,
        "postprocess_check_model": "gpt-5-mini",
        "custom_models": "[]",
        "debug_mode": 0,
    }
    if not os.path.exists(legacy_path):
        return blank
    try:
        with open(legacy_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return blank

    oauth = data.get("oauth_tokens", [])
    if isinstance(oauth, str):
        oauth = [oauth] if oauth else []

    custom = data.get("custom_models", [])
    if isinstance(custom, str):
        custom = [custom] if custom else []

    return {
        "anthropic_api_key": data.get("anthropic_api_key", "") or "",
        "openai_api_key": data.get("openai_api_key", "") or "",
        "oauth_tokens": json.dumps(oauth, ensure_ascii=False),
        "gpt_service_tier": data.get("gpt_service_tier", "default") or "default",
        "gpt_use_cache": 1 if data.get("gpt_use_cache", True) else 0,
        "postprocess_check_model": data.get("postprocess_check_model", "gpt-5-mini") or "gpt-5-mini",
        "custom_models": json.dumps(custom, ensure_ascii=False),
        "debug_mode": 1 if data.get("debug_mode", False) else 0,
    }


def migrate_user_keys(
    sessions_db_path: str,
    legacy_config_path: str = "/root/.rewrite_keys_config.json",
) -> Tuple[int, int, str]:
    """
    Применяет миграцию.

    Returns: (table_created, admin_row_inserted, log_message)
    """
    table_created = 0
    admin_inserted = 0

    with sqlite3.connect(sessions_db_path) as conn:
        existed = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_keys'"
        ).fetchone()

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_keys (
                user_id                 TEXT PRIMARY KEY,
                anthropic_api_key       TEXT NOT NULL DEFAULT '',
                openai_api_key          TEXT NOT NULL DEFAULT '',
                oauth_tokens            TEXT NOT NULL DEFAULT '[]',
                gpt_service_tier        TEXT NOT NULL DEFAULT 'default',
                gpt_use_cache           INTEGER NOT NULL DEFAULT 1,
                postprocess_check_model TEXT NOT NULL DEFAULT 'gpt-5-mini',
                custom_models           TEXT NOT NULL DEFAULT '[]',
                debug_mode              INTEGER NOT NULL DEFAULT 0,
                updated_at              REAL NOT NULL
            )
            """
        )
        if not existed:
            table_created = 1

        # FIX-2026-07-02: идемпотентно добавляем колонки под серверное хранение UI-пресетов
        # настроек (rew_presets / gen_settings_presets) — раньше они жили только в localStorage
        # браузера и терялись между устройствами. SQLite не умеет ADD COLUMN IF NOT EXISTS,
        # поэтому сверяемся с PRAGMA table_info.
        _cols = {r[1] for r in conn.execute("PRAGMA table_info(user_keys)").fetchall()}
        for _col in ("rew_presets", "gen_settings_presets"):
            if _col not in _cols:
                conn.execute(
                    "ALTER TABLE user_keys ADD COLUMN {} TEXT NOT NULL DEFAULT '{{}}'".format(_col)
                )
        # BYOK-2026-07-30: своя подписка Codex тенанта (содержимое его auth.json).
        # Дефолт — пустая строка, не '{}': пустой JSON-объект выглядел бы как
        # «подписка задана».
        if "codex_auth" not in _cols:
            conn.execute(
                "ALTER TABLE user_keys ADD COLUMN codex_auth TEXT NOT NULL DEFAULT ''"
            )

        row = conn.execute(
            "SELECT user_id FROM user_keys WHERE user_id = ?", (ADMIN_USER_ID,)
        ).fetchone()
        if not row:
            data = _load_legacy_config(legacy_config_path)
            conn.execute(
                """
                INSERT INTO user_keys
                  (user_id, anthropic_api_key, openai_api_key, oauth_tokens,
                   gpt_service_tier, gpt_use_cache, postprocess_check_model,
                   custom_models, debug_mode, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ADMIN_USER_ID,
                    data["anthropic_api_key"],
                    data["openai_api_key"],
                    data["oauth_tokens"],
                    data["gpt_service_tier"],
                    data["gpt_use_cache"],
                    data["postprocess_check_model"],
                    data["custom_models"],
                    data["debug_mode"],
                    time.time(),
                ),
            )
            admin_inserted = 1

    msg = (
        "[migrate user_keys_001] table "
        + ("created" if table_created else "already exists")
        + "; admin row "
        + ("inserted from legacy config" if admin_inserted else "already present")
    )
    return table_created, admin_inserted, msg


if __name__ == "__main__":
    sessions_db = os.environ.get("SESSION_DB", "/root/rewrite-server/sessions.db")
    legacy = os.environ.get("LEGACY_KEYS_CONFIG", "/root/.rewrite_keys_config.json")
    _, _, msg = migrate_user_keys(sessions_db, legacy)
    print(msg)
