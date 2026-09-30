# -*- coding: utf-8 -*-
"""
auth.py — Система лицензий и сессий для Rewrite Studio Web.
Лицензии хранятся в приватном GitHub Gist. Сессии — в SQLite.
"""

import os
import json
import time
import uuid
import sqlite3
import hashlib
import threading
import urllib.request
from typing import Optional, Dict

# Лицензии: GitHub Gist. Секреты ТОЛЬКО из env — никаких хардкод-дефолтов (B2).
# Fail-fast на старте, если не заданы (PATCH-2026-06-25-secfix).
GIST_ID = os.environ.get("LICENSE_GIST_ID")
GIST_TOKEN = os.environ.get("GIST_TOKEN")
if not GIST_ID or not GIST_TOKEN:
    raise RuntimeError(
        "LICENSE_GIST_ID and GIST_TOKEN env vars are required "
        "(no hardcoded defaults — security). Set them in the service environment."
    )
LICENSE_CACHE_TTL = 300
SESSION_TIMEOUT = 600  # 10 минут (автовыход при неактивности — раньше был 60 мин, из-за чего "призрачные" вкладки копились)
SESSION_DB = os.environ.get("SESSION_DB", "sessions.db")


class LicenseManager:
    """Управление лицензиями (GitHub Gist) и сессиями (SQLite)."""

    def __init__(self, db_path: str = SESSION_DB):
        self.db_path = db_path
        self._license_cache: Dict = {}
        self._cache_time: float = 0
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            # M4 PATCH-2026-06-25-secfix: WAL — sessions.db пишется из auth (на каждый запрос)
            # + воркер-потоков; без WAL/busy_timeout под конкуренцией ловили 'database is locked'.
            conn.execute("PRAGMA journal_mode=WAL")
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

    def _fetch_licenses(self) -> Dict:
        """Загружает лицензии из GitHub Gist с кэшированием.

        Опционально merge с локальным /root/test_licenses.json (для smoke-тестов
        closed beta). Local-файл переопределяет/дополняет Gist; удаление файла —
        следующий refresh забывает добавленные ключи.
        """
        now = time.time()
        if self._license_cache and (now - self._cache_time) < LICENSE_CACHE_TTL:
            return self._license_cache

        licenses: Dict = {}
        try:
            url = f"https://api.github.com/gists/{GIST_ID}"
            req = urllib.request.Request(url)
            if GIST_TOKEN:
                req.add_header("Authorization", f"token {GIST_TOKEN}")
            req.add_header("Accept", "application/vnd.github.v3+json")

            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            for filename, file_data in data.get("files", {}).items():
                if "license" in filename.lower() and filename.endswith(".json"):
                    content = json.loads(file_data["content"])
                    licenses = content.get("licenses", {})
                    break
        except Exception as e:
            print(f"[AUTH] Ошибка загрузки лицензий из Gist: {e}")

        local_path = os.environ.get("TEST_LICENSES_FILE", "/root/test_licenses.json")
        try:
            if os.path.exists(local_path):
                with open(local_path, "r", encoding="utf-8") as f:
                    local_data = json.load(f)
                licenses.update(local_data.get("licenses", {}))
        except Exception as e:
            print(f"[AUTH] Ошибка загрузки локальных лицензий ({local_path}): {e}")

        if licenses:
            self._license_cache = licenses
            self._cache_time = now
        return self._license_cache

    def _cleanup_expired(self):
        cutoff = time.time() - SESSION_TIMEOUT
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            conn.execute("DELETE FROM sessions WHERE last_ping < ?", (cutoff,))

    def _count_active_sessions(self, license_key: str) -> int:
        self._cleanup_expired()
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE license_key = ?",
                (license_key,)
            ).fetchone()
        return row[0] if row else 0

    def login(self, license_key: str, ip: str) -> tuple:
        """
        Попытка входа.
        Returns: (success, session_id_or_error, user_name)
        """
        with self._lock:
            licenses = self._fetch_licenses()

            if license_key not in licenses:
                return (False, "Неверный лицензионный ключ", "")

            license_info = licenses[license_key]

            if not license_info.get("active", False):
                return (False, "Лицензия деактивирована", "")

            user_name = license_info.get("name", "Unknown")
            max_sessions = license_info.get("max_sessions", 1)

            # При повторном логине с того же IP — считаем это той же вкладкой/пользователем
            # и удаляем его старые сессии (лечит "призрачные" сессии от перезагрузок).
            # PATCH-2026-06-18-multiuser-temp: ВРЕМЕННО отключено удаление сессий с того же IP.
            # Двое на одном ключе с одного IP — прежний DELETE на логине выбивал сессию
            # напарника -> пинг-понг на вход. Ghost-сессии всё равно истекают по SESSION_TIMEOUT.
            # Вернуть = раскомментировать блок ниже.
            # with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            #     conn.execute(
            #         "DELETE FROM sessions WHERE license_key = ? AND ip = ?",
            #         (license_key, ip)
            #     )
            pass

            active = self._count_active_sessions(license_key)
            if active >= max_sessions:
                return (False, f"Превышен лимит сессий ({active}/{max_sessions}). Закройте другие вкладки или подождите 10 минут.", "")

            session_id = str(uuid.uuid4())
            now = time.time()
            with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                conn.execute(
                    "INSERT INTO sessions (session_id, license_key, user_name, ip, created_at, last_ping) VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, license_key, user_name, ip, now, now)
                )

            return (True, session_id, user_name)

    def ping(self, session_id: str) -> bool:
        now = time.time()
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            result = conn.execute(
                "UPDATE sessions SET last_ping = ? WHERE session_id = ?",
                (now, session_id)
            )
            return result.rowcount > 0

    def logout(self, session_id: str):
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def validate(self, session_id: str) -> Optional[Dict]:
        """Проверяет валидность сессии. Возвращает dict или None."""
        self._cleanup_expired()
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            row = conn.execute(
                "SELECT license_key, user_name, ip FROM sessions WHERE session_id = ?",
                (session_id,)
            ).fetchone()
        if row:
            return {"license_key": row[0], "user_name": row[1], "ip": row[2]}
        return None

    def get_user_id(self, session_id: str) -> Optional[str]:
        """Хеш license_key как user_id для изоляции файлов."""
        info = self.validate(session_id)
        if info:
            return hashlib.md5(info["license_key"].encode()).hexdigest()[:12]
        return None
