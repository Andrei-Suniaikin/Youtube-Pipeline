# -*- coding: utf-8 -*-
"""
Миграция 001 — API v1.

Idempotent:
  1. Создаёт таблицу `api_keys` в sessions.db (CREATE TABLE IF NOT EXISTS).
  2. Переводит зомби-jobs (status='running' на момент старта сервера) в status='interrupted'.
     Это корневой fix для backlog #4 (zombie cleanup at restart).

Безопасно вызывать на каждом старте сервера: повторный запуск ничего не ломает.
"""
import os
import sqlite3
import time
from typing import Optional, Tuple


def migrate_001_api_v1(sessions_db_path: str, jobs_db_path: str,
                       requeue_running: bool = False) -> Tuple[int, int, str]:
    """
    Применяет миграцию.

    requeue_running: вернуть прерванные рерайт-джобы в очередь вместо того, чтобы
    хоронить их в терминальном 'interrupted'. Прогресс по чанкам лежит на диске
    (ProgressManager), поэтому такой джоб доклеивается с места обрыва, как по кнопке
    «Повтор». Дефолт False обязателен: ручной прогон миграции или импорт модуля не
    должен откл(а)ймить задачи ЖИВОГО сервера — включать только из lifespan.

    Returns: (api_keys_created, zombies_marked_interrupted, log_message)
    """
    api_keys_created = 0
    zombies = 0

    # --- 1. api_keys в sessions.db ---
    with sqlite3.connect(sessions_db_path) as conn:
        existed = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='api_keys'"
        ).fetchone()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                key_id       TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                license_key  TEXT NOT NULL,
                key_hash     TEXT NOT NULL UNIQUE,
                prefix       TEXT NOT NULL,
                name         TEXT NOT NULL,
                created_at   REAL NOT NULL,
                last_used_at REAL,
                status       TEXT NOT NULL DEFAULT 'active'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_status ON api_keys(status)")
        if not existed:
            api_keys_created = 1

    # --- 2. zombie cleanup в job_queue.db ---
    requeued = 0
    with sqlite3.connect(jobs_db_path) as conn:
        if requeue_running:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            if "resume_count" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN resume_count INTEGER NOT NULL DEFAULT 0")
            # Лимит нужен, чтобы джоб, который падает сам по себе, не крутился вечно
            # по кругу «рестарт → очередь → падение».
            limit = 2
            try:
                limit = max(0, int(os.environ.get("REWRITE_RESTART_RESUME_LIMIT", "2") or 2))
            except (TypeError, ValueError):
                limit = 2
            cur = conn.execute(
                "UPDATE jobs SET status='pending', run_token='', cancel_requested=0, "
                "resume_count = COALESCE(resume_count,0) + 1, "
                "log = COALESCE(log,'') || ? "
                "WHERE status='running' AND COALESCE(cancel_requested,0)=0 "
                "AND COALESCE(resume_count,0) < ?",
                (f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] [migrate 001] возвращено в очередь "
                 f"после рестарта — продолжит с места обрыва", limit)
            )
            requeued = cur.rowcount
        # всё, что не прошло фильтр (отменённые, исчерпавшие лимит), хороним как раньше.
        # ТОЛЬКО при requeue_running — то есть только со старта сервера. Раньше этот UPDATE
        # шёл безусловно, и ручной прогон миграции на живом сервере рубил ВСЕ работающие
        # задачи (проверено: running → interrupted на боевой базе).
        if requeue_running:
            cur = conn.execute(
                "UPDATE jobs SET status='interrupted', "
                "log = COALESCE(log,'') || ? "
                "WHERE status='running'",
                (f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] [migrate 001] startup zombie cleanup",)
            )
            zombies = cur.rowcount

    msg = (
        f"[migrate 001] api_keys "
        + ("created" if api_keys_created else "already exists")
        + (f"; {requeued} job(s) возвращено в очередь" if requeued else "")
        + f"; {zombies} zombie job(s) marked 'interrupted'"
    )
    return api_keys_created, zombies, msg


if __name__ == "__main__":
    # Запуск вручную для тестирования
    sessions_db = os.environ.get("SESSION_DB", "/root/rewrite-server/sessions.db")
    jobs_db = os.environ.get("JOBS_DB", "/root/rewrite-server/job_queue.db")
    created, zombies, msg = migrate_001_api_v1(sessions_db, jobs_db)
    print(msg)
