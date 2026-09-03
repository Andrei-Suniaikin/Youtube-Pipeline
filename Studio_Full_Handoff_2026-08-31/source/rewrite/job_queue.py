# -*- coding: utf-8 -*-
"""Persistent, tenant-fair job queue for Rewrite Studio.

The status column, run token and cancellation bit form one atomic lifecycle.
Worker writes are accepted only while the same run token still owns a running
job, so a cancelled/retried job cannot be completed by a stale worker.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional


class JobType(str, Enum):
    GENERATION = "generation"
    REWRITE = "rewrite"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    RESETTING = "resetting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"
    INTERRUPTED = "interrupted"


TERMINAL_STATUSES = frozenset(
    {
        JobStatus.COMPLETED.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
        JobStatus.PARTIAL.value,
        JobStatus.INTERRUPTED.value,
    }
)
RETRYABLE_STATUSES = frozenset(
    {
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
        JobStatus.PARTIAL.value,
        JobStatus.INTERRUPTED.value,
    }
)
PROVIDERS = ("openai", "claude", "codex")


def classify_provider_from_model(model: str) -> str:
    """Compatibility classifier for legacy rows without a trusted provider."""
    value = (model or "").strip().lower()
    if value.startswith("codex:"):
        return "codex"
    if "claude" in value:
        return "claude"
    return "openai"


@dataclass
class Job:
    id: str = ""
    job_type: str = "generation"
    status: str = "pending"
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0

    model: str = ""
    api_key: str = ""
    use_subscription: bool = False
    oauth_token: str = ""
    max_threads: int = 1
    output_dir: str = ""

    gen_tasks: str = "[]"
    preset_name: str = ""
    gen_languages: str = "[]"
    gen_safe_text: bool = False

    rewrite_files: str = "[]"
    rewrite_languages: str = "[]"
    rewrite_chunk_size: int = 8000
    rewrite_address_style: str = ""
    rewrite_preserve_paragraphs: bool = False
    oauth_tokens: str = "[]"

    progress_percent: int = 0
    progress_detail: str = ""
    log: str = ""
    output_files: str = "[]"
    # Crash-safe full-retry cleanup manifest. Never exposed as a credential.
    reset_manifest: str = "[]"
    error: str = ""

    # Retained in the object/JSON only for old clients. Scheduling ignores it.
    parallel: bool = False
    user_id: str = ""

    # Authoritative copies live in dedicated DB columns.
    provider: str = ""
    run_token: str = ""
    cancel_requested: bool = False


@dataclass(frozen=True)
class LifecycleResult:
    changed: bool
    code: str
    previous: str
    status: str


@dataclass
class RetryTicket:
    job: Job
    token: str
    previous_status: str
    full: bool


_COLUMN_FIELDS = {
    "log",
    "user_id",
    "status",
    "provider",
    "run_token",
    "cancel_requested",
}
_SENSITIVE_FIELDS = {"api_key", "oauth_token", "oauth_tokens"}
_JOB_FIELD_NAMES = {field.name for field in fields(Job)}


class JobQueue:
    """SQLite queue with atomic lifecycle and persistent fair-provider rings."""

    def __init__(
        self,
        db_path: str = "job_queue.db",
        provider_classifier: Optional[Callable[[str], str]] = None,
        redactor: Optional[Callable[[Any], Any]] = None,
    ):
        self.db_path = db_path
        self._provider_classifier = provider_classifier or classify_provider_from_model
        self._redactor = redactor or (lambda value: value)
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            timeout=15.0,
            isolation_level=None,
        )
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _immediate_locked(self):
        """BEGIN IMMEDIATE transaction. Caller must hold ``self._lock``."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    priority INTEGER DEFAULT 0
                )
                """
            )
        self._migrate_db()

    def _migrate_db(self) -> None:
        """Schema-only migration.

        Legacy credential scrubbing intentionally does *not* happen here. It is
        controlled by the two-phase shared-key migration and its feature flag.
        """
        with self._lock:
            with self._immediate_locked():
                existing = {
                    row[1] for row in self._conn.execute("PRAGMA table_info(jobs)")
                }
                additions = {
                    # не выдавать джоб раньше этого времени: так исчерпанное окно
                    # подписки откладывает работу, а не хоронит её
                    "not_before": "REAL NOT NULL DEFAULT 0",
                    "user_id": "TEXT DEFAULT ''",
                    "log": "TEXT DEFAULT ''",
                    "provider": "TEXT DEFAULT ''",
                    "run_token": "TEXT DEFAULT ''",
                    "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
                }
                for column, ddl in additions.items():
                    if column not in existing:
                        self._conn.execute(
                            f"ALTER TABLE jobs ADD COLUMN {column} {ddl}"
                        )

                rows = self._conn.execute(
                    "SELECT id, data, COALESCE(user_id,''), COALESCE(log,''), "
                    "COALESCE(provider,'') FROM jobs"
                ).fetchall()
                for row_id, raw, column_uid, column_log, column_provider in rows:
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    uid = column_uid or str(data.get("user_id", "") or "")
                    log = column_log or str(data.get("log", "") or "")
                    provider = column_provider or self._provider_classifier(
                        str(data.get("model", "") or "")
                    )
                    self._conn.execute(
                        "UPDATE jobs SET user_id=?, log=?, provider=? WHERE id=?",
                        (uid, log, provider, row_id),
                    )

                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS fair_queue_users (
                        provider TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        first_seen REAL NOT NULL,
                        PRIMARY KEY(provider, user_id)
                    )
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS fair_queue_state (
                        provider TEXT PRIMARY KEY,
                        last_user_id TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_provider_fair "
                    "ON jobs(status, provider, user_id, created_at, id)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_run_token "
                    "ON jobs(id, status, run_token)"
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO fair_queue_users(provider,user_id,first_seen) "
                    "SELECT provider,user_id,MIN(created_at) FROM jobs "
                    "WHERE provider<>'' GROUP BY provider,user_id"
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _provider_for(self, job: Job) -> str:
        provider = (job.provider or "").strip().lower()
        if provider not in PROVIDERS:
            provider = self._provider_classifier(job.model)
        if provider not in PROVIDERS:
            raise ValueError("unsupported provider")
        return provider

    def _serialize_data(self, job: Job) -> str:
        data = asdict(job)
        for field_name in _COLUMN_FIELDS | _SENSITIVE_FIELDS:
            data.pop(field_name, None)
        data = self._redactor(data)
        return json.dumps(data, ensure_ascii=False)

    def _row_to_job(self, row: Iterable[Any]) -> Job:
        values = list(row)
        data = json.loads(values[0])
        for field_name in _SENSITIVE_FIELDS:
            data.pop(field_name, None)
        data = {key: value for key, value in data.items() if key in _JOB_FIELD_NAMES}
        data["log"] = values[1] or data.get("log", "")
        data["user_id"] = values[2] or data.get("user_id", "")
        data["status"] = values[3] or data.get("status", JobStatus.PENDING.value)
        data["provider"] = values[4] or self._provider_classifier(
            str(data.get("model", "") or "")
        )
        data["run_token"] = values[5] or ""
        data["cancel_requested"] = bool(values[6])
        data = self._redactor(data)
        return Job(**data)

    @staticmethod
    def _select_columns() -> str:
        return (
            "data, COALESCE(log,''), COALESCE(user_id,''), status, "
            "COALESCE(provider,''), COALESCE(run_token,''), "
            "COALESCE(cancel_requested,0)"
        )

    def add_job(self, job: Job) -> str:
        job.id = uuid.uuid4().hex[:12]
        job.created_at = time.time()
        job.status = JobStatus.PENDING.value
        job.provider = self._provider_for(job)
        job.run_token = ""
        job.cancel_requested = False
        with self._lock:
            with self._immediate_locked():
                self._conn.execute(
                    "INSERT INTO jobs "
                    "(id,data,status,created_at,user_id,log,provider,run_token,cancel_requested) "
                    "VALUES (?,?,?,?,?,?,?,?,0)",
                    (
                        job.id,
                        self._serialize_data(job),
                        job.status,
                        job.created_at,
                        job.user_id or "",
                        job.log or "",
                        job.provider,
                        "",
                    ),
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO fair_queue_users(provider,user_id,first_seen) "
                    "VALUES (?,?,?)",
                    (job.provider, job.user_id or "", job.created_at),
                )
        return job.id

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def get_all_jobs(self) -> List[Job]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._select_columns()} FROM jobs "
                "ORDER BY created_at DESC,id DESC"
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def get_jobs_by_user(self, user_id: str, limit: int = 200) -> List[Job]:
        safe_limit = max(1, min(int(limit), 5000))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._select_columns()} FROM jobs "
                "WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
                (user_id, safe_limit),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def _current_lifecycle_locked(self, job_id: str):
        return self._conn.execute(
            "SELECT status,COALESCE(run_token,''),COALESCE(cancel_requested,0) "
            "FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()

    def update_running(self, job: Job) -> bool:
        """Write running progress only for the current uncancelled run token."""
        if not job.run_token:
            return False
        with self._lock:
            with self._immediate_locked():
                cur = self._conn.execute(
                    "UPDATE jobs SET data=? WHERE id=? AND status=? "
                    "AND run_token=? AND cancel_requested=0",
                    (
                        self._serialize_data(job),
                        job.id,
                        JobStatus.RUNNING.value,
                        job.run_token,
                    ),
                )
                return cur.rowcount == 1

    def finish_job(self, job: Job, run_token: Optional[str] = None) -> bool:
        """Atomically finalize the run owned by ``run_token``.

        A persistent cancel request wins over any executor-reported outcome.
        """
        token = run_token or job.run_token
        if not token:
            return False
        target = job.status
        if target == JobStatus.RUNNING.value:
            target = JobStatus.COMPLETED.value
        if target not in TERMINAL_STATUSES:
            target = JobStatus.FAILED.value
            job.error = job.error or "invalid terminal state"
        with self._lock:
            with self._immediate_locked():
                row = self._current_lifecycle_locked(job.id)
                if not row or row[0] != JobStatus.RUNNING.value or row[1] != token:
                    return False
                if bool(row[2]):
                    target = JobStatus.CANCELLED.value
                    job.error = "Остановлено пользователем"
                job.status = target
                job.finished_at = job.finished_at or time.time()
                job.cancel_requested = False
                cur = self._conn.execute(
                    "UPDATE jobs SET data=?,status=?,run_token='',cancel_requested=0 "
                    "WHERE id=? AND status=? AND run_token=?",
                    (
                        self._serialize_data(job),
                        target,
                        job.id,
                        JobStatus.RUNNING.value,
                        token,
                    ),
                )
                return cur.rowcount == 1

    def defer_job(self, job_id: str, until_ts: float, run_token: str = "") -> bool:
        """Вернуть выполняющийся джоб в очередь и не выдавать его до until_ts.

        Для исчерпанного окна подписки: слот воркера освобождается сразу, джоб ждёт
        сброса в базе, а не в спящем потоке — иначе четыре ждущих джоба занимают весь
        сервис. Прогресс по чанкам привязан к job_id, поэтому после сброса дописываются
        только упавшие части. CAS по run_token тот же, что у finish_job: чужой claim
        не тронем, отменённый пользователем джоб не воскресим.
        """
        token = run_token or ""
        if not token:
            return False
        with self._lock:
            with self._immediate_locked():
                row = self._current_lifecycle_locked(job_id)
                if not row or row[0] != JobStatus.RUNNING.value or row[1] != token:
                    return False
                if bool(row[2]):
                    return False
                cur = self._conn.execute(
                    "UPDATE jobs SET status=?,run_token='',cancel_requested=0,not_before=? "
                    "WHERE id=? AND status=? AND run_token=?",
                    (JobStatus.PENDING.value, float(until_ts), job_id,
                     JobStatus.RUNNING.value, token),
                )
                return cur.rowcount == 1

    def update_job(self, job: Job) -> bool:
        """Compatibility write routed through the token-aware lifecycle."""
        if job.run_token:
            if job.status == JobStatus.RUNNING.value:
                return self.update_running(job)
            return self.finish_job(job, job.run_token)
        # Non-worker edits may update data only while status is unchanged.
        with self._lock:
            with self._immediate_locked():
                cur = self._conn.execute(
                    "UPDATE jobs SET data=? WHERE id=? AND status=?",
                    (self._serialize_data(job), job.id, job.status),
                )
                return cur.rowcount == 1

    def append_log(
        self, job_id: str, text: str, run_token: Optional[str] = None
    ) -> bool:
        if not text:
            return False
        text = str(self._redactor(str(text)))
        with self._lock:
            with self._immediate_locked():
                if run_token:
                    cur = self._conn.execute(
                        "UPDATE jobs SET log=COALESCE(log,'')||? "
                        "WHERE id=? AND status=? AND run_token=? "
                        "AND cancel_requested=0",
                        (
                            text,
                            job_id,
                            JobStatus.RUNNING.value,
                            run_token,
                        ),
                    )
                else:
                    cur = self._conn.execute(
                        "UPDATE jobs SET log=COALESCE(log,'')||? WHERE id=?",
                        (text, job_id),
                    )
                return cur.rowcount == 1

    def request_cancel(self, job_id: str) -> LifecycleResult:
        with self._lock:
            with self._immediate_locked():
                row = self._conn.execute(
                    f"SELECT {self._select_columns()} FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if not row:
                    return LifecycleResult(False, "not_found", "", "")
                job = self._row_to_job(row)
                previous = job.status
                if previous in TERMINAL_STATUSES:
                    return LifecycleResult(
                        False, "already_terminal", previous, previous
                    )
                if previous == JobStatus.RESETTING.value:
                    return LifecycleResult(False, "resetting", previous, previous)
                if previous == JobStatus.PENDING.value:
                    job.status = JobStatus.CANCELLED.value
                    job.finished_at = time.time()
                    job.error = "Отменено до запуска"
                    cur = self._conn.execute(
                        "UPDATE jobs SET data=?,status=?,cancel_requested=0,"
                        "run_token='' WHERE id=? AND status=?",
                        (
                            self._serialize_data(job),
                            JobStatus.CANCELLED.value,
                            job_id,
                            JobStatus.PENDING.value,
                        ),
                    )
                    return LifecycleResult(
                        cur.rowcount == 1,
                        "cancelled_pending" if cur.rowcount else "raced",
                        previous,
                        JobStatus.CANCELLED.value if cur.rowcount else previous,
                    )
                if previous == JobStatus.RUNNING.value:
                    if job.cancel_requested:
                        return LifecycleResult(
                            False, "already_requested", previous, "cancelling"
                        )
                    cur = self._conn.execute(
                        "UPDATE jobs SET cancel_requested=1 "
                        "WHERE id=? AND status=? AND cancel_requested=0",
                        (job_id, JobStatus.RUNNING.value),
                    )
                    return LifecycleResult(
                        cur.rowcount == 1,
                        "cancel_requested" if cur.rowcount else "raced",
                        previous,
                        "cancelling" if cur.rowcount else previous,
                    )
                return LifecycleResult(False, "invalid_state", previous, previous)

    def cancel_job(self, job_id: str) -> bool:
        return self.request_cancel(job_id).changed

    def is_cancel_requested(
        self, job_id: str, run_token: Optional[str] = None
    ) -> bool:
        with self._lock:
            row = self._current_lifecycle_locked(job_id)
        if not row:
            return True
        status, token, requested = row
        if run_token and token != run_token:
            return True
        return status != JobStatus.RUNNING.value or bool(requested)

    def begin_retry(self, job_id: str, *, full: bool = False) -> Optional[RetryTicket]:
        allowed = set(RETRYABLE_STATUSES)
        if full:
            allowed.add(JobStatus.COMPLETED.value)
        token = uuid.uuid4().hex
        with self._lock:
            with self._immediate_locked():
                row = self._conn.execute(
                    f"SELECT {self._select_columns()} FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if not row:
                    return None
                job = self._row_to_job(row)
                if job.status not in allowed:
                    return None
                previous = job.status
                if full:
                    try:
                        manifest = json.loads(job.reset_manifest or "[]")
                    except Exception:
                        manifest = []
                    if isinstance(manifest, dict):
                        manifest = list(manifest.values())
                    if not isinstance(manifest, list):
                        manifest = []
                    try:
                        recorded_outputs = json.loads(job.output_files or "[]")
                    except Exception:
                        recorded_outputs = []
                    if isinstance(recorded_outputs, dict):
                        recorded_outputs = list(recorded_outputs.values())
                    if not isinstance(recorded_outputs, list):
                        recorded_outputs = []
                    for filename in recorded_outputs:
                        if isinstance(filename, str) and filename not in manifest:
                            manifest.append(filename)
                    job.reset_manifest = json.dumps(manifest, ensure_ascii=False)
                    # Persist references removed under the RESETTING CAS before
                    # any filesystem unlink. A crash can never advertise gone files.
                    job.output_files = "[]"
                    job.progress_percent = 0
                    job.progress_detail = ""
                job.status = JobStatus.RESETTING.value
                job.run_token = token
                cur = self._conn.execute(
                    "UPDATE jobs SET data=?,status=?,run_token=?,cancel_requested=0 "
                    "WHERE id=? AND status=?",
                    (
                        self._serialize_data(job),
                        JobStatus.RESETTING.value,
                        token,
                        job_id,
                        previous,
                    ),
                )
                if cur.rowcount != 1:
                    return None
                job.cancel_requested = False
                return RetryTicket(job, token, previous, full)

    def complete_retry(self, ticket: RetryTicket) -> bool:
        job = ticket.job
        job.status = JobStatus.PENDING.value
        job.started_at = 0.0
        job.finished_at = 0.0
        job.progress_percent = 0
        job.progress_detail = ""
        job.error = ""
        if ticket.full:
            job.output_files = "[]"
            job.reset_manifest = "[]"
        job.run_token = ""
        job.cancel_requested = False
        with self._lock:
            with self._immediate_locked():
                cur = self._conn.execute(
                    "UPDATE jobs SET data=?,status=?,run_token='',cancel_requested=0 "
                    "WHERE id=? AND status=? AND run_token=? "
                    "AND cancel_requested=0",
                    (
                        self._serialize_data(job),
                        JobStatus.PENDING.value,
                        job.id,
                        JobStatus.RESETTING.value,
                        ticket.token,
                    ),
                )
                if cur.rowcount:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO fair_queue_users"
                        "(provider,user_id,first_seen) VALUES (?,?,?)",
                        (job.provider, job.user_id or "", job.created_at),
                    )
                return cur.rowcount == 1

    def fail_retry(self, ticket: RetryTicket, error: str) -> bool:
        job = ticket.job
        job.status = JobStatus.FAILED.value
        job.error = str(error)[:2000]
        job.finished_at = time.time()
        with self._lock:
            with self._immediate_locked():
                cur = self._conn.execute(
                    "UPDATE jobs SET data=?,status=?,run_token='',cancel_requested=0 "
                    "WHERE id=? AND status=? AND run_token=?",
                    (
                        self._serialize_data(job),
                        JobStatus.FAILED.value,
                        job.id,
                        JobStatus.RESETTING.value,
                        ticket.token,
                    ),
                )
                return cur.rowcount == 1

    def retry_job(self, job_id: str) -> bool:
        ticket = self.begin_retry(job_id, full=False)
        if not ticket:
            return False
        self.append_log(job_id, "\n--- Повторный запуск ---\n")
        return self.complete_retry(ticket)

    def delete_terminal(self, job_id: str) -> LifecycleResult:
        with self._lock:
            with self._immediate_locked():
                row = self._conn.execute(
                    "SELECT status FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                if not row:
                    return LifecycleResult(False, "not_found", "", "")
                status = row[0]
                if status not in TERMINAL_STATUSES:
                    return LifecycleResult(False, "not_terminal", status, status)
                cur = self._conn.execute(
                    "DELETE FROM jobs WHERE id=? AND status=?",
                    (job_id, status),
                )
                return LifecycleResult(
                    cur.rowcount == 1,
                    "deleted" if cur.rowcount else "raced",
                    status,
                    "deleted" if cur.rowcount else status,
                )

    def delete_job(self, job_id: str) -> bool:
        return self.delete_terminal(job_id).changed

    def mark_job_parallel(self, job_id: str, parallel: bool = True) -> bool:
        """Removed scheduler hint. Kept only so old imports fail safely."""
        return False

    def _active_users_in_ring_locked(self, provider: str) -> List[str]:
        pending = {
            row[0]
            for row in self._conn.execute(
                "SELECT DISTINCT user_id FROM jobs "
                "WHERE status=? AND provider=? AND COALESCE(not_before,0)<=?",
                (JobStatus.PENDING.value, provider, time.time()),
            ).fetchall()
        }
        if not pending:
            return []
        known = {
            row[0]
            for row in self._conn.execute(
                "SELECT user_id FROM fair_queue_users WHERE provider=?",
                (provider,),
            ).fetchall()
        }
        now = time.time()
        for uid in sorted(pending - known):
            self._conn.execute(
                "INSERT OR IGNORE INTO fair_queue_users"
                "(provider,user_id,first_seen) VALUES (?,?,?)",
                (provider, uid, now),
            )
        ring = [
            row[0]
            for row in self._conn.execute(
                "SELECT user_id FROM fair_queue_users WHERE provider=? "
                "ORDER BY first_seen,user_id",
                (provider,),
            ).fetchall()
        ]
        return [uid for uid in ring if uid in pending]

    def claim_next(self, provider: str) -> Optional[Job]:
        provider = (provider or "").strip().lower()
        if provider not in PROVIDERS:
            return None
        with self._lock:
            with self._immediate_locked():
                active = self._active_users_in_ring_locked(provider)
                if not active:
                    return None
                state = self._conn.execute(
                    "SELECT last_user_id FROM fair_queue_state WHERE provider=?",
                    (provider,),
                ).fetchone()
                last = state[0] if state else ""
                full_ring = [
                    row[0]
                    for row in self._conn.execute(
                        "SELECT user_id FROM fair_queue_users WHERE provider=? "
                        "ORDER BY first_seen,user_id",
                        (provider,),
                    ).fetchall()
                ]
                start = full_ring.index(last) + 1 if last in full_ring else 0
                active_set = set(active)
                chosen = next(
                    (
                        full_ring[(start + offset) % len(full_ring)]
                        for offset in range(len(full_ring))
                        if full_ring[(start + offset) % len(full_ring)] in active_set
                    ),
                    None,
                )
                if chosen is None:
                    return None
                row = self._conn.execute(
                    f"SELECT {self._select_columns()} FROM jobs "
                    "WHERE status=? AND provider=? AND user_id=? "
                    "AND COALESCE(not_before,0)<=? "
                    "ORDER BY created_at,id LIMIT 1",
                    (JobStatus.PENDING.value, provider, chosen, time.time()),
                ).fetchone()
                if not row:
                    return None
                job = self._row_to_job(row)
                token = uuid.uuid4().hex
                job.status = JobStatus.RUNNING.value
                job.started_at = time.time()
                job.finished_at = 0.0
                job.run_token = token
                job.cancel_requested = False
                cur = self._conn.execute(
                    "UPDATE jobs SET data=?,status=?,run_token=?,cancel_requested=0 "
                    "WHERE id=? AND status=?",
                    (
                        self._serialize_data(job),
                        JobStatus.RUNNING.value,
                        token,
                        job.id,
                        JobStatus.PENDING.value,
                    ),
                )
                if cur.rowcount != 1:
                    return None
                self._conn.execute(
                    "INSERT INTO fair_queue_state(provider,last_user_id) VALUES (?,?) "
                    "ON CONFLICT(provider) DO UPDATE SET last_user_id=excluded.last_user_id",
                    (provider, chosen),
                )
                return job

    # Compatibility helpers: scheduling now ignores the legacy parallel bit.
    def get_next_pending_sequential(self, last_user_id: str = "") -> Optional[Job]:
        return self.claim_next("openai")

    def get_pending_parallel_jobs(
        self,
        limit: int,
        last_user_id: str = "",
        running_counts: Optional[dict] = None,
    ) -> List[Job]:
        result: List[Job] = []
        for _ in range(max(0, int(limit))):
            job = self.claim_next("openai")
            if not job:
                break
            result.append(job)
        return result

    def queue_pulse(self) -> List[tuple]:
        """Read-only снимок активных строк для виджета живой очереди.

        Читаются ТОЛЬКО status и user_id: колонку data (JSON) не трогаем,
        иначе индексный скан превращается в row lookup. `resetting` —
        транзитное состояние полного повтора, слот оно занимает, поэтому
        наверху считается ждущим.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COALESCE(user_id,'') FROM jobs "
                "WHERE status IN (?,?,?)",
                (
                    JobStatus.PENDING.value,
                    JobStatus.RESETTING.value,
                    JobStatus.RUNNING.value,
                ),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def pending_providers(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT provider FROM jobs WHERE status=?",
                (JobStatus.PENDING.value,),
            ).fetchall()
        return [row[0] for row in rows if row[0] in PROVIDERS]

    def cancel_jobs_for_user(self, user_id: str) -> Dict[str, List[str]]:
        """Atomically cancel pending jobs and persist cancellation for running."""
        now = time.time()
        with self._lock:
            with self._immediate_locked():
                pending = [
                    row[0]
                    for row in self._conn.execute(
                        "SELECT id FROM jobs WHERE user_id=? AND status=?",
                        (user_id, JobStatus.PENDING.value),
                    ).fetchall()
                ]
                running = [
                    row[0]
                    for row in self._conn.execute(
                        "SELECT id FROM jobs WHERE user_id=? AND status=?",
                        (user_id, JobStatus.RUNNING.value),
                    ).fetchall()
                ]
                resetting = [
                    row[0]
                    for row in self._conn.execute(
                        "SELECT id FROM jobs WHERE user_id=? AND status=?",
                        (user_id, JobStatus.RESETTING.value),
                    ).fetchall()
                ]
                for job_id, expected_status in (
                    [(value, JobStatus.PENDING.value) for value in pending]
                    + [
                        (value, JobStatus.RESETTING.value)
                        for value in resetting
                    ]
                ):
                    row = self._conn.execute(
                        f"SELECT {self._select_columns()} FROM jobs WHERE id=?",
                        (job_id,),
                    ).fetchone()
                    if not row:
                        continue
                    job = self._row_to_job(row)
                    job.status = JobStatus.CANCELLED.value
                    job.finished_at = now
                    job.error = (
                        "Аккаунт отключён во время полного повтора"
                        if expected_status == JobStatus.RESETTING.value
                        else "Аккаунт отключён до запуска"
                    )
                    self._conn.execute(
                        "UPDATE jobs SET data=?,status=?,run_token='',"
                        "cancel_requested=0 WHERE id=? AND status=?",
                        (
                            self._serialize_data(job),
                            JobStatus.CANCELLED.value,
                            job_id,
                            expected_status,
                        ),
                    )
                if running:
                    placeholders = ",".join("?" for _ in running)
                    self._conn.execute(
                        f"UPDATE jobs SET cancel_requested=1 "
                        f"WHERE id IN ({placeholders}) AND status=?",
                        (*running, JobStatus.RUNNING.value),
                    )
        return {
            "pending": pending,
            "resetting": resetting,
            "running": running,
        }

    def reap_stale_running(self) -> int:
        with self._lock:
            with self._immediate_locked():
                rows = self._conn.execute(
                    f"SELECT {self._select_columns()} FROM jobs "
                    "WHERE status IN (?,?,?)",
                    (
                        JobStatus.RUNNING.value,
                        "processing",
                        JobStatus.RESETTING.value,
                    ),
                ).fetchall()
                count = 0
                for row in rows:
                    job = self._row_to_job(row)
                    old_status = job.status
                    resetting = old_status == JobStatus.RESETTING.value
                    job.status = (
                        JobStatus.FAILED.value
                        if resetting
                        else JobStatus.INTERRUPTED.value
                    )
                    job.finished_at = job.finished_at or time.time()
                    job.error = (
                        "Полный повтор прерван рестартом; запустите повтор с нуля ещё раз"
                        if resetting
                        else (
                            job.error
                            or "Сервер перезапущен во время выполнения — задача прервана"
                        )
                    )
                    cur = self._conn.execute(
                        "UPDATE jobs SET data=?,status=?,run_token='',"
                        "cancel_requested=0 WHERE id=? AND status=?",
                        (
                            self._serialize_data(job),
                            job.status,
                            job.id,
                            old_status,
                        ),
                    )
                    count += cur.rowcount
                return count


class JobWorker:
    """Single provider-aware dispatcher with exact global/provider job caps."""

    def __init__(
        self,
        queue: JobQueue,
        generation_executor: Optional[Callable[[Job], None]] = None,
        rewrite_executor: Optional[Callable[[Job], None]] = None,
        max_concurrent_jobs: int = 5,
        provider_caps: Optional[Dict[str, int]] = None,
        execution_guard: Optional[Callable[[Job], bool]] = None,
    ):
        self.queue = queue
        self._gen_exec = generation_executor
        self._rew_exec = rewrite_executor
        self._max_concurrent = max(1, int(max_concurrent_jobs))
        configured = provider_caps or {}
        self._provider_caps = {
            provider: max(
                0, min(self._max_concurrent, int(configured.get(provider, self._max_concurrent)))
            )
            for provider in PROVIDERS
        }
        self._execution_guard = execution_guard
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._running: Dict[Any, Job] = {}
        self._running_lock = threading.Lock()
        self._provider_cursor = -1

    def start(self) -> None:
        self._stop_event.clear()
        self.queue.reap_stale_running()
        self._executor = ThreadPoolExecutor(max_workers=self._max_concurrent)
        self._thread = threading.Thread(target=self._run_dispatcher, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=False)

    def running_counts(self) -> Dict[str, int]:
        with self._running_lock:
            counts = {provider: 0 for provider in PROVIDERS}
            for job in self._running.values():
                counts[job.provider] = counts.get(job.provider, 0) + 1
            counts["total"] = len(self._running)
            return counts

    def _execute_job(self, job: Job) -> None:
        try:
            if self.queue.is_cancel_requested(job.id, job.run_token):
                job.status = JobStatus.CANCELLED.value
                job.error = "Остановлено до выполнения"
            elif self._execution_guard and not self._execution_guard(job):
                self.queue.request_cancel(job.id)
                job.status = JobStatus.CANCELLED.value
                job.error = "Аккаунт отключён"
            elif job.job_type == JobType.GENERATION.value:
                if self._gen_exec:
                    self._gen_exec(job)
                else:
                    job.status = JobStatus.FAILED.value
                    job.error = "Generation executor not configured"
            elif job.job_type == JobType.REWRITE.value:
                if self._rew_exec:
                    self._rew_exec(job)
                else:
                    job.status = JobStatus.FAILED.value
                    job.error = "Rewrite executor not configured"
            else:
                job.status = JobStatus.FAILED.value
                job.error = f"Unknown job type: {job.job_type}"
        except Exception as exc:
            job.status = JobStatus.FAILED.value
            job.error = str(exc)
        if job.status == JobStatus.RUNNING.value:
            job.status = JobStatus.COMPLETED.value
        job.finished_at = time.time()
        self.queue.finish_job(job, job.run_token)

    def _ordered_providers(self) -> List[str]:
        order = list(PROVIDERS)
        self._provider_cursor = (self._provider_cursor + 1) % len(order)
        return order[self._provider_cursor :] + order[: self._provider_cursor]

    def _run_dispatcher(self) -> None:
        while not self._stop_event.is_set():
            with self._running_lock:
                done = [future for future in self._running if future.done()]
                for future in done:
                    self._running.pop(future, None)
                running_total = len(self._running)
                provider_running = {provider: 0 for provider in PROVIDERS}
                for job in self._running.values():
                    provider_running[job.provider] += 1

            free_global = self._max_concurrent - running_total
            made_progress = False
            while free_global > 0 and not self._stop_event.is_set():
                claimed = None
                for provider in self._ordered_providers():
                    if provider_running[provider] >= self._provider_caps[provider]:
                        continue
                    claimed = self.queue.claim_next(provider)
                    if claimed:
                        break
                if not claimed:
                    break
                assert self._executor is not None
                future = self._executor.submit(self._execute_job, claimed)
                with self._running_lock:
                    self._running[future] = claimed
                provider_running[claimed.provider] += 1
                free_global -= 1
                made_progress = True
            self._stop_event.wait(0.1 if made_progress else 0.35)
