# -*- coding: utf-8 -*-
"""
API v1 ключи для Cover Server.

Per-user API ключи (формат cvr_*), храним в той же БД cover_job_queue.db,
рядом с таблицей jobs. Plain key показывается ОДИН РАЗ при создании,
далее в БД только sha256(plain) + первые 12 chars как prefix.
"""

import hashlib
import secrets
import sqlite3
from contextlib import closing
import time
import uuid
from typing import Optional, List, Dict


KEY_PREFIX = "cvr_"
PLAIN_LEN_BYTES = 24  # secrets.token_urlsafe(24) даёт ~32 base64url chars


class ApiAuthState:
    """Singleton-конфигуратор пути к БД."""
    _db_path: Optional[str] = None

    @classmethod
    def configure(cls, db_path: str):
        cls._db_path = db_path

    @classmethod
    def db_path(cls) -> str:
        if not cls._db_path:
            raise RuntimeError("ApiAuthState не сконфигурирован — вызови configure(db_path)")
        return cls._db_path

    @classmethod
    def init_db(cls):
        """Создаёт таблицу api_keys (idempotent)."""
        with closing(sqlite3.connect(cls.db_path())) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_id       TEXT PRIMARY KEY,
                    user_id      TEXT NOT NULL,
                    name         TEXT NOT NULL,
                    key_hash     TEXT NOT NULL UNIQUE,
                    prefix       TEXT NOT NULL,
                    created_at   REAL NOT NULL,
                    last_used_at REAL,
                    status       TEXT NOT NULL DEFAULT 'active'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)")


def extract_api_key(authorization: Optional[str], x_api_key: Optional[str]) -> Optional[str]:
    """Извлечь plain key из Bearer-токена или X-API-Key header. Только если выглядит как cvr_*."""
    candidate = None
    if authorization:
        s = authorization.strip()
        if s.lower().startswith("bearer "):
            candidate = s[7:].strip()
        elif s.startswith(KEY_PREFIX):
            candidate = s
    if not candidate and x_api_key:
        s = x_api_key.strip()
        if s.startswith(KEY_PREFIX):
            candidate = s
    if candidate and candidate.startswith(KEY_PREFIX):
        return candidate
    return None


def _hash(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def verify_api_key(plain_key: str) -> Optional[Dict]:
    """sha256 → SELECT FROM api_keys → return {user_id, key_id, key_name} или None.
    Обновляет last_used_at если ключ найден и активен."""
    if not plain_key or not plain_key.startswith(KEY_PREFIX):
        return None
    h = _hash(plain_key)
    with closing(sqlite3.connect(ApiAuthState.db_path())) as conn, conn:
        row = conn.execute(
            "SELECT key_id, user_id, name, status FROM api_keys WHERE key_hash = ?",
            (h,)
        ).fetchone()
        if not row:
            return None
        key_id, user_id, name, status = row
        if status != "active":
            return None
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE key_id = ?",
            (time.time(), key_id)
        )
    return {"user_id": user_id, "key_id": key_id, "key_name": name}


def create_api_key(user_id: str, name: str) -> Dict:
    """Создать новый ключ. Возвращает {plain_key, prefix, key_id, name}.
    Plain key показывается ТОЛЬКО здесь — в БД хранится только хеш."""
    if not (name or "").strip():
        raise ValueError("name is required")
    plain = KEY_PREFIX + secrets.token_urlsafe(PLAIN_LEN_BYTES)
    h = _hash(plain)
    prefix = plain[:12]  # cvr_xxxxxxxx — 8 chars после префикса видны
    key_id = uuid.uuid4().hex[:12]
    now = time.time()
    with closing(sqlite3.connect(ApiAuthState.db_path())) as conn, conn:
        conn.execute(
            """INSERT INTO api_keys (key_id, user_id, name, key_hash, prefix, created_at, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            (key_id, user_id, name.strip(), h, prefix, now)
        )
    return {
        "plain_key": plain,
        "prefix": prefix,
        "key_id": key_id,
        "name": name.strip(),
        "created_at": now,
    }


def list_api_keys(user_id: str) -> List[Dict]:
    """Все ключи пользователя (без plain)."""
    with closing(sqlite3.connect(ApiAuthState.db_path())) as conn, conn:
        rows = conn.execute(
            """SELECT key_id, name, prefix, created_at, last_used_at, status
               FROM api_keys WHERE user_id = ? ORDER BY created_at DESC""",
            (user_id,)
        ).fetchall()
    return [
        {
            "key_id": r[0],
            "name": r[1],
            "prefix": r[2],
            "created_at": r[3],
            "last_used_at": r[4],
            "status": r[5],
        }
        for r in rows
    ]


def revoke_api_key(user_id: str, key_id: str) -> bool:
    """Soft delete: status='revoked'. Возвращает True если что-то изменилось."""
    with closing(sqlite3.connect(ApiAuthState.db_path())) as conn, conn:
        cursor = conn.execute(
            "UPDATE api_keys SET status = 'revoked' WHERE key_id = ? AND user_id = ? AND status = 'active'",
            (key_id, user_id)
        )
        return cursor.rowcount > 0
