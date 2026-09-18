# -*- coding: utf-8 -*-
"""
Локальная очередь заданий для Cover Generator (по образцу job_queue.py).
SQLite-очередь + один честный dispatcher:
FIFO внутри пользователя, round-robin между пользователями.
"""

import json
import uuid
import time
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict, fields as dc_fields
from typing import List, Optional, Callable, Any, Dict, Iterable
from enum import Enum

from provider_policy import (
    InvalidGenerationProvider,
    canonical_generation_provider,
    provider_scheduler_group,
)


class IdempotencyConflict(ValueError):
    pass


class JobStatus(str, Enum):
    PENDING = "pending"
    RESETTING = "resetting"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_PARTIAL = "completed_partial"  # завершено, но сохранена только часть обложек
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    """Одно задание генерации. Vendor credentials здесь намеренно отсутствуют."""
    id: str = ""
    status: str = "pending"
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0

    urls: str = "[]"           # JSON list
    preset_data: str = "{}"     # JSON from Preset asdict
    # v1.0.4: title-mode (генерация по названию без YouTube URL)
    mode: str = "url"            # url | title | seed
    titles_per_lang: str = "{}"  # JSON dict {lang_display: title_text}
    extra_prompt: str = ""       # доп.детали сцены для title-mode
    # seed-mode: механизм «пресет+семя → N обложек» (niche-agnostic)
    seeds_per_lang: str = "{}"   # JSON dict {lang_display: seed_block_text}
    n_variants: int = 3          # сколько вариантов обложки на язык
    output_dir: str = ""
    languages: str = "[]"       # JSON list
    refs_per_language: str = "{}"  # JSON dict
    max_threads: int = 1
    parallel: bool = False      # legacy-only; новый scheduler поле игнорирует
    user_id: str = ""           # для сервера: изоляция по пользователю
    generation_provider: str = "flow"  # canonical provider для admission/scheduler
    cancel_requested: bool = False      # persistent cooperative cancel
    run_token: str = ""                 # internal per-claim lease; never exposed by API
    artifact_namespace: str = ""     # new jobs isolate output paths by job id
    recovery_attempts: int = 0
    restore_hold: bool = False  # Only offline reconciliation can clear this hold.
    credential_mode: str = ""           # own/server only, frozen at admission; no secrets

    progress_percent: int = 0
    progress_detail: str = ""
    log: str = ""
    error: str = ""
    results_count: int = 0      # успешных
    output_files: str = "[]"    # JSON list относительных путей (для сервера)
    fallback_files: str = "[]"  # JSON list путей обложек сохранённых через fallback (без текста)


_JOB_FIELDS = None
_LEGACY_SECRET_FIELDS = ("openai_api_key", "fast_gen_api_key", "veo_api_key")
_RESERVED_QUEUE_STATUSES = (
    JobStatus.PENDING.value,
    JobStatus.RESETTING.value,
)


class PendingJobLimitExceeded(RuntimeError):
    """Atomic enqueue rejected because the user's pending queue is full."""

    def __init__(self, pending: int, limit: int):
        self.pending = int(pending)
        self.limit = int(limit)
        super().__init__(
            f"too many pending jobs ({self.pending}); max {self.limit}"
        )


def _scrub_job_data(data: dict) -> tuple:
    """Удаляет legacy vendor secrets и возвращает (data, changed)."""
    clean = dict(data or {})
    changed = False
    for key in _LEGACY_SECRET_FIELDS:
        if key in clean:
            clean.pop(key, None)
            changed = True
    return clean, changed


def _job_json(job: "Job") -> str:
    """Единственная сериализация Job: секретные legacy-поля никогда не попадут в DB."""
    data, _ = _scrub_job_data(asdict(job))
    return json.dumps(data, ensure_ascii=False)


def _provider_from_data(data: dict, column_provider: str = "") -> str:
    raw = data.get("generation_provider")
    if not raw:
        try:
            preset = json.loads(data.get("preset_data") or "{}")
        except Exception:
            preset = {}
        raw = (preset or {}).get("generation_provider") or column_provider or "flow"
    try:
        return canonical_generation_provider(raw)
    except InvalidGenerationProvider:
        # Legacy bad value remains visible and will fail explicitly in worker/admission.
        return str(raw)


def _job_from_data(data: dict) -> "Job":
    """Создаёт Job из dict, игнорируя неизвестные поля (для совместимости со старыми/чужими записями)."""
    global _JOB_FIELDS
    if _JOB_FIELDS is None:
        _JOB_FIELDS = {f.name for f in dc_fields(Job)}
    clean, _ = _scrub_job_data(data)
    return Job(**{k: v for k, v in clean.items() if k in _JOB_FIELDS})


class JobQueue:
    """SQLite-очередь заданий. Локальная, переживает рестарт приложения."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            import os
            base = os.path.join(os.path.expanduser("~"), "cover_presets")
            os.makedirs(base, exist_ok=True)
            db_path = os.path.join(base, "cover_job_queue.db")
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            # v1.3.0: WAL + busy_timeout — иначе под нагрузкой (много юзеров + 20 sub-потоков)
            # ловим 'database is locked'. WAL персистентен на файле, busy_timeout — на коннект.
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
            except Exception:
                pass
            secure_delete = conn.execute("PRAGMA secure_delete=ON").fetchone()
            if not secure_delete or int(secure_delete[0]) != 1:
                raise RuntimeError("SQLite secure_delete could not be enabled")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    user_id TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT 'flow',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    run_token TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("CREATE TABLE IF NOT EXISTS job_requests (user_id TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL, job_id TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(user_id,request_key))")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "user_id" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
            if "provider" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN provider TEXT NOT NULL DEFAULT 'flow'")
            if "cancel_requested" not in columns:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
                )
            if "run_token" not in columns:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN run_token TEXT NOT NULL DEFAULT ''"
                )

            # Идемпотентная миграция: backfill отдельной scheduler-колонки и scrub
            # всех vendor credentials из legacy jobs.data.
            rows = conn.execute(
                """
                SELECT id, data, user_id, provider, cancel_requested, run_token
                FROM jobs
                """
            ).fetchall()
            for (
                job_id,
                raw,
                column_uid,
                column_provider,
                column_cancel,
                column_run_token,
            ) in rows:
                try:
                    data = json.loads(raw or "{}")
                except Exception:
                    continue
                clean, changed = _scrub_job_data(data)
                uid = str(clean.get("user_id") or column_uid or "")
                provider = _provider_from_data(clean, column_provider)
                cancel_requested = bool(
                    clean.get("cancel_requested", False) or column_cancel
                )
                run_token = str(
                    clean.get("run_token") or column_run_token or ""
                )
                if clean.get("user_id", "") != uid:
                    clean["user_id"] = uid
                    changed = True
                if clean.get("generation_provider") != provider:
                    clean["generation_provider"] = provider
                    changed = True
                if bool(clean.get("cancel_requested", False)) != cancel_requested:
                    clean["cancel_requested"] = cancel_requested
                    changed = True
                if str(clean.get("run_token") or "") != run_token:
                    clean["run_token"] = run_token
                    changed = True
                if (
                    changed
                    or str(column_uid or "") != uid
                    or str(column_provider or "") != provider
                    or bool(column_cancel) != cancel_requested
                    or str(column_run_token or "") != run_token
                ):
                    conn.execute(
                        """
                        UPDATE jobs
                        SET data = ?, user_id = ?, provider = ?,
                            cancel_requested = ?, run_token = ?
                        WHERE id = ?
                        """,
                        (
                            json.dumps(clean, ensure_ascii=False),
                            uid,
                            provider,
                            int(cancel_requested),
                            run_token,
                            job_id,
                        ),
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_sched "
                "ON jobs(status, user_id, created_at, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_provider_sched "
                "ON jobs(status, cancel_requested, provider, user_id, created_at, id)"
            )
            # Persist the scrub before the worker can open another connection, then
            # remove obsolete WAL frames without VACUUM or touching old backups.
            conn.commit()
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if not checkpoint or int(checkpoint[0]) != 0:
                raise RuntimeError("SQLite WAL checkpoint remained busy after migration")

    @staticmethod
    def _reserved_count(
        conn,
        user_id: str,
        *,
        exclude_job_id: str = "",
    ) -> int:
        params: List[Any] = [
            JobStatus.PENDING.value,
            JobStatus.RESETTING.value,
            user_id or "",
        ]
        exclude_sql = ""
        if exclude_job_id:
            exclude_sql = " AND id != ?"
            params.append(exclude_job_id)
        row = conn.execute(
            f"""
            SELECT COUNT(*) FROM jobs
            WHERE status IN (?, ?) AND user_id = ?
            {exclude_sql}
            """,
            params,
        ).fetchone()
        return int(row[0] if row else 0)

    @classmethod
    def _reserve_pending_slot(
        cls,
        conn,
        user_id: str,
        limit: int,
        *,
        exclude_job_id: str = "",
    ) -> int:
        limit = max(1, int(limit))
        reserved = cls._reserved_count(
            conn,
            user_id,
            exclude_job_id=exclude_job_id,
        )
        if reserved >= limit:
            raise PendingJobLimitExceeded(reserved, limit)
        return reserved

    def get_idempotent_job(self, user_id, request_key, request_hash):
        if not request_key:
            return None
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            row = conn.execute("SELECT request_hash,job_id FROM job_requests WHERE user_id=? AND request_key=?", (user_id, request_key)).fetchone()
            if row is None:
                return None
            if row[0] != request_hash:
                raise IdempotencyConflict("Idempotency-Key уже использован с другим запросом")
            if not conn.execute("SELECT 1 FROM jobs WHERE id=? AND user_id=?", (row[1], user_id)).fetchone():
                raise IdempotencyConflict("Задача этого Idempotency-Key удалена; повторное создание запрещено")
            return row[1]

    def add_job(
        self,
        job: Job,
        *,
        max_pending_for_user: Optional[int] = None,
        idempotency_key: str = "",
        request_hash: str = "",
    ) -> str:
        """Atomic count+INSERT. `max_pending_for_user` is a technical cap."""
        job.id = uuid.uuid4().hex[:16]
        job.artifact_namespace = job.id
        job.created_at = time.time()
        job.status = JobStatus.PENDING.value
        job.generation_provider = canonical_generation_provider(
            job.generation_provider or _provider_from_data(asdict(job))
        )
        job.cancel_requested = False
        job.run_token = ""
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                row = conn.execute("SELECT request_hash,job_id FROM job_requests WHERE user_id=? AND request_key=?", (job.user_id, idempotency_key)).fetchone()
                if row:
                    if row[0] != request_hash or not conn.execute("SELECT 1 FROM jobs WHERE id=?", (row[1],)).fetchone():
                        raise IdempotencyConflict("Idempotency-Key конфликтует или задача удалена")
                    conn.execute("COMMIT")
                    return row[1]
                conn.execute("INSERT INTO job_requests VALUES(?,?,?,?,?)", (job.user_id,idempotency_key,request_hash,job.id,job.created_at))
            if max_pending_for_user is not None:
                self._reserve_pending_slot(
                    conn,
                    job.user_id or "",
                    max_pending_for_user,
                )
            conn.execute(
                """
                INSERT INTO jobs
                    (id, data, status, created_at, user_id, provider,
                     cancel_requested, run_token)
                VALUES (?, ?, ?, ?, ?, ?, 0, '')
                """,
                (
                    job.id,
                    _job_json(job),
                    job.status,
                    job.created_at,
                    job.user_id or "",
                    job.generation_provider,
                )
            )
            conn.execute("COMMIT")
        except PendingJobLimitExceeded:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
        return job.id

    def get_pending_providers(self) -> List[str]:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            rows = conn.execute(
                """
                SELECT provider, MIN(created_at) AS oldest
                FROM jobs
                WHERE status = ? AND cancel_requested = 0
                GROUP BY provider
                ORDER BY oldest ASC, provider ASC
                """,
                (JobStatus.PENDING.value,),
            ).fetchall()
        return [str(row[0] or "flow") for row in rows]

    def queue_pulse(self) -> List[tuple]:
        """Read-only снимок активных строк для виджета живой очереди.

        Отдаёт сырые кортежи (status, user_id, cancel_requested) — колонка data
        намеренно НЕ читается (чтение JSON превращает индексный скан в row lookup).
        Покрывается idx_jobs_provider_sched.
        """
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            rows = conn.execute(
                """
                SELECT status, COALESCE(user_id, ''), COALESCE(cancel_requested, 0)
                FROM jobs
                WHERE status IN (?, ?, ?)
                """,
                (
                    JobStatus.PENDING.value,
                    JobStatus.RESETTING.value,
                    JobStatus.RUNNING.value,
                ),
            ).fetchall()
        return [
            (str(row[0] or ""), str(row[1] or ""), int(row[2] or 0))
            for row in rows
        ]

    def get_pending_user_ids(
        self, providers: Optional[Iterable[str]] = None
    ) -> List[str]:
        """Пользователи с pending jobs, упорядоченные по их самой старой job."""
        provider_list = list(providers) if providers is not None else None
        if provider_list == []:
            return []
        provider_sql = ""
        params: List[Any] = [JobStatus.PENDING.value]
        if provider_list is not None:
            provider_sql = (
                " AND provider IN ("
                + ",".join("?" for _ in provider_list)
                + ")"
            )
            params.extend(provider_list)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            rows = conn.execute(
                f"""
                SELECT user_id, MIN(created_at) AS oldest
                FROM jobs
                WHERE status = ? AND cancel_requested = 0
                {provider_sql}
                GROUP BY user_id
                ORDER BY oldest ASC, user_id ASC
                """,
                params,
            ).fetchall()
        return [str(row[0] or "") for row in rows]

    def has_pending_for_user(self, user_id: str) -> bool:
        return self.count_pending_for_user(user_id) > 0

    def count_pending_for_user(self, user_id: str) -> int:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM jobs
                WHERE status = ? AND cancel_requested = 0 AND user_id = ?
                """,
                (JobStatus.PENDING.value, user_id or ""),
            ).fetchone()
        return int(row[0] if row else 0)

    def count_reserved_for_user(self, user_id: str) -> int:
        """Pending + resetting jobs that already own a queue slot."""
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            return self._reserved_count(conn, user_id)

    def peek_pending_for_user(self, user_id, providers):
        providers = list(providers)
        if not providers:
            return None
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            row = conn.execute(
                "SELECT data FROM jobs WHERE status='pending' AND user_id=? AND provider IN ("
                + ",".join("?" for _ in providers) + ") ORDER BY created_at,id LIMIT 1",
                [user_id, *providers],
            ).fetchone()
        return _job_from_data(json.loads(row[0])) if row else None

    def claim_next_for_user(
        self,
        user_id: str,
        providers: Optional[Iterable[str]] = None,
    ) -> Optional[Job]:
        """Атомарно claim-ит старейшую pending job пользователя."""
        provider_list = list(providers) if providers is not None else None
        if provider_list == []:
            return None
        provider_sql = ""
        params: List[Any] = [JobStatus.PENDING.value, user_id or ""]
        if provider_list is not None:
            provider_sql = (
                " AND provider IN ("
                + ",".join("?" for _ in provider_list)
                + ")"
            )
            params.extend(provider_list)
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    f"""
                    SELECT id, data, run_token
                    FROM jobs
                    WHERE status = ? AND user_id = ? AND cancel_requested = 0
                    {provider_sql}
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                    """,
                    params,
                ).fetchone()
                if not row:
                    conn.execute("COMMIT")
                    return None
                job = _job_from_data(json.loads(row[1]))
                job.status = JobStatus.RUNNING.value
                job.started_at = time.time()
                job.cancel_requested = False
                previous_run_token = str(row[2] or "")
                job.run_token = uuid.uuid4().hex
                cur = conn.execute(
                    """
                    UPDATE jobs
                    SET data = ?, status = ?, user_id = ?, provider = ?,
                        cancel_requested = 0, run_token = ?
                    WHERE id = ? AND status = ? AND cancel_requested = 0
                      AND run_token = ?
                    """,
                    (
                        _job_json(job),
                        JobStatus.RUNNING.value,
                        job.user_id or "",
                        job.generation_provider,
                        job.run_token,
                        row[0],
                        JobStatus.PENDING.value,
                        previous_run_token,
                    ),
                )
                if cur.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return None
                conn.execute("COMMIT")
                return job
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def update_job(
        self,
        job: Job,
        *,
        expected_status=None,
        expected_run_token: Optional[str] = None,
        reset_cancel_requested: bool = False,
        max_pending_for_user: Optional[int] = None,
    ) -> bool:
        """CAS update по `(id, status, run_token)`.

        Running/resetting writers обязаны владеть lease. При переходе к pending
        token очищается, поэтому worker старого run не сможет писать после recovery.
        """
        expected = None
        if expected_status is not None:
            if isinstance(expected_status, (tuple, list, set)):
                expected = {str(value) for value in expected_status}
            else:
                expected = {str(expected_status)}
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT status, cancel_requested, run_token, user_id
                FROM jobs WHERE id = ?
                """,
                (job.id,),
            ).fetchone()
            if not row or (expected is not None and str(row[0]) not in expected):
                conn.execute("ROLLBACK")
                return False
            current_status = str(row[0])
            current_cancel = bool(row[1])
            current_run_token = str(row[2] or "")
            current_user_id = str(row[3] or "")
            cas_run_token = (
                str(expected_run_token)
                if expected_run_token is not None
                else str(job.run_token or "")
            )
            if current_run_token != cas_run_token:
                conn.execute("ROLLBACK")
                return False
            if (
                max_pending_for_user is not None
                and str(job.status) in _RESERVED_QUEUE_STATUSES
            ):
                self._reserve_pending_slot(
                    conn,
                    current_user_id,
                    max_pending_for_user,
                    exclude_job_id=job.id,
                )
            if reset_cancel_requested:
                job.cancel_requested = False
            elif current_cancel:
                job.cancel_requested = True
                # Progress may keep RUNNING while executor cooperatively stops.
                # Any terminal write after a cancel request becomes CANCELLED.
                if job.status != JobStatus.RUNNING.value:
                    job.status = JobStatus.CANCELLED.value
                    job.finished_at = job.finished_at or time.time()
            if job.status == JobStatus.PENDING.value:
                job.run_token = ""
            provider = str(job.generation_provider or "flow")
            cur = conn.execute(
                """
                UPDATE jobs
                SET data = ?, status = ?, user_id = ?, provider = ?,
                    cancel_requested = ?, run_token = ?
                WHERE id = ? AND status = ? AND run_token = ?
                """,
                (
                    _job_json(job),
                    job.status,
                    job.user_id or "",
                    provider,
                    int(bool(job.cancel_requested)),
                    str(job.run_token or ""),
                    job.id,
                    current_status,
                    current_run_token,
                ),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return False
            conn.execute("COMMIT")
            return True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def get_job(self, job_id: str) -> Optional[Job]:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            row = conn.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job_from_data(json.loads(row[0])) if row else None

    def get_all_jobs(self) -> List[Job]:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            rows = conn.execute("SELECT data FROM jobs ORDER BY created_at DESC").fetchall()
        return [_job_from_data(json.loads(r[0])) for r in rows]

    def cancel_job(self, job_id: str) -> bool:
        """Атомарно отменяет pending или персистит cancel_requested для running."""
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT data, status, run_token FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not row or row[1] not in (
                JobStatus.PENDING.value,
                JobStatus.RUNNING.value,
            ):
                conn.execute("ROLLBACK")
                return False
            job = _job_from_data(json.loads(row[0]))
            current_status = str(row[1])
            current_run_token = str(row[2] or "")
            job.run_token = current_run_token
            job.cancel_requested = True
            if current_status == JobStatus.PENDING.value:
                job.status = JobStatus.CANCELLED.value
                job.finished_at = time.time()
            else:
                job.status = JobStatus.RUNNING.value
            cur = conn.execute(
                """
                UPDATE jobs
                SET data = ?, status = ?, cancel_requested = 1
                WHERE id = ? AND status = ? AND run_token = ?
                """,
                (
                    _job_json(job),
                    job.status,
                    job_id,
                    current_status,
                    current_run_token,
                ),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return False
            conn.execute("COMMIT")
            return True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def is_cancelled(self, job_id: str) -> bool:
        """Persistent cancel-check: переживает restart процесса."""
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            row = conn.execute(
                "SELECT cancel_requested, status FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return bool(
            row
            and (
                bool(row[0])
                or str(row[1]) == JobStatus.CANCELLED.value
            )
        )

    def should_stop_run(self, job_id: str, run_token: str) -> bool:
        """Cancel-aware lease check for waits and provider polling."""
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            row = conn.execute(
                """
                SELECT status, cancel_requested, run_token
                FROM jobs WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
        return bool(
            not row
            or str(row[0]) != JobStatus.RUNNING.value
            or bool(row[1])
            or str(row[2] or "") != str(run_token or "")
        )

    def clear_cancelled(self, job_id: str):
        """Legacy no-op: cancel_requested очищается только явным retry/resume."""
        return None

    def recover_interrupted_jobs(
        self,
        *,
        max_pending_for_user: Optional[int] = None,
    ) -> Dict[str, int]:
        """running/resetting→pending, persistent cancel_requested→cancelled."""
        recovered = 0
        cancelled = 0
        queue_full_failed = 0
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id, data, cancel_requested
                FROM jobs
                WHERE status IN (?, ?)
                ORDER BY created_at, id
                """,
                (JobStatus.RUNNING.value, JobStatus.RESETTING.value),
            ).fetchall()
            for job_id, raw, cancel_requested in rows:
                job = _job_from_data(json.loads(raw or "{}"))
                if bool(cancel_requested) or bool(job.cancel_requested):
                    job.status = JobStatus.CANCELLED.value
                    job.cancel_requested = True
                    job.finished_at = job.finished_at or time.time()
                    cancelled += 1
                elif job.recovery_attempts >= 3:
                    job.status = JobStatus.FAILED.value
                    job.finished_at = time.time()
                    job.error = 'Лимит восстановления исчерпан; задача изолирована, результаты и generation IDs сохранены'
                    queue_full_failed += 1
                else:
                    job.recovery_attempts += 1
                    try:
                        if max_pending_for_user is not None:
                            self._reserve_pending_slot(
                                conn,
                                job.user_id or "",
                                max_pending_for_user,
                                exclude_job_id=job_id,
                            )
                        job.status = JobStatus.PENDING.value
                        job.started_at = 0
                        job.cancel_requested = False
                        recovered += 1
                    except PendingJobLimitExceeded as exc:
                        job.status = JobStatus.FAILED.value
                        job.cancel_requested = False
                        job.finished_at = time.time()
                        job.error = (
                            "Восстановление остановлено: очередь заполнена "
                            f"({exc.pending}/{exc.limit}). Повторите retry позже."
                        )
                        queue_full_failed += 1
                job.run_token = ""
                conn.execute(
                    """
                    UPDATE jobs
                    SET data = ?, status = ?, cancel_requested = ?, run_token = ''
                    WHERE id = ? AND status IN (?, ?)
                    """,
                    (
                        _job_json(job),
                        job.status,
                        int(job.cancel_requested),
                        job_id,
                        JobStatus.RUNNING.value,
                        JobStatus.RESETTING.value,
                    ),
                )
            conn.execute("COMMIT")
            result = {
                "requeued": recovered,
                "cancelled": cancelled,
            }
            if queue_full_failed:
                result["queue_full_failed"] = queue_full_failed
            return result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def delete_job(self, job_id: str):
        """Удалить задание из очереди (любой статус)."""
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


class JobWorker:
    """
    Один work-conserving fair dispatcher и один общий ThreadPoolExecutor.
    Legacy поле Job.parallel намеренно не участвует в выборе.
    """

    def __init__(
        self,
        queue: JobQueue,
        executor: Callable[[Job], None],
        max_concurrent_jobs: int = 3,
        poll_interval: float = 0.5,
        provider_limits: Optional[Dict[str, int]] = None,
        job_admission_guard: Optional[Callable] = None,
    ):
        self.queue = queue
        self._executor = executor
        self._max_concurrent = max(1, max_concurrent_jobs)
        self._provider_limits = {
            str(group): max(1, int(limit))
            for group, limit in (provider_limits or {}).items()
        }
        self._job_admission_guard = job_admission_guard
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._running: dict = {}  # future -> job
        self._running_lock = threading.Lock()
        self._last_user_id: Optional[str] = None
        self._last_user_by_group: Dict[str, Optional[str]] = {}
        self._user_rings: Dict[str, List[str]] = {}
        self._last_group: Optional[str] = None
        self._group_ring: List[str] = []

    def start(self):
        self._stop_event.clear()
        self._pool = ThreadPoolExecutor(max_workers=self._max_concurrent)
        self._thread = threading.Thread(target=self._run_dispatcher, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._pool:
            self._pool.shutdown(wait=False)

    def get_running_jobs(self) -> List[Job]:
        with self._running_lock:
            return list(self._running.values())

    def _execute_job(self, job: Job):
        try:
            self._executor(job)
            if self.queue.is_cancelled(job.id):
                job.status = JobStatus.CANCELLED.value
            elif job.status == JobStatus.RUNNING.value:
                job.status = JobStatus.COMPLETED.value
        except Exception as e:
            job.status = JobStatus.FAILED.value
            job.error = str(e)
        finally:
            job.finished_at = time.time()
        self.queue.update_job(job, expected_status=JobStatus.RUNNING.value)

    @staticmethod
    def _provider_group(provider: str) -> str:
        try:
            return provider_scheduler_group(provider)
        except InvalidGenerationProvider:
            return "invalid"

    def _provider_limit(self, group: str) -> int:
        return min(
            self._max_concurrent,
            self._provider_limits.get(group, self._max_concurrent),
        )

    def _running_group_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        with self._running_lock:
            jobs = list(self._running.values())
        for job in jobs:
            group = self._provider_group(job.generation_provider)
            counts[group] = counts.get(group, 0) + 1
        return counts

    def _eligible_pending_groups(self) -> Dict[str, List[str]]:
        counts = self._running_group_counts()
        eligible: Dict[str, List[str]] = {}
        for provider in self.queue.get_pending_providers():
            group = self._provider_group(provider)
            if self._job_admission_guard is not None or counts.get(group, 0) < self._provider_limit(group):
                eligible.setdefault(group, []).append(provider)
        if self._job_admission_guard is not None:
            eligible = {group: providers for group, providers in eligible.items()
                        if any(self._user_can_dispatch(uid, providers) for uid in self.queue.get_pending_user_ids(providers))}
        return eligible

    def _user_can_dispatch(self, user_id, providers):
        if self._job_admission_guard is None:
            return True
        job = self.queue.peek_pending_for_user(user_id, providers)
        with self._running_lock:
            running = list(self._running.values())
        return bool(job and self._job_admission_guard(job, running))

    def _next_group(self, groups: Iterable[str]) -> Optional[str]:
        pending_groups = list(groups)
        if not pending_groups:
            return None
        for group in pending_groups:
            if group not in self._group_ring:
                self._group_ring.append(group)
        if self._last_group is None:
            return pending_groups[0]
        if self._last_group not in self._group_ring:
            self._group_ring.append(self._last_group)
        start = self._group_ring.index(self._last_group)
        pending = set(pending_groups)
        for offset in range(1, len(self._group_ring) + 1):
            candidate = self._group_ring[
                (start + offset) % len(self._group_ring)
            ]
            if candidate in pending:
                return candidate
        return pending_groups[0]

    def _next_user_id(
        self,
        providers: Optional[Iterable[str]] = None,
        *,
        group: str = "all",
    ) -> Optional[str]:
        users = self.queue.get_pending_user_ids(providers)
        if self._job_admission_guard is not None:
            users = [uid for uid in users if self._user_can_dispatch(uid, providers)]
        if not users:
            return None
        ring = self._user_rings.setdefault(group, [])
        last_user_id = self._last_user_by_group.get(group)
        for user_id in users:
            if user_id not in ring:
                ring.append(user_id)
        if last_user_id is None:
            return users[0]
        if last_user_id not in ring:
            ring.append(last_user_id)
        start = ring.index(last_user_id)
        pending = set(users)
        for offset in range(1, len(ring) + 1):
            candidate = ring[(start + offset) % len(ring)]
            if candidate in pending:
                return candidate
        # Defensive fallback; обычно цикл выше всегда находит pending user.
        return users[0]

    def _reap_finished(self):
        with self._running_lock:
            done = [future for future in self._running if future.done()]
            for future in done:
                self._running.pop(future, None)

    def _dispatch_available(self) -> int:
        """Заполняет все свободные slots. Возвращает число новых claims."""
        self._reap_finished()
        with self._running_lock:
            free_slots = self._max_concurrent - len(self._running)
        dispatched = 0
        while free_slots > 0 and not self._stop_event.is_set():
            eligible_groups = self._eligible_pending_groups()
            group = self._next_group(eligible_groups)
            if group is None:
                break
            eligible_providers = eligible_groups[group]
            user_id = self._next_user_id(
                eligible_providers,
                group=group,
            )
            if user_id is None:
                break
            job = self.queue.claim_next_for_user(user_id, eligible_providers)
            if job is None:
                # Между выбором и claim job могла отмениться; перечитаем очередь.
                continue
            self._last_user_id = user_id
            self._last_user_by_group[group] = user_id
            self._last_group = group
            future = self._pool.submit(self._execute_job, job)
            with self._running_lock:
                self._running[future] = job
            free_slots -= 1
            dispatched += 1
        return dispatched

    def _run_dispatcher(self):
        while not self._stop_event.is_set():
            self._dispatch_available()
            time.sleep(self._poll_interval)
