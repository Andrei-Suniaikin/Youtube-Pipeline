# -*- coding: utf-8 -*-
"""
Cover Server — FastAPI-сервер для генерации обложек на VPS.
Запуск: uvicorn cover_server:app --host 0.0.0.0 --port 8900

Авторизация: аккаунты логин/пароль, сессии в SQLite.
Публичные пути: /, /auth/login-password, /auth/setup, /docs, /static.
"""

import os
import re
import hmac
import json
import traceback
import zipfile
import io
import queue as std_queue
import threading
import time
import shutil
import secrets
from pathlib import Path
from dataclasses import asdict, fields as dc_fields
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, Response
from pydantic import BaseModel, Field, model_validator

BASE_DIR = Path(os.environ.get("COVER_SERVER_DIR", os.path.dirname(os.path.abspath(__file__))))
OUTPUT_DIR = Path(os.environ.get("COVER_OUTPUT_DIR", str(BASE_DIR / "outputs")))
UPLOAD_DIR = Path(os.environ.get("COVER_UPLOAD_DIR", str(BASE_DIR / "uploads")))
PRESETS_DIR = Path(os.environ.get("COVER_PRESETS_DIR", str(BASE_DIR / "presets")))
STATIC_DIR = Path(os.environ.get("COVER_STATIC_DIR", str(BASE_DIR / "static")))
DB_PATH = os.environ.get("COVER_DB_PATH", str(BASE_DIR / "cover_job_queue.db"))

# --- настройки генерации в стиле VideoStudio (cover_gen_settings.json, hot-reload по mtime) ---
GEN_SETTINGS_PATH = str(BASE_DIR / "cover_gen_settings.json")
_gen_settings_cache = {"mtime": 0.0, "data": {}}
def gen_settings() -> dict:
    try:
        mt = os.path.getmtime(GEN_SETTINGS_PATH)
        if mt != _gen_settings_cache["mtime"]:
            with open(GEN_SETTINGS_PATH, encoding="utf-8") as f:
                _gen_settings_cache["data"] = json.load(f) or {}
            _gen_settings_cache["mtime"] = mt
    except Exception:
        return _gen_settings_cache["data"] or {}
    return _gen_settings_cache["data"]
API_SECRET = os.environ.get("COVER_API_SECRET", "")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PRESETS_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

from job_queue_cover import (
    Job,
    JobQueue,
    JobWorker,
    JobStatus,
    PendingJobLimitExceeded,
)
from cover_core import (
    process_batch,
    Preset,
    PresetManager,
    asdict,
    get_default_prompt,
    set_prompts_config,
    get_lang_code,
    FASTGEN_WEIGHTED_BUDGET_LIMIT,
    VEO_INFLIGHT_LIMIT,
    GPT_IMAGE2_INFLIGHT_LIMIT,
    XAI_INFLIGHT_LIMIT,
)
from api_cover import build_api_router
from api_cover_helpers import (
    acquire_server_keys_for_job,
    credential_pool_summary,
    ensure_server_keys_for_job,
)
from provider_policy import (
    InvalidGenerationProvider,
    canonical_generation_provider,
)
from api_cover_auth import (
    ApiAuthState, extract_api_key, verify_api_key,
    create_api_key, list_api_keys, revoke_api_key,
)

PROMPTS_KEYS = [
    "analyze_prompt", "style_analyze_prompt", "check_prompt_template",
    "adapt_text_system_template", "generation_prompt_template",
    "simple_edit_prompt_template", "simple_check_prompt_template",
    "style_ref_prompt_template",
]
queue = JobQueue(DB_PATH)

def _server_int(env_name: str, setting_name: str, default: int, low: int, high: int) -> int:
    """Server-controlled hot setting; environment has priority over admin JSON."""
    raw = os.environ.get(env_name)
    if raw in (None, ""):
        raw = gen_settings().get(setting_name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _server_job_slots() -> int:
    return _server_int("COVER_JOB_SLOTS", "max_parallel_jobs", 10, 1, 100)


def _server_max_threads() -> int:
    return _server_int("COVER_MAX_THREADS_PER_JOB", "max_threads_per_job", 8, 1, 25)


def _server_max_retries() -> int:
    return _server_int("COVER_MAX_GENERATION_RETRIES", "max_generation_retries", 2, 1, 10)


def _server_queue_limit() -> int:
    # Технический anti-accident cap, не тарифная/дневная квота.
    return _server_int("COVER_MAX_PENDING", "queue_limit_per_user", 100, 1, 1000)


def _server_provider_job_limits() -> dict:
    """Job-level admission caps; request-level semaphores остаются в cover_core."""
    total = _server_job_slots()
    pool = credential_pool_summary()
    fastgen_keys = max(1, int(pool["fastgen"]["unique_keys"]))
    veo_keys = max(1, int(pool["veo"]["unique_keys"]))
    openai_keys = max(1, int(pool["openai"]["unique_keys"]))
    veo_inflight = _server_int(
        "COVER_VEO_INFLIGHT", "veo_inflight", VEO_INFLIGHT_LIMIT, 1, total
    )
    gpt_inflight = _server_int(
        "COVER_GPT_IMAGE2_INFLIGHT",
        "gpt_image2_inflight",
        GPT_IMAGE2_INFLIGHT_LIMIT,
        1,
        total,
    )
    return {
        "fastgen": _server_int(
            "COVER_FASTGEN_JOB_SLOTS",
            "fastgen_job_slots",
            min(total, total * fastgen_keys),
            1,
            total,
        ),
        "veo": _server_int(
            "COVER_VEO_JOB_SLOTS",
            "veo_job_slots",
            min(total, veo_inflight * veo_keys),
            1,
            total,
        ),
        "gpt_image2": _server_int(
            "COVER_GPT_IMAGE2_JOB_SLOTS",
            "gpt_image2_job_slots",
            min(total, gpt_inflight * openai_keys),
            1,
            total,
        ),
        # Прямой xAI: считаем по своим ключам, чужие пулы не трогаем.
        "xai": _server_int(
            "COVER_XAI_JOB_SLOTS",
            "xai_job_slots",
            min(total, XAI_INFLIGHT_LIMIT * max(1, int(pool.get("xai", {}).get("unique_keys", 0) or 1))),
            1,
            total,
        ),
    }

_preset_managers_lock = threading.Lock()
_preset_managers_cache: dict = {}


def _safe_user_id(user_id: str) -> str:
    """Безопасное имя каталога для user_id."""
    if not (user_id or "").strip():
        return "default"
    return re.sub(r"[^\w\-]", "_", str(user_id).strip()) or "default"


def _user_presets_path(user_id: str) -> Path:
    path = PRESETS_DIR / _safe_user_id(user_id) / "presets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _user_prompts_path(user_id: str) -> Path:
    path = PRESETS_DIR / _safe_user_id(user_id) / "prompts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _get_preset_manager(user_id: str) -> PresetManager:
    safe = _safe_user_id(user_id)
    with _preset_managers_lock:
        if safe not in _preset_managers_cache:
            mgr = PresetManager(str(_user_presets_path(user_id)))
            # vF: сидируем дефолтный пресет новому пользователю — иначе quickstart из
            # партнёрской доки (preset_name="Default RU") падает 422 на пустом хранилище.
            if not mgr._presets:
                try:
                    mgr.save_preset(Preset(name="Default RU", language="Русский", languages="Русский,Английский"))
                except Exception:
                    pass  # сид не критичен; пресет можно создать руками/через API
            _preset_managers_cache[safe] = mgr
        return _preset_managers_cache[safe]


try:
    from cover_auth import LicenseManager
    license_mgr = LicenseManager()
except ImportError:
    license_mgr = None


def _get_user_output_dir(user_id: str) -> Path:
    if user_id == "desktop":
        return OUTPUT_DIR
    user_out = OUTPUT_DIR / user_id
    user_out.mkdir(parents=True, exist_ok=True)
    return user_out


def _job_belongs_to_user(job: Job, user_id: str) -> bool:
    # v1.1.0 security: убран обход для 'desktop' (видел ВСЕ джобы всех тенантов).
    # Теперь любой identity (включая desktop) видит только свои джобы.
    return getattr(job, "user_id", "") == (user_id or "")


def _resolve_ref_paths(refs_per_language: dict, user_id: str) -> dict:
    """Резолвит имя файла или file_id в полный путь СТРОГО внутри папки юзера.
    v1.1.0 security: убраны абсолютные/чужие пути и кросс-юзер fallback — тенант
    не может подсунуть путь к чужому файлу. Нерезолвленное молча пропускаем
    (генерация пойдёт без рефа, а не прочитает произвольный файл).
    v1.0.9: резолв file_id — UI шлёт 'sIQRwsHg', файл хранится как 'sIQRwsHg_<name>.jpg'."""
    if not refs_per_language:
        return {}
    user_dir = (UPLOAD_DIR / user_id) if user_id != "desktop" else UPLOAD_DIR
    try:
        base = user_dir.resolve()
    except Exception:
        return {}
    out = {}
    for lang, path_or_name in refs_per_language.items():
        name = (path_or_name or "").strip()
        if not name:
            continue
        basename = os.path.basename(name)
        candidate = None
        p = user_dir / basename
        if p.exists() and p.is_file():
            candidate = p
        else:
            safe_id = re.sub(r"[^\w\-]", "", basename)
            if safe_id and user_dir.exists():
                for entry in user_dir.iterdir():
                    if entry.is_file() and entry.name.startswith(safe_id + "_"):
                        candidate = entry
                        break
        if candidate is None:
            continue  # не нашли в своей папке — игнорируем, не передаём произвольный путь
        rp = candidate.resolve()
        if rp.is_relative_to(base) and rp.exists() and rp.is_file():
            out[lang] = str(rp)
    return out


def _load_prompts_for_job(job: Job):
    """Загрузить промпты пользователя и применить для воркера."""
    custom = {}
    path = _user_prompts_path(getattr(job, "user_id", "") or "default")
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                custom = json.load(f)
        except Exception:
            pass
    merged = {}
    for key in PROMPTS_KEYS:
        merged[key] = custom.get(key) or get_default_prompt(key)
    set_prompts_config(merged)


def _execute_cover_job(job: Job):
    """Выполняет одно задание генерации обложек."""
    credential_lease = None
    try:
        _load_prompts_for_job(job)
        urls = json.loads(job.urls)
        preset_data = json.loads(job.preset_data or "{}")
        user_id = getattr(job, "user_id", "") or "desktop"
        preset = None
        if preset_data and preset_data.get("name"):
            preset = _get_preset_manager(user_id).get(preset_data["name"])
            if preset:
                # копия: ниже поля мутируются (резолв reference_image в абс. путь),
                # а mgr.get() отдаёт живой объект кэша — мутация утекала в GET /presets
                preset = Preset(**asdict(preset))
        if not preset:
            valid = {f.name for f in dc_fields(Preset)}
            preset = Preset(**{k: v for k, v in preset_data.items() if k in valid}) if preset_data else Preset(name="default")
        if not preset or not getattr(preset, "name", None):
            preset = Preset(name="default")
        # vS: инлайн-тумблер simple_mode не должен теряться, даже если пресет
        # перезагрузили из хранилища по имени (checkbox важнее сохранённого пресета).
        if isinstance(preset_data, dict) and "simple_mode" in preset_data:
            preset.simple_mode = bool(preset_data.get("simple_mode"))
        # vR: то же для инлайн-референса стиля (file_id из preset_data важнее сохранённого).
        if isinstance(preset_data, dict) and "reference_image" in preset_data:
            preset.reference_image = (preset_data.get("reference_image") or "").strip()
        # seed-mode: инлайн шаблон-пресет важнее сохранённого (юзер правит под нишу прямо в форме).
        if isinstance(preset_data, dict) and "prompt_template" in preset_data:
            preset.prompt_template = preset_data.get("prompt_template") or ""
        # v1.4: инлайн-выбор модели генерации важнее сохранённого пресета (дропдаун в форме =
        # что юзер видит; раньше смена модели молча игнорировалась для сохранённых пресетов,
        # а ключ-своп veo/gpt_image2 обязан совпадать с провайдером, который реально генерит).
        if isinstance(preset_data, dict) and preset_data.get("generation_provider"):
            preset.generation_provider = str(preset_data["generation_provider"])
            preset.generation_model = str(preset_data.get("generation_model") or "")
        # vR: резолвим file_id референса → абсолютный путь СТРОГО внутри папки юзера
        # (та же security-логика, что и refs_per_language). Не резолвится → пусто (без рефа).
        ref_id = (getattr(preset, "reference_image", "") or "").strip()
        if ref_id:
            preset.reference_image = _resolve_ref_paths({"_ref": ref_id}, user_id).get("_ref", "") or ""
        # Общие ключи и физические caps управляются только сервером.
        preset.max_generation_retries = _server_max_retries()
        job.max_threads = _server_max_threads()
        _gen_provider = canonical_generation_provider(
            getattr(preset, "generation_provider", "") or "flow"
        )
        _job_provider = canonical_generation_provider(
            getattr(job, "generation_provider", "") or _gen_provider
        )
        if _job_provider != _gen_provider:
            raise InvalidGenerationProvider(
                "Job generation_provider changed after admission "
                f"({_job_provider!r} != {_gen_provider!r})"
            )
        preset.generation_provider = _gen_provider
        credential_lease = acquire_server_keys_for_job(job)
        openai_key = credential_lease.openai_key
        gen_api_key = credential_lease.generation_key
        languages = json.loads(job.languages or "[]")
        refs_raw = json.loads(job.refs_per_language or "{}") if job.refs_per_language else {}
        refs_per_language = _resolve_ref_paths(refs_raw, user_id)
        output_dir = str(_get_user_output_dir(user_id))

        def log_fn(msg: str):
            # v1.3.0: кольцевой кап лога (иначе строка джобы растёт без предела →
            # O(N²) запись и раздувание БД). Держим последние ~24 КБ.
            new = (job.log or "") + msg + "\n"
            if len(new) > 24576:
                new = "…(лог обрезан)…\n" + new[-24576:]
            job.log = new
            queue.update_job(job, expected_status=JobStatus.RUNNING.value)

        # v1.3.2: инкрементальный прогресс — бар двигался 0→100 рывком, теперь по ходу.
        # vX: + инкрементально персистим готовые файлы → скачивание на ходу (не ждать конца/отмены).
        # progress_cb вызывается ПОСЛЕ _save_result (обложка уже на диске и в out_files).
        def progress_cb(done: int, total: int):
            try:
                job.output_files = json.dumps(out_files, ensure_ascii=False)
                job.fallback_files = json.dumps(fallback_files, ensure_ascii=False)
                pct = int(done * 99 / total) if total else 0  # 100 — только финал
                job.progress_percent = pct
                job.progress_detail = f"{done}/{total} обложек"
                queue.update_job(job, expected_status=JobStatus.RUNNING.value)
            except Exception:
                pass

        existing_output = json.loads(job.output_files or "[]")
        existing_fallback = json.loads(job.fallback_files or "[]")
        skip_existing = len(existing_output) > 0
        out_files = list(existing_output) if skip_existing else []
        fallback_files = list(existing_fallback) if skip_existing else []

        # v1.0.4: title-mode передаёт titles_per_lang + extra_prompt, urls пустой.
        job_mode = getattr(job, "mode", "url") or "url"
        titles_per_lang = None
        extra_prompt = ""
        seeds_per_lang = None
        n_variants = getattr(job, "n_variants", 3) or 3
        if job_mode == "title":
            try:
                titles_per_lang = json.loads(job.titles_per_lang or "{}") or None
            except Exception:
                titles_per_lang = None
            extra_prompt = job.extra_prompt or ""
        elif job_mode == "seed":
            # seed-mode: механизм «пресет+семя → N обложек» (niche-agnostic)
            try:
                seeds_per_lang = json.loads(getattr(job, "seeds_per_lang", "") or "{}") or None
            except Exception:
                seeds_per_lang = None

        results = process_batch(
            urls, preset,
            "", openai_key, gen_api_key,
            output_dir=output_dir,
            languages=languages if languages else None,
            refs_per_language=refs_per_language if refs_per_language else None,
            log_fn=log_fn,
            max_threads=max(1, job.max_threads),
            out_files=out_files,
            skip_existing=skip_existing,
            cancel_check=lambda: queue.should_stop_run(job.id, job.run_token),
            fallback_files=fallback_files,
            titles_per_lang=titles_per_lang,
            extra_prompt=extra_prompt,
            seeds_per_lang=seeds_per_lang,
            n_variants=n_variants,
            progress_cb=progress_cb,
        )
        total = len(results)
        job.results_count = sum(1 for r in results if getattr(r, "success", False))
        job.output_files = json.dumps(out_files, ensure_ascii=False)
        job.fallback_files = json.dumps(fallback_files, ensure_ascii=False)
        job.progress_percent = 100
        fb_count = len(fallback_files)
        if fb_count > 0:
            job.progress_detail = f"{job.results_count}/{total} обложек (из них {fb_count} без текста)"
        else:
            job.progress_detail = f"{job.results_count}/{total} обложек"
        if job.status == JobStatus.RUNNING.value:
            # Если были fallback-обложки — считаем задание не полностью успешным, чтобы было очевидно что нужен resume
            if job.results_count >= total and fb_count == 0:
                job.status = JobStatus.COMPLETED.value
            else:
                job.status = JobStatus.COMPLETED_PARTIAL.value
    except Exception as e:
        job.error = str(e)
        job.status = JobStatus.FAILED.value
        job.log = (job.log or "") + "\n--- Ошибка ---\n" + traceback.format_exc()
        import sys
        print(f"[JOB {job.id} FAILED] {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    finally:
        if credential_lease is not None:
            credential_lease.release()
        job.finished_at = job.finished_at or time.time()
        queue.update_job(job, expected_status=JobStatus.RUNNING.value)


worker = JobWorker(
    queue,
    executor=_execute_cover_job,
    max_concurrent_jobs=_server_job_slots(),
    provider_limits=_server_provider_job_limits(),
)

RESULTS_MAX_AGE_DAYS = 7
_cleanup_stop = threading.Event()

def _cleanup_old_results():
    """Удалить файлы и записи заданий, завершённых более RESULTS_MAX_AGE_DAYS дней назад."""
    import sys
    try:
        max_age = time.time() - (RESULTS_MAX_AGE_DAYS * 24 * 3600)
        jobs = queue.get_all_jobs()
        removed = 0
        for job in jobs:
            finished = getattr(job, "finished_at", 0) or 0
            if finished > 0 and finished < max_age:
                output_dir = Path(job.output_dir or OUTPUT_DIR).resolve()
                for rel in json.loads(job.output_files or "[]"):
                    path = (output_dir / rel).resolve()
                    if path.is_relative_to(output_dir) and path.exists() and path.is_file():
                        try:
                            path.unlink()
                        except OSError:
                            pass
                queue.delete_job(job.id)
                removed += 1
        if removed:
            print(f"[cleanup] Удалено заданий старше {RESULTS_MAX_AGE_DAYS} дн.: {removed}", file=sys.stderr)

        # v1.0.8: чистка orphan uploads — файлы старше RESULTS_MAX_AGE_DAYS из UPLOAD_DIR/<user_id>/.
        # uploads-файлы используются как референсы; после деректории job-а они уже не нужны.
        # Защита: file_id, записанный в пресете (reference_image/seed_reference_image), живёт дольше 7 дней — не удаляем.
        protected = set()
        for pf in PRESETS_DIR.glob("*/presets.json"):
            try:
                data = json.loads(pf.read_text(encoding="utf-8"))
            except Exception:
                continue  # битый presets.json — молча пропускаем
            if not isinstance(data, dict):
                continue
            for p in data.values():
                if not isinstance(p, dict):
                    continue
                for fld in ("reference_image", "seed_reference_image"):
                    # basename: в поле может оказаться и file_id, и абсолютный путь
                    # (старые пресеты, куда утёк резолвленный путь) — защищаем оба вида
                    val = os.path.basename(str(p.get(fld) or "").strip())
                    if val:
                        protected.add(val)
        uploads_removed = 0
        if UPLOAD_DIR.exists():
            for user_dir in UPLOAD_DIR.iterdir():
                if not user_dir.is_dir():
                    continue
                for f in user_dir.iterdir():
                    try:
                        if any(f.name == v or f.name.startswith(v + "_") for v in protected):
                            continue
                        if f.is_file() and f.stat().st_mtime < max_age:
                            f.unlink()
                            uploads_removed += 1
                    except OSError:
                        pass
        if uploads_removed:
            print(f"[cleanup] Удалено uploads старше {RESULTS_MAX_AGE_DAYS} дн.: {uploads_removed}", file=sys.stderr)
    except Exception as e:
        print(f"[cleanup] Ошибка: {e}", file=sys.stderr)

def _cleanup_loop():
    while not _cleanup_stop.is_set():
        _cleanup_stop.wait(24 * 3600)
        if not _cleanup_stop.is_set():
            _cleanup_old_results()

_cleanup_thread = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    ApiAuthState.configure(DB_PATH)
    ApiAuthState.init_db()
    # Восстановление после рестарта атомарно учитывает persistent cancel_requested:
    # обычные running → pending, отменённые running → cancelled.
    try:
        recovery = queue.recover_interrupted_jobs(
            max_pending_for_user=_server_queue_limit(),
        )
        if (
            recovery["requeued"]
            or recovery["cancelled"]
            or recovery.get("queue_full_failed", 0)
        ):
            print(
                "[startup] recovery: "
                f"running→pending={recovery['requeued']}, "
                f"cancelled={recovery['cancelled']}, "
                f"queue-full→failed={recovery.get('queue_full_failed', 0)}"
            )
    except Exception as e:
        print(f"[startup] recovery error: {e}")
    worker.start()
    _cleanup_old_results()
    global _cleanup_thread
    _cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
    _cleanup_thread.start()
    yield
    _cleanup_stop.set()
    worker.stop()


app = FastAPI(title="Cover Server", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(RequestValidationError)
async def sanitized_request_validation_error(
    _request: Request,
    _exc: RequestValidationError,
):
    """Never reflect request input or validator context in a public 422."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "type": "request_validation_error",
                    "loc": ["body"],
                    "msg": "Request validation failed",
                }
            ]
        },
    )


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
    public_paths = ("/", "/index.html", "/auth/login-password", "/auth/setup", "/auth/claim-password", "/auth/status", "/auth/ping", "/auth/logout", "/docs", "/openapi.json", "/redoc", "/static", "/api/v1/presets")
    if any(path == p or path.startswith(p + "/") for p in public_paths):
        # GET /api/v1/presets public — но если передан валидный cvr_* ключ, поставим user_id для персональных пресетов
        if path == "/api/v1/presets" or path.startswith("/api/v1/presets/"):
            authorization = request.headers.get("Authorization", "")
            api_key_hdr = request.headers.get("X-API-Key", "")
            plain_key = extract_api_key(authorization, api_key_hdr)
            if plain_key:
                user_info = verify_api_key(plain_key)
                if user_info:
                    request.state.user_id = user_info["user_id"]
                    request.state.user_name = user_info["key_name"]
                    request.state.auth_mode = "api_v1"
                    return await call_next(request)
            # Fall through к session-id если есть
            session_id = request.headers.get("X-Session-Id", "") or request.query_params.get("sid", "")
            if license_mgr and session_id:
                session_info = license_mgr.validate(session_id)
                if session_info:
                    request.state.user_id = license_mgr.get_user_id(session_id)
                    request.state.user_name = session_info["user_name"]
                    request.state.auth_mode = "session"
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

    session_id = request.headers.get("X-Session-Id", "") or request.query_params.get("sid", "")
    api_key = request.headers.get("X-API-Key", "")
    authorization = request.headers.get("Authorization", "")

    # API v1 ключи (cvr_*) — проверяем первыми, до старого API_SECRET (cvr_* не совпадёт с глобальным секретом)
    plain_key = extract_api_key(authorization, api_key)
    if plain_key:
        user_info = verify_api_key(plain_key)
        if user_info:
            request.state.user_id = user_info["user_id"]
            request.state.user_name = user_info["key_name"]
            request.state.auth_mode = "api_v1"
            return await call_next(request)
        return JSONResponse(status_code=401, content={"error": "invalid or revoked api key"})

    if license_mgr and session_id:
        session_info = license_mgr.validate(session_id)
        if not session_info:
            return JSONResponse(status_code=401, content={"error": "Сессия истекла. Войдите заново."})
        request.state.user_id = session_info.get("user_id") or "default"
        request.state.user_name = session_info["user_name"]
        request.state.auth_mode = "session"
    elif api_key and API_SECRET and hmac.compare_digest(str(api_key), str(API_SECRET)):
        request.state.user_id = "desktop"
        request.state.user_name = "Desktop Client"
        request.state.auth_mode = "api_key"
    elif not license_mgr and os.getenv("COVER_ALLOW_ANONYMOUS", "") == "1":
        # v1.1.0 security: аноним-доступ только при ЯВНОМ флаге. Иначе fail-CLOSED
        # (раньше при сбое импорта auth.py любой запрос пускался как 'default').
        request.state.user_id = "default"
        request.state.user_name = "User"
        request.state.auth_mode = "none"
    else:
        return JSONResponse(status_code=401, content={"error": "Требуется авторизация"})

    return await call_next(request)


# --------------- Auth ---------------

# Rate-limit на парольный вход (анти-брутфорс)
_login_attempts: dict = {}   # ip → [timestamps неудачных попыток]
_login_lock = threading.Lock()
_LOGIN_MAX_FAILS = int(os.environ.get("COVER_LOGIN_MAX_FAILS", "10"))
_LOGIN_WINDOW = int(os.environ.get("COVER_LOGIN_WINDOW", "300"))  # 5 мин


def _login_throttled(ip: str) -> bool:
    now = time.time()
    with _login_lock:
        att = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW]
        _login_attempts[ip] = att
        return len(att) >= _LOGIN_MAX_FAILS


def _login_record_fail(ip: str):
    with _login_lock:
        _login_attempts.setdefault(ip, []).append(time.time())


def _login_record_success(ip: str):
    with _login_lock:
        _login_attempts.pop(ip, None)


def _client_ip(request: Request) -> str:
    """Доверяем proxy-заголовку только от локального reverse proxy."""
    direct_ip = request.client.host if request.client else "unknown"
    if direct_ip in ("127.0.0.1", "::1"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip() or direct_ip
    return direct_ip


# --------------- Аккаунты (v2.0, порт из Rewrite Studio) ---------------

@app.get("/auth/status")
async def auth_status():
    """Публично: нужен ли первичный setup (users пуста) — UI решает, какой экран показать."""
    if not license_mgr:
        return {"needs_setup": False, "auth": "disabled"}
    return {"needs_setup": not license_mgr.has_users()}


@app.post("/auth/setup")
async def auth_setup(request: Request):
    """Одноразовое создание ПЕРВОГО (админ) аккаунта. Работает только пока нет ни одного юзера."""
    if not license_mgr:
        return JSONResponse(status_code=503, content={"error": "Авторизация не настроена"})
    ip = _client_ip(request)
    if _login_throttled(ip):
        return JSONResponse(status_code=429, content={"error": "Слишком много попыток. Подождите несколько минут."})
    body = await request.json()
    success, session_id_or_error, user_name = license_mgr.setup_admin(
        body.get("username", ""), body.get("password", ""), ip)
    if success:
        _login_record_success(ip)
        user_id = license_mgr.get_user_id(session_id_or_error) or ""
        return {"session_id": session_id_or_error, "user_name": user_name, "user_id": user_id}
    _login_record_fail(ip)
    return JSONResponse(status_code=403, content={"error": session_id_or_error})


@app.post("/auth/login-password")
async def auth_login_password(request: Request):
    """Вход по логину/паролю. Пароль не логируется."""
    if not license_mgr:
        return JSONResponse(status_code=503, content={"error": "Авторизация не настроена"})
    ip = _client_ip(request)
    if _login_throttled(ip):
        return JSONResponse(status_code=429, content={"error": "Слишком много попыток входа. Подождите несколько минут."})
    body = await request.json()
    success, session_id_or_error, user_name = license_mgr.login_password(
        body.get("username", ""), body.get("password", ""), ip)
    if success:
        _login_record_success(ip)
        user_id = license_mgr.get_user_id(session_id_or_error) or ""
        return {"session_id": session_id_or_error, "user_name": user_name, "user_id": user_id}
    _login_record_fail(ip)
    return JSONResponse(status_code=403, content={"error": session_id_or_error})


@app.post("/auth/claim-password")
async def auth_claim_password(request: Request):
    """Одноразовая установка пароля по своему логину (без старого пароля).

    Открыто только для аккаунтов, которым админ поднял pw_reset_pending
    (POST /auth/require-password-reset). Успех сразу логинит и гасит флаг.
    """
    if not license_mgr:
        return JSONResponse(status_code=503, content={"error": "Авторизация не настроена"})
    ip = _client_ip(request)
    if _login_throttled(ip):
        return JSONResponse(status_code=429, content={"error": "Слишком много попыток. Подождите несколько минут."})
    body = await request.json()
    success, session_id_or_error, user_name = license_mgr.claim_password(
        body.get("username", ""), body.get("new_password", ""), ip)
    if success:
        _login_record_success(ip)
        user_id = license_mgr.get_user_id(session_id_or_error) or ""
        return {"session_id": session_id_or_error, "user_name": user_name, "user_id": user_id}
    _login_record_fail(ip)
    return JSONResponse(status_code=403, content={"error": session_id_or_error})


def _require_admin(request: Request):
    from cover_auth import ADMIN_USER_ID
    if getattr(request.state, "user_id", "") != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="Только для администратора")


# Провайдеры, которые тратят личный счёт владельца, а не общий пул сервиса.
ADMIN_ONLY_PROVIDERS = {"grok_api"}


def _is_admin_only_provider(value) -> bool:
    return str(value or "").strip().lower() in ADMIN_ONLY_PROVIDERS


@app.post("/auth/require-password-reset")
async def auth_require_password_reset(request: Request):
    """Админ: выдать (или отозвать) право одноразово задать пароль по логину.

    body: {"usernames": ["user_alpha", "user_beta"]} либо {"all": true}; "cancel": true — отозвать.
    """
    _require_admin(request)
    body = await request.json()
    usernames = body.get("usernames")
    if not usernames and not body.get("all"):
        return JSONResponse(status_code=400, content={"error": "Укажите usernames или all: true"})
    if usernames is not None and not isinstance(usernames, list):
        return JSONResponse(status_code=400, content={"error": "usernames должен быть списком"})
    affected = license_mgr.set_password_reset_pending(
        None if body.get("all") else usernames,
        pending=not body.get("cancel"))
    return {"status": "ok", "affected": affected, "pending": not body.get("cancel")}


@app.get("/admin/gen-settings")
def admin_get_gen_settings(request: Request):
    _require_admin(request)
    return _admin_gen_settings_payload()


def _admin_gen_settings_payload() -> dict:
    env_map = {
        "max_parallel_jobs": "COVER_JOB_SLOTS",
        "max_threads_per_job": "COVER_MAX_THREADS_PER_JOB",
        "max_generation_retries": "COVER_MAX_GENERATION_RETRIES",
        "queue_limit_per_user": "COVER_MAX_PENDING",
        "fastgen_job_slots": "COVER_FASTGEN_JOB_SLOTS",
        "veo_job_slots": "COVER_VEO_JOB_SLOTS",
        "gpt_image2_job_slots": "COVER_GPT_IMAGE2_JOB_SLOTS",
        "fastgen_weighted_budget_per_key": "COVER_FASTGEN_WEIGHTED_BUDGET",
        "veo_inflight_per_key": "COVER_VEO_INFLIGHT",
        "gpt_image2_inflight_per_key": "COVER_GPT_IMAGE2_INFLIGHT",
    }
    active_worker = globals().get("worker")
    active_slots = (
        int(active_worker._max_concurrent)
        if active_worker is not None
        else _server_job_slots()
    )
    active_provider_limits = (
        dict(active_worker._provider_limits)
        if active_worker is not None
        else _server_provider_job_limits()
    )
    return {
        "settings": gen_settings(),
        "effective": {
            "max_parallel_jobs": active_slots,
            "max_threads_per_job": _server_max_threads(),
            "max_generation_retries": _server_max_retries(),
            "queue_limit_per_user": _server_queue_limit(),
            "provider_job_slots": active_provider_limits,
            "fastgen_weighted_budget_per_key": FASTGEN_WEIGHTED_BUDGET_LIMIT,
            "veo_inflight_per_key": VEO_INFLIGHT_LIMIT,
            "gpt_image2_inflight_per_key": GPT_IMAGE2_INFLIGHT_LIMIT,
            "credential_pool": credential_pool_summary(),
        },
        "env_overrides": {
            field: bool(os.environ.get(env_name, ""))
            for field, env_name in env_map.items()
        },
        "restart_required": [
            "max_parallel_jobs",
            "provider_job_slots",
            "fastgen_weighted_budget_per_key",
            "veo_inflight_per_key",
            "gpt_image2_inflight_per_key",
            "credential_pool",
        ],
    }


@app.post("/admin/gen-settings")
async def admin_set_gen_settings(request: Request):
    _require_admin(request)
    body = await request.json()
    cur = dict(gen_settings())
    for k in ("max_parallel_jobs", "max_threads_per_job", "max_generation_retries", "queue_limit_per_user"):
        if body.get(k) is not None:
            cur[k] = max(1, min(100, int(body[k])))
    tmp = GEN_SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, GEN_SETTINGS_PATH)
    payload = _admin_gen_settings_payload()
    payload["status"] = "ok"
    return payload


@app.post("/auth/change-password")
async def auth_change_password(request: Request):
    """Смена пароля: свой — с old_password; админ меняет любому без старого."""
    body = await request.json()
    target = (body.get("username") or "").strip()
    if not target:
        return JSONResponse(status_code=400, content={"error": "username обязателен"})
    from cover_auth import ADMIN_USER_ID
    me_uid = getattr(request.state, "user_id", "")
    acc = next((a for a in license_mgr.list_accounts() if a["username"] == target), None)
    if not acc:
        return JSONResponse(status_code=404, content={"error": "Пользователь не найден"})
    if me_uid == ADMIN_USER_ID:
        ok, err = license_mgr.change_password(target, body.get("new_password", ""), require_old=False)
    elif acc["user_id"] == me_uid:
        ok, err = license_mgr.change_password(
            target, body.get("new_password", ""), body.get("old_password", ""), require_old=True)
    else:
        raise HTTPException(status_code=403, detail="Можно менять только свой пароль")
    if ok:
        return {"status": "ok"}
    return JSONResponse(status_code=400, content={"error": err})


@app.get("/auth/accounts")
def auth_list_accounts(request: Request):
    _require_admin(request)
    return {"accounts": license_mgr.list_accounts()}


@app.post("/auth/accounts")
async def auth_create_account(request: Request):
    _require_admin(request)
    body = await request.json()
    success, err = license_mgr.create_account(
        body.get("username", ""), body.get("password", ""), body.get("user_name", ""))
    if success:
        return {"status": "ok"}
    return JSONResponse(status_code=400, content={"error": err})


@app.delete("/auth/accounts/{username}")
def auth_delete_account(username: str, request: Request):
    _require_admin(request)
    if license_mgr.delete_account(username):
        return {"status": "ok"}
    return JSONResponse(status_code=400, content={"error": "Нельзя удалить (не найден или последний админ)"})


@app.post("/auth/ping")
async def auth_ping(request: Request):
    """«Я ещё в системе?» — теперь на это есть два способа ответить «да».

    Путь публичный (иначе экран входа не смог бы спросить), поэтому единый вход
    проверяется здесь явно: middleware для публичных путей состояние не заполняет.
    """
    if not license_mgr:
        return {"status": "ok"}
    if cabinet_user(request):                 # ДОБАВЛЕНО 04.09.2026: единый вход
        return {"status": "ok", "via": "cabinet"}
    session_id = request.headers.get("X-Session-Id", "")
    if session_id and license_mgr.ping(session_id):
        return {"status": "ok"}
    return JSONResponse(status_code=401, content={"error": "Сессия истекла"})


@app.post("/auth/logout")
async def auth_logout(request: Request):
    if license_mgr:
        session_id = request.headers.get("X-Session-Id", "")
        if session_id:
            license_mgr.logout(session_id)
    return {"status": "ok"}


# --------------- Models ---------------

class CoverJobRequest(BaseModel):
    urls: list = []
    languages: list
    preset_name: str = ""
    preset_data: dict = {}
    refs_per_language: dict = {}
    # v1.0.4 title-mode
    mode: str = "url"             # url | title | seed
    titles_per_lang: dict = {}    # {lang_display: title_text}
    extra_prompt: str = ""
    # seed-mode: механизм «пресет+семя → N обложек» (niche-agnostic)
    seeds_per_lang: dict = {}     # {lang_display: seed_block_text}
    n_variants: int = Field(3, ge=1, le=8)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_vendor_keys(cls, value):
        data = dict(value or {})
        secret_fields = ("openai_api_key", "fast_gen_api_key", "veo_api_key")
        if any(str(data.get(name) or "").strip() for name in secret_fields):
            raise ValueError(
                "Vendor API keys больше не принимаются: используются только серверные ключи."
            )
        # Empty legacy fields and old client-side scheduling knobs are harmless,
        # but are removed before validation and never influence execution.
        for name in secret_fields + ("max_threads", "parallel"):
            data.pop(name, None)
        return data


def _get_user_id(request: Request) -> str:
    return getattr(request.state, "user_id", "default")


# --------------- Frontend ---------------

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Cover Server</h1><p>index.html не найден в static/</p>")


_STATIC_ASSET_SUFFIXES = {
    ".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".svg", ".ico", ".woff", ".woff2", ".ttf",
}


@app.get("/static/{asset_path:path}", include_in_schema=False)
def serve_static_asset(asset_path: str):
    """Allowlisted static assets only; editor/backups are never web-readable."""
    relative = Path(asset_path)
    if (
        not asset_path
        or relative.is_absolute()
        or ".." in relative.parts
        or any(".bak" in part.lower() for part in relative.parts)
    ):
        raise HTTPException(status_code=404, detail="static asset not found")
    if asset_path != "index.html" and relative.suffix.lower() not in _STATIC_ASSET_SUFFIXES:
        raise HTTPException(status_code=404, detail="static asset not found")
    base = STATIC_DIR.resolve()
    candidate = (base / relative).resolve()
    if (
        not candidate.is_relative_to(base)
        or not candidate.exists()
        or not candidate.is_file()
    ):
        raise HTTPException(status_code=404, detail="static asset not found")
    return FileResponse(str(candidate))


# --------------- Data ---------------

@app.get("/languages")
def get_languages():
    from cover_core import LANG_DISPLAY_TO_CODE
    return [{"display": d, "code": c} for d, c in LANG_DISPLAY_TO_CODE.items()]


@app.get("/presets")
def get_presets(request: Request):
    user_id = _get_user_id(request)
    mgr = _get_preset_manager(user_id)
    return {name: asdict(preset) for name, preset in mgr._presets.items()}


@app.post("/presets/sync")
def sync_presets(presets: dict, request: Request):
    user_id = _get_user_id(request)
    data = presets.get("presets", presets)
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="presets: ожидается объект { имя: пресет }")
    mgr = _get_preset_manager(user_id)
    valid = {f.name for f in dc_fields(Preset)}
    new_presets = {}
    for name, p in data.items():
        if not isinstance(p, dict):
            continue
        kwargs = {k: v for k, v in p.items() if k in valid}
        try:
            kwargs["generation_provider"] = canonical_generation_provider(
                kwargs.get("generation_provider") or "flow"
            )
            new_presets[name] = Preset(**kwargs)
        except (TypeError, ValueError, InvalidGenerationProvider) as e:
            raise HTTPException(status_code=400, detail=f"Пресет '{name}': {e}")
    mgr._presets = new_presets
    mgr.save()
    return {"status": "ok"}


@app.get("/prompts")
def get_prompts(request: Request):
    """Промпты текущего пользователя."""
    user_id = _get_user_id(request)
    path = _user_prompts_path(user_id)
    custom = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                custom = json.load(f)
        except Exception:
            pass
    return {key: custom.get(key) or get_default_prompt(key) for key in PROMPTS_KEYS}


@app.post("/prompts/sync")
def sync_prompts(prompts: dict, request: Request):
    """Сохранить промпты пользователя на сервер (без глобального применения — промпты загружаются per-job)."""
    user_id = _get_user_id(request)
    data = prompts.get("prompts", prompts)
    path = _user_prompts_path(user_id)
    merged = {key: (data.get(key) if key in data else get_default_prompt(key)) for key in PROMPTS_KEYS}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return {"status": "ok"}


# --------------- Jobs ---------------

# v1.1.0 security/anti-cost: лимиты размера задачи и очереди (анти cost-drain / DoS)
MAX_URLS_PER_JOB = int(os.environ.get("COVER_MAX_URLS", "50"))
MAX_LANGUAGES_PER_JOB = int(os.environ.get("COVER_MAX_LANGUAGES", "10"))
MAX_TASKS_PER_JOB = int(os.environ.get("COVER_MAX_TASKS", "200"))


def _enforce_job_limits(user_id: str, n_urls: int, n_langs: int, n_titles: int):
    """v1.1.0: режем огромные задачи и затопление очереди до постановки в очередь."""
    if n_urls > MAX_URLS_PER_JOB:
        raise HTTPException(status_code=422, detail=f"too many urls: {n_urls} (max {MAX_URLS_PER_JOB})")
    if n_langs > MAX_LANGUAGES_PER_JOB:
        raise HTTPException(status_code=422, detail=f"too many languages: {n_langs} (max {MAX_LANGUAGES_PER_JOB})")
    total = max(n_urls, n_titles, 1) * max(n_langs, 1)
    if total > MAX_TASKS_PER_JOB:
        raise HTTPException(status_code=422, detail=f"job too large: {total} tasks (max {MAX_TASKS_PER_JOB})")
    pending = queue.count_reserved_for_user(user_id)
    _qlim = _server_queue_limit()
    if pending >= _qlim:
        raise HTTPException(
            status_code=429,
            detail=(
                f"too many pending/resetting jobs ({pending}); "
                f"технический максимум {_qlim}"
            ),
        )


@app.post("/jobs/cover")
def create_cover_job(req: CoverJobRequest, request: Request):
    user_id = _get_user_id(request)
    _mode_early = (req.mode or "url").strip().lower()
    n_urls, n_langs = len(req.urls or []), len(req.languages or [])
    # для лимитов/квоты — «единиц» на язык: seed → n_variants (вариантов/язык), title → тайтлов, url → n/д.
    if _mode_early == "seed":
        n_titles = max(1, min(8, int(req.n_variants or 1)))
    else:
        n_titles = len(req.titles_per_lang or {})
    _enforce_job_limits(user_id, n_urls, n_langs, n_titles)
    user_output = _get_user_output_dir(user_id)
    preset = None
    if req.preset_data:
        valid = {f.name for f in dc_fields(Preset)}
        preset = Preset(**{k: v for k, v in req.preset_data.items() if k in valid})
    if not preset and req.preset_name:
        preset = _get_preset_manager(user_id).get(req.preset_name)
    if not preset:
        preset = Preset(name=req.preset_name or "default")
    # Прямой xAI платит личный счёт владельца, поэтому доступен только админу.
    # Проверяем на сервере, а не только прятанием пункта в интерфейсе: подставить
    # provider в запрос может кто угодно.
    if _is_admin_only_provider(getattr(preset, "generation_provider", "")):
        _require_admin(request)
    # v1.0.4: url / title / seed роутинг
    mode = (req.mode or "url").strip().lower()
    if mode not in ("url", "title", "seed"):
        raise HTTPException(status_code=422, detail=f"mode must be 'url', 'title' or 'seed', got {req.mode!r}")
    seeds_json = "{}"
    n_variants_val = 3
    if mode == "title":
        if not req.titles_per_lang:
            raise HTTPException(status_code=422, detail="titles_per_lang required for mode=title")
        if req.urls:
            raise HTTPException(status_code=422, detail="urls only allowed for mode=url")
        # Каждый язык должен иметь титул
        title_keys = set(req.titles_per_lang.keys())
        lang_set = set(req.languages)
        missing = lang_set - title_keys
        extra = title_keys - lang_set
        if missing or extra:
            raise HTTPException(
                status_code=422,
                detail=f"titles_per_lang keys must match languages. missing: {sorted(missing)}, extra: {sorted(extra)}",
            )
        if len(req.extra_prompt) > 2000:
            raise HTTPException(status_code=422, detail="extra_prompt too long (max 2000 chars)")
        urls_json = "[]"
        titles_json = json.dumps(req.titles_per_lang, ensure_ascii=False)
        extra_prompt = req.extra_prompt
    elif mode == "seed":
        # seed-mode: механизм «пресет+семя → N обложек». Семя (блок ХУКИ+ЯКОРЬ) на каждый язык.
        if not req.seeds_per_lang:
            raise HTTPException(status_code=422, detail="seeds_per_lang required for mode=seed")
        if req.urls:
            raise HTTPException(status_code=422, detail="urls only allowed for mode=url")
        if req.titles_per_lang:
            raise HTTPException(status_code=422, detail="titles_per_lang only allowed for mode=title")
        seed_keys = set(req.seeds_per_lang.keys())
        lang_set = set(req.languages)
        extra = seed_keys - lang_set
        missing = lang_set - seed_keys
        # Ключи семян должны ТОЧНО совпадать с языками — иначе квота (n_langs×N) разойдётся
        # с фактической генерацией (len(seeds)×N): переплата + недодача. Симметрично title-mode.
        if extra or missing:
            raise HTTPException(
                status_code=422,
                detail=f"seeds_per_lang keys must match languages. missing: {sorted(missing)}, extra: {sorted(extra)}",
            )
        empty = sorted([k for k, v in req.seeds_per_lang.items() if not (str(v or "").strip())])
        if empty:
            raise HTTPException(status_code=422, detail=f"seeds_per_lang has empty seed(s) for: {empty}")
        urls_json = "[]"
        titles_json = "{}"
        extra_prompt = ""
        seeds_json = json.dumps(req.seeds_per_lang, ensure_ascii=False)
        n_variants_val = max(1, min(8, int(req.n_variants or 3)))
    else:
        if not req.urls:
            raise HTTPException(status_code=422, detail="urls required for mode=url")
        if req.titles_per_lang:
            raise HTTPException(status_code=422, detail="titles_per_lang only allowed for mode=title")
        urls_json = json.dumps(req.urls, ensure_ascii=False)
        titles_json = "{}"
        extra_prompt = ""

    job = Job(
        urls=urls_json,
        preset_data=json.dumps(asdict(preset), ensure_ascii=False),
        output_dir=str(user_output),
        languages=json.dumps(req.languages, ensure_ascii=False),
        refs_per_language=json.dumps(req.refs_per_language or {}, ensure_ascii=False),
        mode=mode,
        titles_per_lang=titles_json,
        extra_prompt=extra_prompt,
        seeds_per_lang=seeds_json,
        n_variants=n_variants_val,
        max_threads=_server_max_threads(),
        parallel=False,
        user_id=user_id,
        generation_provider=getattr(preset, "generation_provider", "") or "flow",
    )
    # Admission до enqueue: не создаём заведомо падающую job.
    ensure_server_keys_for_job(job)
    try:
        job_id = queue.add_job(
            job,
            max_pending_for_user=_server_queue_limit(),
        )
    except PendingJobLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail=(
                f"too many pending/resetting jobs ({exc.pending}); "
                f"технический максимум {exc.limit}"
            ),
        ) from exc
    return {"job_id": job_id, "status": "pending"}


# --------------- Живая очередь (виджет) ---------------

# Режим показа числа людей: exact — точное число, bucket — «1» / «2-3» / «4-9» / «10+».
# Подпись считает сервер, клиент рисует готовую строку и о режиме не знает.
PULSE_PEOPLE_MODE = "exact"
_PULSE_TTL = 3.0
_PULSE_CACHE: dict = {"ts": 0.0, "rows": None}
_PULSE_LOCK = threading.Lock()
# Служебные user_id, которые подставляет middleware: 'desktop' — глобальный API_SECRET,
# 'default' — COVER_ALLOW_ANONYMOUS. Их задачи в очереди считаем, живыми людьми — нет.
_PULSE_SERVICE_USERS = ("desktop", "default", "")


def _pulse_people_label(people: int) -> str:
    if PULSE_PEOPLE_MODE != "bucket":
        return str(people)
    if people <= 1:
        return str(max(0, people))
    if people <= 3:
        return "2-3"
    if people <= 9:
        return "4-9"
    return "10+"


def _pulse_rows():
    """Глобальный снимок активных строк с TTL-кэшем (одинаков для всех юзеров)."""
    now = time.time()
    with _PULSE_LOCK:
        rows = _PULSE_CACHE.get("rows")
        age = now - float(_PULSE_CACHE.get("ts") or 0.0)
        if rows is not None and age < _PULSE_TTL:
            return rows, age
    rows = queue.queue_pulse()
    with _PULSE_LOCK:
        _PULSE_CACHE["rows"] = rows
        _PULSE_CACHE["ts"] = now
    return rows, 0.0


def _pulse_payload(waiting, running, people, mine_waiting, mine_running,
                   cache_age, degraded):
    return {
        "service": "cover",
        "ts": time.time(),
        "cache_age": round(float(cache_age), 3),
        # Механизма паузы очереди в cover нет — всегда false.
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
            "ahead": None,
            "eta_sec": None,
            "eta_approx": True,
        },
        "lanes": [],
        "health": {"worker": "ok", "note": ""},
        "degraded": degraded,
    }


@app.get("/api/queue/pulse")
def queue_pulse(request: Request):
    """Живая очередь для виджета. Наружу уходят только целые числа-агрегаты
    и собственные числа юзера — ни логинов, ни id, ни названий задач.
    Ошибка БД → 200 с degraded:true и нулями, а не 500 (виджет не должен ронять страницу)."""
    user_id = _get_user_id(request)
    try:
        rows, cache_age = _pulse_rows()
    except Exception:
        return _pulse_payload(0, 0, 0, 0, 0, 0.0, True)

    waiting = running = mine_waiting = mine_running = 0
    people = set()
    for status, uid, cancel_requested in rows:
        if status == JobStatus.RUNNING.value:
            running += 1
            is_waiting = False
        elif status == JobStatus.RESETTING.value:
            waiting += 1
            is_waiting = True
        elif status == JobStatus.PENDING.value and not cancel_requested:
            # именно так pending видит шедулер (get_pending_providers)
            waiting += 1
            is_waiting = True
        else:
            continue
        if uid not in _PULSE_SERVICE_USERS:
            people.add(uid)
        if uid == user_id:
            if is_waiting:
                mine_waiting += 1
            else:
                mine_running += 1

    return _pulse_payload(
        waiting, running, len(people), mine_waiting, mine_running,
        cache_age, False,
    )


@app.get("/jobs")
def list_jobs(request: Request, page: int = 1, page_size: int = 20, status: str = ""):
    """Список задач с пагинацией.
    page: 1-based номер страницы
    page_size: количество задач на странице (макс 100)
    status: опциональный фильтр по статусу
    Возвращает {items, total, page, page_size, total_pages}.
    Для обратной совместимости старых клиентов: если page и page_size не переданы — возвращает плоский список.
    """
    user_id = _get_user_id(request)
    jobs = queue.get_all_jobs()
    user_jobs = [j for j in jobs if _job_belongs_to_user(j, user_id)]
    if status:
        user_jobs = [j for j in user_jobs if j.status == status]
    # Свежие сверху
    user_jobs.sort(key=lambda j: getattr(j, "created_at", 0) or 0, reverse=True)

    def serialize(j):
        # v1.3.0: log исключён из списка (грузился для всех джоб → память/латентность).
        # Полный лог доступен в GET /jobs/{id}.
        return {
            "id": j.id,
            "status": j.status,
            "progress": j.progress_percent,
            "detail": j.progress_detail,
            "results_count": getattr(j, "results_count", 0),
            "parallel": getattr(j, "parallel", False),
            "created_at": j.created_at,
        }

    # Если клиент явно не запросил пагинацию — старый формат
    qp = request.query_params
    if "page" not in qp and "page_size" not in qp:
        return [serialize(j) for j in user_jobs]

    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    total = len(user_jobs)
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = (page - 1) * page_size
    end = start + page_size
    page_items = user_jobs[start:end]
    return {
        "items": [serialize(j) for j in page_items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


def _job_public_dict(job: Job) -> dict:
    """Allowlist публичных полей. Новое внутреннее поле не утечёт автоматически."""
    safe_fields = (
        "id", "status", "created_at", "started_at", "finished_at",
        "mode", "languages", "n_variants",
        "progress_percent", "progress_detail", "log", "error",
        "results_count", "output_files", "fallback_files",
    )
    return {name: getattr(job, name) for name in safe_fields}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    return _job_public_dict(job)


@app.get("/jobs/{job_id}/files")
def get_job_files(job_id: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    return json.loads(job.output_files or "[]")


@app.get("/jobs/{job_id}/files/{filename:path}")
def download_file(job_id: str, filename: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    output_dir = Path(job.output_dir or OUTPUT_DIR)
    # v1.1.0 security: запрет выхода за пределы папки результатов (path traversal).
    if os.path.isabs(filename) or ".." in Path(filename).parts:
        raise HTTPException(status_code=403, detail="invalid path")
    base = output_dir.resolve()
    path = (output_dir / filename).resolve()
    if not path.is_relative_to(base):
        raise HTTPException(status_code=403, detail="access denied")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(path), filename=os.path.basename(filename))


@app.get("/jobs/{job_id}/download")
def download_job_archive(job_id: str, request: Request):
    """Скачать все файлы задачи одним ZIP-архивом. Внутри ZIP — папка с именем пресета+дата."""
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    output_dir = Path(job.output_dir or OUTPUT_DIR)
    files = json.loads(job.output_files or "[]")
    if not files:
        raise HTTPException(status_code=404, detail="no files")

    # Имя архива и папки внутри: preset_name + дата
    preset_name = ""
    try:
        pd = json.loads(job.preset_data or "{}")
        preset_name = re.sub(r'[^\w\-]', '_', (pd.get("name") or "").strip())
    except Exception:
        pass
    from datetime import datetime as _dt
    date_str = _dt.fromtimestamp(job.created_at).strftime("%Y-%m-%d") if job.created_at else _dt.now().strftime("%Y-%m-%d")
    if preset_name:
        archive_name = f"{preset_name}_{date_str}"
    else:
        archive_name = f"covers_{date_str}"
    folder_in_zip = archive_name

    # v1.3.0: пишем ZIP во временный файл и стримим через FileResponse —
    # не держим весь архив в памяти (раньше getvalue() = OOM на больших задачах).
    import tempfile
    from starlette.background import BackgroundTask
    base = output_dir.resolve()
    tmp = tempfile.NamedTemporaryFile(prefix="cover_zip_", suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_path in files:
                path = (output_dir / rel_path).resolve()
                # containment: только файлы внутри папки результатов
                if path.is_relative_to(base) and path.exists() and path.is_file():
                    zf.write(path, arcname=f"{folder_in_zip}/{rel_path}")
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=f"{archive_name}.zip",
        background=BackgroundTask(lambda: os.path.exists(tmp_path) and os.remove(tmp_path)),
    )


# --------------- Избранное ---------------

_FAV_LOCK = threading.Lock()


def _favorites_dir(user_id: str) -> Path:
    fav_dir = Path(_get_user_output_dir(user_id)) / "_favorites"
    fav_dir.mkdir(parents=True, exist_ok=True)
    return fav_dir


def _favorites_index_path(user_id: str) -> Path:
    return _favorites_dir(user_id) / "index.json"


def _load_favorites(user_id: str) -> list:
    try:
        with open(_favorites_index_path(user_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_favorites(user_id: str, items: list):
    # Атомарная запись: tmp-файл + os.replace (либо новый целиком, либо старый).
    path = _favorites_index_path(user_id)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(path))


class FavoriteRequest(BaseModel):
    path: str = ""


@app.post("/jobs/{job_id}/favorites")
def add_favorite(job_id: str, req: FavoriteRequest, request: Request):
    """Добавить файл результата в избранное (копия в outputs/<user>/_favorites). Идемпотентно."""
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    rel_path = (req.path or "").strip()
    if not rel_path:
        raise HTTPException(status_code=400, detail="path required")
    # Валидация пути — как в download_file: запрет абсолютных и '..', containment в базе.
    if os.path.isabs(rel_path) or ".." in Path(rel_path).parts:
        raise HTTPException(status_code=400, detail="invalid path")
    output_dir = Path(job.output_dir or OUTPUT_DIR)
    base = output_dir.resolve()
    src = (output_dir / rel_path).resolve()
    if not src.is_relative_to(base):
        raise HTTPException(status_code=400, detail="invalid path")
    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    basename = src.name
    # lang: префикс имени файла до первого '_' (ru_..., en_...)
    prefix = basename.split("_", 1)[0]
    lang = prefix if re.fullmatch(r"[a-z]{2,3}", prefix) else ""
    preset_name = ""
    try:
        preset_name = json.loads(job.preset_data or "{}").get("name", "") or ""
    except Exception:
        pass
    mode = getattr(job, "mode", "") or ""
    # final_prompt — best-effort из meta.json (только seed-mode); любая ошибка → ""
    final_prompt = ""
    try:
        if mode == "seed" and lang:
            meta_path = src.parent / "meta.json"
            if meta_path.resolve().is_relative_to(base) and meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                for lang_display, info in (meta.get("languages") or {}).items():
                    if get_lang_code(lang_display) == lang:
                        final_prompt = (info or {}).get("final_prompt", "") or ""
                        break
    except Exception:
        final_prompt = ""

    with _FAV_LOCK:
        items = _load_favorites(user_id)
        # Идемпотентность: повторный POST той же пары (job_id, path) возвращает существующую запись.
        for it in items:
            if it.get("source_job_id") == job_id and it.get("source_path") == rel_path:
                return it
        fav_id = secrets.token_hex(4)
        stored_name = f"{fav_id}_{basename}"
        shutil.copy2(str(src), str(_favorites_dir(user_id) / stored_name))
        record = {
            "id": fav_id,
            "filename": stored_name,
            "source_job_id": job_id,
            "source_path": rel_path,
            "lang": lang,
            "preset": preset_name,
            "mode": mode,
            "final_prompt": final_prompt,
            "created_at": time.time(),
        }
        items.append(record)
        _save_favorites(user_id, items)
    return record


@app.get("/favorites")
def list_favorites(request: Request):
    user_id = _get_user_id(request)
    items = _load_favorites(user_id)
    items.sort(key=lambda it: it.get("created_at", 0) or 0, reverse=True)
    return {"items": items}


@app.get("/favorites/download")
def download_favorites_archive(request: Request, ids: str = ""):
    """Скачать избранное одним ZIP. ?ids=a,b,c — только указанные; без ids — все."""
    user_id = _get_user_id(request)
    items = _load_favorites(user_id)
    wanted = {s.strip() for s in ids.split(",") if s.strip()}
    if wanted:
        items = [it for it in items if it.get("id") in wanted]
    fav_dir = _favorites_dir(user_id)
    base = fav_dir.resolve()
    from datetime import datetime as _dt
    folder_in_zip = f"favorites_{_dt.now().strftime('%Y-%m-%d')}"

    import tempfile
    from starlette.background import BackgroundTask
    tmp = tempfile.NamedTemporaryFile(prefix="cover_fav_zip_", suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()
    added = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for it in items:
                path = (fav_dir / (it.get("filename") or "")).resolve()
                # containment + отсутствующие на диске пропускаем
                if path.is_relative_to(base) and path.exists() and path.is_file():
                    zf.write(path, arcname=f"{folder_in_zip}/{it.get('filename')}")
                    added += 1
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise
    if not added:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise HTTPException(status_code=404, detail="no files")
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=f"{folder_in_zip}.zip",
        background=BackgroundTask(lambda: os.path.exists(tmp_path) and os.remove(tmp_path)),
    )


@app.get("/favorites/{fav_id}/file")
def get_favorite_file(fav_id: str, request: Request):
    user_id = _get_user_id(request)
    items = _load_favorites(user_id)
    rec = next((it for it in items if it.get("id") == fav_id), None)
    if not rec:
        raise HTTPException(status_code=404, detail="favorite not found")
    path = _favorites_dir(user_id) / (rec.get("filename") or "")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    # Без filename= → inline, чтобы <img>/лайтбокс показывали картинку, а не скачивали.
    return FileResponse(str(path))


@app.delete("/favorites/{fav_id}")
def delete_favorite(fav_id: str, request: Request):
    user_id = _get_user_id(request)
    with _FAV_LOCK:
        items = _load_favorites(user_id)
        rec = next((it for it in items if it.get("id") == fav_id), None)
        if not rec:
            raise HTTPException(status_code=404, detail="favorite not found")
        try:
            (_favorites_dir(user_id) / (rec.get("filename") or "")).unlink(missing_ok=True)
        except OSError:
            pass
        items = [it for it in items if it.get("id") != fav_id]
        _save_favorites(user_id, items)
    return {"ok": True}


@app.post("/jobs/{job_id}/cancel")
def cancel_cover_job(job_id: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    if queue.cancel_job(job_id):
        return {"status": "cancelled"}
    return {"status": job.status, "detail": "can only cancel pending or running jobs"}


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    if job.status not in (JobStatus.FAILED.value, JobStatus.CANCELLED.value, JobStatus.COMPLETED_PARTIAL.value):
        raise HTTPException(status_code=400, detail="retry only for failed/cancelled/partial")
    previous_status = job.status
    # v1.3.0: НЕ стираем output_files/results_count — skip_existing догенерит только
    # недостающее, а не переплатит за уже готовые обложки (как resume).
    ensure_server_keys_for_job(job)
    job.status = JobStatus.PENDING.value
    job.error = ""
    job.log = ""
    job.progress_percent = 0
    job.progress_detail = ""
    job.finished_at = 0
    job.cancel_requested = False
    try:
        updated = queue.update_job(
            job,
            expected_status=previous_status,
            reset_cancel_requested=True,
            max_pending_for_user=_server_queue_limit(),
        )
    except PendingJobLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail=(
                f"too many pending/resetting jobs ({exc.pending}); "
                f"технический максимум {exc.limit}"
            ),
        ) from exc
    if not updated:
        raise HTTPException(status_code=409, detail="job status changed; refresh and retry")
    return {"status": "pending"}


def _reset_job_fields(job: Job):
    job.error = ""
    job.log = ""
    job.progress_percent = 0
    job.progress_detail = ""
    job.finished_at = 0
    job.cancel_requested = False


def _resume_with_fallback_regeneration(
    job: Job,
    *,
    previous_status: str,
    user_id: str,
) -> int:
    """Crash-safe reset protocol: DB references are removed before files.

    A failed CAS can therefore never delete a file still referenced by the
    authoritative row. Interrupted `resetting` rows are requeued on startup.
    """
    try:
        fallback_list = json.loads(job.fallback_files or "[]")
        output_list = json.loads(job.output_files or "[]")
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"cannot parse fallback metadata: {exc}",
        ) from exc
    if not isinstance(fallback_list, list) or not isinstance(output_list, list):
        raise HTTPException(
            status_code=400,
            detail="cannot clean fallbacks: file metadata must be arrays",
        )

    previous_run_token = str(job.run_token or "")
    reset_token = secrets.token_hex(16)
    job.status = JobStatus.RESETTING.value
    job.run_token = reset_token
    _reset_job_fields(job)
    try:
        updated = queue.update_job(
            job,
            expected_status=previous_status,
            expected_run_token=previous_run_token,
            reset_cancel_requested=True,
            max_pending_for_user=_server_queue_limit(),
        )
    except PendingJobLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail=(
                f"too many pending/resetting jobs ({exc.pending}); "
                f"технический максимум {exc.limit}"
            ),
        ) from exc
    if not updated:
        raise HTTPException(
            status_code=409,
            detail="job status changed; refresh and retry",
        )

    fallback_set = {str(rel) for rel in fallback_list}
    job.output_files = json.dumps(
        [rel for rel in output_list if str(rel) not in fallback_set],
        ensure_ascii=False,
    )
    job.fallback_files = "[]"
    try:
        updated = queue.update_job(
            job,
            expected_status=JobStatus.RESETTING.value,
            expected_run_token=reset_token,
            reset_cancel_requested=True,
            max_pending_for_user=_server_queue_limit(),
        )
    except PendingJobLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail=(
                f"too many pending/resetting jobs ({exc.pending}); "
                f"технический максимум {exc.limit}"
            ),
        ) from exc
    if not updated:
        # No unlink happened: DB still owns every file it references.
        raise HTTPException(
            status_code=409,
            detail="job reset lease changed before cleanup; no files were deleted",
        )

    removed_count = 0
    base = _get_user_output_dir(user_id).resolve()
    for rel in fallback_list:
        try:
            path = (base / str(rel)).resolve()
            if (
                path.is_relative_to(base)
                and path.exists()
                and path.is_file()
            ):
                path.unlink()
                removed_count += 1
        except (OSError, RuntimeError):
            # Metadata is already safe/unreferenced; an orphan can be cleaned later.
            pass

    job.status = JobStatus.PENDING.value
    job.run_token = ""
    try:
        updated = queue.update_job(
            job,
            expected_status=JobStatus.RESETTING.value,
            expected_run_token=reset_token,
            reset_cancel_requested=True,
            max_pending_for_user=_server_queue_limit(),
        )
    except PendingJobLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail=(
                f"too many pending/resetting jobs ({exc.pending}); "
                f"технический максимум {exc.limit}"
            ),
        ) from exc
    if not updated:
        # Files are already unreferenced. Startup recovery safely requeues RESETTING.
        raise HTTPException(
            status_code=409,
            detail="job reset interrupted after cleanup; it will recover on restart",
        )
    return removed_count


@app.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str, request: Request):
    """Вернуть задачу в очередь и догенерировать только недостающие обложки (не перезапуск с нуля).
    Опционально принимает body:
      - override_provider, override_model — сменить модель
      - regenerate_fallbacks: bool — также перегенерить обложки сохранённые через fallback (без текста)
    """
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    has_fallbacks = False
    try:
        has_fallbacks = len(json.loads(job.fallback_files or "[]")) > 0
    except Exception:
        pass
    allowed = (JobStatus.FAILED.value, JobStatus.CANCELLED.value, JobStatus.COMPLETED_PARTIAL.value)
    # Разрешаем resume и для COMPLETED, если там есть fallback-обложки для перегенерации
    if job.status not in allowed and not (job.status == JobStatus.COMPLETED.value and has_fallbacks):
        raise HTTPException(status_code=400, detail="resume only for failed/cancelled/partial (or completed with fallback covers)")
    previous_status = job.status

    # Опции resume
    override_provider = ""
    override_model = ""
    regenerate_fallbacks = False
    try:
        body = await request.json()
        if isinstance(body, dict):
            raw_override_provider = body.get("override_provider")
            if raw_override_provider not in (None, ""):
                try:
                    override_provider = canonical_generation_provider(
                        raw_override_provider
                    )
                except InvalidGenerationProvider as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
            override_model = (body.get("override_model") or "").strip()
            regenerate_fallbacks = bool(body.get("regenerate_fallbacks", False))
    except HTTPException:
        raise
    except Exception:
        pass

    if override_provider:
        try:
            pd = json.loads(job.preset_data or "{}")
            pd["generation_provider"] = override_provider
            pd["generation_model"] = override_model
            job.preset_data = json.dumps(pd, ensure_ascii=False)
            job.generation_provider = override_provider
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid preset_data: {e}")

    # Сначала валидируем effective provider/SIMPLE_MODE_PROVIDER и server key.
    # Только после успешного admission-check разрешено удалять fallback-файлы.
    ensure_server_keys_for_job(job)

    removed_count = 0
    if regenerate_fallbacks:
        removed_count = _resume_with_fallback_regeneration(
            job,
            previous_status=previous_status,
            user_id=user_id,
        )
    else:
        job.status = JobStatus.PENDING.value
        _reset_job_fields(job)
        try:
            updated = queue.update_job(
                job,
                expected_status=previous_status,
                reset_cancel_requested=True,
                max_pending_for_user=_server_queue_limit(),
            )
        except PendingJobLimitExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"too many pending/resetting jobs ({exc.pending}); "
                    f"технический максимум {exc.limit}"
                ),
            ) from exc
        if not updated:
            raise HTTPException(
                status_code=409,
                detail="job status changed; refresh and retry",
            )
    return {
        "status": "pending",
        "override_provider": override_provider,
        "override_model": override_model,
        "regenerate_fallbacks": regenerate_fallbacks,
        "removed_fallback_files": removed_count,
    }


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    queue.delete_job(job_id)
    return {"status": "deleted"}


# --------------- API v1 ---------------

api_v1_router = build_api_router(
    queue=queue,
    get_preset_manager=_get_preset_manager,
    get_user_output_dir=_get_user_output_dir,
    job_belongs_to_user=_job_belongs_to_user,
    resolve_ref_paths=_resolve_ref_paths,
    upload_dir=UPLOAD_DIR,
    output_dir=OUTPUT_DIR,
    queue_limit_resolver=_server_queue_limit,
    max_threads_resolver=_server_max_threads,
)
app.include_router(api_v1_router, prefix="/api/v1")


@app.get("/usage")
def get_usage(request: Request):
    """Deprecated compatibility endpoint: тарифных/дневных квот больше нет."""
    _get_user_id(request)
    return {"mode": "server_keys", "unlimited": True}


# --------------- API keys (session-auth UI) ---------------

@app.post("/api-keys")
async def api_keys_create(request: Request):
    """Создать новый API ключ. Plain key возвращается ОДИН раз."""
    user_id = _get_user_id(request)
    if user_id in ("desktop", "default", ""):
        raise HTTPException(status_code=403, detail="API keys can be created only by authenticated user (session)")
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = (body.get("name") or "").strip() if isinstance(body, dict) else ""
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        result = create_api_key(user_id, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@app.get("/api-keys")
def api_keys_list(request: Request):
    user_id = _get_user_id(request)
    if user_id in ("desktop", "default", ""):
        return {"items": []}
    return {"items": list_api_keys(user_id)}


@app.delete("/api-keys/{key_id}")
def api_keys_revoke(key_id: str, request: Request):
    user_id = _get_user_id(request)
    if user_id in ("desktop", "default", ""):
        raise HTTPException(status_code=403, detail="not allowed for anonymous user")
    ok = revoke_api_key(user_id, key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="key not found or already revoked")
    return {"status": "revoked", "key_id": key_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("COVER_PORT", "8900")))
