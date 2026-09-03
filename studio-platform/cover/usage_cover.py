# -*- coding: utf-8 -*-
"""
usage_cover.py — учёт расхода обложек per-user (v1.2.0, Фаза 2).

Зачем:
- Free-тариф: серверный (owner) ключ, лимит N обложек/день на user_id.
- Own-key тариф: юзер вводит свой ключ → без лимита, но расход тоже логируется
  (основа для расчёта % с каналов).
- Чётко различаем пул задач на серверном ключе и на личном (поле key_source).

Хранится в той же cover_job_queue.db (отдельная таблица usage).
Учёт ведётся по планируемому числу обложек на момент постановки задачи (reserve):
это защищает кошелёк владельца от гонок (несколько одновременных задач не пробьют лимит).
"""

import time
import sqlite3
import threading
from typing import Optional


class UsageTracker:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path, timeout=30) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS usage (
                    user_id    TEXT NOT NULL,
                    day        TEXT NOT NULL,
                    key_source TEXT NOT NULL,
                    covers     INTEGER NOT NULL DEFAULT 0,
                    credits    INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, day, key_source)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_usage_user_day ON usage(user_id, day)")

    @staticmethod
    def _today() -> str:
        # UTC-день, чтобы сброс лимита был детерминированным
        return time.strftime("%Y-%m-%d", time.gmtime())

    def used_today(self, user_id: str, key_source: str = "server") -> int:
        with sqlite3.connect(self.db_path, timeout=30) as c:
            row = c.execute(
                "SELECT covers FROM usage WHERE user_id=? AND day=? AND key_source=?",
                (user_id, self._today(), key_source),
            ).fetchone()
        return int(row[0]) if row else 0

    def add(self, user_id: str, key_source: str, covers: int, credits: int = 0):
        """Атомарно прибавляет covers/credits за сегодня."""
        if covers <= 0 and credits <= 0:
            return
        with self._lock:
            with sqlite3.connect(self.db_path, timeout=30) as c:
                c.execute("""
                    INSERT INTO usage (user_id, day, key_source, covers, credits, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, day, key_source) DO UPDATE SET
                        covers = covers + excluded.covers,
                        credits = credits + excluded.credits,
                        updated_at = excluded.updated_at
                """, (user_id, self._today(), key_source, covers, credits, time.time()))

    def remaining_free(self, user_id: str, daily_limit: int) -> int:
        return max(0, daily_limit - self.used_today(user_id, "server"))

    def summary(self, user_id: str) -> dict:
        """Для UI: сколько использовано сегодня по обоим пулам."""
        return {
            "day": self._today(),
            "server_covers_today": self.used_today(user_id, "server"),
            "personal_covers_today": self.used_today(user_id, "personal"),
        }
