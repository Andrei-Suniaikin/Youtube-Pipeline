# -*- coding: utf-8 -*-
"""
auth.py — Сессии и аккаунты (логин/пароль) для Rewrite Studio Web. Хранилище — SQLite.
FIX-2026-07-02: вход по лицензионному ключу удалён (перешли на логин/пароль),
license/Gist-логика вычищена как мёртвая.
"""

import os
import json
import time
import uuid
import sqlite3
import hashlib
import hmac
import threading
from typing import Optional, Dict

SESSION_TIMEOUT = int(os.environ.get("REWRITE_SESSION_TIMEOUT", "2592000"))
# FIX-2026-07-17: 30 дней rolling (паритет с gen-кабинетом), переопределяется env.
# История: 600с → 401-шторм; C9 FIX-2026-07-02: 7200 — всё равно вылет после 2ч простоя.
SESSION_PING_THROTTLE = 30  # C9: last_ping в validate обновляем не чаще раза в 30с (не долбить БД на каждый запрос)
SESSION_CLEANUP_INTERVAL_SEC = 600  # FIX-2026-09-13: просроченные сессии удаляем не чаще раза в 10 мин (было — на каждый запрос)
PBKDF2_ITERATIONS = 200_000  # хеш паролей аккаунтов (pbkdf2_hmac sha256, stdlib — без внешних зависимостей)
# ИСПРАВЛЕНО 04.09.2026: путь считался от РАБОЧЕГО каталога процесса.
# В трёх сервисах это скрывал systemd WorkingDirectory; в одном процессе
# скрывать нечем — сервис открывал бы чужую или пустую базу.
_HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_DB = os.environ.get("SESSION_DB") or os.path.join(_HERE, "sessions.db")
# Админ-тенант: аккаунт с этим user_id = админ (наследует существующие пресеты/токены/данные).
# Согласовано с server.py / api.py / index.html.
# ИСПРАВЛЕНО 04.09.2026: было литералом. После слияния личности id админа —
# это tenant_id кабинета, и зашитое значение перестаёт совпадать: админ
# молча теряет права. Cover читал его из окружения давно, здесь так же.
# Правильный конец истории — роль из users.role кабинета, а не отдельный id.
ADMIN_USER_ID = os.environ.get("REWRITE_ADMIN_USER_ID") or "REDACTED_ADMIN_USER_ID"


class LicenseManager:
    """Сессии и аккаунты (логин/пароль) в SQLite. (Имя историческое — раньше вёл и лицензии.)"""

    def __init__(self, db_path: str = SESSION_DB):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._cleanup_lock = threading.Lock()  # отдельный: self._lock держит отключение аккаунта до 15с
        self._last_cleanup = 0.0
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            # M4 PATCH-2026-06-25-secfix: WAL — sessions.db пишется из auth (на каждый запрос)
            # + воркер-потоков; без WAL/busy_timeout под конкуренцией ловили 'database is locked'.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    license_key TEXT NOT NULL DEFAULT '',
                    user_name TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_ping REAL NOT NULL,
                    user_id TEXT NOT NULL DEFAULT ''
                )
            """)
            # FIX-2026-07-02: user_id хранится в СЕССИИ напрямую (аккаунты логин/пароль больше
            # не выводят его из license_key). Идемпотентный ALTER для существующих прод-БД.
            _scols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if "user_id" not in _scols:
                conn.execute("ALTER TABLE sessions ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
            # FIX-2026-07-02: аккаунты логин/пароль. user_id — СВОЙ у аккаунта (не из ключа).
            # Bootstrap-админ получает user_id = ADMIN_USER_ID → наследует все данные/пресеты/права.
            # Новые аккаунты — чистый профиль (сгенерённый user_id).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username      TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    salt          TEXT NOT NULL,
                    user_id       TEXT NOT NULL,
                    user_name     TEXT NOT NULL,
                    created_at    REAL NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'active',
                    disabled_at   REAL
                )
            """)
            # Shared-server mode: offboarding is reversible/auditable.  Existing
            # accounts stay active; jobs/results are intentionally retained.
            _ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "status" not in _ucols:
                conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            if "disabled_at" not in _ucols:
                conn.execute("ALTER TABLE users ADD COLUMN disabled_at REAL")
            conn.execute("UPDATE users SET status='active' WHERE status IS NULL OR status=''")

    def _cleanup_expired(self):
        # FIX-2026-09-13: DELETE шёл на КАЖДЫЙ запрос веб-интерфейса — лишняя запись в sessions.db
        # в ту же блокировку, что и API-опрос. Теперь не чаще SESSION_CLEANUP_INTERVAL_SEC;
        # просроченную, но ещё не удалённую сессию validate не пускает сам (условие по last_ping).
        now = time.time()
        with self._cleanup_lock:
            if now - self._last_cleanup < SESSION_CLEANUP_INTERVAL_SEC:
                return
            self._last_cleanup = now
        cutoff = now - SESSION_TIMEOUT
        try:
            with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                conn.execute("DELETE FROM sessions WHERE last_ping < ?", (cutoff,))
        except sqlite3.OperationalError as exc:
            # Уборка служебная: занятая база (locked/busy) не должна ронять запрос пользователя.
            print(f"[auth] уборка просроченных сессий пропущена: {exc}")

    def _create_session(self, user_id: str, user_name: str, ip: str) -> str:
        """Создаёт сессию для аккаунта (user_id — свой у аккаунта). Возвращает session_id.
        НЕ берёт self._lock (зовётся под ним). Лимита сессий нет — у каждого свой логин."""
        session_id = str(uuid.uuid4())
        now = time.time()
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, license_key, user_name, ip, created_at, last_ping, user_id) "
                "VALUES (?, '', ?, ?, ?, ?, ?)",
                (session_id, user_name, ip, now, now, user_id)
            )
        return session_id

    # ---- Аккаунты логин/пароль (FIX-2026-07-02). Пароли: pbkdf2, constant-time, plaintext НЕ логируется ----
    @staticmethod
    def _make_salt() -> str:
        return os.urandom(16).hex()

    @staticmethod
    def _hash_password(password: str, salt_hex: str) -> str:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS)
        return dk.hex()

    @staticmethod
    def _validate_credentials(username: str, password: str) -> tuple:
        """(ok, error, normalized_username)."""
        username = (username or "").strip()
        if not username or not password:
            return (False, "Введите логин и пароль", None)
        if len(username) < 3:
            return (False, "Логин минимум 3 символа", None)
        if len(password) < 6:
            return (False, "Пароль минимум 6 символов", None)
        return (True, "", username)

    def has_users(self) -> bool:
        """Есть ли хоть один аккаунт (для gating setup-экрана первичной настройки)."""
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def _insert_user(self, conn, username: str, password: str, user_id: str, user_name: str):
        salt = self._make_salt()
        pwh = self._hash_password(password, salt)
        conn.execute(
            "INSERT INTO users "
            "(username, password_hash, salt, user_id, user_name, created_at, status, disabled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', NULL)",
            (username, pwh, salt, user_id, user_name, time.time())
        )

    def setup_admin(self, username: str, password: str, ip: str) -> tuple:
        """Bootstrap: создаёт ПЕРВЫЙ (админский) аккаунт. Работает ТОЛЬКО пока users пуста.
        Админ получает user_id = ADMIN_USER_ID → наследует ВСЕ текущие данные/пресеты/права.
        Returns (success, session_id_or_error, user_name)."""
        ok, err, username = self._validate_credentials(username, password)
        if not ok:
            return (False, err, "")
        with self._lock:
            if self.has_users():
                return (False, "Первичная настройка уже выполнена", "")
            user_name = "Админ"
            with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                self._insert_user(conn, username, password, ADMIN_USER_ID, user_name)
            return (True, self._create_session(ADMIN_USER_ID, user_name, ip), user_name)

    def create_account(self, username: str, password: str, user_name: str) -> tuple:
        """Админ создаёт аккаунт с ЧИСТЫМ профилем (свой сгенерённый user_id). Returns (success, error)."""
        ok, err, username = self._validate_credentials(username, password)
        if not ok:
            return (False, err)
        display = (user_name or "").strip() or username
        with self._lock:
            with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                    return (False, "Логин уже занят")
                new_uid = os.urandom(6).hex()  # 12 hex — свой тенант (чистый профиль)
                self._insert_user(conn, username, password, new_uid, display)
        return (True, "")

    def list_accounts(self) -> list:
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            rows = conn.execute(
                "SELECT username, user_name, user_id, created_at, status, disabled_at "
                "FROM users ORDER BY created_at"
            ).fetchall()
        return [{"username": r[0], "user_name": r[1], "user_id": r[2],
                 "is_admin": r[2] == ADMIN_USER_ID, "created_at": r[3],
                 "status": r[4] or "active", "disabled_at": r[5]} for r in rows]

    def is_user_active(self, user_id: str) -> bool:
        """Cheap worker-side recheck immediately before provider execution."""
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM users WHERE user_id=? AND status='active'",
                    (user_id,),
                ).fetchone()
                is not None
            )

    def disable_account(
        self, username: str, jobs_db_path: Optional[str] = None
    ) -> Optional[Dict]:
        """Recoverable cross-DB offboarding protocol.

        WAL transactions spanning ATTACHed databases are not crash-atomic. The
        account is therefore fenced first in sessions.db (``disabling``), then
        jobs are durably cancelled in jobs.db, and only then is the account
        finalized as ``disabled``. Every phase is idempotent; startup recovery
        resumes any account left in the fenced intermediate state.
        """
        username = (username or "").strip()
        with self._lock:
            user_id = ""
            now = time.time()
            conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None)
            try:
                conn.execute("PRAGMA busy_timeout=15000")
                conn.execute("BEGIN IMMEDIATE")
                r = conn.execute(
                    "SELECT user_id, status FROM users WHERE username = ?", (username,)
                ).fetchone()
                if not r:
                    conn.rollback()
                    return None
                if r[0] == ADMIN_USER_ID:
                    conn.rollback()
                    return None
                current_status = r[1] or "active"
                if current_status == "disabled":
                    conn.rollback()
                    return None
                if current_status not in {"active", "disabling"}:
                    conn.rollback()
                    return None
                user_id = r[0]
                conn.execute(
                    "UPDATE users SET status='disabling', "
                    "disabled_at=COALESCE(disabled_at,?) "
                    "WHERE username=? AND status IN ('active','disabling')",
                    (now, username),
                )
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_keys'"
                ).fetchone():
                    conn.execute(
                        "UPDATE api_keys SET status='revoked' "
                        "WHERE user_id=? AND status='active'",
                        (user_id,),
                    )
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn.close()

            self._run_disable_stage_hook("after_fence", username, user_id)
            cancelled = self._cancel_jobs_for_disabled_user(
                user_id, jobs_db_path
            )
            self._run_disable_stage_hook("after_jobs", username, user_id)

            conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None)
            try:
                conn.execute("PRAGMA busy_timeout=15000")
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "UPDATE users SET status='disabled', "
                    "disabled_at=COALESCE(disabled_at,?) "
                    "WHERE username=? AND user_id=? AND status='disabling'",
                    (now, username, user_id),
                )
                if cur.rowcount != 1:
                    status_row = conn.execute(
                        "SELECT status FROM users WHERE username=? AND user_id=?",
                        (username, user_id),
                    ).fetchone()
                    if not status_row or status_row[0] != "disabled":
                        raise RuntimeError("account offboarding ownership was lost")
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn.close()
            self._run_disable_stage_hook("after_finalize", username, user_id)
            return {
                "user_id": user_id,
                "pending_jobs": cancelled["pending_jobs"],
                "resetting_jobs": cancelled["resetting_jobs"],
                "running_jobs": cancelled["running_jobs"],
            }

    def _run_disable_stage_hook(
        self, stage: str, username: str, user_id: str
    ) -> None:
        hook = getattr(self, "_disable_stage_hook", None)
        if hook is not None:
            hook(stage, username, user_id)

    @staticmethod
    def _cancel_jobs_for_disabled_user(
        user_id: str, jobs_db_path: Optional[str]
    ) -> Dict[str, list]:
        result = {
            "pending_jobs": [],
            "resetting_jobs": [],
            "running_jobs": [],
        }
        if not jobs_db_path:
            return result
        conn = sqlite3.connect(
            os.path.abspath(jobs_db_path),
            timeout=15.0,
            isolation_level=None,
        )
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='jobs'"
            ).fetchone():
                conn.commit()
                return result
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if not {"id", "status", "user_id"}.issubset(columns):
                conn.commit()
                return result
            for status, key in (
                ("pending", "pending_jobs"),
                ("resetting", "resetting_jobs"),
                ("running", "running_jobs"),
            ):
                result[key] = [
                    row[0]
                    for row in conn.execute(
                        "SELECT id FROM jobs WHERE user_id=? AND status=? "
                        "ORDER BY created_at,id",
                        (user_id, status),
                    ).fetchall()
                ]
            now = time.time()
            for status, key, message in (
                ("pending", "pending_jobs", "Аккаунт отключён до запуска"),
                (
                    "resetting",
                    "resetting_jobs",
                    "Аккаунт отключён во время полного повтора",
                ),
            ):
                for job_id in result[key]:
                    data = {}
                    if "data" in columns:
                        row = conn.execute(
                            "SELECT data FROM jobs WHERE id=?", (job_id,)
                        ).fetchone()
                        try:
                            parsed = json.loads(row[0]) if row else {}
                            if isinstance(parsed, dict):
                                data = parsed
                        except (json.JSONDecodeError, TypeError):
                            data = {}
                        data["finished_at"] = now
                        data["error"] = message
                    assignments = ["status='cancelled'"]
                    params = []
                    if "data" in columns:
                        assignments.append("data=?")
                        params.append(json.dumps(data, ensure_ascii=False))
                    if "run_token" in columns:
                        assignments.append("run_token=''")
                    if "cancel_requested" in columns:
                        assignments.append("cancel_requested=0")
                    params.extend([job_id, status])
                    conn.execute(
                        f"UPDATE jobs SET {','.join(assignments)} "
                        "WHERE id=? AND status=?",
                        params,
                    )
            if "cancel_requested" in columns:
                conn.execute(
                    "UPDATE jobs SET cancel_requested=1 "
                    "WHERE user_id=? AND status='running'",
                    (user_id,),
                )
            conn.commit()
            return result
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def recover_disabling_accounts(
        self, jobs_db_path: Optional[str] = None
    ) -> int:
        """Resume fenced offboarding transactions after an interrupted startup."""
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            usernames = [
                row[0]
                for row in conn.execute(
                    "SELECT username FROM users WHERE status='disabling' "
                    "ORDER BY username"
                ).fetchall()
            ]
        recovered = 0
        for username in usernames:
            if self.disable_account(username, jobs_db_path) is not None:
                recovered += 1
        return recovered

    def delete_account(
        self, username: str, jobs_db_path: Optional[str] = None
    ) -> bool:
        """Compatibility wrapper for the historical endpoint name."""
        return self.disable_account(username, jobs_db_path) is not None

    def login_password(self, username: str, password: str, ip: str) -> tuple:
        """Вход по логину/паролю → сессия с user_id аккаунта.
        Returns (success, session_id_or_error, user_name)."""
        username = (username or "").strip()
        if not username or not password:
            return (False, "Введите логин и пароль", "")
        with self._lock:
            with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                row = conn.execute(
                    "SELECT password_hash, salt, user_id, user_name, status "
                    "FROM users WHERE username = ?",
                    (username,)
                ).fetchone()
            # Одинаковое сообщение для неверного логина И пароля — не раскрываем существование логина.
            if not row:
                return (False, "Неверный логин или пароль", "")
            pwh, salt, user_id, user_name, account_status = row
            if account_status != "active":
                return (False, "Неверный логин или пароль", "")
            if not hmac.compare_digest(self._hash_password(password, salt), pwh):
                return (False, "Неверный логин или пароль", "")
            return (True, self._create_session(user_id, user_name, ip), user_name)

    def ping(self, session_id: str) -> bool:
        now = time.time()
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            result = conn.execute(
                "UPDATE sessions SET last_ping = ? "
                "WHERE session_id = ? AND EXISTS ("
                "SELECT 1 FROM users "
                "WHERE users.user_id=sessions.user_id AND users.status='active')",
                (now, session_id),
            )
            return result.rowcount > 0

    def logout(self, session_id: str):
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def validate(self, session_id: str) -> Optional[Dict]:
        """Проверяет валидность сессии. Возвращает dict или None.
        C9 FIX-2026-07-02: rolling — любая активность (валидируемый запрос) продлевает сессию,
        обновляя last_ping (троттл SESSION_PING_THROTTLE, чтобы не писать БД на каждый запрос).
        Раньше сессию двигал только явный /auth/ping (30с setInterval, тормозится в фоновых
        вкладках) → активный юзер вылетал через 10 мин."""
        self._cleanup_expired()
        now = time.time()
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            # FIX-2026-09-13: уборка теперь редкая — просроченную сессию отсекаем здесь же,
            # иначе до ближайшей уборки она бы проходила и «оживала» через last_ping.
            row = conn.execute(
                "SELECT s.user_id, s.user_name, s.ip, s.last_ping "
                "FROM sessions s JOIN users u ON u.user_id=s.user_id "
                "WHERE s.session_id = ? AND u.status='active' AND s.last_ping >= ?",
                (session_id, now - SESSION_TIMEOUT),
            ).fetchone()
            if row and (now - row[3]) > SESSION_PING_THROTTLE:
                conn.execute("UPDATE sessions SET last_ping = ? WHERE session_id = ?", (now, session_id))
        if row:
            return {"user_id": row[0], "user_name": row[1], "ip": row[2]}
        return None

    def get_user_id(self, session_id: str) -> Optional[str]:
        """user_id аккаунта из сессии (для изоляции файлов/данных)."""
        info = self.validate(session_id)
        return info["user_id"] if info else None
