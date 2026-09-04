# -*- coding: utf-8 -*-
"""
Авторизация Cover Tool.

v3.0 (2026-07-26):
- Единственный способ веб-входа — аккаунты логин/пароль.
- Новые пароли хранятся как Argon2id; старые PBKDF2-хэши автоматически мигрируют
  на Argon2id после первого успешного входа.
- /auth/setup — одноразовое создание ПЕРВОГО (админ) аккаунта, пока users пуста.
- /auth/claim-password — одноразовая установка пароля по своему логину (после утечки
  разосланных паролей): работает, пока админ держит поднятым pw_reset_pending.
- Админ (фиксированный ADMIN_USER_ID) создаёт/удаляет аккаунты.
- Сессии: роллинг-TTL 2 часа (продлеваются активностью, throttle 30с).
"""

import os
import time
import uuid
import hmac
import hashlib
import sqlite3
import threading
from typing import Optional, Dict, List

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

SESSION_TIMEOUT = int(os.environ.get("COVER_SESSION_TIMEOUT", "7200"))  # v2.0: 2ч роллинг (было 5 мин)
SESSION_PING_THROTTLE = 30      # last_ping обновляем не чаще раза в 30с (меньше записей в SQLite)
# ИСПРАВЛЕНО 04.09.2026: путь считался от РАБОЧЕГО каталога процесса.
# В трёх сервисах это скрывал systemd WorkingDirectory; в одном процессе
# скрывать нечем — сервис открывал бы чужую или пустую базу.
_HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_DB = os.environ.get("COVER_SESSION_DB") or os.path.join(_HERE, "cover_sessions.db")
PBKDF2_ITERATIONS = 200_000     # только для проверки и миграции старых хэшей
PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
# user_id первого админа. Дефолт = действующий tenant владельца на проде
# (md5 его лицензии — под ним лежат outputs/presets/избранное), чтобы данные пережили переход.
ADMIN_USER_ID = os.environ.get("COVER_ADMIN_USER_ID", "REDACTED_ADMIN_USER_ID")


class LicenseManager:
    """Аккаунты логин/пароль + сессии SQLite."""

    def __init__(self, db_path: str = SESSION_DB):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    license_key TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_ping REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_license
                ON sessions(license_key)
            """)
            # Совместимость со старой схемой БД: колонку license_key пока не удаляем,
            # но для аккаунтных сессий она всегда пустая.
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # колонка уже есть
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username      TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    salt          TEXT NOT NULL,
                    user_id       TEXT NOT NULL,
                    user_name     TEXT NOT NULL,
                    created_at    REAL NOT NULL
                )
            """)
            # v3.1: одноразовая установка пароля (пароли были скомпрометированы —
            # юзер по своему логину сам задаёт новый; флаг гасится первым же использованием)
            try:
                conn.execute("ALTER TABLE users ADD COLUMN pw_reset_pending INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # колонка уже есть
            # Старые лицензионные сессии больше не должны давать доступ.
            conn.execute("""
                DELETE FROM sessions
                WHERE user_id = ''
                   OR user_id NOT IN (SELECT user_id FROM users)
            """)

    # --------------- Пароли ---------------

    @staticmethod
    def _hash_password(password: str) -> str:
        return PASSWORD_HASHER.hash(password)

    @staticmethod
    def _hash_password_legacy(password: str, salt_hex: str) -> str:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), PBKDF2_ITERATIONS)
        return dk.hex()

    @classmethod
    def _verify_password(cls, password: str, stored_hash: str, legacy_salt: str) -> tuple:
        """Returns (valid, needs_argon2_rehash)."""
        if stored_hash.startswith("$argon2id$"):
            try:
                valid = PASSWORD_HASHER.verify(stored_hash, password)
                return (bool(valid), PASSWORD_HASHER.check_needs_rehash(stored_hash))
            except (InvalidHashError, VerificationError, VerifyMismatchError):
                return (False, False)
        if not legacy_salt:
            return (False, False)
        try:
            valid = hmac.compare_digest(
                cls._hash_password_legacy(password, legacy_salt), stored_hash)
        except (ValueError, TypeError):
            return (False, False)
        return (valid, valid)

    # --------------- Аккаунты ---------------

    def has_users(self) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            return bool(row and row[0] > 0)

    def setup_admin(self, username: str, password: str, ip: str) -> tuple:
        """Одноразовое создание ПЕРВОГО аккаунта (админ). Работает только пока users пуста.
        Returns (success, session_id_or_error, user_name)."""
        username = (username or "").strip()
        if len(username) < 3:
            return (False, "Логин — минимум 3 символа", "")
        if len(password or "") < 6:
            return (False, "Пароль — минимум 6 символов", "")
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
                if row and row[0] > 0:
                    return (False, "Настройка уже выполнена — войдите по логину/паролю", "")
                conn.execute(
                    "INSERT INTO users (username, password_hash, salt, user_id, user_name, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (username, self._hash_password(password), "",
                     ADMIN_USER_ID, username, time.time())
                )
            return (True, self._create_session(ADMIN_USER_ID, username, ip), username)

    def login_password(self, username: str, password: str, ip: str) -> tuple:
        """Вход по логину/паролю. Returns (success, session_id_or_error, user_name)."""
        username = (username or "").strip()
        if not username or not password:
            return (False, "Введите логин и пароль", "")
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT password_hash, salt, user_id, user_name FROM users WHERE username = ?",
                    (username,)
                ).fetchone()
                # Единый текст для несуществующего логина и неверного пароля.
                if not row:
                    return (False, "Неверный логин или пароль", "")
                pwh, salt, user_id, user_name = row
                valid, needs_rehash = self._verify_password(password, pwh, salt)
                if not valid:
                    return (False, "Неверный логин или пароль", "")
                if needs_rehash:
                    conn.execute(
                        "UPDATE users SET password_hash = ?, salt = '' WHERE username = ?",
                        (self._hash_password(password), username)
                    )
            return (True, self._create_session(user_id, user_name, ip), user_name)

    def create_account(self, username: str, password: str, user_name: str = "") -> tuple:
        """Создание аккаунта (только админ, проверка на уровне endpoint'а).
        Returns (success, error_or_empty)."""
        username = (username or "").strip()
        if len(username) < 3:
            return (False, "Логин — минимум 3 символа")
        if len(password or "") < 6:
            return (False, "Пароль — минимум 6 символов")
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
                if exists:
                    return (False, "Такой логин уже есть")
                conn.execute(
                    "INSERT INTO users (username, password_hash, salt, user_id, user_name, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (username, self._hash_password(password), "",
                     os.urandom(6).hex(), (user_name or username).strip(), time.time())
                )
        return (True, "")

    def change_password(self, username: str, new_password: str,
                        old_password: str = None, require_old: bool = True) -> tuple:
        """Смена пароля. require_old=False — админский сброс без старого пароля.
        Returns (success, error_or_empty)."""
        username = (username or "").strip()
        if len(new_password or "") < 6:
            return (False, "Пароль — минимум 6 символов")
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT password_hash, salt FROM users WHERE username = ?", (username,)
                ).fetchone()
                if not row:
                    return (False, "Пользователь не найден")
                if require_old:
                    valid, _ = self._verify_password(old_password or "", row[0], row[1])
                    if not valid:
                        return (False, "Неверный текущий пароль")
                conn.execute(
                    "UPDATE users SET password_hash = ?, salt = ? WHERE username = ?",
                    (self._hash_password(new_password), "", username)
                )
        return (True, "")

    # --------------- Одноразовая установка пароля ---------------

    def set_password_reset_pending(self, usernames: Optional[List[str]] = None,
                                   pending: bool = True) -> List[str]:
        """Выдаёт (или отзывает) право один раз задать новый пароль по своему логину.
        usernames=None — всем аккаунтам. Returns список затронутых логинов."""
        flag = 1 if pending else 0
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                if usernames is None:
                    rows = conn.execute("SELECT username FROM users").fetchall()
                    conn.execute("UPDATE users SET pw_reset_pending = ?", (flag,))
                else:
                    names = [(u or "").strip() for u in usernames if (u or "").strip()]
                    if not names:
                        return []
                    marks = ",".join("?" for _ in names)
                    rows = conn.execute(
                        f"SELECT username FROM users WHERE username IN ({marks})", names
                    ).fetchall()
                    conn.execute(
                        f"UPDATE users SET pw_reset_pending = ? WHERE username IN ({marks})",
                        [flag] + names
                    )
        return [r[0] for r in rows]

    def claim_password(self, username: str, new_password: str, ip: str) -> tuple:
        """Одноразовая установка пароля по логину (без старого пароля).
        Работает только пока у аккаунта поднят pw_reset_pending; первое же успешное
        использование гасит флаг и убивает все прежние сессии этого пользователя.
        Returns (success, session_id_or_error, user_name)."""
        username = (username or "").strip()
        # Единый текст для несуществующего логина и погашенного флага — чтобы форма
        # не работала оракулом «такой логин существует».
        denied = "Для этого логина установка пароля недоступна — войдите обычным способом"
        if not username:
            return (False, denied, "")
        if len(new_password or "") < 6:
            return (False, "Пароль — минимум 6 символов", "")
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT user_id, user_name, pw_reset_pending FROM users WHERE username = ?",
                    (username,)
                ).fetchone()
                if not row or not row[2]:
                    return (False, denied, "")
                user_id, user_name = row[0], row[1]
                conn.execute(
                    "UPDATE users SET password_hash = ?, salt = '', pw_reset_pending = 0 "
                    "WHERE username = ? AND pw_reset_pending = 1",
                    (self._hash_password(new_password), username)
                )
                # старый пароль скомпрометирован — активные сессии под ним больше не валидны
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return (True, self._create_session(user_id, user_name, ip), user_name)

    def list_accounts(self) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT username, user_name, user_id, created_at, pw_reset_pending "
                "FROM users ORDER BY created_at"
            ).fetchall()
        return [
            {"username": r[0], "user_name": r[1], "user_id": r[2],
             "is_admin": r[2] == ADMIN_USER_ID, "created_at": r[3],
             "pw_reset_pending": bool(r[4])}
            for r in rows
        ]

    def delete_account(self, username: str) -> bool:
        """Удаляет аккаунт (и его сессии). Последнего админа удалить нельзя (анти-lockout)."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone()
                if not row:
                    return False
                if row[0] == ADMIN_USER_ID:
                    admins = conn.execute(
                        "SELECT COUNT(*) FROM users WHERE user_id = ?", (ADMIN_USER_ID,)
                    ).fetchone()[0]
                    if admins <= 1:
                        return False
                conn.execute("DELETE FROM users WHERE username = ?", (username,))
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (row[0],))
        return True

    # --------------- Сессии ---------------

    def _create_session(self, user_id: str, user_name: str, ip: str) -> str:
        session_id = str(uuid.uuid4())
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, license_key, user_name, ip, created_at, last_ping, user_id) "
                "VALUES (?, '', ?, ?, ?, ?, ?)",
                (session_id, user_name, ip, now, now, user_id)
            )
        return session_id

    def _cleanup_expired(self, conn=None):
        cutoff = time.time() - SESSION_TIMEOUT
        if conn:
            conn.execute("DELETE FROM sessions WHERE last_ping < ?", (cutoff,))
        else:
            with sqlite3.connect(self.db_path) as c:
                c.execute("DELETE FROM sessions WHERE last_ping < ?", (cutoff,))

    def ping(self, session_id: str) -> bool:
        now = time.time()
        cutoff = now - SESSION_TIMEOUT
        with sqlite3.connect(self.db_path) as conn:
            # истёкшие чистим и НЕ воскрешаем (UPDATE только свежих) — иначе ping
            # с мёртвым session_id продлевал бы сессию мимо роллинг-TTL
            conn.execute("DELETE FROM sessions WHERE last_ping < ?", (cutoff,))
            result = conn.execute(
                "UPDATE sessions SET last_ping = ? WHERE session_id = ? AND last_ping >= ?",
                (now, session_id, cutoff)
            )
            return result.rowcount > 0

    def logout(self, session_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def validate(self, session_id: str) -> Optional[Dict]:
        """Роллинг-валидация: сессия жива, пока есть активность (продление не чаще раза в 30с)."""
        now = time.time()
        cutoff = now - SESSION_TIMEOUT
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE last_ping < ?", (cutoff,))
            row = conn.execute(
                "SELECT user_name, ip, last_ping, user_id FROM sessions WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            if row and (now - row[2]) > SESSION_PING_THROTTLE:
                conn.execute(
                    "UPDATE sessions SET last_ping = ? WHERE session_id = ?",
                    (now, session_id)
                )
        if row:
            return {"user_name": row[0], "ip": row[1], "user_id": row[3]}
        return None

    def get_user_id(self, session_id: str) -> Optional[str]:
        info = self.validate(session_id)
        if info:
            return info.get("user_id") or None
        return None
