# -*- coding: utf-8 -*-
"""
Rewrite Server — FastAPI-сервер для выполнения очереди задач на VPS.
Запуск: uvicorn server:app --host 0.0.0.0 --port 8899
"""

import os
import json
import re
import time
import io
import zipfile
import tempfile
import uuid
import urllib.parse
from pathlib import Path
from typing import Annotated, Any, List, Optional
from dataclasses import asdict
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Базовые пути (переопределяются через env)
BASE_DIR = Path(os.environ.get("REWRITE_SERVER_DIR", os.path.dirname(os.path.abspath(__file__))))
OUTPUT_DIR = Path(os.environ.get("REWRITE_OUTPUT_DIR", BASE_DIR / "outputs"))
UPLOAD_DIR = Path(os.environ.get("REWRITE_UPLOAD_DIR", BASE_DIR / "uploads"))
PRESETS_DIR = Path(os.environ.get("REWRITE_PRESETS_DIR", BASE_DIR / "presets"))
STATIC_DIR = Path(os.environ.get("REWRITE_STATIC_DIR", BASE_DIR / "static"))
DB_PATH = os.environ.get("REWRITE_DB_PATH", str(BASE_DIR / "job_queue.db"))
# PATCH-2026-06-25-secfix (M1): god-mode 'desktop' по X-API-Key удалён, API_SECRET больше не используется.

# PATCH-2026-08-01-publiclog: в БД (а значит в UI и /api/v1) пишем только
# публичный прогресс; полный технический лог задачи остаётся на сервере файлом.
JOB_LOG_DIR = Path(os.environ.get("REWRITE_JOB_LOG_DIR", BASE_DIR / "job_logs"))
JOB_LOG_MAX_BYTES = int(os.environ.get("REWRITE_JOB_LOG_MAX_BYTES", 20 * 1024 * 1024))
# Аварийный тумблер: 0 → старое поведение (полный лог в БД), без правки кода.
LOG_PUBLIC_ENABLED = os.environ.get(
    "REWRITE_LOG_PUBLIC", "1"
).strip().lower() not in {"0", "false", "no", "off"}

# Создаём директории
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PRESETS_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# Импорт ядра (без tkinter)
from job_queue import Job, JobQueue, JobWorker, JobType, JobStatus
import rewrite_core
from rewrite_core import (
    generate_and_postprocess_single_text,
    rewrite_single_language,
    PresetManager,
    Preset,
    STOP_FLAG,
    SAFE_TEXT_SUPPLEMENT,
    make_output_filename,
    LANGUAGES,
    get_keys_config,
    save_keys_config,
    ADMIN_USER_ID,
    set_current_user_id,
    clear_current_user_id,
    get_server_openai_key,
    get_server_openai_keys,
    get_server_claude_oauth_tokens,
    get_server_anthropic_keys,
    get_server_codex_homes,
    get_effective_openai_key,
    get_effective_openai_keys,
    get_effective_claude_oauth_tokens,
    get_claude_admission_credential,
    get_effective_anthropic_key,
    get_effective_anthropic_keys,
    get_user_openai_keys,
    get_user_claude_oauth_tokens,
    get_user_anthropic_keys,
    get_user_codex_auth,
    server_credential_owner,
    credential_source,
    get_allowed_openai_models,
    get_allowed_claude_models,
    get_allowed_codex_models,
    provider_user_allowed,
    classify_provider,
    preflight_server_credential_pools,
    server_credential_pool_stats,
    set_provider_execution_guard,
    clear_provider_execution_guard,
    redact_server_credentials,
)
from auth import LicenseManager, ADMIN_USER_ID
from log_public import PublicLogFilter
from concurrent.futures import ThreadPoolExecutor, as_completed
import queue as std_queue
import threading

# PresetManager с путём для сервера
PRESETS_FILE = PRESETS_DIR / "generation.json"
PRESET_MANAGER = PresetManager(str(PRESETS_FILE))
REWRITE_SHARED_KEYS_ENABLED = os.environ.get(
    "REWRITE_SHARED_KEYS_ENABLED", ""
).strip().lower() in {"1", "true", "yes", "on"}
REWRITE_STUDIO_PREFER_CODEX = os.environ.get(
    "REWRITE_STUDIO_PREFER_CODEX", "1"
).strip().lower() not in {"0", "false", "no", "off"}
# Phase 1 must happen before JobQueue/LicenseManager schema migrations.
# BYOK-2026-07-30: пустой серверный пул больше НЕ роняет старт — пользователи
# со своими ключами должны работать, даже если серверный ключ мёртв/не задан.
# Отсутствие кредов ловится на admission-этапе конкретной задачи.
if REWRITE_SHARED_KEYS_ENABLED:
    try:
        preflight_server_credential_pools()
    except Exception as _preflight_exc:
        print(
            "[startup] server credential preflight warning: "
            f"{_preflight_exc} (users with their own keys are unaffected)"
        )
queue = JobQueue(
    DB_PATH,
    provider_classifier=classify_provider,
    redactor=redact_server_credentials,
)
license_mgr = LicenseManager()


def _add_job_for_active_user(job: Job) -> str:
    """Close the enqueue/offboarding race with pre/post active-state checks."""
    user_id = job.user_id or ADMIN_USER_ID
    if not license_mgr.is_user_active(user_id):
        raise HTTPException(status_code=403, detail="account is disabled")
    job_id = queue.add_job(job)
    if not license_mgr.is_user_active(user_id):
        queue.cancel_jobs_for_user(user_id)
        raise HTTPException(status_code=403, detail="account is disabled")
    return job_id

# ---------- Job stop events (per-job cancellation) ----------
_job_stop_events: dict = {}
_job_stop_events_lock = threading.Lock()


def _server_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


REWRITE_MAX_THREADS_PER_JOB = _server_int(
    "REWRITE_MAX_THREADS_PER_JOB", 3, 1, 16
)
REWRITE_MAX_TASK_RETRIES = _server_int(
    "REWRITE_MAX_TASK_RETRIES", 3, 1, 6
)
REWRITE_MAX_BATCH_ITEMS = _server_int(
    "REWRITE_MAX_BATCH_ITEMS", 100, 1, 100
)
REWRITE_MIN_CHUNK_SIZE = 500
REWRITE_MAX_CHUNK_SIZE = 20000
REWRITE_MAX_JSON_BODY_BYTES = _server_int(
    "REWRITE_MAX_JSON_BODY_BYTES", 1024 * 1024, 64 * 1024, 4 * 1024 * 1024
)
REWRITE_ZIP_MAX_JOB_IDS = _server_int(
    "REWRITE_ZIP_MAX_JOB_IDS", 25, 1, 100
)
REWRITE_ZIP_MAX_FILES = _server_int(
    "REWRITE_ZIP_MAX_FILES", 250, 1, 2000
)
REWRITE_ZIP_MAX_UNCOMPRESSED_BYTES = _server_int(
    "REWRITE_ZIP_MAX_UNCOMPRESSED_BYTES",
    512 * 1024 * 1024,
    1024 * 1024,
    2 * 1024 * 1024 * 1024,
)
REWRITE_ZIP_MAX_ARCHIVE_BYTES = _server_int(
    "REWRITE_ZIP_MAX_ARCHIVE_BYTES",
    512 * 1024 * 1024,
    1024 * 1024,
    2 * 1024 * 1024 * 1024,
)
REWRITE_ZIP_RAM_SPOOL_BYTES = _server_int(
    "REWRITE_ZIP_RAM_SPOOL_BYTES",
    8 * 1024 * 1024,
    64 * 1024,
    64 * 1024 * 1024,
)

# PATCH-2026-08-02-subs: вкладка «Субтитры». Скачивание синхронное, поэтому все
# лимиты держат ровно две вещи — чистый IP сервера у YouTube и anyio-пул uvicorn.
REWRITE_SUBS_MAX_LINKS = _server_int("REWRITE_SUBS_MAX_LINKS", 30, 1, 50)
REWRITE_SUBS_PARALLEL = _server_int("REWRITE_SUBS_PARALLEL", 3, 1, 6)
REWRITE_SUBS_TIMEOUT_SEC = _server_int("REWRITE_SUBS_TIMEOUT_SEC", 20, 5, 60)
REWRITE_SUBS_MAX_CONCURRENT = _server_int("REWRITE_SUBS_MAX_CONCURRENT", 2, 1, 8)
REWRITE_SUBS_BATCH_TTL_SEC = _server_int("REWRITE_SUBS_BATCH_TTL_SEC", 900, 60, 7200)
REWRITE_SUBS_MAX_BATCH_BYTES = _server_int(
    "REWRITE_SUBS_MAX_BATCH_BYTES",
    32 * 1024 * 1024,
    1024 * 1024,
    128 * 1024 * 1024,
)
# YouTube блокирует адреса облачных провайдеров, поэтому на VPS субтитры идут
# только через прокси: http://user:pass@host:port или socks5://host:port.
# Пусто — ходим напрямую (годится для запуска на домашней машине).
REWRITE_SUBS_PROXY = os.environ.get("REWRITE_SUBS_PROXY", "").strip()

# Готовые тексты живут в памяти процесса, чтобы кнопка «скачать» не дёргала
# YouTube второй раз. Ключ — batch_id, значение привязано к владельцу.
_subs_batches: dict = {}
_subs_batches_lock = threading.Lock()
_subs_slots = threading.BoundedSemaphore(REWRITE_SUBS_MAX_CONCURRENT)
_subs_active_users: set = set()


def _run_task_with_resume(attempts, backoff, log_q, stop_event, user_id, fn, *args, **kwargs):
    """PATCH-2026-06-12-task-auto-resume: авто-ретрай таски при success=False.
    Переинвок fn — resume в rewrite_core подхватывает ТОЛЬКО упавшие чанки из
    PROGRESS_MANAGER (готовые не перегенерируются -> качество и токены не тратятся).
    Файл по-прежнему сохраняется лишь когда ВСЕ чанки валидны (дыр не пропускаем)."""
    attempts = min(max(1, int(attempts)), REWRITE_MAX_TASK_RETRIES)
    backoff = min(max(0, int(backoff)), 60)
    res = (None, False)
    for _att in range(attempts):
        res = _run_with_job_stop(stop_event, user_id, fn, *args, **kwargs)
        try:
            ok = bool(res[1])
        except Exception:
            ok = False
        if ok or STOP_FLAG.is_stopped():
            return res
        if _att < attempts - 1:
            try:
                log_q.put(f"\U0001F501 \u0430\u0432\u0442\u043E-\u0440\u0435\u0442\u0440\u0430\u0439 \u0442\u0430\u0441\u043A\u0438 {_att + 2}/{attempts}: \u0434\u043E\u0437\u0430\u043B\u0438\u0432 \u0443\u043F\u0430\u0432\u0448\u0438\u0445 \u0447\u0430\u043D\u043A\u043E\u0432 (resume)")
            except Exception:
                pass
            time.sleep(backoff)
    return res


def _run_with_job_stop(stop_event, user_id, fn, *args, **kwargs):
    """Обёртка: устанавливает thread-local stop event + user_id ctx для воркера ThreadPoolExecutor.

    user_id ctx гарантирует, что глубокие утилиты в rewrite_core (get_postprocess_openai_key,
    debug_log, и т.п. без явного user_id) читают ключи правильного партнёра."""
    STOP_FLAG.set_thread_stop_event(stop_event)
    set_current_user_id(user_id or ADMIN_USER_ID)
    job_id = getattr(stop_event, "_rewrite_job_id", "")
    run_token = getattr(stop_event, "_rewrite_run_token", "")

    def provider_guard(_provider: str) -> bool:
        if stop_event.is_set():
            return False
        if not license_mgr.is_user_active(user_id or ADMIN_USER_ID):
            stop_event.set()
            if job_id:
                queue.request_cancel(job_id)
            return False
        if job_id and queue.is_cancel_requested(job_id, run_token):
            stop_event.set()
            return False
        return True

    set_provider_execution_guard(provider_guard)
    try:
        return fn(*args, **kwargs)
    finally:
        clear_provider_execution_guard()
        STOP_FLAG.clear_thread_stop_event()
        clear_current_user_id()


def _drain_queue(q):
    """Вычитать все сообщения из очереди без блокировки."""
    lines = []
    try:
        while True:
            lines.append(q.get_nowait())
    except std_queue.Empty:
        pass
    return lines


def _raw_log_path(job: Job) -> Path:
    """Файл полного технического лога задачи. Наружу не отдаётся никогда."""
    safe_uid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(job.user_id or "anon"))[:64] or "anon"
    safe_jid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(job.id))[:64] or "job"
    return JOB_LOG_DIR / safe_uid / f"{safe_jid}.log"


def _write_raw_log(path: Path, lines: List[str]) -> None:
    """Дописать сырые строки в файл задачи. Ошибки записи не роняют задачу."""
    if not lines:
        return
    try:
        if path.exists() and path.stat().st_size >= JOB_LOG_MAX_BYTES:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(redact_server_credentials("\n".join(lines)) + "\n")
    except Exception as exc:  # диск полон / права — теряем отладку, но не задачу
        print(f"[joblog] raw log write failed for {path.name}: {exc}")


def _finalize_job_status(job: Job, total: int, success_count: int):
    """Выставить финальный статус задачи по результатам выполнения."""
    if STOP_FLAG.is_stopped():
        job.status = JobStatus.CANCELLED.value
        job.error = "Остановлено пользователем"
    elif total > 0 and success_count < total:
        job.status = JobStatus.PARTIAL.value
        job.error = f"{total - success_count} из {total} задач не выполнено"


def _get_effective_api_key(job: Job):
    """BYOK-first resolve in memory; jobs never carry vendor secrets.

    BYOK-2026-07-30: сначала СВОЙ ключ юзера (sessions.db.user_keys), при его
    отсутствии — серверный. Значение остаётся только в памяти процесса: в job
    JSON / job-БД / HTTP-ответы / логи оно не попадает.

    Для Claude-моделей: подписка (OAuth) ключа не требует → ""; путь прямого
    Anthropic API (задача A) получает свой Anthropic-ключ тенанта.
    """
    model = (job.model or "").lower()
    if model.startswith("codex:"):
        return ""
    if "claude" in model:
        if job.use_subscription:
            return ""
        return get_effective_anthropic_key(job.user_id or "")
    return get_effective_openai_key(job.user_id or "")


def _server_oauth_tokens(job: Job) -> list:
    """BYOK-first Claude tokens: свои токены юзера, иначе серверный пул.

    Никогда не читается из job JSON — только из настроек тенанта или ENV.
    """
    return get_effective_claude_oauth_tokens(job.user_id or "")


def _resolve_studio_model(model: str, user_id: str) -> str:
    """Recover an unavailable Studio OpenAI selection through Codex.

    Old tabs and presets can still send an allowed OpenAI model even when that
    account has no API key.  Resolve only that impossible request before
    admission/enqueue; explicit Claude, custom/invalid and working API choices
    keep their normal semantics and errors.
    """
    selected = (model or "").strip()
    if not REWRITE_STUDIO_PREFER_CODEX:
        return selected
    if classify_provider(selected) != "openai":
        return selected
    if selected not in get_allowed_openai_models():
        return selected
    if not provider_user_allowed("openai", user_id):
        return selected
    if credential_source("openai", user_id) != "none":
        return selected
    if not provider_user_allowed("codex", user_id):
        return selected
    if credential_source("codex", user_id) == "none":
        return selected

    allowed = sorted(get_allowed_codex_models())
    if not allowed:
        return selected
    preferred = os.environ.get(
        "REWRITE_STUDIO_CODEX_MODEL", "codex:gpt-5.4"
    ).strip()
    return preferred if preferred in allowed else allowed[0]


def _validate_model_admission(model: str, user_id: str) -> tuple:
    """Validate provider/model and return (is_claude, is_codex, oauth_tokens).

    BYOK-2026-07-30: задача допускается, если есть ЛИБО свой ключ юзера, ЛИБО
    серверный. Ошибка 503 честно говорит, что ключа нет ни своего, ни серверного.
    """
    model = (model or "").strip()
    provider = classify_provider(model)
    model_is_codex = provider == "codex"
    model_is_claude = provider == "claude"
    if not provider_user_allowed(provider, user_id):
        raise HTTPException(
            status_code=403, detail="provider is not enabled for this account"
        )
    if model_is_claude:
        if model not in get_allowed_claude_models():
            raise HTTPException(
                status_code=422, detail="Claude model is not allowed by server policy"
            )
        # FIX-2026-07-30 (P1 №2): единый порядок «своё важнее серверного» —
        # свой OAuth → свой Anthropic-ключ → серверный OAuth → серверный
        # Anthropic. Раньше здесь звался get_effective_claude_oauth_tokens,
        # который при отсутствии СВОИХ токенов отдаёт СЕРВЕРНЫЙ пул, и свой
        # Anthropic-ключ тенанта проигрывал подписке владельца.
        tokens = get_claude_admission_credential(user_id)
        if tokens is not None:
            # [] = OAuth не выигрывает → прямой Anthropic API на своём ключе.
            return True, False, tokens
        raise HTTPException(
            status_code=503,
            detail=(
                "Claude credential is not configured: neither your own OAuth "
                "token nor your own Anthropic API key in settings, nor a "
                "server subscription."
            ),
        )
    if model_is_codex:
        if model not in get_allowed_codex_models():
            raise HTTPException(
                status_code=422, detail="Codex model is not allowed by server policy"
            )
        # BYOK-2026-07-30 (задачи A+B): своя подписка Codex тенанта, либо
        # серверная — но серверная только владельцу. Иначе честная 503, а не
        # тихая работа на подписке владельца.
        if credential_source("codex", user_id) == "none":
            raise HTTPException(
                status_code=503,
                detail=(
                    "Codex credential is not configured: paste your own Codex "
                    "auth.json in settings (the server subscription belongs to "
                    "the owner and is not shared)."
                ),
            )
        return False, True, []
    admin_custom = {
        str(v).strip()
        for v in (get_keys_config(ADMIN_USER_ID).get("custom_models") or [])
        if str(v).strip()
    }
    if model not in get_allowed_openai_models() and not (
        user_id == ADMIN_USER_ID and model in admin_custom
    ):
        raise HTTPException(status_code=422, detail="model is not allowed by server policy")
    if not get_effective_openai_keys(user_id):
        raise HTTPException(
            status_code=503,
            detail=(
                "OpenAI key is not configured: neither your own key in settings "
                "nor a server key."
            ),
        )
    return False, False, []


def _execute_job_common(job: Job, job_fn):
    """Общая обёртка выполнения задачи: stop event, лог-дрейн, очистка.
    job_fn(job, log_queue, stop_event) содержит бизнес-логику."""
    # key_source — только источник (user/server/none), никогда не значение.
    print(
        f"[{job.job_type.upper()} JOB] id={job.id} model='{job.model}' "
        f"use_sub={job.use_subscription} "
        f"key_source={credential_source(classify_provider(job.model), job.user_id or '')}"
    )
    STOP_FLAG.reset()
    stop_event = threading.Event()
    stop_event._rewrite_job_id = job.id
    stop_event._rewrite_run_token = job.run_token
    if queue.is_cancel_requested(job.id, job.run_token):
        stop_event.set()
    with _job_stop_events_lock:
        _job_stop_events[job.id] = stop_event
    STOP_FLAG.set_thread_stop_event(stop_event)
    set_current_user_id(job.user_id or ADMIN_USER_ID)

    def provider_guard(provider: str) -> bool:
        if classify_provider(job.model) != provider:
            stop_event.set()
            return False
        if stop_event.is_set() or queue.is_cancel_requested(job.id, job.run_token):
            stop_event.set()
            return False
        if not license_mgr.is_user_active(job.user_id or ADMIN_USER_ID):
            queue.request_cancel(job.id)
            stop_event.set()
            return False
        return True

    set_provider_execution_guard(provider_guard)
    log_queue = std_queue.Queue()
    stop_drain = threading.Event()

    # PATCH-2026-08-01-publiclog: сырой лог → файл на сервере, в БД → прогресс.
    raw_log_path = _raw_log_path(job)
    public_filter = PublicLogFilter() if LOG_PUBLIC_ENABLED else None

    def _flush(lines):
        """Сырое — в файл, публичное — в БД. Пустой list = только heartbeat."""
        if public_filter is None:
            if lines:
                queue.append_log(
                    job.id, "\n".join(lines) + "\n", run_token=job.run_token
                )
            return
        _write_raw_log(raw_log_path, lines)
        public = public_filter.feed(lines)
        if public:
            queue.append_log(
                job.id, "\n".join(public) + "\n", run_token=job.run_token
            )

    def drain_log():
        while not stop_drain.is_set():
            # feed зовём и на пустом дрейне — иначе heartbeat не сработает
            # там, где он и нужен: на длинном молчаливом этапе.
            _flush(_drain_queue(log_queue))
            stop_drain.wait(0.5)

    if public_filter is not None:
        queue.append_log(
            job.id,
            "\n".join(public_filter.start()) + "\n",
            run_token=job.run_token,
        )

    drain_thread = threading.Thread(target=drain_log, daemon=True)
    drain_thread.start()
    try:
        if not provider_guard(classify_provider(job.model)):
            job.status = JobStatus.CANCELLED.value
            job.error = "Аккаунт отключён или остановка запрошена"
            return
        _claude, _codex, runtime_oauth_tokens = _validate_model_admission(
            job.model, job.user_id or ""
        )
        job.provider = classify_provider(job.model)
        # FIX-2026-07-30 (P0 №1): раньше здесь стояло безусловное
        # ``job.use_subscription = job.provider == "claude"`` — рантайм перетирал
        # effective_use_subscription, посчитанный на admission, и ВЕСЬ путь
        # «свой Anthropic API-ключ для claude-модели» уезжал на серверную
        # подписку владельца. Теперь режим подписки включается только когда
        # OAuth-токен реально выиграл (свой, иначе серверный пул) — тот же
        # порядок, что и на admission.
        job.use_subscription = bool(job.provider == "claude" and runtime_oauth_tokens)
        # Per-job inner concurrency is a server policy, never a tenant input.
        job.max_threads = REWRITE_MAX_THREADS_PER_JOB
        job_fn(job, log_queue, stop_event)
    except Exception as e:
        job.error = str(e)
        job.status = JobStatus.FAILED.value
    finally:
        stop_drain.set()
        drain_thread.join(timeout=3)
        _flush(_drain_queue(log_queue))
        if public_filter is not None:
            # Явный финал: иначе после последней части лог просто обрывается.
            final = public_filter.close(job.status)
            if final:
                queue.append_log(
                    job.id, "\n".join(final) + "\n", run_token=job.run_token
                )
        clear_provider_execution_guard()
        STOP_FLAG.clear_thread_stop_event()
        clear_current_user_id()
        with _job_stop_events_lock:
            _job_stop_events.pop(job.id, None)
        queue.update_job(job)


def _gen_filename_base(title: str, topic: str = "", preview: str = "", job_id: str = "") -> str:
    """PATCH-2026-06-28-filename: читаемое имя файла генерации = сгенерённое НАЗВАНИЕ + короткий id.
    Фолбэк title→topic→preview→'script'. Чистит ТОЛЬКО запрещённые в Windows/Mac символы
    (< > : " / \\ | ? * и управляющие), схлопывает пробелы, режет до ~90 симв по границе слова.
    Пробелы и юникод (кириллица/диакритика) СОХРАНЯЮТСЯ — читаемо. Короткий [id] защищает от перезаписи."""
    name = (title or topic or preview or "script").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)   # запрещённые ФС-символы → пробел
    name = re.sub(r"\s+", " ", name).strip(" .")          # схлопнуть пробелы, убрать концевые точки/пробелы
    if len(name) > 90:
        name = name[:90].rsplit(" ", 1)[0].rstrip(" .,;:—-")  # обрезать по последнему слову
    name = name or "script"
    sid = (job_id or "")[:8]
    return f"{name} [{sid}]" if sid else name


def _execute_generation_job(job: Job):
    """Generation v2 worker (Round 2, 2026-05-23): chunk-loop + JSON output + postprocess pipeline.

    Old flow (3-step bait funnel + N×M tasks×languages cross product) removed.
    New flow: single topic × single language → chunked LLM calls returning strict JSON
    {preview, title, script, log_update} → assemble → save script .txt + meta .json.
    2026-06-03: multi-chunk (num_chunks > 1) для subscription provider теперь идёт через
    ClaudeSDKClient в ОДНОМ разговоре (continue-turns) — модель помнит весь предыдущий
    текст сама, не нужен previous_tail; cache reuse на turn 2+ дешевле. Single-shot
    (num_chunks == 1) и не-subscription провайдеры остаются на старом one-shot пути.
    Rotation log (anti-repeat) убран — conversation-mode даёт это бесплатно.
    Postprocess (Pass A/B/C/D) ОТКЛЮЧЁН для generation (Path B v2 design, 2026-05-23).
    """
    def _run(job, log_queue, stop_event):
        from rewrite_core import (
            build_chunk_prompt, parse_chunk_response,
            extract_field_from_content,
            calculate_generation_chunks, call_api_universal,
            call_claude_conversation, call_codex_conversation,
            build_funnel_prompt, parse_funnel_response,  # PATCH-2026-06-27-funnelstep (был не импортирован → NameError)
            _char_length_line,  # PATCH-2026-06-27-chars
        )

        # === Parse v2 payload из gen_tasks ===
        try:
            payload = json.loads(job.gen_tasks or "{}")
        except Exception:
            job.error = "gen_tasks: invalid JSON"
            job.status = JobStatus.FAILED.value
            return
        if not isinstance(payload, dict) or payload.get("v") != 2:
            job.error = "gen_tasks: legacy format не поддерживается в Round 2 (v=2 required)"
            job.status = JobStatus.FAILED.value
            return

        topic = payload.get("topic") or ""
        preset_id = payload.get("preset_id") or job.preset_name
        language_req = payload.get("language")
        target_words_req = payload.get("target_length_words")
        max_log_entries = int(payload.get("max_log_entries") or 5)
        chunk_override = payload.get("chunk_size_words")  # None если не задан
        try:
            chunk_override = int(chunk_override) if chunk_override else None
        except (TypeError, ValueError):
            chunk_override = None

        preset = _preset_manager_for(job.user_id or ADMIN_USER_ID).get_preset(preset_id)  # B1: пресет владельца задачи
        if not preset:
            job.error = f"preset not found: {preset_id}"
            job.status = JobStatus.FAILED.value
            return

        # === Resolve language ===
        language = (language_req or preset.language or extract_field_from_content(preset.content, "language") or "English").strip()
        # === Resolve target_words ===
        target_words = target_words_req or preset.target_length_words or 0
        # === Chunk decision (per-model defaults + опц. override) ===
        model_for_chunking = job.model or ""
        if not target_words:
            num_chunks, words_per_chunk = 1, 0
        else:
            num_chunks, words_per_chunk = calculate_generation_chunks(
                target_words, language, model=model_for_chunking, chunk_size_override=chunk_override,
            )

        # Rotation log убран 2026-06-03 (см. rewrite_core.build_chunk_prompt комментарий).
        recent_phrases: List[str] = []

        # === Auth resolve ===
        oauth_tokens_list = _server_oauth_tokens(job)
        _primary_oauth_token = oauth_tokens_list[0] if oauth_tokens_list else ""
        model = job.model or "claude-sonnet-4-6"
        model_is_codex = model.lower().startswith("codex:")
        model_is_claude = "claude" in model.lower()
        if model_is_codex:
            provider = "codex_subscription"
        elif model_is_claude:
            provider = "subscription" if job.use_subscription else "claude"
        else:
            provider = "openai"
        api_key_effective = _get_effective_api_key(job)

        _chunk_src = f"override={chunk_override}" if chunk_override else "auto"
        log_queue.put(
            f"📐 {preset.name}: {num_chunks} chunk(s) × ~{words_per_chunk or 'auto'} words, "
            f"model={model}, language={language}, chunk_size={_chunk_src}"
        )

        # === Chunk loop ===
        chunks_output: List[str] = []
        preview = title = ""
        all_log_updates: List[str] = []
        previous_tail = ""

        host_name_hint = extract_field_from_content(preset.content, "host_name") or ""
        channel_name_hint = extract_field_from_content(preset.content, "channel_name") or ""

        # === PATCH-2026-06-27-funnelstep: отдельный ВИДИМЫЙ шаг воронки ПЕРЕД генерацией ===
        # Claude проектирует превью/название/хук/план (план = бюджет слов по битам) → текст пишется ПО плану,
        # длина держится планом за один проход (без дорогих recovery-повторов).
        funnel_hook = ""
        funnel_plan: List[str] = []
        _v7_owns_funnel = (os.environ.get("GEN_V7", "0") == "1"
                           and provider == "subscription" and num_chunks > 1)
        if target_words and not _v7_owns_funnel:  # воронка осмысленна только когда задана цель длины (v7 несёт её в чате)
            try:
                log_queue.put("📐 Проектирую воронку (превью → название → хук → план-бюджет)…")
                _fmsgs = build_funnel_prompt(
                    preset_content=preset.content, topic=topic, language=language,
                    target_words=target_words, num_chunks=num_chunks, words_per_chunk=words_per_chunk or 0,
                    host_name=host_name_hint, channel_name=channel_name_hint,
                )
                _fok, _fraw = call_api_universal(
                    _fmsgs, api_key_effective, model, log_queue, "gen-funnel",
                    provider=provider, oauth_token=_primary_oauth_token,
                )
                _fp = parse_funnel_response(_fraw) if _fok else None
                # PATCH-2026-06-27-thinking: если ответ не JSON (thinking/ack «I'll design…» вместо воронки) — ОДИН ретрай.
                if not (_fp and (_fp.get("hook") or _fp.get("plan"))):
                    log_queue.put(f"↻ воронка: ответ не JSON ({(str(_fraw)[:48]).strip()!r}…) — ретрай со строгим хинтом")
                    _fmsgs2 = build_funnel_prompt(
                        preset_content=preset.content, topic=topic, language=language,
                        target_words=target_words, num_chunks=num_chunks, words_per_chunk=words_per_chunk or 0,
                        host_name=host_name_hint, channel_name=channel_name_hint,
                        retry_hint=("REMINDER: output ONLY the JSON object and start with '{' as the very first character. "
                                    "Do NOT write 'I'll', 'Let me', 'Here is', or any sentence/plan before the JSON. "
                                    "Do NOT think out loud. Emit the JSON now."),
                    )
                    _fok2, _fraw2 = call_api_universal(
                        _fmsgs2, api_key_effective, model, log_queue, "gen-funnel-retry",
                        provider=provider, oauth_token=_primary_oauth_token,
                    )
                    _fp2 = parse_funnel_response(_fraw2) if _fok2 else None
                    if _fp2 and (_fp2.get("hook") or _fp2.get("plan")):
                        _fp = _fp2
                if _fp and (_fp.get("hook") or _fp.get("plan")):
                    funnel_hook = _fp["hook"]
                    funnel_plan = _fp["plan"]
                    if _fp.get("preview"):
                        preview = _fp["preview"]
                    if _fp.get("title"):
                        title = _fp["title"]
                    job.progress_detail = f"воронка готова: preview={preview!r} title={title!r}"
                    queue.update_job(job)
                    log_queue.put(
                        f"✅ Воронка: preview={_fp['preview']!r} title={_fp['title']!r} "
                        f"hook={(_fp['hook'][:60])!r} план={len(funnel_plan)} частей"
                    )
                    if funnel_plan and len(funnel_plan) != num_chunks:
                        log_queue.put(
                            f"ℹ️ план {len(funnel_plan)} частей ≠ {num_chunks} чанков — раздам что есть, лишнее по индексу"
                        )
                else:
                    log_queue.put("⚠️ Воронка не распарсилась — генерация в обычном режиме (inline-воронка + recovery)")
            except Exception as _fe:
                log_queue.put(f"⚠️ Шаг воронки упал ({type(_fe).__name__}: {_fe}) — генерация в обычном режиме")

        def _plan_for(_idx):  # план части _idx (пусто если воронки нет / индекс вне диапазона)
            return funnel_plan[_idx] if (funnel_plan and 0 <= _idx < len(funnel_plan)) else ""

        # M3 PATCH-2026-07-02: если воронка уже дала preview/title — chunk-0 НЕ обязан их возвращать.
        # build_chunk_prompt в этом случае не просит preview/title, значит parse_chunk_response(expect_funnel=True)
        # ложно роняет валидный script. Требуем funnel-поля из чанка только когда воронка их НЕ дала.
        _funnel_has_preview_title = bool(preview and title)

        # === Multi-chunk conversation mode (one ClaudeSDKClient, continue-turns) ===
        # Применяется только когда num_chunks > 1 И provider==subscription.
        # Преимущества: модель видит весь предыдущий текст (континуити, нет повторов),
        # cache reuse (system+turn1 закэшируются, на turn 2+ платим только за delta).
        # Single-shot (num_chunks==1) остаётся на старом one-shot пути ниже.
        _multi_done = False
        # PATCH-2026-07-18-v7 (гейт GEN_V7=1): воронка нулевым ходом ТОГО ЖЕ чата,
        # части — сырым текстом между маркерами (без JSON). Fail-open на старый путь.
        if (num_chunks > 1 and provider == "subscription"
                and os.environ.get("GEN_V7", "0") == "1"):
            try:
                from gen_v7 import run_v7
                _ok7, _chunks7, _pv7, _tt7, _err7 = run_v7(
                    preset_content=preset.content, topic=topic, language=language,
                    target_words=target_words, num_chunks=num_chunks,
                    words_per_chunk=words_per_chunk or 0, model=model,
                    oauth_token=_primary_oauth_token, log_queue=log_queue,
                    host_name=host_name_hint, channel_name=channel_name_hint,
                )
                if _ok7 and _chunks7:
                    chunks_output.extend(_chunks7)
                    if _pv7:
                        preview = _pv7
                    if _tt7:
                        title = _tt7
                    _multi_done = True
                else:
                    log_queue.put(f"⚠️ v7: {str(_err7)[:90]} — падаю на обычный conversation-путь")
            except Exception as _v7e:
                log_queue.put(f"⚠️ v7 упал ({type(_v7e).__name__}: {str(_v7e)[:80]}) — обычный путь")
        if not _multi_done and num_chunks > 1 and provider in ("subscription", "codex_subscription"):
            log_queue.put(
                f"💬 Conversation mode ({provider}): {num_chunks} turns в ОДНОМ разговоре "
                f"(модель помнит весь контекст сама)"
            )

            # System prompt — тот же что в build_chunk_prompt
            _system_prompt = (
                "You are a YouTube script writer. Apply the channel preset strictly. "
                "Output ONLY a valid JSON object — no markdown wrapper, no prefix or suffix text, no comments."
            )

            # Turn 0: full preset + topic + chunk position (как build_chunk_prompt для chunk 0)
            _msgs0 = build_chunk_prompt(
                preset_content=preset.content,
                topic=topic,
                chunk_idx=0,
                num_chunks=num_chunks,
                target_words_this_chunk=words_per_chunk or None,
                previous_tail="",
                recent_log_phrases=recent_phrases,
                language=language,
                host_name=host_name_hint or None,
                channel_name=channel_name_hint or None,
                plan_for_chunk=_plan_for(0),   # PATCH-2026-06-27-funnelstep
                hook=funnel_hook,
            )
            _turn0 = _msgs0[-1].get("content", "")
            # PATCH-2026-07-18-v5.8: полная ретеншн-карта в первый ход — чат помнит замысел целиком.
            if funnel_plan and len(funnel_plan) > 1:
                _full_map = "\n\n".join(
                    f"— PART {i + 1} of {len(funnel_plan)} —\n{p}" for i, p in enumerate(funnel_plan)
                )
                _turn0 += (
                    "\n\nFULL RETENTION MAP of the WHOLE video (context only — RIGHT NOW you are writing "
                    "PART 1 and nothing else; later parts will be requested in later turns; loops you OPEN "
                    "now may close in later parts exactly as mapped):\n" + _full_map
                )
            _turn_prompts = [_turn0]

            # Turn 1..N-1: короткий continue (БЕЗ повторного preset/topic — модель помнит сама)
            for _k in range(1, num_chunks):
                _is_last_k = (_k == num_chunks - 1)
                _parts = [f"Continue: write chunk {_k + 1} of {num_chunks}."]
                # PATCH-2026-06-27-funnelstep: если есть внешняя воронка — даём план ИМЕННО этой части (бюджет слов);
                # иначе (inline-воронка) — продолжай по плану, спроектированному в chunk 1.
                _plan_k = _plan_for(_k)
                # PATCH-2026-07-18-v5.7: дайджест покрытого — модель не пересказывает прошлые части.
                if funnel_plan:
                    _covered = []
                    for _pp in funnel_plan[:_k]:
                        _covered += [c.strip() for c in re.findall(r"NEW:\s*([^|)]+)", str(_pp))]
                    if _covered:
                        _parts.append(
                            "ALREADY COVERED in previous parts — do NOT retell, re-explain or summarize any "
                            "of these (one passing clause maximum if continuity demands): "
                            + "; ".join(_covered[:60])
                        )
                if _plan_k:
                    # PATCH-2026-07-18-v5: план части = ретеншн-карта (петли/NEW/ре-хуки), не только бюджет слов.
                    _parts.append(
                        "PLAN FOR THIS PART (retention map from the funnel step) — follow it EXACTLY: each beat to its "
                        "~word budget («Слов»), each beat DELIVERS its NEW element, execute loop ops (OPEN = plant and "
                        "go silent; CLOSE = full payoff at the planned position; keep-open loops stay open past this "
                        "part unless closed here). Re-hooks are structural one-liners (chapter turn / irony-forward / "
                        "trivia spike), not questions:\n" + _plan_k
                    )
                elif funnel_plan:
                    # внешняя воронка есть, но план ЭТОЙ части не пришёл (план короче N чанков) — нейтрально, без ссылки на несуществующий план
                    _parts.append("Continue the SAME arc toward the promised payoff; keep developing at full length with concrete detail.")
                else:
                    _parts.append("Follow the overall PLAN you designed in chunk 1 (keep the promised beats moving toward the payoff).")
                if words_per_chunk:
                    # PATCH-2026-06-28-words: словесный коридор («Слов») + само-перепроверка перед ответом.
                    _parts.append(_char_length_line(words_per_chunk, language))
                # PATCH-2026-07-18-v5: короткое напоминание правил интереса в каждом continue-turn.
                _parts.append(
                    "INTEREST RULES from chunk 1 stay in force: every paragraph adds NEW information — never "
                    "re-state; one physical detail, then cut — no explaining what to feel, no praising your own "
                    "material; no 'remember this / we will return to it'; questions to the viewer are over after "
                    "the first 150 words; sentences 12-20 words with the turn inside, short fragments only at "
                    "peaks; uncertainty gets one sentence, not a seminar."
                )
                if _is_last_k:
                    # PATCH-2026-07-18-v5: FINALE DISCIPLINE + гейт имени (пустое имя → нейтральное аутро, не выдумка).
                    if channel_name_hint or host_name_hint:
                        _outro = "ONE short outro that NAMES the host and the channel"
                    else:
                        _outro = ("ONE short neutral outro in the narrator's voice; do NOT invent, mention or imply "
                                  "any channel name, host name, brand or narrator identity")
                    _parts.append(
                        "FINALE DISCIPLINE: deliver the title payoff exactly where the map places it (inside the "
                        "70-92% window of the WHOLE video). After the payoff: only NEW aftermath material — fates "
                        "of the people, consequences, what was found later — then " + _outro + ", and STOP. "
                        "FORBIDDEN after the payoff: recap, re-explaining the meaning, a second ending, returning "
                        "to earlier scenes, restating the paradox. "
                        "SELF-CHECK before closing: every mandatory dispute-formula, verbatim quote and "
                        "anchor fact from the brief must have been voiced exactly ONCE — if one is missing, "
                        "weave it into the aftermath BEFORE the outro."
                    )
                else:
                    _parts.append("CONTINUE the narrative — do not repeat content from previous chunks.")
                # PATCH-2026-07-18-v5: CTA только если имя канала задано (иначе модель выдумывает бренд).
                if channel_name_hint and _k == num_chunks // 2:
                    _parts.append(
                        "In THIS chunk, naturally insert ONE short call-to-action (like + subscribe) mentioning "
                        "the channel name — a single in-voice sentence, never jarring. (Only here.)"
                    )
                _continue_keys = '{"script": "...", "log_update": [...]}'
                _parts.append(
                    "\nCRITICAL OUTPUT RULES:\n"
                    "- Output ONLY valid JSON matching the schema below.\n"
                    "- Do NOT wrap JSON in markdown fences.\n"
                    "- Do NOT add any text before or after the JSON object.\n"
                    f"\nOutput STRICTLY this JSON shape:\n{_continue_keys}\n\n"
                    "Rules:\n"
                    f"- TTS-ready: no markdown, numbers/dates/currencies/abbreviations as WORDS in {language}.\n"
                    "- Same voice and style as established in chunk 1.\n"
                    "- `script` must be a single string (no nested objects)."
                )
                _turn_prompts.append("\n".join(_parts))

            log_queue.put(
                f"🔬 conv-mode: turn1_len={len(_turn_prompts[0])} chars, "
                f"continue_len={len(_turn_prompts[1]) if len(_turn_prompts) > 1 else 0} chars"
            )

            # FIX-2026-07-08-genprogress: живой прогресс-бар по чанкам (turn'ам).
            # Раньше progress_percent двигался только ПОСЛЕ всей беседы (парс-цикл ниже) → бар прыгал 0→100.
            # Теперь call_claude_conversation дёргает этот cb по завершении каждого чанка → бар шагает.
            # Шкала 0..80 совпадает с парс-циклом ниже (int((_ti+1)/num_chunks*80)) — без отката назад.
            def _gen_progress_cb(_turn_no, _total, _words):
                try:
                    job.progress_percent = min(80, int(_turn_no / max(1, _total) * 80))
                    job.progress_detail = f"чанк {_turn_no}/{_total} готов (~{_words} слов)"
                    queue.update_job(job)
                except Exception:
                    pass

            _conv_common = dict(
                turn_prompts=_turn_prompts,
                system_prompt=_system_prompt,
                model=model,
                log_queue=log_queue,
                target_words_per_turn=(words_per_chunk or 0),
                max_recovery_turns=2,
                progress_cb=_gen_progress_cb,
            )
            if provider == "codex_subscription":
                _conv_success, _raw_responses, _conv_err = call_codex_conversation(
                    task_info=f"gen-codex-conv-{num_chunks}chunks",
                    **_conv_common,
                )
            else:
                _conv_success, _raw_responses, _conv_err = call_claude_conversation(
                    oauth_token=_primary_oauth_token,
                    task_info=f"gen-conv-{num_chunks}chunks",
                    **_conv_common,
                )

            # Парсим каждый turn через тот же parse_chunk_response (дубль-emit от SDK
            # уже учтён через raw_decode → берёт ПЕРВЫЙ JSON, игнорирует повтор)
            _parse_failed_at = None
            for _ti, _raw in enumerate(_raw_responses):
                if STOP_FLAG.is_stopped():
                    log_queue.put("⛔ Остановлено пользователем mid-conversation")
                    job.status = JobStatus.CANCELLED.value
                    return
                _parsed = parse_chunk_response(_raw, expect_funnel=(_ti == 0 and not _funnel_has_preview_title), job_id=job.id, model=model)
                if not _parsed:
                    try:
                        dump_path = Path("/tmp") / f"parse_fail_{job.id[:8]}_conv_turn{_ti + 1}.txt"
                        dump_path.write_text(str(_raw), encoding="utf-8")
                        log_queue.put(f"📝 conv raw dumped: {dump_path} ({len(_raw)} chars)")
                    except Exception:
                        pass
                    _parse_failed_at = _ti + 1
                    break
                if _ti == 0:
                    # PATCH-2026-06-27-funnelstep: не перетирать превью/название из шага воронки.
                    if not preview:
                        preview = _parsed["preview"]
                    if not title:
                        title = _parsed["title"]
                chunks_output.append(_parsed["script"])
                all_log_updates.extend(_parsed["log_update"])
                job.progress_percent = int((_ti + 1) / num_chunks * 80)
                job.progress_detail = f"conv turn {_ti + 1}/{num_chunks} ok ({len(_parsed['script'].split())} слов)"
                queue.update_job(job)
                log_queue.put(
                    f"✅ Conv turn {_ti + 1}/{num_chunks}: {len(_parsed['script'].split())} слов ({len(_parsed['script'])} симв), "
                    f"log_update={len(_parsed['log_update'])}"
                )

            # Решаем что делать с partial — abandon-to-partial стратегия
            _done_count = len(chunks_output)
            if _parse_failed_at is not None:
                _err = f"Conversation: malformed JSON at turn {_parse_failed_at}/{num_chunks}"
                if _done_count > 0:
                    job.error = f"{_err}. Partial: saved {_done_count} chunks. {_conv_err or ''}"
                    job.status = JobStatus.PARTIAL.value
                else:
                    job.error = f"{_err}. {_conv_err or ''}"
                    job.status = JobStatus.FAILED.value
                    return
            elif not _conv_success:
                # conv call вернул False (timeout/error mid-conversation)
                if _done_count > 0:
                    job.error = f"Conversation incomplete: {_done_count}/{num_chunks} turns. {_conv_err}"
                    job.status = JobStatus.PARTIAL.value
                else:
                    job.error = _conv_err or f"Conversation failed before any turn completed"
                    job.status = JobStatus.FAILED.value
                    return

            _multi_done = True  # → старый for-loop сделает break

        for chunk_idx in range(num_chunks):
            if _multi_done:
                break  # conversation mode уже всё сделал — skip single-shot loop
            if STOP_FLAG.is_stopped():
                log_queue.put("⛔ Остановлено пользователем mid-chunk")
                job.status = JobStatus.CANCELLED.value
                return

            messages = build_chunk_prompt(
                preset_content=preset.content,
                topic=topic,
                chunk_idx=chunk_idx,
                num_chunks=num_chunks,
                target_words_this_chunk=words_per_chunk or None,
                previous_tail=previous_tail,
                recent_log_phrases=recent_phrases,
                language=language,
                host_name=host_name_hint or None,
                channel_name=channel_name_hint or None,
                plan_for_chunk=_plan_for(chunk_idx),   # PATCH-2026-06-27-funnelstep
                hook=funnel_hook,
            )

            # 🔬 DIAG (Round 2 debug): фиксируем точные длины prompt'а до LLM-вызова
            _diag_total = sum(len(m.get("content", "")) for m in messages)
            _diag_sys = len(messages[0].get("content", "")) if messages else 0
            _diag_usr = len(messages[-1].get("content", "")) if messages else 0
            _diag_has_tail = "Previous chunk ended" in (messages[-1].get("content", "") if messages else "")
            _diag_has_preset = "Channel preset" in (messages[-1].get("content", "") if messages else "")
            log_queue.put(
                f"🔬 chunk {chunk_idx + 1}/{num_chunks} prompt: total={_diag_total} sys={_diag_sys} "
                f"usr={_diag_usr} has_preset={_diag_has_preset} has_prev_tail={_diag_has_tail}"
            )

            task_info = f"gen chunk {chunk_idx + 1}/{num_chunks}"

            # Retry-loop поверх subscription timeout (claude CLI иногда виснет на больших prompts).
            SUBSCRIPTION_RETRY_MAX = 2
            SUBSCRIPTION_RETRY_BACKOFF_SEC = 30
            success, raw = False, ""
            for _sub_attempt in range(SUBSCRIPTION_RETRY_MAX + 1):
                success, raw = call_api_universal(
                    messages, api_key_effective, model, log_queue, task_info,
                    provider=provider, oauth_token=_primary_oauth_token,
                )
                _is_sub_timeout = (
                    not success
                    and isinstance(raw, str)
                    and raw.startswith("SUBSCRIPTION_TIMEOUT")
                )
                if success or not _is_sub_timeout:
                    break
                if _sub_attempt < SUBSCRIPTION_RETRY_MAX:
                    log_queue.put(
                        f"⏳ chunk {chunk_idx + 1}: subscription timeout — wait "
                        f"{SUBSCRIPTION_RETRY_BACKOFF_SEC}s, retry "
                        f"{_sub_attempt + 2}/{SUBSCRIPTION_RETRY_MAX + 1}"
                    )
                    time.sleep(SUBSCRIPTION_RETRY_BACKOFF_SEC)

            # 🔬 DIAG: фиксируем что вернул LLM
            log_queue.put(
                f"🔬 chunk {chunk_idx + 1} response: success={success} raw_len={len(raw) if raw else 0} "
                f"head={(str(raw)[:120]).replace(chr(10),' ')!r}"
            )
            if not success:
                if isinstance(raw, str) and raw.startswith("SUBSCRIPTION_TIMEOUT"):
                    job.error = (
                        f"Chunk {chunk_idx + 1}: Claude Code CLI stream stalled после "
                        f"{SUBSCRIPTION_RETRY_MAX + 1} attempts. Известный upstream bug — "
                        f"anthropics/claude-code#25979 (regression в claude-code 2.1.42+). "
                        f"Что попробовать: повторить чуть позже, другой OAuth-токен (rotation), "
                        f"Haiku вместо Sonnet/Opus, или downgrade CLI до 2.1.40. "
                        f"Forensics: /tmp/cli_hang_*"
                    )
                else:
                    job.error = f"LLM error on chunk {chunk_idx + 1}: {str(raw)[:300]}"
                job.status = JobStatus.FAILED.value
                return

            parsed = parse_chunk_response(raw, expect_funnel=(chunk_idx == 0 and not _funnel_has_preview_title), job_id=job.id, model=model)
            if not parsed:
                # Dump raw в файл для будущей диагностики (raw обрезается в логе до 200 chars).
                try:
                    dump_path = Path("/tmp") / f"parse_fail_{job.id[:8]}_chunk{chunk_idx + 1}_a.txt"
                    dump_path.write_text(str(raw), encoding="utf-8")
                    log_queue.put(f"📝 raw dumped: {dump_path} ({len(raw)} chars)")
                except Exception:
                    pass

                # Rebuild messages через build_chunk_prompt с retry_hint.
                # Старая логика (append к messages) не работала: claude-code-sdk
                # flattens список — берёт ТОЛЬКО последний user message, теряя preset+topic.
                retry_messages = build_chunk_prompt(
                    preset_content=preset.content,
                    topic=topic,
                    chunk_idx=chunk_idx,
                    num_chunks=num_chunks,
                    target_words_this_chunk=words_per_chunk or None,
                    previous_tail=previous_tail,
                    recent_log_phrases=recent_phrases,
                    language=language,
                    host_name=host_name_hint or None,
                    channel_name=channel_name_hint or None,
                    plan_for_chunk=_plan_for(chunk_idx),   # PATCH-2026-06-27-funnelstep
                    hook=funnel_hook,
                    retry_hint=(
                        "Previous response was not valid JSON or was truncated. "
                        "Output ONLY a single valid JSON object — no markdown wrapper, "
                        "no leading/trailing text, no comments, no duplicated objects. "
                        "Start with { and end with }. Escape all internal quotes inside "
                        "string values with backslash. Keep `script` as ONE string, "
                        "stay within target word count to avoid truncation."
                    ),
                )
                success2, raw2 = call_api_universal(
                    retry_messages, api_key_effective, model, log_queue,
                    f"{task_info}:json_retry",
                    provider=provider, oauth_token=_primary_oauth_token,
                )
                log_queue.put(
                    f"🔬 chunk {chunk_idx + 1} retry response: success={success2} "
                    f"raw_len={len(raw2) if raw2 else 0} "
                    f"head={(str(raw2)[:120]).replace(chr(10),' ')!r}"
                )
                if not success2:
                    job.error = f"LLM error on chunk {chunk_idx + 1} retry: {str(raw2)[:300]}"
                    job.status = JobStatus.FAILED.value
                    return
                parsed = parse_chunk_response(raw2, expect_funnel=(chunk_idx == 0 and not _funnel_has_preview_title), job_id=job.id, model=model)
                if not parsed:
                    try:
                        dump_path = Path("/tmp") / f"parse_fail_{job.id[:8]}_chunk{chunk_idx + 1}_b.txt"
                        dump_path.write_text(str(raw2), encoding="utf-8")
                        log_queue.put(f"📝 retry raw dumped: {dump_path} ({len(raw2)} chars)")
                    except Exception:
                        pass
                    job.error = (
                        f"chunk {chunk_idx + 1}: malformed JSON after 1 retry. "
                        f"Response prefix: {str(raw2)[:300]}"
                    )
                    job.status = JobStatus.FAILED.value
                    return

            # PATCH-2026-06-27-length: single-shot recovery — дожать чанк до нижней границы коридора.
            # PATCH-2026-06-28-words: recovery РАБОТАЕТ ВСЕГДА (раньше был ВЫКЛ при воронке, но план длину НЕ держал → тексты короче).
            # Меряем СЛОВА через .split() (= «Слов», НЕ «буквенных»). floor 0.8 = дожим ТОЛЬКО если короче >20% (как рерайт);
            # до 20% недобора — норма, без лишнего повторного запроса (он дорог + иногда падает пустым на 4-8).
            if words_per_chunk and words_per_chunk > 0:
                _lo = int(words_per_chunk * 0.8); _hi = int(words_per_chunk * 1.15)
                _is_outro_chunk = (chunk_idx == num_chunks - 1)  # последний/единственный чанк держит аутро
                _rs = 0
                while _rs < 2:
                    _w = len((parsed.get("script") or "").split())
                    if _w >= _lo:
                        break
                    _rs += 1
                    _deficit = _lo - _w
                    _tail = (parsed.get("script") or "")[-1500:]
                    if _is_outro_chunk:
                        _close = ("If the text already ended with a sign-off, treat that as PREMATURE — keep developing, and "
                                  "only at the VERY END write the final short sign-off naming the host and the channel.")
                    else:
                        _close = "Do NOT write any closing/outro/sign-off yet."
                    _rec_msgs = [
                        messages[0],
                        {"role": "user", "content": (
                            f"You are continuing a script you already wrote. It is currently {_w} words but MUST reach "
                            f"at least {_lo} words (band {_lo}-{_hi}). Here is how it currently ENDS:\n\n\"\"\"{_tail}\"\"\"\n\n"
                            f"Continue from EXACTLY where it ends, seamlessly. Add about {_deficit} more words of real "
                            f"substance (depth, concrete detail, an extra scene/development). Do NOT repeat earlier text, "
                            f"do NOT restart. {_close} "
                            f"Output ONLY JSON: {{\"script\": \"<the additional continuation text only>\"}}."
                        )},
                    ]
                    _ok_r, _raw_r = call_api_universal(
                        _rec_msgs, api_key_effective, model, log_queue,
                        f"{task_info}:len_recovery{_rs}",
                        provider=provider, oauth_token=_primary_oauth_token,
                    )
                    if not _ok_r:
                        log_queue.put(f"⚠️ chunk {chunk_idx + 1} length recovery {_rs}: call failed, stop")
                        break
                    _pr = parse_chunk_response(_raw_r, expect_funnel=False, job_id=job.id, model=model)
                    if not _pr or not (_pr.get("script") or "").strip():
                        log_queue.put(f"⚠️ chunk {chunk_idx + 1} length recovery {_rs}: parse/empty, stop")
                        break
                    parsed["script"] = (parsed.get("script") or "").rstrip() + "\n\n" + _pr["script"].lstrip()
                    log_queue.put(
                        f"↪ chunk {chunk_idx + 1} length recovery {_rs}: {_w}→{len(parsed['script'].split())} words "
                        f"(band {_lo}-{_hi})"
                    )

            if chunk_idx == 0:
                # PATCH-2026-06-27-funnelstep: не перетирать превью/название из шага воронки.
                if not preview:
                    preview = parsed["preview"]
                if not title:
                    title = parsed["title"]
            chunks_output.append(parsed["script"])
            all_log_updates.extend(parsed["log_update"])
            previous_tail = parsed["script"][-500:] if parsed["script"] else ""

            job.progress_percent = int((chunk_idx + 1) / num_chunks * 80)  # 80% — chunks, 20% — postprocess
            job.progress_detail = f"chunk {chunk_idx + 1}/{num_chunks} ok ({len(parsed['script'].split())} слов)"
            queue.update_job(job)
            log_queue.put(
                f"✅ Chunk {chunk_idx + 1}/{num_chunks}: {len(parsed['script'].split())} слов ({len(parsed['script'])} симв), "
                f"log_update={len(parsed['log_update'])}"
            )

        # === Assemble raw script ===
        raw_script = "\n\n".join(chunks_output).strip()

        # === Postprocess: DISABLED for generation (Path B v2 design, 2026-05-23) ===
        # На сильных main-моделях (Sonnet 4.6 / Opus 4.7) output уже native-quality;
        # слабый postprocess (gpt-4.1-nano) делал synonym swaps + изредка factual errors
        # (см. memory project_overview.md: "Generation Path B v2", live-diff 2026-05-23).
        # Постпроцесс остаётся для rewrite path — там main source = transcript, нужна шлифовка.
        final_script = raw_script
        log_queue.put("📐 Postprocess SKIPPED for generation (Path B v2 design)")

        # FIX-2026-07-10-cleaner: чистка артефактов СБОРКИ (QA bulk-7): JSON-огрызки склейки
        # чанков, англ. мета-эхо recovery-промпта, рециклинг хвостового блока (дубль финала),
        # CTA вне последних 200 слов. Fail-open: сбой чистки не роняет джобу.
        _clean_rep = []  # PATCH-2026-07-21-findings: нужен ниже для qa_findings.ingest_job
        try:
            from gen_output_cleaner import clean_generated_text
            final_script, _clean_rep = clean_generated_text(raw_script)
            for _cl in (_clean_rep or ["чистка сборки: артефактов не найдено"]):
                log_queue.put(f"🧹 {_cl}")
        except Exception as _ce:
            final_script = raw_script
            log_queue.put(f"⚠️ cleaner failed ({type(_ce).__name__}: {str(_ce)[:80]}) — текст без чистки")

        # PATCH-2026-07-20-judge: режим судьи-редактора. edit → судья ЗАМЕНЯЕТ gen_editor
        # (объединённый вызов режет сам); report → судья только пишет отчёт, editor работает.
        _judge_mode = os.environ.get("GEN_JUDGE", "0").strip().lower()
        if _judge_mode not in ("report", "edit"):
            _judge_mode = ""

        # PATCH-2026-07-18: ретеншн-редактор (DELETE-only, автоматизация ручной QA). Гейт GEN_EDITOR=1.
        if os.environ.get("GEN_EDITOR", "0") == "1" and _judge_mode != "edit":
            try:
                from gen_editor import edit_for_retention
                _edited, _erep = edit_for_retention(
                    final_script, language, model, _primary_oauth_token, log_queue,
                )
                # re-clean после удалений (склейки абзацев)
                try:
                    from gen_output_cleaner import clean_generated_text as _cg2
                    _edited, _ = _cg2(_edited)
                except Exception:
                    pass
                final_script = _edited
                for _er in (_erep or ["редактор: без правок"]):
                    log_queue.put(f"✂️ {_er}")
            except Exception as _ee:
                log_queue.put(f"⚠️ editor failed ({type(_ee).__name__}: {str(_ee)[:80]}) — текст без ретеншн-правок")

        # PATCH-2026-07-20-judge: судья-редактор ретеншна (средний ярус, план
        # tasks/todo_retention_judge.md). Один sonnet-вызов: оценка + якоря-цитаты +
        # delete-кластеры; применение/гейты/protected — детерминированный код в gen_judge.
        # Гейт запуска: только подписка, только RUNNING (не судим PARTIAL-обрубки).
        # Fail-open: любой сбой судьи не трогает текст и не роняет джобу.
        _judge_report = None
        if _judge_mode and provider == "subscription" and job.status == JobStatus.RUNNING.value:
            try:
                from gen_judge import run_judge
                job.progress_detail = "retention judge..."
                queue.update_job(job)
                _jtext, _judge_report, _jlog = run_judge(
                    final_script, language, _primary_oauth_token,
                    mode=_judge_mode, log_queue=log_queue, topic=topic,
                )
                for _jl in _jlog:
                    log_queue.put(_jl)
                if _judge_mode == "edit" and _jtext:
                    final_script = _jtext
            except Exception as _je:
                log_queue.put(
                    f"⚠️ judge failed ({type(_je).__name__}: {str(_je)[:80]}) — текст без судьи"
                )

        # === Save outputs ===
        output_dir = str(Path(job.output_dir or OUTPUT_DIR))
        os.makedirs(output_dir, exist_ok=True)
        # PATCH-2026-06-28-filename: читаемое имя = сгенерённое НАЗВАНИЕ [короткий-id] (вместо slug-пресета_тема_хэш).
        base = _gen_filename_base(title, topic, preview, job.id)
        script_path = Path(output_dir) / f"{base}.txt"
        # M5 PATCH-2026-07-02: запись файла — источник правды. Если запись упала / файла нет /
        # он пустой — НЕ помечаем COMPLETED (иначе клиент получит 404 при скачивании).
        _save_error = ""
        try:
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(final_script)
        except Exception as e:
            _save_error = f"Failed to save outputs: {e}"
            log_queue.put(f"⚠️ {_save_error}")

        # PATCH-2026-07-20-judge: отчёт судьи рядом с текстом (НЕ в output_files —
        # клиентский список остаётся чистым). Собственный try: сбой отчёта не мешает джобе.
        if _judge_report:
            try:
                with open(Path(output_dir) / f"{base}.retention.json", "w", encoding="utf-8") as _jf:
                    # allow_nan=False: NaN из ответа судьи не должен дать невалидный JSON
                    json.dump(_judge_report, _jf, ensure_ascii=False, indent=1, allow_nan=False)
            except Exception as _jwe:
                log_queue.put(f"⚠️ retention.json не записан: {_jwe}")

        # PATCH-2026-07-21-findings: контур [F] — все находки QA (судья + cleaner +
        # детерминированные чеки: палевный класс цитат / обрыв / длина) в qa_findings.
        # Смысл: агрегация раз в волну → правки пресетов (ww2: агрегация дала 72→88).
        # Non-fatal: сбой записи не влияет на джобу.
        try:
            from qa_findings import ingest_job as _qa_ingest
            _qa_n = _qa_ingest(job.id, text=final_script, judge_report=_judge_report,
                               cleaner_lines=_clean_rep, target_words=target_words or 0)
            if _qa_n:
                log_queue.put(f"🗃️ qa_findings: записано {_qa_n} находок")
        except Exception as _qe:
            log_queue.put(f"⚠️ qa_findings не записаны: {str(_qe)[:80]}")

        # Rotation log запись убрана 2026-06-03 (см. build_chunk_prompt комментарий).
        # Файлы rotation_logs/ на диске не удаляются (audit), просто не дописываем.

        # === Update preset last_used_at ===
        try:
            preset.last_used_at = time.time()
            _preset_manager_for(job.user_id or ADMIN_USER_ID).save_preset(preset)  # B1
        except Exception:
            pass

        # === Finalize job ===
        # M5 PATCH-2026-07-02: сверяем файл на диске (как rewrite-путь), иначе не COMPLETED.
        _file_ok = False
        try:
            _file_ok = script_path.exists() and script_path.stat().st_size > 0
        except Exception:
            _file_ok = False
        if _file_ok:
            job.output_files = json.dumps([script_path.name], ensure_ascii=False)
            job.progress_percent = 100
            job.progress_detail = f"done: preview='{preview}' title='{title}'"
            if job.status == JobStatus.RUNNING.value:
                job.status = JobStatus.COMPLETED.value
            log_queue.put(
                f"🎬 Done: preview={preview!r} title={title!r} script={len(final_script.split())} слов ({len(final_script)} симв) "
                f"(postprocess=skipped)"
            )
        else:
            # Файл не записался / пустой — нечего скачивать. FAILED (или сохраняем уже
            # выставленный PARTIAL/CANCELLED, если статус не RUNNING).
            job.output_files = json.dumps([], ensure_ascii=False)
            _reason = _save_error or "final script file missing or empty on disk"
            job.error = ((job.error or "") + (" | " if job.error else "") + _reason)
            if job.status == JobStatus.RUNNING.value:
                job.status = JobStatus.FAILED.value
            log_queue.put(f"❌ Не сохранён финальный скрипт: {_reason} — статус {job.status}")

    _execute_job_common(job, _run)


def _execute_rewrite_job(job: Job):
    """Выполняет задачу рерайта."""
    def _run(job, log_queue, stop_event):
        files = json.loads(job.rewrite_files)
        languages = json.loads(job.rewrite_languages)
        oauth_tokens_list = _server_oauth_tokens(job)
        output_dir = str(Path(job.output_dir or OUTPUT_DIR))
        os.makedirs(output_dir, exist_ok=True)
        resolved_files = []
        user_id = getattr(job, "user_id", "")
        # B1 PATCH-2026-07-02-secfix: резолвим СТРОГО внутри директории тенанта (UPLOAD_DIR/user_id).
        # НЕ принимаем произвольный абсолютный путь (был LFI), НЕ падаем в глобальный UPLOAD_DIR root.
        # Containment по образцу /upload и api.py _resolve_file_id.
        if user_id:
            _tenant_root = (UPLOAD_DIR / user_id).resolve()
            for fp in files:
                cand = (UPLOAD_DIR / user_id / os.path.basename(fp)).resolve()
                if str(cand).startswith(str(_tenant_root) + os.sep) and cand.exists():
                    resolved_files.append(str(cand))
                else:
                    log_queue.put(f"⚠️ файл вне тенанта / не найден, пропущен: {os.path.basename(fp)!r}")
        else:
            for fp in files:
                log_queue.put(f"⚠️ user_id пуст — файл пропущен: {os.path.basename(fp)!r}")
        is_safe_text = getattr(job, "gen_safe_text", False)
        tasks = []
        for file_path in resolved_files:
            for lang in languages:
                cp = lang.get("custom_prompt", "")
                if is_safe_text:
                    cp = (cp + "\n\n" + SAFE_TEXT_SUPPLEMENT) if cp else SAFE_TEXT_SUPPLEMENT
                tasks.append((
                    file_path,
                    lang.get("display", ""),
                    lang.get("code", lang.get("display", "")),
                    lang.get("channel", ""),
                    lang.get("host", ""),
                    cp,
                ))
        total = len(tasks)
        completed = 0
        success_count = 0
        api_key_effective = _get_effective_api_key(job)
        with ThreadPoolExecutor(
            max_workers=min(max(1, int(job.max_threads)), REWRITE_MAX_THREADS_PER_JOB)
        ) as executor:
            futures = {}
            for task_idx, (file_path, lang_display, lang_code, channel, host, custom_prompt) in enumerate(tasks):
                if STOP_FLAG.is_stopped():
                    break
                start_idx = (task_idx % len(oauth_tokens_list)) if oauth_tokens_list else 0
                oauth_token = oauth_tokens_list[start_idx] if oauth_tokens_list else ""
                future = executor.submit(
                    _run_task_with_resume, 3, 20, log_queue,
                    stop_event, job.user_id or ADMIN_USER_ID,
                    rewrite_single_language,
                    file_path, lang_display, lang_code,
                    job.rewrite_chunk_size, job.model,
                    api_key_effective,
                    output_dir, log_queue,
                    channel, host, job.rewrite_address_style or "", custom_prompt,
                    job.use_subscription, oauth_token,
                    oauth_tokens_list, start_idx,
                    bool(getattr(job, "rewrite_preserve_paragraphs", False)),
                    job.id,
                )
                futures[future] = (file_path, lang_display, lang_code)
            for future in as_completed(futures):
                if STOP_FLAG.is_stopped():
                    break
                try:
                    _, success = future.result()
                    completed += 1
                    if success:
                        success_count += 1
                    fp, ld, lc = futures.get(future, ("", "", ""))
                    job.progress_percent = int(completed / total * 100)
                    job.progress_detail = f"{completed}/{total} — {os.path.basename(fp)} ({ld})"
                    queue.update_job(job)
                except Exception as e:
                    log_queue.put(f"❌ Ошибка: {e}")
                    completed += 1
        _finalize_job_status(job, total, success_count)
        output_files = []
        for file_path in resolved_files:
            base_name = os.path.splitext(os.path.basename(file_path))[0][:50]
            for lang in languages:
                lc = lang.get("code", lang.get("display", ""))
                fname = make_output_filename(base_name, lc)
                if (Path(output_dir) / fname).exists():
                    output_files.append(fname)
        job.output_files = json.dumps(output_files, ensure_ascii=False)

        # Защита от ложного COMPLETED (backlog #1):
        # _finalize_job_status решает по success_count из ThreadPoolExecutor,
        # а реальная правда — что сохранилось на диске в output_files.
        # Возможен gap: rewrite_single_language вернул True, но файл не дошёл
        # до диска (race / exception при финальной записи). В этом случае
        # success_count == total, _finalize_job_status не понизил статус,
        # worker поставит COMPLETED — клиент через API получит 404 при скачивании.
        if not STOP_FLAG.is_stopped() and total > 0:
            saved = len(output_files)
            if saved == 0:
                # Упёрлись в окно подписки — это не провал, а «приходите позже»:
                # джоб уезжает обратно в очередь до времени сброса, слот воркера
                # освобождается сразу. finish_job после нашего возврата не сработает:
                # его CAS ищет строку в состоянии running с тем же run_token, а она
                # уже pending. Прогресс по чанкам сохранён — дописываются только
                # упавшие части.
                from rewrite_core import pop_limit_hit
                until = pop_limit_hit(job.id)
                if until and queue.defer_job(job.id, until, job.run_token):
                    when = time.strftime("%H:%M", time.localtime(until))
                    log_queue.put(f"⏳ окно подписки исчерпано — джоб отложен до {when}")
                    return
                # FAILED override любого предыдущего статуса (включая PARTIAL от
                # _finalize_job_status): 0 файлов на диске = клиенту нечего качать,
                # это hard fail независимо от того что насчитал success_count.
                job.status = JobStatus.FAILED.value
                job.error = (job.error or "") + f" | no output files saved (0/{total})"
            elif saved < total and job.status == JobStatus.RUNNING.value:
                # success_count == total, но на диске меньше — корень бага #1.
                # Понижаем только если статус ещё RUNNING (не перетираем CANCELLED/PARTIAL).
                job.status = JobStatus.PARTIAL.value
                job.error = (job.error or "") + f" | saved {saved}/{total} files on disk"
            # saved == total — статус не трогаем (worker поставит COMPLETED штатно)

    _execute_job_common(job, _run)


_pool_stats = server_credential_pool_stats()
_global_job_cap = _server_int(
    "REWRITE_MAX_CONCURRENT_JOBS",
    _server_int("MAX_CONCURRENT_JOBS", 15, 1, 64),
    1,
    64,
)
_provider_job_caps = {
    "openai": _server_int(
        "REWRITE_OPENAI_MAX_CONCURRENT_JOBS",
        max(1, _pool_stats["openai"]["capacity"]),
        0,
        _global_job_cap,
    ),
    "claude": _server_int(
        # BYOK-2026-07-30 (находка ревью): без max(1,…) пустой СЕРВЕРНЫЙ пул
        # claude давал cap=0 → claude-задачи висели pending вечно, даже когда у
        # юзеров есть свои OAuth-токены. Кап — не место для проверки кредов:
        # их наличие проверяет admission (_validate_model_admission).
        "REWRITE_CLAUDE_MAX_CONCURRENT_JOBS",
        max(1, _pool_stats["claude"]["capacity"]),
        0,
        _global_job_cap,
    ),
    "codex": _server_int(
        "REWRITE_CODEX_MAX_CONCURRENT_JOBS",
        max(1, _pool_stats["codex"]["capacity"]),
        0,
        _global_job_cap,
    ),
}


def _job_execution_allowed(job: Job) -> bool:
    return (
        license_mgr.is_user_active(job.user_id or ADMIN_USER_ID)
        and not queue.is_cancel_requested(job.id, job.run_token)
    )


worker = JobWorker(
    queue,
    generation_executor=_execute_generation_job,
    rewrite_executor=_execute_rewrite_job,
    max_concurrent_jobs=_global_job_cap,
    provider_caps=_provider_job_caps,
    execution_guard=_job_execution_allowed,
)

FILE_MAX_AGE_DAYS = int(os.environ.get("REWRITE_FILE_MAX_AGE_DAYS", "7"))
_cleanup_stop = threading.Event()


def _cleanup_old_files():
    """Фоновый поток: удаляет файлы и задачи старше FILE_MAX_AGE_DAYS."""
    while not _cleanup_stop.wait(3600):
        cutoff = time.time() - FILE_MAX_AGE_DAYS * 86400
        for root_dir in (OUTPUT_DIR, UPLOAD_DIR):
            if not root_dir.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(str(root_dir), topdown=False):
                for fname in filenames:
                    fpath = os.path.join(dirpath, fname)
                    try:
                        if os.path.getmtime(fpath) < cutoff:
                            os.remove(fpath)
                    except OSError:
                        pass
                try:
                    if dirpath != str(root_dir) and not os.listdir(dirpath):
                        os.rmdir(dirpath)
                except OSError:
                    pass
        try:
            for job in queue.get_all_jobs():
                if job.finished_at and job.finished_at < cutoff and job.status in ("completed", "failed", "cancelled", "partial"):
                    queue.delete_job(job.id)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Migration 001: api_keys table + zombie cleanup (idempotent, safe on every restart)
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE_DIR / "migrations"))
        from migrations.api_v1_001 import migrate_001_api_v1  # type: ignore
        created, zombies, msg = migrate_001_api_v1(
            sessions_db_path=os.environ.get("SESSION_DB", str(BASE_DIR / "sessions.db")),
            jobs_db_path=DB_PATH,
            # только здесь, в lifespan: на старте процесса рабочих потоков ещё нет,
            # поэтому вернуть прерванные джобы в очередь безопасно
            requeue_running=True,
        )
        print(f"[startup] {msg}")
    except Exception as _e:
        print(f"[startup] migration 001 failed: {_e}")
    # Migration user_keys_001: per-user keys storage (2026-05-24) — idempotent
    try:
        from migrations.user_keys_001 import migrate_user_keys  # type: ignore
        _t, _ins, _msg_uk = migrate_user_keys(
            sessions_db_path=os.environ.get("SESSION_DB", str(BASE_DIR / "sessions.db")),
        )
        print(f"[startup] {_msg_uk}")
    except Exception as _e:
        print(f"[startup] migration user_keys_001 failed: {_e}")
    # Phase 2: scrub only after import-time pool preflight succeeded. Any
    # flag-on failure aborts startup; running with a partial migration is unsafe.
    if REWRITE_SHARED_KEYS_ENABLED:
        from migrations.shared_keys_fair_queue_001 import (  # type: ignore
            migrate_shared_keys_fair_queue,
        )
        _sk, _sj, _shared_msg = migrate_shared_keys_fair_queue(
            sessions_db_path=os.environ.get(
                "SESSION_DB", str(BASE_DIR / "sessions.db")
            ),
            jobs_db_path=DB_PATH,
            enabled=True,
        )
        print(f"[startup] {_shared_msg}")
    else:
        print("[startup] shared-key migration disabled; no credential scrub")
    recovered_accounts = license_mgr.recover_disabling_accounts(DB_PATH)
    if recovered_accounts:
        print(
            f"[startup] recovered {recovered_accounts} interrupted "
            "account offboarding operation(s)"
        )
    # Migration presets_v2 (Round 1 of Generation refactor, 2026-05-23) — idempotent
    try:
        from migrations.presets_v2_001 import migrate_presets_v2  # type: ignore
        _pres = migrate_presets_v2(str(PRESETS_FILE))
        print(f"[startup] {_pres['message']}")
        if _pres.get("backup"):
            print(f"[startup]   backup: {_pres['backup']}")
    except Exception as _e:
        print(f"[startup] migration presets_v2 failed: {_e}")
    # Migration B1 (2026-06-25-secfix): пресеты per-tenant. Глобальный generation.json
    # переносим в presets/<ADMIN_USER_ID>/ (все существующие пресеты принадлежат админу). Идемпотентно.
    try:
        _admin_pf = PRESETS_DIR / ADMIN_USER_ID / "generation.json"
        if PRESETS_FILE.exists() and not _admin_pf.exists():
            _admin_pf.parent.mkdir(parents=True, exist_ok=True)
            import shutil as _shutil
            _shutil.move(str(PRESETS_FILE), str(_admin_pf))
            print(f"[startup] B1 presets migrated: global -> {_admin_pf}")
        elif PRESETS_FILE.exists() and _admin_pf.exists():
            print("[startup] B1 presets: и глобальный, и admin-файл существуют — глобальный оставлен (нужен ручной merge)")
    except Exception as _e:
        print(f"[startup] B1 presets migration failed: {_e}")
    # Configure api-auth (sessions_db path for /api/v1/* dependency)
    try:
        import api_auth as _api_auth
        _api_auth.configure(
            os.environ.get("SESSION_DB", str(BASE_DIR / "sessions.db")),
            license_mgr=license_mgr,   # для session-fallback в /api/v1/* (UI)
        )
    except Exception as _e:
        print(f"[startup] api_auth configure failed: {_e}")
    worker.start()
    cleanup_thread = threading.Thread(target=_cleanup_old_files, daemon=True)
    cleanup_thread.start()
    yield
    _cleanup_stop.set()
    worker.stop()


app = FastAPI(
    title="Rewrite Studio API",
    description=(
        "Programmatic access to multi-language rewrite jobs.\n\n"
        "- Session-based UI lives at `/`.\n"
        "- API v1 lives under `/api/v1/*` and uses api-key auth (Bearer or X-API-Key).\n"
        "- See README_API.md and /docs for details."
    ),
    version="1.0.0",
    openapi_tags=[
        {"name": "API v1", "description": "Programmatic REST API (api-key auth)"},
    ],
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ============== AUTH MIDDLEWARE ==============

@app.middleware("http")
async def json_body_limit_middleware(request: Request, call_next):
    """Bound JSON payload cost even when Content-Length is absent."""
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("application/json"):
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                if int(raw_length) > REWRITE_MAX_JSON_BODY_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"error": "JSON request body is too large"},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"error": "invalid Content-Length"},
                )
        # Находка ревью: `await request.body()` буферизует тело ЦЕЛИКОМ и только
        # потом сверяет длину — при отсутствии/лживом Content-Length это давало
        # неограниченный расход памяти. Читаем инкрементально и обрываем сразу
        # за лимитом; прочитанное кладём в request._body, поэтому downstream
        # (`await request.body()` / pydantic) видит тело как раньше.
        chunks: list = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > REWRITE_MAX_JSON_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"error": "JSON request body is too large"},
                )
            chunks.append(chunk)
        request._body = b"".join(chunks)
    return await call_next(request)


# ДОБАВЛЕНО 04.09.2026: единый вход. Модуль есть только когда сервис поднят внутри
# единой платформы; при раздельном запуске вход остаётся свой.
try:
    from platform_sso import cabinet_user
except ImportError:                       # pragma: no cover
    def cabinet_user(_request):
        return None

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    public_paths = ("/", "/index.html", "/auth/status", "/auth/setup", "/auth/login-password", "/docs", "/openapi.json", "/redoc", "/static")
    if any(path == p or path.startswith(p + "/") for p in public_paths):
        return await call_next(request)

    # /api/v1/* — собственный api-key auth через Depends(get_api_user) внутри router'а.
    # Middleware пропускает; внутри router'а возврат 401 если ключ невалиден.
    if path.startswith("/api/v1/") or path == "/api/v1":
        return await call_next(request)

    # ДОБАВЛЕНО 04.09.2026: единый вход. Кука кабинета проверяется САМИМ кабинетом —
    # это не доверенный заголовок. Свой вход ниже остаётся рабочим: он нужен при
    # раздельном запуске и как запасной путь, пока переезд не закончен.
    _who = cabinet_user(request)
    if _who:
        request.state.user_id = _who["user_id"]
        request.state.user_name = _who["user_name"]
        request.state.auth_mode = "cabinet"
        return await call_next(request)

    # Авторизация веб-интерфейса: только X-Session-Id (лицензия).
    # PATCH-2026-06-25-secfix (M1): god-mode 'desktop' по X-API-Key удалён —
    # он давал доступ ко ВСЕМ тенантам по одному статичному секрету.
    session_id = request.headers.get("X-Session-Id", "") or request.query_params.get("sid", "")

    if session_id:
        session_info = license_mgr.validate(session_id)
        if not session_info:
            return JSONResponse(status_code=401, content={"error": "Сессия истекла. Войдите заново."})
        request.state.user_id = session_info["user_id"]
        request.state.user_name = session_info["user_name"]
        request.state.auth_mode = "session"
    else:
        return JSONResponse(status_code=401, content={"error": "Требуется авторизация"})

    return await call_next(request)


# ============== AUTH ENDPOINTS ==============

@app.get("/auth/status")
def auth_status():
    """FIX-2026-07-02: нужна ли первичная настройка (в системе ещё нет ни одного аккаунта)."""
    return {"needs_setup": not license_mgr.has_users()}


@app.post("/auth/setup")
async def auth_setup(request: Request):
    """FIX-2026-07-02: одноразовое создание ПЕРВОГО (админ) аккаунта. Работает только пока нет юзеров.
    Админ наследует все текущие данные (user_id = ADMIN_USER_ID). Пароль НЕ логируется."""
    body = await request.json()
    ip = request.client.host if request.client else "unknown"
    success, session_id_or_error, user_name = license_mgr.setup_admin(body.get("username", ""), body.get("password", ""), ip)
    if success:
        return {"session_id": session_id_or_error, "user_name": user_name, "user_id": license_mgr.get_user_id(session_id_or_error) or ""}
    return JSONResponse(status_code=400, content={"error": session_id_or_error})


@app.post("/auth/ping")
async def auth_ping(request: Request):
    """«Я ещё в системе?» — теперь на это есть два способа ответить «да».

    Первый способ ставит middleware по сессии кабинета (единый вход), второй — своя
    сессия. Второй остаётся рабочим: он нужен при раздельном запуске.
    """
    if getattr(request.state, "auth_mode", "") == "cabinet":
        return {"status": "ok", "via": "cabinet",
                "user_id": getattr(request.state, "user_id", "")}
    session_id = request.headers.get("X-Session-Id", "")
    if session_id and license_mgr.ping(session_id):
        # user_id отдаём, чтобы фронт мог восстановить gate при resume-сессии без relogin.
        return {"status": "ok", "user_id": license_mgr.get_user_id(session_id) or ""}
    return JSONResponse(status_code=401, content={"error": "Сессия истекла"})


@app.post("/auth/logout")
async def auth_logout(request: Request):
    session_id = request.headers.get("X-Session-Id", "")
    if session_id:
        license_mgr.logout(session_id)
    return {"status": "ok"}


def _require_admin(request: Request):
    """Гейт: только админ-тенант (user_id == ADMIN_USER_ID) управляет аккаунтами."""
    if getattr(request.state, "user_id", None) != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="Только для администратора")


@app.get("/auth/accounts")
def auth_list_accounts(request: Request):
    """FIX-2026-07-02: список аккаунтов (только админ)."""
    _require_admin(request)
    return {"accounts": license_mgr.list_accounts()}


@app.post("/auth/accounts")
async def auth_create_account(request: Request):
    """FIX-2026-07-02: админ создаёт аккаунт (чистый профиль, свой user_id). Пароль НЕ логируется."""
    _require_admin(request)
    body = await request.json()
    success, err = license_mgr.create_account(body.get("username", ""), body.get("password", ""), body.get("user_name", ""))
    if success:
        return {"status": "ok"}
    return JSONResponse(status_code=400, content={"error": err})


@app.delete("/auth/accounts/{username}")
def auth_delete_account(username: str, request: Request):
    """Disable account and revoke all sessions/API keys (data is retained)."""
    _require_admin(request)
    result = license_mgr.disable_account(username, DB_PATH)
    if result:
        with _job_stop_events_lock:
            for job_id in result["running_jobs"]:
                event = _job_stop_events.get(job_id)
                if event:
                    event.set()
        return {
            "status": "disabled",
            "cancelled_pending": len(result["pending_jobs"]),
            "cancelled_resetting": len(result["resetting_jobs"]),
            "signalled_running": len(result["running_jobs"]),
        }
    return JSONResponse(
        status_code=400,
        content={"error": "Нельзя отключить (не найден, уже отключён или последний админ)"},
    )


@app.post("/auth/login-password")
async def auth_login_password(request: Request):
    """FIX-2026-07-02: вход по логину/паролю (дуал-режим с /auth/login по ключу). Пароль НЕ логируется."""
    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")
    ip = request.client.host if request.client else "unknown"
    success, session_id_or_error, user_name = license_mgr.login_password(username, password, ip)
    if success:
        user_id = license_mgr.get_user_id(session_id_or_error) or ""
        return {"session_id": session_id_or_error, "user_name": user_name, "user_id": user_id}
    return JSONResponse(status_code=403, content={"error": session_id_or_error})


# ============== MODELS ==============

class GenerationBatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    topic: str = Field(..., min_length=10, max_length=20000)
    language: Optional[str] = Field(None, max_length=64)


class GenerationLegacyTask(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    title: str = Field(..., min_length=10, max_length=20000)


class GenerationLegacyLanguage(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    name: str = Field("", max_length=64)
    code: str = Field("", max_length=64)
    display: str = Field("", max_length=64)


class GenerationJobRequest(BaseModel):
    """Round 2 (2026-05-23): single-topic flow + backward compat для старых клиентов.

    Новый flow требует preset_id + topic (+ optional language/model/target_length_words).
    Старый flow с {tasks, languages, preset_name, ...} принимается, если len(tasks)==1;
    при len(tasks) > 1 → 422 (multi-task removed).
    """
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    preset_id: Optional[str] = Field(None, max_length=128)
    topic: Optional[str] = Field(None, max_length=20000)
    language: Optional[str] = Field(None, max_length=64)
    target_length_words: Optional[int] = Field(None, ge=300, le=50000)
    max_log_entries: int = Field(5, ge=0, le=20)
    chunk_size_words: Optional[int] = Field(
        None,
        description="Max слов на один LLM-вызов (override). Если не задано — auto по модели.",
    )
    topics: Optional[List[str]] = Field(
        None,
        max_length=REWRITE_MAX_BATCH_ITEMS,
        description="Batch (2026-06-20): список тем в один POST → N джобов. Если задан — topic игнорируется.",
    )
    items: Optional[List[GenerationBatchItem]] = Field(
        None,
        max_length=REWRITE_MAX_BATCH_ITEMS,
        description="Batch с языком на строку (2026-06-20): [{topic, language?}, ...]. Приоритетнее topics/topic; "
                    "пустой language в строке → берётся общий language/пресет.",
    )

    preset_name: Optional[str] = Field(None, max_length=128)
    tasks: Optional[List[GenerationLegacyTask]] = Field(
        None, max_length=REWRITE_MAX_BATCH_ITEMS
    )
    languages: Optional[List[GenerationLegacyLanguage]] = Field(
        None, max_length=REWRITE_MAX_BATCH_ITEMS
    )
    max_threads: Optional[int] = None  # accepted, clamped, scheduling ignores it
    model: str = Field("gpt-4.1-nano", min_length=1, max_length=128)
    use_subscription: bool = False  # deprecated; server derives provider mode
    parallel: bool = False
    safe_text: bool = False

    @field_validator("topics")
    @classmethod
    def _bounded_topics(cls, values):
        if values is None:
            return values
        bounded = []
        for value in values:
            text = str(value or "").strip()
            if len(text) < 10 or len(text) > 20000:
                raise ValueError("each topic must contain 10..20000 characters")
            bounded.append(text)
        return bounded

    @field_validator("chunk_size_words", mode="before")
    @classmethod
    def _clamp_generation_chunk(cls, value):
        if value in (None, ""):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError("chunk_size_words must be an integer")
        return max(REWRITE_MIN_CHUNK_SIZE, min(REWRITE_MAX_CHUNK_SIZE, parsed))

    @field_validator("max_threads", mode="before")
    @classmethod
    def _clamp_generation_threads(cls, value):
        if value in (None, ""):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError("max_threads must be an integer")
        return max(1, min(REWRITE_MAX_THREADS_PER_JOB, parsed))


class RewriteLanguageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    code: str = Field(..., min_length=1, max_length=64)
    display: str = Field("", max_length=64)
    host: str = Field("", max_length=200)
    channel: str = Field("", max_length=200)
    custom_prompt: str = Field("", max_length=8000)


class RewriteJobRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    files: List[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        ..., min_length=1, max_length=REWRITE_MAX_BATCH_ITEMS
    )
    languages: List[RewriteLanguageInput] = Field(
        ..., min_length=1, max_length=REWRITE_MAX_BATCH_ITEMS
    )
    model: str = Field("gpt-4.1-nano", min_length=1, max_length=128)
    max_threads: int = 1
    chunk_size: int = 8000
    address_style: str = Field("", max_length=64)
    host_name: str = Field("", max_length=200)
    channel_name: str = Field("", max_length=200)
    use_subscription: bool = False  # deprecated; server derives provider mode
    parallel: bool = False
    safe_text: bool = False
    preserve_paragraphs: bool = True

    @field_validator("languages", mode="before")
    @classmethod
    def _coerce_languages(cls, v):
        from api import resolve_lang_input
        if not isinstance(v, list) or not v:
            raise ValueError("languages must be a non-empty list")
        out = []
        for item in v:
            if isinstance(item, str):
                raw = item
                d = {"code": raw, "display": raw}
            elif isinstance(item, dict):
                raw = item.get("code") or item.get("display") or ""
                d = dict(item)
            else:
                raise ValueError(f"invalid language entry: {item!r}")
            canon, candidates = resolve_lang_input(raw)
            if canon:
                d["code"] = canon
                if not d.get("display"):
                    d["display"] = canon
            elif candidates:
                raise ValueError(
                    f"ambiguous short code '{raw}': matches {candidates}. Use canonical name to disambiguate."
                )
            else:
                raise ValueError(
                    f"unsupported language: '{raw}'. Use canonical name (e.g. 'English'), "
                    f"Russian display ('Английский'), or short code (e.g. 'en')."
                )
            out.append(d)
        return out

    @field_validator("max_threads", mode="before")
    @classmethod
    def _clamp_threads(cls, value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError("max_threads must be an integer")
        return max(1, min(REWRITE_MAX_THREADS_PER_JOB, parsed))

    @field_validator("chunk_size", mode="before")
    @classmethod
    def _clamp_chunk_size(cls, value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError("chunk_size must be an integer")
        return max(REWRITE_MIN_CHUNK_SIZE, min(REWRITE_MAX_CHUNK_SIZE, parsed))


# ============== HELPER ==============

def _get_user_id(request: Request) -> str:
    # PATCH-2026-06-25-secfix: fail-closed. Аутентифицированный запрос ВСЕГДА имеет
    # request.state.user_id (middleware иначе вернул бы 401). Отсутствие = баг → 401.
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return uid


def _get_user_output_dir(request: Request) -> Path:
    user_id = _get_user_id(request)
    user_out = OUTPUT_DIR / user_id
    user_out.mkdir(parents=True, exist_ok=True)
    return user_out


# M10 FIX-2026-07-02: кэш PresetManager по user_id (см. _preset_manager_for).
_PRESET_MANAGERS: dict = {}
_PRESET_MANAGERS_LOCK = threading.Lock()


def _preset_manager_for(user_id: str) -> PresetManager:
    """Per-tenant PresetManager (B1 PATCH-2026-06-25-secfix): пресеты скоупятся по user_id
    (как outputs/keys). Раньше был один глобальный стор — любой тенант читал/перезаписывал/стирал чужие.

    M10 FIX-2026-07-02: кэшируем ОДИН инстанс на user_id. Раньше новый PresetManager на
    КАЖДЫЙ вызов → per-instance self._lock не сериализовал конкурентные save/load одного
    тенанта (lost-update пресетов после каждой генерации + гонка на общем generation.json.tmp
    → битый JSON → load() сбрасывал пресеты в {}). Один инстанс → self._lock реально защищает.
    Все обращения к пресетам тенанта идут через эту функцию (worker + CRUD), поэтому
    in-memory состояние консистентно в пределах процесса."""
    key = user_id or ADMIN_USER_ID
    d = PRESETS_DIR / key
    d.mkdir(parents=True, exist_ok=True)
    with _PRESET_MANAGERS_LOCK:
        pm = _PRESET_MANAGERS.get(key)
        if pm is None:
            pm = PresetManager(str(d / "generation.json"))
            _PRESET_MANAGERS[key] = pm
        return pm


def _job_belongs_to_user(job: Job, user_id: str) -> bool:
    # PATCH-2026-06-25-secfix (M1): убран обход 'desktop'. Только владелец задачи.
    return getattr(job, "user_id", "") == user_id


def _job_display_meta(j: Job) -> dict:
    """Читаемое имя задачи + бейджи из JSON-блобов джобы. Без изменения схемы БД.
    PATCH-2026-06-20-joblist-names. Генерация -> topic/preset; рерайт -> имя исходного файла."""
    name = ""
    langs = ""
    nfiles = 0
    try:
        if j.job_type == JobType.GENERATION.value:
            payload = json.loads(j.gen_tasks or "{}")
            if isinstance(payload, list):  # legacy v1: список задач
                payload = payload[0] if payload else {}
            topic = (payload.get("topic") or "").strip() if isinstance(payload, dict) else ""
            first_line = topic.split("\n", 1)[0].strip()  # только заголовок, без "ИДЕЯ:/ХУКИ:" простыни
            name = first_line[:80] or (j.preset_name or "Генерация")
            lang = payload.get("language") if isinstance(payload, dict) else None
            langs = lang or ""
            nfiles = 1
        else:
            files = json.loads(j.rewrite_files or "[]")
            if files:
                base = os.path.basename(str(files[0]))
                name = base + (f" +{len(files) - 1}" if len(files) > 1 else "")
            else:
                name = "Рерайт"
            langs_list = json.loads(j.rewrite_languages or "[]")
            langs = ", ".join(
                (l.get("code") or l.get("display") or "")
                for l in langs_list if isinstance(l, dict)
            )
            nfiles = len(files)
    except Exception:
        name = name or (j.preset_name or j.job_type or "задача")
    return {"name": name, "langs": langs, "nfiles": nfiles}


# ============== FRONTEND ==============

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Rewrite Studio</h1><p>index.html не найден в static/</p>")


# ============== DATA ENDPOINTS ==============

@app.get("/languages")
def get_languages():
    return [{"display": display, "code": code} for display, code in LANGUAGES.items()]


@app.get("/models")
def get_models(request: Request):
    uid = _get_user_id(request)
    labels = {
        "gpt-4.1-nano": "GPT-4.1 Nano",
        "gpt-4.1-mini": "GPT-4.1 Mini",
        "gpt-4o-mini": "GPT-4o Mini",
    }
    base = []
    # Не рекламируем API-модели, которые admission всё равно отклонит с 503.
    # Это также не даёт старому браузеру снова выбрать первый unusable GPT.
    if credential_source("openai", uid) != "none" and provider_user_allowed(
        "openai", uid
    ):
        base.extend(
            {"value": model, "label": labels.get(model, model)}
            for model in sorted(get_allowed_openai_models())
        )
    shared = []
    # BYOK-2026-07-30: Claude-модели видны тем, у кого есть ЛЮБОЙ рабочий
    # credential — свой OAuth-токен, серверный пул или свой Anthropic-ключ.
    if credential_source("claude", uid) != "none" and provider_user_allowed(
        "claude", uid
    ):
        shared.extend(
            {"value": model, "label": model}
            for model in sorted(get_allowed_claude_models())
        )
    # Codex-модели показываем только когда подписка реально доступна: серверная
    # принадлежит владельцу (задача B), остальным нужна своя.
    if provider_user_allowed("codex", uid) and credential_source("codex", uid) != "none":
        shared.extend(
            {"value": model, "label": model.replace("codex:", "") + " — Codex"}
            for model in sorted(get_allowed_codex_models())
        )
    base = shared + base
    if uid == ADMIN_USER_ID:
        for model in get_keys_config(uid).get("custom_models", []):
            model = str(model).strip()
            if model and not any(item["value"] == model for item in base):
                base.append({"value": model, "label": model})
    return base


REWRITE_MAX_OWN_CREDENTIAL_CHARS = 4096
REWRITE_MAX_OWN_OAUTH_TOKENS = 8


def _own_credential_field(value, field: str) -> str:
    """Validate a tenant-supplied credential string (BYOK-2026-07-30).

    Возвращает нормализованное значение; "" = удалить свой ключ. Само значение
    нигде не логируется — в ошибке только имя поля.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"{field} must be a string")
    secret = value.strip()
    if len(secret) > REWRITE_MAX_OWN_CREDENTIAL_CHARS:
        raise HTTPException(status_code=422, detail=f"{field} is too long")
    if any(ch in secret for ch in ("\n", "\r", "\x00")):
        raise HTTPException(
            status_code=422, detail=f"{field} must not contain line breaks"
        )
    return secret


REWRITE_MAX_OWN_CODEX_AUTH_CHARS = 32 * 1024


def _own_codex_auth_field(value) -> str:
    """Validate the tenant's own Codex ``auth.json`` payload (BYOK-2026-07-30).

    В отличие от однострочных ключей это JSON-документ, поэтому переводы строк
    разрешены, но содержимое обязано быть валидным JSON-объектом. "" = удалить
    свою подписку. Само значение никогда не логируется и не возвращается.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="codex_auth must be a string")
    payload = value.strip()
    if not payload:
        return ""
    if len(payload) > REWRITE_MAX_OWN_CODEX_AUTH_CHARS:
        raise HTTPException(status_code=422, detail="codex_auth is too long")
    if "\x00" in payload:
        raise HTTPException(status_code=422, detail="codex_auth is malformed")
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="codex_auth must be the JSON content of your Codex auth.json",
        )
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=422, detail="codex_auth must be a JSON object"
        )
    return payload


def _own_oauth_tokens_field(value) -> list:
    """Validate tenant-supplied Claude OAuth tokens (list or scalar)."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    if len(items) > REWRITE_MAX_OWN_OAUTH_TOKENS:
        raise HTTPException(status_code=422, detail="too many oauth_tokens")
    return [
        token
        for token in (
            _own_credential_field(item, "oauth_tokens") for item in items
        )
        if token
    ]


@app.get("/settings/keys")
def get_settings_keys(request: Request):
    uid = _get_user_id(request)
    cfg = get_keys_config(uid)
    is_admin = (uid == ADMIN_USER_ID)
    # M7 PATCH-2026-07-02-secfix: серверный anthropic_api_key (ENV/дефолт из get_keys_config)
    # НЕ отдаём не-админу — иначе любой тенант вытянет общий Anthropic-ключ. Только админ.
    # BYOK-2026-07-30: наружу уходят ТОЛЬКО признаки «задан» — ни своё, ни
    # серверное значение ключа в браузер не отдаётся никогда.
    own_openai = bool(get_user_openai_keys(uid))
    own_claude = bool(get_user_claude_oauth_tokens(uid))
    # BYOK-2026-07-30 (задача C): признаки «задан» для ВСЕХ видов credential —
    # OpenAI-ключ, Anthropic-ключ, Claude OAuth-токен, своя подписка Codex.
    # Серверные значения не показываются никому, свои — не возвращаются наружу.
    is_server_owner = server_credential_owner(uid)
    resp = {
        "anthropic_api_key": "",
        "openai_configured": bool(get_server_openai_keys()),
        "claude_configured": bool(get_server_claude_oauth_tokens()),
        # Серверная подписка владельца доступна только владельцу (задача B) —
        # остальным сообщаем «нет», чтобы UI не обещал невозможного.
        "anthropic_configured": bool(
            is_server_owner and get_server_anthropic_keys()
        ),
        "codex_configured": is_server_owner,
        "server_credential_owner": is_server_owner,
        # признак «свой ключ задан» + какой источник реально сработает
        "own_openai_key_set": own_openai,
        "own_claude_token_set": own_claude,
        "own_anthropic_key_set": bool(get_user_anthropic_keys(uid)),
        "own_codex_auth_set": bool(get_user_codex_auth(uid)),
        "openai_key_source": credential_source("openai", uid),
        "claude_key_source": credential_source("claude", uid),
        "anthropic_key_source": credential_source("anthropic", uid),
        "codex_key_source": credential_source("codex", uid),
        "provider_pool_sizes": {
            provider: stats["credentials"]
            for provider, stats in server_credential_pool_stats().items()
        },
        # FIX-2026-07-02: UI-пресеты настроек теперь серверные (per-user, не секрет) — отдаём всем.
        "rew_presets": cfg.get("rew_presets", {}),
        "gen_settings_presets": cfg.get("gen_settings_presets", {}),
        "is_admin": is_admin,
    }
    if is_admin:
        resp.update({
            "gpt_service_tier": cfg.get("gpt_service_tier", "default"),
            "gpt_use_cache": cfg.get("gpt_use_cache", True),
            "postprocess_check_model": cfg.get("postprocess_check_model", "gpt-5-mini"),
            "custom_models": cfg.get("custom_models", []),
            "debug_mode": cfg.get("debug_mode", False),
        })
    return resp


@app.post("/settings/keys")
async def save_settings_keys(request: Request):
    body = await request.json()
    uid = _get_user_id(request)
    kwargs = {"user_id": uid}
    # BYOK-2026-07-30: свой ключ может вписать ЛЮБОЙ пользователь (не только
    # админ). Пустая строка = «удалить свой ключ» → снова заработает серверный.
    # Значение никогда не возвращается наружу и не пишется в job/лог.
    if "openai_api_key" in body:
        kwargs["openai_api_key"] = _own_credential_field(
            body.get("openai_api_key"), "openai_api_key"
        )
    if "anthropic_api_key" in body:
        kwargs["anthropic_api_key"] = _own_credential_field(
            body.get("anthropic_api_key"), "anthropic_api_key"
        )
    if "oauth_tokens" in body:
        kwargs["oauth_tokens"] = _own_oauth_tokens_field(body.get("oauth_tokens"))
    if "oauth_token" in body:
        # legacy-скаляр из старого UI/клиента
        kwargs["oauth_tokens"] = _own_oauth_tokens_field(body.get("oauth_token"))
    if "codex_auth" in body:
        # BYOK-2026-07-30 (задача A): своя подписка Codex — доступна каждому
        # авторизованному юзеру, значение хранится только на сервере.
        kwargs["codex_auth"] = _own_codex_auth_field(body.get("codex_auth"))
    # Admin-only поля: non-admin silently drop, даже если пришли в body.
    if uid == ADMIN_USER_ID:
        for k in ("gpt_service_tier", "gpt_use_cache", "postprocess_check_model",
                  "custom_models", "debug_mode"):
            if k in body:
                kwargs[k] = body[k]
    # FIX-2026-07-02: UI-пресеты настроек (per-user, не admin-only) — сохраняем если пришли dict'ом.
    for k in ("rew_presets", "gen_settings_presets"):
        if isinstance(body.get(k), dict):
            kwargs[k] = body[k]
    # FIX-2026-07-30 (P3 №6): не отвечаем "ok", если запись не прошла (например
    # у user_keys нет колонки codex_auth и ALTER не удался) — иначе юзер уверен,
    # что свой credential сохранён, а его нет.
    if not save_keys_config(**kwargs):
        raise HTTPException(
            status_code=500,
            detail="settings were not saved: keys storage is unavailable",
        )
    return {"status": "ok"}


@app.post("/settings/presets")
async def save_settings_presets(request: Request):
    """FIX-2026-07-02: отдельное сохранение UI-пресетов настроек БЕЗ затрагивания ключей.
    Раньше пресеты жили только в localStorage браузера → терялись между устройствами.
    Ключи не передаются (None → partial-update в save_keys_config их не трогает)."""
    body = await request.json()
    uid = _get_user_id(request)
    kwargs = {"user_id": uid}
    for k in ("rew_presets", "gen_settings_presets"):
        if isinstance(body.get(k), dict):
            kwargs[k] = body[k]
    if len(kwargs) > 1:  # что-то кроме user_id пришло
        save_keys_config(**kwargs)
    return {"status": "ok"}


# Preset v2 helpers (Round 1 of Generation refactor, 2026-05-23). Старые URL по {name}
# поддерживаются как backward compat (PRESET_MANAGER.get_preset резолвит id+name).


def _preset_summary(p: "Preset") -> dict:
    """Short summary без content (для list endpoint)."""
    return {
        "id": p.id,
        "name": p.name,
        "target_length_words": p.target_length_words,
        "language": p.language,  # language task
        "created_at": p.created_at,
        "last_used_at": p.last_used_at,
    }


@app.get("/presets")
def list_presets_v2(request: Request):
    """List CURRENT TENANT's presets (B1: per-user стор)."""
    pm = _preset_manager_for(_get_user_id(request))
    return {p.name: _preset_summary(p) for p in pm.list_presets()}


@app.post("/presets/sync")
def sync_presets(presets: dict, request: Request):
    """Полная замена пресетов ТЕКУЩЕГО тенанта (B1: пишет только в его файл, не глобально).
    Принимает или {<name>: ...}, или {presets: {...}}."""
    user_id = _get_user_id(request)
    data = {"presets": presets} if "presets" not in presets else presets
    user_file = PRESETS_DIR / user_id / "generation.json"
    user_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(user_file) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(user_file))  # атомарно
    _preset_manager_for(user_id).load()
    return {"status": "ok"}


@app.get("/presets/{identifier}")
def get_preset_endpoint(identifier: str, request: Request):
    """Получить preset ТЕКУЩЕГО тенанта по id (canonical) или name (backward compat)."""
    pm = _preset_manager_for(_get_user_id(request))
    p = pm.get_preset(identifier)
    if not p:
        raise HTTPException(status_code=404, detail="preset not found")
    return p.to_dict()


@app.post("/presets/{identifier}")
async def create_or_update_preset(identifier: str, request: Request):
    """Create / update preset по id или name.

    Body может содержать:
      - новая schema: {name, target_length_words?, content}
      - старая schema (BACKWARD COMPAT): {prompt_template, custom_instructions, total_words, content_type, ...}
        → автоматически склеиваем в content (как в migration v2).
    """
    body = await request.json()
    pm = _preset_manager_for(_get_user_id(request))  # B1: per-tenant стор
    name = str(body.get("name") or identifier).strip() or identifier

    # Преобразование старого формата в новый (та же логика что в миграции)
    if "content" in body:
        content = str(body.get("content") or "")
    else:
        parts = []
        ct = str(body.get("content_type") or "").strip()
        if ct:
            parts.append(f"Type: {ct}")
        pt = str(body.get("prompt_template") or "").strip()
        if pt:
            parts.append(pt)
        ci = str(body.get("custom_instructions") or "").strip()
        if ci:
            parts.append(ci)
        content = "\n\n".join(parts) if parts else "(empty)"

    twl = body.get("target_length_words") or body.get("total_words")
    try:
        twl = int(twl) if twl is not None else None
    except Exception:
        twl = None

    # Resolve id: если такой preset уже есть (по id или name) — переиспользуем его id
    existing = pm.get_preset(identifier)
    if existing:
        pid = existing.id
        created_at = existing.created_at or time.time()
    else:
        # Generate new id via slugify (та же ф-я что в migration)
        try:
            from migrations.presets_v2_001 import _slugify
            pid = _slugify(name)
            # collision check
            existing_ids = {p.id for p in pm.list_presets()}
            base = pid
            sfx = 1
            while pid in existing_ids:
                sfx += 1
                pid = f"{base}-{sfx}"
        except Exception:
            import secrets as _secrets
            pid = _secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        created_at = time.time()

    # language task: из тела (future UI) → иначе сохраняем существующий (чтобы UI-edit не сбрасывал дефолт)
    lang_default = (str(body.get("language")).strip() or None) if body.get("language") else (existing.language if existing else None)

    p = Preset(
        id=pid,
        name=name,
        target_length_words=twl,
        language=lang_default,
        content=content,
        created_at=created_at,
        last_used_at=existing.last_used_at if existing else None,
    )
    pm.save_preset(p)
    return p.to_dict()


@app.delete("/presets/{identifier}")
def delete_preset_endpoint(identifier: str, request: Request):
    """Удалить preset ТЕКУЩЕГО тенанта по id (или name) (B1)."""
    pm = _preset_manager_for(_get_user_id(request))
    if not pm.delete_preset(identifier):
        raise HTTPException(status_code=404, detail="preset not found")
    return {"status": "deleted"}


# ============== JOB ENDPOINTS ==============

@app.post("/jobs/generation")
def create_generation_job(req: GenerationJobRequest, request: Request):
    """Round 2 generation handler (2026-05-23): single-topic flow + backward compat."""
    user_id = _get_user_id(request)
    user_output = _get_user_output_dir(request)

    # === Resolve preset (id новый, name legacy) ===
    preset_identifier = req.preset_id or req.preset_name or ""
    if not preset_identifier:
        raise HTTPException(status_code=422, detail="preset_id (или preset_name для legacy) required")
    preset_obj = _preset_manager_for(user_id).get_preset(preset_identifier)  # B1: пресет тенанта
    if not preset_obj:
        raise HTTPException(status_code=404, detail=f"preset not found: {preset_identifier}")

    # === Resolve темы (+опц. язык на строку): items[] | topics[] | single topic | legacy tasks ===
    # PATCH-2026-06-20-gen-batch: один POST -> N джобов; язык можно задать на каждую тему (items).
    pairs = []  # [(topic, per_item_language_or_empty), ...]
    if req.items:
        for it in req.items:
            t = it.topic.strip()
            lng = (it.language or "").strip()
            if t:
                pairs.append((t, lng))
    elif req.topics:
        pairs = [(t.strip(), "") for t in req.topics if t and t.strip()]
    else:
        topic = (req.topic or "").strip()
        if not topic and req.tasks:
            if len(req.tasks) > 1:
                raise HTTPException(
                    status_code=422,
                    detail="multi-task generation removed in Round 2. Send one topic per call.",
                )
            first = req.tasks[0]
            topic = first.title.strip()
        pairs = [(topic, "")] if topic else []
    pairs = [(t, lng) for (t, lng) in pairs if len(t) >= 10]
    if not pairs:
        raise HTTPException(status_code=422, detail="topic required (min 10 chars)")
    if len(pairs) > REWRITE_MAX_BATCH_ITEMS:
        raise HTTPException(
            status_code=413,
            detail=f"batch is too large (max {REWRITE_MAX_BATCH_ITEMS} jobs)",
        )

    # === Resolve language (legacy languages[0] fallback) ===
    language = (req.language or "").strip()
    if not language and req.languages:
        first_lang = req.languages[0]
        language = str(
            first_lang.name or first_lang.code or first_lang.display or ""
        )
    # final fallback после worker'а — берёт из preset.content (через extract_field_from_content) или English

    # === Auth resolve ===
    # Vendor credentials from request bodies are deliberately ignored.  Model
    # admission and credentials are controlled by the server.
    effective_model = _resolve_studio_model(req.model, user_id)
    model_is_claude, model_is_codex, oauth_tokens_list = _validate_model_admission(
        effective_model, user_id
    )
    # BYOK-2026-07-30: подписка (OAuth) только когда токен реально есть —
    # иначе Claude идёт прямым Anthropic API на своём ключе тенанта.
    effective_use_subscription = model_is_claude and bool(oauth_tokens_list)

    # === Default target_length_words: если ни в request, ни в preset нет — 8000 ===
    # (raньше worker делал num_chunks=1 на пустом target → single огромный chunk,
    #  что и провоцировало stream-stall на Opus. Sane default снижает риск.)
    target_words_final = req.target_length_words
    if not target_words_final:
        preset_target = getattr(preset_obj, "target_length_words", None)
        if not preset_target:
            target_words_final = 8000
            print(f"[GEN_JOB] target_length_words default 8000 applied (req=None, preset=None) for preset={preset_obj.id}")

    # === Pack payload + создаём по джобу на каждую тему (batch) ===
    # PATCH-2026-06-20-gen-batch: общие поля (preset/auth/language/target) резолвлены выше ОДИН раз;
    # в цикл обёрнут ТОЛЬКО pack+Job+add_job. Каждый job — тот же проверенный single-topic v2-dict.
    job_ids = []
    failed = []
    for topic, item_lang in pairs:
        eff_lang = item_lang or language  # язык строки приоритетнее общего language/пресета
        try:
            payload = {
                "v": 2,
                "topic": topic,
                "preset_id": preset_obj.id,
                "preset_name": preset_obj.name,
                "language": eff_lang or None,
                "target_length_words": target_words_final,
                "max_log_entries": req.max_log_entries,
                "chunk_size_words": req.chunk_size_words,
            }
            job = Job(
                job_type=JobType.GENERATION.value,
                model=effective_model,
                provider=classify_provider(effective_model),
                api_key="",
                use_subscription=effective_use_subscription,
                oauth_token="",
                oauth_tokens="[]",
                max_threads=1,  # single-shot
                output_dir=str(user_output),
                gen_tasks=json.dumps(payload, ensure_ascii=False),  # v2 JSON dict
                preset_name=preset_obj.name,
                gen_languages=json.dumps([{"name": eff_lang}] if eff_lang else [], ensure_ascii=False),
                gen_safe_text=False,
                parallel=False,  # compatibility field; dispatcher ignores it
                user_id=user_id,
            )
            job_ids.append(_add_job_for_active_user(job))
        except Exception as e:
            failed.append({"topic": topic[:60], "error": str(e)})
    if not job_ids:
        raise HTTPException(status_code=500, detail=f"no jobs created (failed={failed})")
    # job_id (первый) — обратная совместимость со старым клиентом; job_ids/count — новый batch-ответ.
    return {"job_id": job_ids[0], "job_ids": job_ids, "count": len(job_ids), "failed": failed, "status": "pending"}


@app.post("/jobs/rewrite")
def create_rewrite_job(req: RewriteJobRequest, request: Request):
    user_id = _get_user_id(request)
    user_output = _get_user_output_dir(request)
    if len(req.files or []) * len(req.languages or []) > REWRITE_MAX_BATCH_ITEMS:
        raise HTTPException(
            status_code=413,
            detail=f"rewrite batch is too large (max {REWRITE_MAX_BATCH_ITEMS} file-language tasks)",
        )
    # B1 PATCH-2026-07-02-secfix: files[] должен быть ТОЛЬКО именем файла (basename тенанта).
    # Запрещаем директории / абсолютные пути / traversal на входе, чтобы воркер не резолвил
    # произвольный os.path.isabs → LFI (чтение sessions.db и т.п.). Containment см. /upload.
    for f in (req.files or []):
        name = os.path.basename(f)
        if name != f or name in ("", ".", ".."):
            raise HTTPException(status_code=422, detail="invalid file name")
    effective_model = _resolve_studio_model(req.model, user_id)
    model_is_claude, model_is_codex, oauth_tokens_list = _validate_model_admission(
        effective_model, user_id
    )
    # BYOK-2026-07-30: подписка (OAuth) только когда токен реально есть —
    # иначе Claude идёт прямым Anthropic API на своём ключе тенанта.
    effective_use_subscription = model_is_claude and bool(oauth_tokens_list)
    # Task 3: для всех кроме админа (Володя) — принудительно preserve_paragraphs=True (для GPT-пути).
    # 2026-06-03: для Claude (subscription/api) — всегда False (естественный реролл; chunk-loop
    # с сохранением абзацев 1:1 заточен под слабую gpt-4.1-nano, для Sonnet/Opus вредит).
    # ADMIN_USER_ID импортирован из auth/rewrite_core и един для всех гейтов.
    if model_is_claude:
        effective_preserve_paragraphs = False
    else:
        effective_preserve_paragraphs = True if user_id != ADMIN_USER_ID else req.preserve_paragraphs
    # Прокидка job-level host/channel в каждую запись languages, НЕ перетирая
    # уже заданные per-language значения (UI исторически передаёт их в lang dict).
    langs_with_branding = []
    for lang in req.languages:
        d = lang.model_dump()
        if not d.get("host"):
            d["host"] = req.host_name
        if not d.get("channel"):
            d["channel"] = req.channel_name
        langs_with_branding.append(d)
    job = Job(
        job_type=JobType.REWRITE.value,
        model=effective_model,
        provider=classify_provider(effective_model),
        api_key="",
        max_threads=REWRITE_MAX_THREADS_PER_JOB,
        output_dir=str(user_output),
        rewrite_files=json.dumps(req.files, ensure_ascii=False),
        rewrite_languages=json.dumps(langs_with_branding, ensure_ascii=False),
        rewrite_chunk_size=req.chunk_size,
        rewrite_address_style=req.address_style,
        rewrite_preserve_paragraphs=effective_preserve_paragraphs,
        use_subscription=effective_use_subscription,
        oauth_token="",
        oauth_tokens="[]",
        gen_safe_text=req.safe_text,
        parallel=False,
        user_id=user_id,
    )
    job_id = _add_job_for_active_user(job)
    return {"job_id": job_id, "status": "pending"}


# ============== ЖИВАЯ ОЧЕРЕДЬ (виджет) ==============
# Наружу уходят ТОЛЬКО целые числа-агрегаты и собственные числа юзера:
# ни логинов, ни user_id, ни названий задач, ни чужих ошибок.
# Ошибка БД → HTTP 200 с degraded:true и нулями, а не 500: виджет не должен
# ронять кабинет. Путь /api/queue/pulse закрыт auth_middleware (его нет в
# public_paths и он не начинается на /api/v1/).
PULSE_PEOPLE_MODE = "exact"  # exact | bucket — подпись считает сервер, не клиент
_PULSE_CACHE_TTL = 3.0
_PULSE_CACHE: dict = {"ts": 0.0, "rows": []}
_PULSE_CACHE_LOCK = threading.Lock()


def _pulse_people_label(people: int) -> str:
    """Готовая подпись числа людей. Огрубление переключается одной константой."""
    if PULSE_PEOPLE_MODE != "bucket":
        return str(people)
    if people <= 1:
        return str(max(0, people))
    if people <= 3:
        return "2-3"
    if people <= 9:
        return "4-9"
    return "10+"


def _pulse_rows() -> tuple:
    """Глобальный снимок активных строк: один запрос на процесс, TTL 3 c.

    Персональная часть считается из этого же снимка — отдельного запроса
    на юзера не делаем (при 5-секундном опросе вкладок это был бы шум).
    """
    now = time.time()
    with _PULSE_CACHE_LOCK:
        age = now - _PULSE_CACHE["ts"]
        if age < _PULSE_CACHE_TTL:
            return _PULSE_CACHE["rows"], age
        try:
            rows = queue.queue_pulse()
        except Exception:
            # БД недоступна: ts всё равно двигаем, иначе в аварии каждый запрос
            # виджета ломится в неё заново — ровно тогда, когда ей и так плохо.
            _PULSE_CACHE["ts"] = now
            _PULSE_CACHE["rows"] = []
            raise
        _PULSE_CACHE["ts"] = now
        _PULSE_CACHE["rows"] = rows
        return rows, 0.0


@app.get("/api/queue/pulse")
def queue_pulse(request: Request):
    """Живая очередь: глобальные агрегаты (все тенанты) + свои числа."""
    user_id = _get_user_id(request)
    waiting = running = people = 0
    mine_waiting = mine_running = 0
    cache_age = 0.0
    degraded = False
    try:
        rows, cache_age = _pulse_rows()
        actors = set()
        for status, row_user in rows:
            if status == JobStatus.RUNNING.value:
                running += 1
                if row_user == user_id:
                    mine_running += 1
            else:
                # pending + resetting: слот очереди занят, юзер ждёт.
                waiting += 1
                if row_user == user_id:
                    mine_waiting += 1
            if row_user:
                # Пустой user_id (legacy-строки) — не человек, в COUNT DISTINCT не идёт.
                actors.add(row_user)
        people = len(actors)
    except Exception:
        degraded = True
    return {
        "service": "rewrite",
        "ts": time.time(),
        "cache_age": round(cache_age, 2),
        # Механизма админской паузы очереди в rewrite нет → всегда false.
        "paused": False,
        "paused_reason": "",
        "totals": {
            "waiting": waiting,
            "running": running,
            "people": None if PULSE_PEOPLE_MODE == "bucket" else people,
            "people_label": _pulse_people_label(people),
        },
        "mine": {
            "waiting": mine_waiting,
            "running": mine_running,
            # Минимальный вариант: ключи присутствуют, значения всегда пустые
            # (фронт один на три кабинета и не должен знать, где что есть).
            "ahead": None,
            "eta_sec": None,
            "eta_approx": True,
        },
        "lanes": [],
        "health": {"worker": "ok", "note": ""},
        "degraded": degraded,
    }


@app.get("/jobs")
def list_jobs(request: Request):
    user_id = _get_user_id(request)
    user_jobs = queue.get_jobs_by_user(user_id)
    return redact_server_credentials([
        {
            "id": j.id,
            "type": j.job_type,
            "status": j.status,
            "progress": j.progress_percent,
            "detail": j.progress_detail,
            "log": "",  # PATCH-2026-06-18-joblist-nolog: лог не тащим в список (3МБ->КБ); берётся через /jobs/{id}
            "parallel": getattr(j, "parallel", False),
            "created_at": j.created_at,
            **_job_display_meta(j),  # PATCH-2026-06-20-joblist-names: name, langs, nfiles
        }
        for j in user_jobs
    ])


def _bounded_zip_members(
    jobs: List[tuple],
) -> List[tuple]:
    """Resolve tenant-owned output files and enforce pre-compression limits."""
    members: List[tuple] = []
    total_bytes = 0
    seen = set()
    for job, archive_prefix in jobs:
        output_root = Path(job.output_dir or OUTPUT_DIR).resolve()
        try:
            filenames = json.loads(job.output_files or "[]")
        except (json.JSONDecodeError, TypeError):
            filenames = []
        if not isinstance(filenames, list):
            continue
        for raw_name in filenames:
            relative = Path(str(raw_name))
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
            ):
                continue
            candidate = (output_root / relative).resolve()
            try:
                candidate.relative_to(output_root)
            except ValueError:
                continue
            if not candidate.is_file():
                continue
            identity = (str(candidate), archive_prefix)
            if identity in seen:
                continue
            seen.add(identity)
            size = candidate.stat().st_size
            total_bytes += max(0, size)
            if len(members) + 1 > REWRITE_ZIP_MAX_FILES:
                raise HTTPException(
                    status_code=413,
                    detail=f"ZIP contains too many files (max {REWRITE_ZIP_MAX_FILES})",
                )
            if total_bytes > REWRITE_ZIP_MAX_UNCOMPRESSED_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="ZIP source files exceed the server byte limit",
                )
            safe_relative = "/".join(relative.parts)
            archive_name = (
                f"{archive_prefix}/{safe_relative}"
                if archive_prefix
                else safe_relative
            )
            members.append((candidate, archive_name))
    return members


def _zip_stream_response(
    members: List[tuple],
    filename: str,
) -> StreamingResponse:
    if not members:
        raise HTTPException(status_code=404, detail="no files to download")
    spool = tempfile.SpooledTemporaryFile(
        max_size=REWRITE_ZIP_RAM_SPOOL_BYTES,
        mode="w+b",
    )
    try:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, archive_name in members:
                archive.write(path, archive_name)
                if spool.tell() > REWRITE_ZIP_MAX_ARCHIVE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="ZIP archive exceeds the server byte limit",
                    )
        archive_size = spool.seek(0, os.SEEK_END)
        if archive_size > REWRITE_ZIP_MAX_ARCHIVE_BYTES:
            raise HTTPException(
                status_code=413,
                detail="ZIP archive exceeds the server byte limit",
            )
        spool.seek(0)
    except BaseException:
        spool.close()
        raise

    def stream():
        try:
            while True:
                chunk = spool.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            spool.close()

    return StreamingResponse(
        stream(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(archive_size),
        },
    )


@app.get("/jobs/download-zip-bulk")
def download_jobs_zip_bulk(request: Request):
    """Скачать файлы нескольких задач одним ZIP. ids через запятую (?ids=a,b,c).
    PATCH-2026-06-20-bulk-download. ВАЖНО: зарегистрирован ДО /jobs/{job_id},
    иначе тот перехватит путь (job_id='download-zip-bulk')."""
    user_id = _get_user_id(request)
    ids = request.query_params.get("ids", "")
    id_list = list(
        dict.fromkeys(s.strip() for s in ids.split(",") if s.strip())
    )
    if not id_list:
        raise HTTPException(status_code=400, detail="no job ids")
    if len(id_list) > REWRITE_ZIP_MAX_JOB_IDS:
        raise HTTPException(
            status_code=413,
            detail=f"too many job ids (max {REWRITE_ZIP_MAX_JOB_IDS})",
        )
    selected = []
    for job_id in id_list:
        if len(job_id) > 128:
            raise HTTPException(status_code=422, detail="invalid job id")
        job = queue.get_job(job_id)
        if not job or not _job_belongs_to_user(job, user_id):
            continue
        selected.append((job, f"job-{job_id}"))
    members = _bounded_zip_members(selected)
    filename = f"jobs-{len(id_list)}-files.zip"
    return _zip_stream_response(members, filename)


@app.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    # B3 PATCH-2026-06-25-secfix: НЕ отдавать секреты. Вырезаем ключи/токены из ответа
    # (UI-контракт сохранён — остальные поля как раньше).
    d = asdict(job)
    for _k in (
        "api_key",
        "oauth_token",
        "oauth_tokens",
        "run_token",
        "reset_manifest",
        "cancel_requested",
    ):
        d.pop(_k, None)
    return redact_server_credentials(d)


@app.get("/jobs/{job_id}/files")
def get_job_files(job_id: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    return json.loads(job.output_files or "[]")


@app.get("/jobs/{job_id}/files/{filename}")
def download_file(job_id: str, filename: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    output_dir = Path(job.output_dir or OUTPUT_DIR)
    path = output_dir / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(path), filename=filename)


@app.get("/jobs/{job_id}/download-zip")
def download_job_zip(job_id: str, request: Request):
    """Скачать все файлы задачи одним ZIP-архивом."""
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    members = _bounded_zip_members([(job, "")])
    filename = f"job-{job_id}-files.zip"
    return _zip_stream_response(members, filename)


def _signal_job_stop(job_id: str) -> None:
    with _job_stop_events_lock:
        event = _job_stop_events.get(job_id)
    if event:
        event.set()


def _full_retry_cleanup(ticket, user_id: str) -> int:
    """Cleanup only after terminal→resetting ownership has been acquired."""
    from rewrite_core import PROGRESS_MANAGER, make_output_filename

    job = ticket.job
    cleared = 0
    if job.job_type == JobType.REWRITE.value:
        files = json.loads(job.rewrite_files or "[]")
        languages = json.loads(job.rewrite_languages or "[]")
        for file_name in files:
            for language in languages:
                lang_name = (
                    language.get("code")
                    if isinstance(language, dict)
                    else str(language)
                )
                if PROGRESS_MANAGER.delete_task(
                    os.path.basename(file_name),
                    lang_name,
                    user_id=user_id,
                    job_id=job.id,
                ):
                    cleared += 1
                base_name = os.path.splitext(os.path.basename(file_name))[0][:50]
                output = (
                    Path(job.output_dir)
                    / make_output_filename(base_name, lang_name)
                )
                if output.is_file():
                    output.unlink()
    # Generation and any legacy output names are constrained to this job's
    # output directory and the recorded basename.
    output_root = Path(job.output_dir).resolve()
    for filename in json.loads(job.reset_manifest or "[]"):
        safe = os.path.basename(str(filename))
        candidate = (output_root / safe).resolve()
        if candidate.parent == output_root and candidate.is_file():
            candidate.unlink()
    job.output_files = "[]"
    return cleared


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    result = queue.request_cancel(job_id)
    if not result.changed:
        raise HTTPException(
            status_code=409,
            detail=f"cancel is a no-op ({result.code}, status={result.status})",
        )
    if result.code == "cancel_requested":
        _signal_job_stop(job_id)
    return {"status": result.status, "was": result.previous}


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    if not license_mgr.is_user_active(user_id):
        raise HTTPException(status_code=403, detail="account is disabled")
    if not queue.retry_job(job_id):
        raise HTTPException(
            status_code=409,
            detail="retry only for failed/cancelled/partial/interrupted jobs",
        )
    return {"status": "pending"}


@app.post("/jobs/{job_id}/retry-full")
def retry_job_full(job_id: str, request: Request):
    """
    Полная перегенерация: сбрасывает весь прогресс этой задачи (все чанки, готовые
    результаты, выходные файлы) и запускает с нуля.
    Отличается от обычного /retry который резюмит только failed/incomplete чанки.
    """
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    if not license_mgr.is_user_active(user_id):
        raise HTTPException(status_code=403, detail="account is disabled")
    ticket = queue.begin_retry(job_id, full=True)
    if not ticket:
        raise HTTPException(
            status_code=409, detail="full retry requires a terminal job"
        )
    try:
        cleared = _full_retry_cleanup(ticket, user_id)
        if not queue.complete_retry(ticket):
            raise RuntimeError("retry ownership was lost")
        queue.append_log(job_id, "\n--- Полный повтор с нуля ---\n")
    except Exception as exc:
        queue.fail_retry(ticket, f"full retry cleanup failed: {exc}")
        raise HTTPException(status_code=500, detail="full retry cleanup failed")
    return {"status": "pending", "cleared_tasks": cleared}


@app.post("/jobs/{job_id}/parallel")
def mark_job_parallel(job_id: str, request: Request, parallel: bool = True):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    raise HTTPException(
        status_code=410,
        detail="parallel scheduling hint was removed; provider fair queue is automatic",
    )


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    result = queue.delete_terminal(job_id)
    if not result.changed:
        raise HTTPException(
            status_code=409,
            detail=f"delete requires a terminal job (status={result.status})",
        )
    return {"status": "deleted"}


MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 МБ


@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    user_id = _get_user_id(request)
    user_upload = UPLOAD_DIR / user_id
    user_upload.mkdir(parents=True, exist_ok=True)
    # B4 PATCH-2026-06-25-secfix: санитайз имени из multipart (роут-регекс не защищает —
    # имя берётся из тела). basename срезает пути, плюс защита от ../ и абсолютных, + containment.
    safe = os.path.basename(file.filename or "uploaded").replace("/", "_").replace("\\", "_")[:200]
    if not safe or safe in (".", ".."):
        safe = "uploaded"
    user_root = user_upload.resolve()
    path = (user_root / safe).resolve()
    if not (path == user_root / safe and str(path).startswith(str(user_root) + os.sep)):
        raise HTTPException(status_code=400, detail="invalid filename")
    total_size = 0
    try:
        with open(path, "wb") as f:
            while True:
                chunk = await file.read(8192)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail=f"Файл слишком большой (макс. {MAX_UPLOAD_SIZE // 1024 // 1024} МБ)")
                f.write(chunk)
    except HTTPException:
        if path.exists():
            os.remove(path)
        raise
    return {"filename": safe, "path": str(path)}  # B4: возвращаем САНИРОВАННОЕ имя (фронт ссылается на него)


# ============== SUBTITLES (PATCH-2026-08-02-subs) ==============
# Скачивание субтитров YouTube. Синхронно и мимо JobQueue: очередь знает только
# GENERATION и REWRITE, а операция занимает секунды — третий тип задачи стоил бы
# миграции схемы и риска для боевой очереди ради 10-40 секунд работы.


def _subs_drop_expired(now: float) -> None:
    """Чистка протухших батчей. Вызывается под _subs_batches_lock."""
    dead = [
        key for key, batch in _subs_batches.items()
        if now - batch["created"] > REWRITE_SUBS_BATCH_TTL_SEC
    ]
    for key in dead:
        _subs_batches.pop(key, None)


def _subs_store_batch(user_id: str, items: list) -> str:
    """Кладёт готовые тексты в память процесса и возвращает id батча."""
    batch_id = uuid.uuid4().hex
    now = time.time()
    with _subs_batches_lock:
        _subs_drop_expired(now)
        _subs_batches[batch_id] = {
            "user_id": user_id,
            "created": now,
            "items": items,
        }
    return batch_id


def _subs_get_batch(batch_id: str, user_id: str) -> dict:
    """Батч своего владельца или 404. Чужой батч неотличим от несуществующего."""
    with _subs_batches_lock:
        _subs_drop_expired(time.time())
        batch = _subs_batches.get(batch_id)
        if not batch or batch["user_id"] != user_id:
            raise HTTPException(status_code=404, detail="Результаты устарели — скачайте субтитры заново")
        return batch


def _subs_unique_names(items: list) -> list:
    """Разводит одинаковые имена файлов внутри одного архива."""
    used = set()
    names = []
    for item in items:
        name = item["filename"]
        if name in used:
            stem, dot, ext = name.rpartition(".")
            counter = 2
            while name in used:
                name = f"{stem}_{counter}{dot}{ext}" if dot else f"{name}_{counter}"
                counter += 1
        used.add(name)
        names.append(name)
    return names


@app.post("/subs/fetch")
def subs_fetch(request: Request, payload: dict = Body(...)):
    user_id = _get_user_id(request)
    if not license_mgr.is_user_active(user_id):
        raise HTTPException(status_code=403, detail="account is disabled")

    try:
        import subs_fetch as subs_engine
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Скачивание субтитров недоступно: на сервере нет пакета youtube-transcript-api",
        )

    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Ожидается объект JSON")
    raw_urls = payload.get("urls", "")
    if isinstance(raw_urls, list):
        raw_urls = "\n".join(str(u) for u in raw_urls)
    if not isinstance(raw_urls, str) or not raw_urls.strip():
        raise HTTPException(status_code=422, detail="Вставьте хотя бы одну ссылку")

    try:
        lang = subs_engine.normalize_lang(payload.get("lang", "auto"))
    except subs_engine.SubtitlesError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    urls = subs_engine.parse_urls(raw_urls, REWRITE_SUBS_MAX_LINKS + 1)
    if not urls:
        raise HTTPException(status_code=422, detail="Не нашёл ни одной ссылки")
    if len(urls) > REWRITE_SUBS_MAX_LINKS:
        raise HTTPException(
            status_code=413,
            detail=f"Слишком много ссылок за раз (максимум {REWRITE_SUBS_MAX_LINKS})",
        )

    # Одна выгрузка на пользователя: иначе F5 на медленной пачке удваивает
    # нагрузку на тот же IP и занимает второй тред пула.
    with _subs_batches_lock:
        if user_id in _subs_active_users:
            raise HTTPException(status_code=429, detail="Предыдущая загрузка ещё идёт — дождитесь её окончания")
        _subs_active_users.add(user_id)
    try:
        if not _subs_slots.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="Сервис занят другими загрузками — попробуйте через минуту")
        try:
            results = subs_engine.fetch_many(
                urls,
                lang=lang,
                timeout=float(REWRITE_SUBS_TIMEOUT_SEC),
                parallel=REWRITE_SUBS_PARALLEL,
                proxy=REWRITE_SUBS_PROXY,
            )
        finally:
            _subs_slots.release()
    finally:
        with _subs_batches_lock:
            _subs_active_users.discard(user_id)

    total_bytes = 0
    stored = []
    items = []
    for result in results:
        public = result.as_public_dict()
        if result.ok:
            total_bytes += len(result.text.encode("utf-8"))
            if total_bytes > REWRITE_SUBS_MAX_BATCH_BYTES:
                public["ok"] = False
                public["error"] = "Слишком много текста за раз — уменьшите список"
            else:
                stored.append({"filename": result.filename, "text": result.text})
                public["text"] = result.text
        items.append(public)

    batch_id = _subs_store_batch(user_id, stored) if stored else ""
    return {
        "batch_id": batch_id,
        "items": items,
        "ok_count": len(stored),
        "total": len(items),
    }


@app.get("/subs/download")
def subs_download(request: Request):
    """Один готовый файл отдаём как .txt, несколько — одним .zip."""
    user_id = _get_user_id(request)
    batch_id = request.query_params.get("batch", "").strip()
    if not batch_id or len(batch_id) > 64:
        raise HTTPException(status_code=422, detail="invalid batch id")
    batch = _subs_get_batch(batch_id, user_id)
    items = batch["items"]
    if not items:
        raise HTTPException(status_code=404, detail="Нет файлов для скачивания")

    if len(items) == 1:
        # Кириллица в имени безопасна только так: Starlette сам соберёт
        # filename*=utf-8''… Руками собранный заголовок упал бы на latin-1.
        return Response(
            content=items[0]["text"].encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": _content_disposition(items[0]["filename"]),
            },
        )

    names = _subs_unique_names(items)
    spool = io.BytesIO()
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, item in zip(names, items):
            archive.writestr(name, item["text"])
    payload = spool.getvalue()
    # Имя архива — только ASCII: этот заголовок собирается вручную.
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="subtitles-{len(items)}.zip"',
            "Content-Length": str(len(payload)),
        },
    )


def _content_disposition(filename: str) -> str:
    """RFC 5987: ASCII-запасное имя + UTF-8 для всех остальных."""
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("_") or "subtitles.txt"
    quoted = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


# ============== API v1 (programmatic, api-key auth) ==============

from api import build_api_router  # noqa: E402

# Секрет в пресете настроек оказаться не должен, но проверяем это здесь, а не надеемся:
# набор полей задаёт интерфейс, и завтра в нём может появиться что угодно.
_SECRETISH = ("key", "token", "secret", "password", "cookie", "auth", "credential")


def _without_secrets(value):
    if isinstance(value, dict):
        return {k: _without_secrets(v) for k, v in value.items()
                if not any(mark in str(k).lower() for mark in _SECRETISH)}
    if isinstance(value, list):
        return [_without_secrets(v) for v in value]
    return value


def _api_presets(user_id: str) -> dict:
    """Три набора пресетов пользователя для GET /api/v1/presets.

    Разделение не косметическое: `rew_presets` (пресеты рерайта по ссылке) и пресеты
    генерации живут в разных хранилищах и означают разное. Канал в кабинете привязан
    именно к пресету рерайта.
    """
    cfg = get_keys_config(user_id) or {}
    rew = cfg.get("rew_presets") or {}
    gen_settings = cfg.get("gen_settings_presets") or {}
    try:
        generation = {p.name: _preset_summary(p)
                      for p in _preset_manager_for(user_id).list_presets()}
    except Exception:
        generation = {}
    return {
        "rewrite": _without_secrets(rew if isinstance(rew, dict) else {}),
        "generation": generation,
        "gen_settings": _without_secrets(
            gen_settings if isinstance(gen_settings, dict) else {}),
    }


api_router = build_api_router(
    queue=queue,
    upload_dir=UPLOAD_DIR,
    output_dir=OUTPUT_DIR,
    job_stop_events=_job_stop_events,
    job_stop_events_lock=_job_stop_events_lock,
    user_is_active=license_mgr.is_user_active,
    presets_provider=_api_presets,
)
app.include_router(api_router)
# Channel Studio (Path B) откачен 2026-05-23 — заменяется модернизацией Generation tab.


# ============== UI helper routes for api-key management (session auth) ==============

# Field уже импортирован выше (см. line ~22)
from api_auth import create_api_key, list_api_keys, revoke_api_key  # noqa: E402


class _ApiKeyCreateRequest(BaseModel):
    name: str


@app.post("/api/keys/create", tags=["UI helpers"], summary="Create api-key (session auth)")
def ui_api_keys_create(req: _ApiKeyCreateRequest, request: Request):
    """UI-only endpoint: создать api-key для текущей сессии. Plain key возвращается ОДИН раз."""
    user_id = _get_user_id(request)
    license_key = getattr(request.state, "license_key", "") or ""
    sessions_db = os.environ.get("SESSION_DB", str(BASE_DIR / "sessions.db"))
    return create_api_key(sessions_db, user_id, license_key, req.name)


@app.get("/api/keys/list", tags=["UI helpers"], summary="List api-keys (session auth)")
def ui_api_keys_list(request: Request):
    user_id = _get_user_id(request)
    sessions_db = os.environ.get("SESSION_DB", str(BASE_DIR / "sessions.db"))
    return {"items": list_api_keys(sessions_db, user_id)}


@app.post("/api/keys/{key_id}/revoke", tags=["UI helpers"], summary="Revoke api-key (session auth)")
def ui_api_keys_revoke(key_id: str, request: Request):
    user_id = _get_user_id(request)
    sessions_db = os.environ.get("SESSION_DB", str(BASE_DIR / "sessions.db"))
    ok = revoke_api_key(sessions_db, key_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="api key not found or already revoked")
    return {"key_id": key_id, "status": "revoked"}


class SafeStaticFiles(StaticFiles):
    """Serve active assets while making backup artifacts undiscoverable."""

    _backup_pattern = re.compile(
        r"(?:\.bak(?:\.|$)|\.backup(?:\.|$)|\.old(?:\.|$)|"
        r"\.orig(?:\.|$)|(?:^|[._-])pre[_-]|~$)",
        re.IGNORECASE,
    )

    async def get_response(self, path: str, scope):
        normalized = path.replace("\\", "/")
        if any(
            self._backup_pattern.search(part)
            for part in normalized.split("/")
        ):
            return Response(status_code=404)
        return await super().get_response(path, scope)


# Статика (CSS, JS, картинки)
if STATIC_DIR.exists():
    app.mount("/static", SafeStaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8899)
