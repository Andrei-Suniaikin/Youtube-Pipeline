# -*- coding: utf-8 -*-
"""
Очередь задач (Job Queue) для Rewrite Studio.
Поддерживает генерацию и рерайт с разными настройками.
Персистентная SQLite-очередь — переживает рестарт приложения.
"""

import json
import uuid
import time
import sqlite3
import threading
import queue as std_queue
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from typing import List, Optional, Callable, Any
from enum import Enum


class JobType(str, Enum):
    GENERATION = "generation"
    REWRITE = "rewrite"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"          # Выполнено частично — часть задач не завершена
    INTERRUPTED = "interrupted"  # Сервер умер во время выполнения; можно ретраить


@dataclass
class Job:
    id: str = ""
    job_type: str = "generation"
    status: str = "pending"
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0

    # Общие настройки
    model: str = ""
    api_key: str = ""
    use_subscription: bool = False
    oauth_token: str = ""
    max_threads: int = 1
    output_dir: str = ""

    # Generation-специфичные
    gen_tasks: str = "[]"
    preset_name: str = ""
    gen_languages: str = "[]"
    gen_safe_text: bool = False

    # Rewrite-специфичные
    rewrite_files: str = "[]"
    rewrite_languages: str = "[]"
    rewrite_chunk_size: int = 8000
    rewrite_address_style: str = ""
    rewrite_preserve_paragraphs: bool = False
    oauth_tokens: str = "[]"

    # Прогресс
    progress_percent: int = 0
    progress_detail: str = ""
    log: str = ""

    # Результаты
    output_files: str = "[]"
    error: str = ""

    # Параллельная обработка: True = задача может идти параллельно с другими (если нажато "Добавить параллельно")
    parallel: bool = False

    # Идентификатор пользователя (для изоляции задач в веб-интерфейсе)
    user_id: str = ""


# Поля, хранимые в отдельных колонках (не дублируются в JSON-блобе data)
_COLUMN_FIELDS = {"log", "user_id"}


class JobQueue:
    """SQLite-based очередь задач. Персистентная — переживает рестарт.
    Использует одно постоянное соединение с WAL-режимом для скорости."""

    def __init__(self, db_path: str = "job_queue.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            # Создаём базовую таблицу (без новых колонок — они добавятся миграцией)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    priority INTEGER DEFAULT 0
                )
            """)
            self._conn.commit()
        # Миграция: добавляет user_id, log колонки и индексы
        self._migrate_db()

    def _migrate_db(self):
        """Добавляет новые колонки в существующие таблицы при необходимости."""
        with self._lock:
            for col in ("user_id", "log"):
                try:
                    self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT DEFAULT ''")
                except sqlite3.OperationalError:
                    pass  # Колонка уже существует
            # Мигрируем user_id и log из JSON-блоба в отдельные колонки для старых строк
            try:
                rows = self._conn.execute(
                    "SELECT id, data FROM jobs WHERE user_id = '' OR user_id IS NULL"
                ).fetchall()
                for row_id, data_json in rows:
                    data = json.loads(data_json)
                    uid = data.get("user_id", "")
                    log = data.get("log", "")
                    if uid or log:
                        self._conn.execute(
                            "UPDATE jobs SET user_id = ?, log = ? WHERE id = ?",
                            (uid, log, row_id)
                        )
                if rows:
                    self._conn.commit()
            except Exception:
                pass
            # Создаём индексы (после того как колонки гарантированно существуют)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
            self._conn.commit()

    def _serialize_data(self, job: Job) -> str:
        """Сериализует Job в JSON, исключая поля из отдельных колонок."""
        data = asdict(job)
        for field in _COLUMN_FIELDS:
            data.pop(field, None)
        return json.dumps(data, ensure_ascii=False)

    def _row_to_job(self, row) -> Job:
        """Конвертирует строку БД (data, log, user_id) в объект Job."""
        data = json.loads(row[0])
        # Колонки имеют приоритет, но fallback на JSON для совместимости со старыми данными
        data["log"] = row[1] or data.get("log", "")
        data["user_id"] = row[2] or data.get("user_id", "")
        return Job(**data)

    def add_job(self, job: Job) -> str:
        """Добавить задачу в очередь. Возвращает job.id."""
        job.id = str(uuid.uuid4())[:12]
        job.created_at = time.time()
        job.status = JobStatus.PENDING.value
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, data, status, created_at, user_id, log) VALUES (?, ?, ?, ?, ?, ?)",
                (job.id, self._serialize_data(job), job.status, job.created_at, job.user_id or "", job.log or "")
            )
            self._conn.commit()
        return job.id

    def get_next_pending(self) -> Optional[Job]:
        """Получить следующую pending задачу (FIFO)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT data, log, user_id FROM jobs WHERE status = ? ORDER BY created_at ASC LIMIT 1",
                (JobStatus.PENDING.value,)
            ).fetchone()
        if row:
            return self._row_to_job(row)
        return None

    def update_job(self, job: Job):
        """Обновить задачу (статус, прогресс, результаты). Не трогает log — он в отдельной колонке."""
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET data = ?, status = ? WHERE id = ?",
                (self._serialize_data(job), job.status, job.id)
            )
            self._conn.commit()

    def append_log(self, job_id: str, text: str):
        """Дописать текст в лог задачи (эффективно — обновляет только колонку log)."""
        if not text:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET log = COALESCE(log, '') || ? WHERE id = ?",
                (text, job_id)
            )
            self._conn.commit()

    def get_all_jobs(self) -> List[Job]:
        """Получить все задачи."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data, log, user_id FROM jobs ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def get_jobs_by_user(self, user_id: str, limit: int = 200) -> List[Job]:
        """Получить задачи пользователя (фильтрация на уровне SQL).
        PATCH-2026-06-25-secfix (M1): убран desktop-обход (возвращал ВСЕ задачи всех тенантов)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data, log, user_id FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit)
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data, log, user_id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def delete_job(self, job_id: str):
        with self._lock:
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._conn.commit()

    def cancel_job(self, job_id: str):
        job = self.get_job(job_id)
        if job and job.status == JobStatus.PENDING.value:
            job.status = JobStatus.CANCELLED.value
            self.update_job(job)

    def retry_job(self, job_id: str) -> bool:
        """Сбрасывает задачу в pending для повторного запуска. Возвращает True если сброс выполнен."""
        job = self.get_job(job_id)
        if job and job.status in (
            JobStatus.FAILED.value, JobStatus.PARTIAL.value,
            JobStatus.CANCELLED.value, JobStatus.INTERRUPTED.value,
        ):
            job.status = JobStatus.PENDING.value
            job.error = ""
            job.progress_percent = 0
            job.progress_detail = ""
            self.update_job(job)
            self.append_log(job_id, "\n--- Повторный запуск ---\n")
            return True
        return False

    def mark_job_parallel(self, job_id: str, parallel: bool = True) -> bool:
        """Помечает задачу для параллельной обработки. Возвращает True если успешно."""
        job = self.get_job(job_id)
        if job and job.status == JobStatus.PENDING.value:
            job.parallel = parallel
            self.update_job(job)
            return True
        return False

    def get_next_pending_sequential(self) -> Optional[Job]:
        """Получить следующую pending задачу с parallel=False (FIFO). Атомарно переводит в running."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data, log, user_id FROM jobs WHERE status = ? ORDER BY created_at ASC",
                (JobStatus.PENDING.value,)
            ).fetchall()
            for row in rows:
                job = self._row_to_job(row)
                if not getattr(job, "parallel", False):
                    job.status = JobStatus.RUNNING.value
                    job.started_at = time.time()
                    self._conn.execute(
                        "UPDATE jobs SET data = ?, status = ? WHERE id = ?",
                        (self._serialize_data(job), job.status, job.id)
                    )
                    self._conn.commit()
                    return job
        return None

    def get_pending_parallel_jobs(self, limit: int) -> List[Job]:
        """Получить до limit pending задач с parallel=True (FIFO). Атомарно переводит в running."""
        result = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT data, log, user_id FROM jobs WHERE status = ? ORDER BY created_at ASC",
                (JobStatus.PENDING.value,)
            ).fetchall()
            for row in rows:
                if len(result) >= limit:
                    break
                job = self._row_to_job(row)
                if getattr(job, "parallel", False):
                    job.status = JobStatus.RUNNING.value
                    job.started_at = time.time()
                    self._conn.execute(
                        "UPDATE jobs SET data = ?, status = ? WHERE id = ?",
                        (self._serialize_data(job), job.status, job.id)
                    )
                    result.append(job)
            if result:
                self._conn.commit()
        return result


class JobWorker:
    """
    Фоновый поток, выполняющий задачи из очереди.
    - Последовательные задачи (parallel=False): одна за раз
    - Параллельные задачи (parallel=True): до max_concurrent_jobs одновременно
    """

    def __init__(self, queue: JobQueue,
                 generation_executor: Optional[Callable[[Job], None]] = None,
                 rewrite_executor: Optional[Callable[[Job], None]] = None,
                 max_concurrent_jobs: int = 5):
        self.queue = queue
        self._gen_exec = generation_executor
        self._rew_exec = rewrite_executor
        self._max_concurrent = max(1, max_concurrent_jobs)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._parallel_thread: Optional[threading.Thread] = None
        self._parallel_executor: Optional[ThreadPoolExecutor] = None
        self._current_job: Optional[Job] = None
        self._parallel_running: dict = {}  # future -> job
        self._parallel_lock = threading.Lock()

    def start(self):
        self._stop_event.clear()
        self._parallel_executor = ThreadPoolExecutor(max_workers=self._max_concurrent)
        self._thread = threading.Thread(target=self._run_sequential, daemon=True)
        self._parallel_thread = threading.Thread(target=self._run_parallel_launcher, daemon=True)
        self._thread.start()
        self._parallel_thread.start()

    def stop(self):
        self._stop_event.set()
        if self._parallel_executor:
            self._parallel_executor.shutdown(wait=False)

    def get_current_job(self) -> Optional[Job]:
        return self._current_job

    def get_running_jobs(self) -> List[Job]:
        """Возвращает все выполняющиеся задачи (последовательные + параллельные)."""
        result = []
        if self._current_job:
            result.append(self._current_job)
        with self._parallel_lock:
            for job in self._parallel_running.values():
                if job and job not in result:
                    result.append(job)
        return result

    def _execute_job(self, job: Job):
        """Выполняет одну задачу (generation или rewrite)."""
        try:
            if job.job_type == JobType.GENERATION.value:
                if self._gen_exec:
                    self._gen_exec(job)
                else:
                    job.error = "Generation executor not configured"
                    job.status = JobStatus.FAILED.value
            elif job.job_type == JobType.REWRITE.value:
                if self._rew_exec:
                    self._rew_exec(job)
                else:
                    job.error = "Rewrite executor not configured"
                    job.status = JobStatus.FAILED.value
            else:
                job.error = f"Unknown job type: {job.job_type}"
                job.status = JobStatus.FAILED.value

            if job.status == JobStatus.RUNNING.value:
                job.status = JobStatus.COMPLETED.value
            job.finished_at = time.time()
        except Exception as e:
            job.status = JobStatus.FAILED.value
            job.error = str(e)
            job.finished_at = time.time()
        self.queue.update_job(job)

    def _run_sequential(self):
        """Цикл для последовательных задач: одна за раз."""
        while not self._stop_event.is_set():
            job = self.queue.get_next_pending_sequential()
            if job is None:
                time.sleep(1)
                continue

            self._current_job = job
            self.queue.update_job(job)

            try:
                self._execute_job(job)
            finally:
                self._current_job = None

    def _run_parallel_launcher(self):
        """Запускает параллельные задачи в пул воркеров."""
        while not self._stop_event.is_set():
            # Очистить завершённые
            with self._parallel_lock:
                done = [f for f in self._parallel_running if f.done()]
                for f in done:
                    del self._parallel_running[f]
                free_slots = self._max_concurrent - len(self._parallel_running)

            if free_slots > 0:
                jobs = self.queue.get_pending_parallel_jobs(free_slots)
                for job in jobs:
                    if self._stop_event.is_set():
                        break
                    future = self._parallel_executor.submit(self._execute_job, job)
                    with self._parallel_lock:
                        self._parallel_running[future] = job

            time.sleep(0.5)
