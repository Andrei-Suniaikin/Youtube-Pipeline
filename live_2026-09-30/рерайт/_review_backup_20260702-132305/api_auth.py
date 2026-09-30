# -*- coding: utf-8 -*-
"""
api_auth.py — API-key authentication для /api/v1/*.

Полностью независим от session-based auth (auth.py / LicenseManager).
Ключи хранятся в той же sessions.db, но в отдельной таблице `api_keys`.

Plain key format: rws_live_<32 url-safe random chars>  (≈190 bits entropy)
В БД хранится только sha256(plain_key) — plain показывается ТОЛЬКО при создании.
"""
import hashlib
import secrets
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

from fastapi import Header, HTTPException, status

# --- Конфиг ---
MAX_ACTIVE_API_KEYS = 10        # soft-limit на user
PLAIN_KEY_PREFIX = "rws_live_"   # для grep в логах + UI display
PLAIN_KEY_RANDOM_LEN = 32        # символы (token_urlsafe(24) ≈ 32 chars)
PREFIX_DISPLAY_LEN = 16          # сколько символов показывать в UI (rws_live_ + 8)


# --- Helpers ---

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _generate_plain_key() -> str:
    """Plain key: rws_live_<random>. Длина ≈ 41 символ."""
    return PLAIN_KEY_PREFIX + secrets.token_urlsafe(24)


# --- DB operations ---

def create_api_key(sessions_db: str, user_id: str, license_key: str, name: str) -> Dict:
    """
    Создаёт новый api-key для user_id. Возвращает dict с plain_key (показать ОДИН раз).

    Raises HTTPException 409 если лимит ключей достигнут или name дубль.
    """
    if not name or not name.strip():
        raise HTTPException(status_code=422, detail="name is required")
    name = name.strip()[:64]

    with sqlite3.connect(sessions_db) as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE user_id=? AND status='active'",
            (user_id,)
        ).fetchone()[0]
        if active >= MAX_ACTIVE_API_KEYS:
            raise HTTPException(
                status_code=409,
                detail=f"Limit reached: {MAX_ACTIVE_API_KEYS} active keys per user. Revoke unused keys first."
            )

        # Защита от дублирования имени (для UX, не для безопасности)
        existed_name = conn.execute(
            "SELECT key_id FROM api_keys WHERE user_id=? AND name=? AND status='active'",
            (user_id, name)
        ).fetchone()
        if existed_name:
            raise HTTPException(status_code=409, detail=f"Key with name '{name}' already exists. Revoke it or choose another name.")

        plain = _generate_plain_key()
        key_hash = _sha256(plain)
        key_id = secrets.token_urlsafe(12)
        prefix = plain[:PREFIX_DISPLAY_LEN]
        now = time.time()

        conn.execute(
            "INSERT INTO api_keys (key_id, user_id, license_key, key_hash, prefix, name, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
            (key_id, user_id, license_key, key_hash, prefix, name, now)
        )

    return {
        "key_id": key_id,
        "name": name,
        "prefix": prefix,
        "plain_key": plain,            # ОДИН раз!
        "created_at": now,
        "status": "active",
    }


def list_api_keys(sessions_db: str, user_id: str) -> List[Dict]:
    """Список ВСЕХ (active + revoked) ключей user'а. Plain key НЕ возвращается."""
    with sqlite3.connect(sessions_db) as conn:
        rows = conn.execute(
            "SELECT key_id, name, prefix, created_at, last_used_at, status "
            "FROM api_keys WHERE user_id=? ORDER BY created_at DESC",
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


def revoke_api_key(sessions_db: str, key_id: str, user_id: str) -> bool:
    """Помечает ключ status='revoked' (НЕ DELETE, для аудита). Returns True если был active."""
    with sqlite3.connect(sessions_db) as conn:
        cur = conn.execute(
            "UPDATE api_keys SET status='revoked' WHERE key_id=? AND user_id=? AND status='active'",
            (key_id, user_id)
        )
        return cur.rowcount > 0


# --- Auth dependency ---

class _ApiAuthState:
    """Storage для sessions_db path + license_mgr (устанавливается при подключении router'а)."""
    sessions_db: Optional[str] = None
    license_mgr: Optional[object] = None  # LicenseManager instance — для session fallback


def configure(sessions_db: str, license_mgr=None):
    """Вызывается из server.py до подключения router'а.

    license_mgr — опционально, LicenseManager для session-fallback в get_api_user.
    Если задан — UI с session_id может звать /api/v1/* без отдельного api-key.
    """
    _ApiAuthState.sessions_db = sessions_db
    _ApiAuthState.license_mgr = license_mgr


def _extract_key(authorization: Optional[str], x_api_key: Optional[str]) -> Optional[str]:
    """Из Authorization: Bearer <k> или X-API-Key: <k> достаёт plain key. Authorization приоритет."""
    if authorization:
        a = authorization.strip()
        if a.lower().startswith("bearer "):
            return a[7:].strip() or None
        # Без префикса — принимаем как plain (например `Authorization: rws_live_...`)
        if a.startswith(PLAIN_KEY_PREFIX):
            return a
    if x_api_key:
        return x_api_key.strip() or None
    return None


def get_api_user(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    x_session_id: Optional[str] = Header(None, alias="X-Session-Id"),
) -> Dict:
    """
    FastAPI dependency для /api/v1/*. Принимает 3 формы auth (любая):
      1. `Authorization: Bearer <api_key>`  (primary, RFC 6750)
      2. `X-API-Key: <api_key>`             (convenience)
      3. `X-Session-Id: <session_id>`       (fallback — UI с session-auth)

    API-key checks first; если ключа нет — пробуем session_id (если configure(license_mgr)).
    Returns: {"user_id": ..., "license_key": ..., "key_id": ... or None, "key_name": ...}
    Raises:  401 если ни одна форма не валидна.
    """
    if _ApiAuthState.sessions_db is None:
        raise HTTPException(status_code=500, detail="api auth not configured")

    # === 1) API key path (Bearer / X-API-Key) ===
    raw = _extract_key(authorization, x_api_key)
    if raw:
        key_hash = _sha256(raw)
        now = time.time()
        with sqlite3.connect(_ApiAuthState.sessions_db) as conn:
            row = conn.execute(
                "SELECT key_id, user_id, license_key, status, name "
                "FROM api_keys WHERE key_hash=?",
                (key_hash,)
            ).fetchone()
            if row:
                key_id, user_id, license_key, st, name = row
                if st != "active":
                    raise HTTPException(status_code=401, detail=f"api key is {st}")
                conn.execute("UPDATE api_keys SET last_used_at=? WHERE key_id=?", (now, key_id))
                return {
                    "user_id": user_id,
                    "license_key": license_key,
                    "key_id": key_id,
                    "key_name": name,
                }
            # plain key пришёл, но не нашёлся — попробуем session ниже (а не сразу 401),
            # на случай если клиент случайно сложил оба header'а.

    # === 2) Session fallback (UI) ===
    if x_session_id and _ApiAuthState.license_mgr is not None:
        try:
            session_info = _ApiAuthState.license_mgr.validate(x_session_id)
        except Exception:
            session_info = None
        if session_info:
            return {
                "user_id": _ApiAuthState.license_mgr.get_user_id(x_session_id),
                "license_key": session_info.get("license_key", ""),
                "key_id": None,  # отметка: auth via session, не api-key
                "key_name": "session:" + str(session_info.get("user_name", "?")),
            }

    # === 3) Ничего не сработало ===
    if raw:
        # был ключ, но не нашёлся
        raise HTTPException(status_code=401, detail="invalid api key")
    raise HTTPException(
        status_code=401,
        detail="missing auth (use Bearer <api_key>, X-API-Key, or X-Session-Id)",
        headers={"WWW-Authenticate": "Bearer"},
    )
