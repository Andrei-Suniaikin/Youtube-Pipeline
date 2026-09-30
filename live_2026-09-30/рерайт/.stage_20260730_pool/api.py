# -*- coding: utf-8 -*-
"""
api.py — REST API v1 для Rewrite Studio.

Все routes под /api/v1/* защищены api-key auth (см. api_auth.get_api_user).
Сосуществует со старыми session-based routes — никаких breaking changes.

Endpoints:
  POST   /api/v1/files                          — upload входного файла
  POST   /api/v1/jobs                           — создать rewrite-job
  GET    /api/v1/jobs                           — список jobs юзера (paginated)
  GET    /api/v1/jobs/{id}                      — детали + progress_by_lang
  DELETE /api/v1/jobs/{id}                      — отменить job (graceful)
  GET    /api/v1/jobs/{id}/files/{lang_code}    — скачать результат (text/plain)
  POST   /api/v1/jobs/{id}/retry?full=false     — повторить (full=true → с нуля)
"""
import json
import os
import secrets
import time
from dataclasses import asdict
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api_auth import get_api_user
from job_queue import Job, JobQueue, JobStatus, JobType
from rewrite_core import (
    LANGUAGES,
    lang_name_to_code,
    ADMIN_USER_ID,
    get_allowed_openai_models,
    get_allowed_claude_models,
    get_allowed_codex_models,
    get_keys_config,
    get_server_openai_keys,
    get_server_claude_oauth_tokens,
    classify_provider,
    provider_user_allowed,
    redact_server_credentials,
)

# --- Конфиг ---
MAX_UPLOAD_SIZE_BYTES = 50 * 1024 * 1024     # 50 MB (как в существующем /upload)
TERMINAL_STATUSES = {
    JobStatus.COMPLETED.value, JobStatus.FAILED.value,
    JobStatus.CANCELLED.value, JobStatus.PARTIAL.value, "interrupted",
}
DOWNLOADABLE_STATUSES = {JobStatus.COMPLETED.value, JobStatus.PARTIAL.value, "interrupted"}
LANG_CODE_ALIAS = {
    # ISO 639-1 → проектный legacy (если есть расхождение)
    "pt": "po",  # Portuguese — в проекте используется "po" (legacy)
}
LANG_MAX_LEN_RETURNED = 5000     # log_tail в /jobs/{id} include_log
try:
    API_MAX_THREADS_PER_JOB = max(
        1, min(16, int(os.environ.get("REWRITE_MAX_THREADS_PER_JOB", "3")))
    )
except (TypeError, ValueError):
    API_MAX_THREADS_PER_JOB = 3


# --- Language resolver (canonical lang_name из любого входа) ---

# canonical lang_names (как в LANGUAGES.values()): "English", "Russian", ...
_CANONICAL_LANGS = frozenset(LANGUAGES.values())
# display→canonical: "Английский"→"English". Case-insensitive lookup.
_DISPLAY_TO_CANONICAL = {ru.lower(): en for ru, en in LANGUAGES.items()}
# canonical_lower → canonical (case-insensitive lookup)
_LCASE_TO_CANONICAL = {canon.lower(): canon for canon in _CANONICAL_LANGS}
# short_code → [canonical1, canonical2, ...] (выявляет коллизии вроде arabic/armenian → 'ar')
_SHORT_TO_CANONICAL: Dict[str, List[str]] = {}
for _canon in _CANONICAL_LANGS:
    _sc = lang_name_to_code(_canon)
    if _sc:
        _SHORT_TO_CANONICAL.setdefault(_sc, []).append(_canon)

# canonical → short_code (для GET /languages); при коллизиях все равно даём short_code,
# но коллизионные клиенты увидят в warnings.ambiguous_short_codes какие именно.
_CANONICAL_TO_SHORT: Dict[str, str] = {
    canon: lang_name_to_code(canon) for canon in _CANONICAL_LANGS
}

# Short-коды с >1 кандидатом (коллизии для отчёта в /languages → warnings).
_AMBIGUOUS_SHORT_CODES: Dict[str, List[str]] = {
    sc: sorted(canons) for sc, canons in _SHORT_TO_CANONICAL.items() if len(canons) > 1
}

# Legacy short_codes (исторические отклонения от ISO 639-1).
_LEGACY_SHORT_CODES: Dict[str, str] = {
    "po": "Portuguese (legacy short code, not ISO 'pt'; both 'po' and 'pt' accepted on input, response uses 'po')",
}


def resolve_lang_input(raw: str) -> Tuple[Optional[str], List[str]]:
    """
    Резолв любой формы языка → canonical lang_name (из LANGUAGES.values()).

    Принимает: 'English', 'english', 'EN', 'Английский', 'en'.
    Возвращает: (canonical | None, ambiguity_candidates).
      - (canon, [])      — однозначно резолвилось
      - (None, [a,b,..]) — короткий код матчит >1 canonical (коллизия)
      - (None, [])       — не найдено вообще
    """
    if not raw:
        return None, []
    s = raw.strip()
    if not s:
        return None, []
    # 1. Точный canonical (case-insensitive)
    canon = _LCASE_TO_CANONICAL.get(s.lower())
    if canon:
        return canon, []
    # 2. Display (русское имя из LANGUAGES.keys(), case-insensitive)
    canon = _DISPLAY_TO_CANONICAL.get(s.lower())
    if canon:
        return canon, []
    # 3. Short code (en/ru/...) — только если 1 кандидат
    short = s.lower()
    candidates = _SHORT_TO_CANONICAL.get(short, [])
    if len(candidates) == 1:
        return candidates[0], []
    if len(candidates) > 1:
        return None, sorted(candidates)
    return None, []


# --- Pydantic schemas ---

class ApiLanguage(BaseModel):
    """Один язык в запросе на создание job.

    Поле `code` принимает любую форму: canonical ('English'), display ('Английский'),
    short code ('en'). Валидатор нормализует на canonical lang_name из LANGUAGES.values().
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    code: str = Field(
        ...,
        description="Language: canonical name ('English'), Russian display ('Английский'), "
                    "or short code ('en'). Normalized to canonical on input.",
        min_length=1, max_length=32,
        examples=["English", "en", "Английский"],
    )
    display: str = Field("", description="UI display name (optional)", max_length=64)

    @field_validator("code")
    @classmethod
    def _validate_code(cls, v: str) -> str:
        canon, candidates = resolve_lang_input(v)
        if canon:
            return canon  # ← нормализация: на выходе всегда canonical
        if candidates:
            raise ValueError(
                f"ambiguous short code '{v}': matches {candidates}. "
                f"Use canonical name to disambiguate."
            )
        raise ValueError(
            f"unsupported language: '{v}'. "
            f"Use canonical name (e.g. 'English'), Russian display ('Английский'), "
            f"or short code (e.g. 'en'). Full list: GET /languages or see README_API.md."
        )


class ApiJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    files: List[str] = Field(..., min_length=1, max_length=1,
                              description="Array of file_ids returned by POST /files. v1 accepts exactly 1.")
    languages: List[ApiLanguage] = Field(..., min_length=1, max_length=20,
                              description="Target languages. Each entry's `code` accepts canonical/display/short forms.")
    model: str = Field(
        "gpt-4.1-nano", min_length=1, max_length=128, description="LLM model name"
    )
    chunk_size: int = 3500
    preserve_paragraphs: bool = True
    address_style: str = Field(
        "",
        description="Forms of address: 'vy' (formal), 'ty' (informal), '' (auto). Free text — no validation.",
        max_length=8,
    )
    host_name: str = Field(
        "",
        description="Имя автора/диктора для intro/outro брендинга. "
                    "Пусто → fallback 'Anatolij Eugenjanovič Kazancev' в prompt.",
        max_length=80,
        examples=["Иван Иванов"],
    )
    channel_name: str = Field(
        "",
        description="Название канала для брендинга (intro/outro/CTA). "
                    "Пусто → канал не упоминается в выходном тексте.",
        max_length=80,
        examples=["Скрытый Архив"],
    )
    max_threads: int = 1
    use_subscription: bool = False  # deprecated; server derives provider mode
    parallel: bool = True
    safe_text: bool = False
    webhook_url: Optional[str] = Field(None, description="RESERVED — not supported in v1; will return 422 if set.")

    @field_validator("files")
    @classmethod
    def _bounded_file_ids(cls, values):
        cleaned = []
        for value in values:
            text = str(value or "").strip()
            if not text or len(text) > 128:
                raise ValueError("file_id must contain 1..128 characters")
            cleaned.append(text)
        return cleaned

    @field_validator("chunk_size", mode="before")
    @classmethod
    def _clamp_chunk_size(cls, value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError("chunk_size must be an integer")
        return max(500, min(20000, parsed))

    @field_validator("max_threads", mode="before")
    @classmethod
    def _clamp_max_threads(cls, value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError("max_threads must be an integer")
        return max(1, min(API_MAX_THREADS_PER_JOB, parsed))


class ApiFileUploadResponse(BaseModel):
    file_id: str
    filename: str
    size_bytes: int
    uploaded_at: float


class ApiJobCreateResponse(BaseModel):
    job_id: str
    status: str
    created_at: float
    languages_count: int


class ApiErrorEnvelope(BaseModel):
    error: Dict[str, Any]


class LanguageItem(BaseModel):
    canonical: str = Field(..., description="Internal lang_name as stored in Job.rewrite_languages[].code", examples=["English"])
    display_ru: str = Field(..., description="Russian display name (UI label)", examples=["Английский"])
    short_code: str = Field(..., description="Short code used in URLs and output_files keys", examples=["en"])


class LanguageWarnings(BaseModel):
    legacy_codes: Dict[str, str] = Field(
        default_factory=dict,
        description="Short codes that deviate from ISO 639-1; both legacy and ISO accepted on input.",
    )
    ambiguous_short_codes: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Short codes that match >1 canonical language; rejected on input with 422 — use canonical name.",
    )


class LanguagesResponse(BaseModel):
    total: int = Field(..., description="Total number of supported languages")
    languages: List[LanguageItem]
    warnings: LanguageWarnings
    accepted_input_formats: List[str]


# --- Helpers ---

def _api_user_id(user: Dict) -> str:
    # FIX-2026-07-02: fail-closed — пустой user_id (напр. осиротевшая старая ключ-сессия) → 401,
    # а не работа в скоупе пустого тенанта (симметрично server.py _get_user_id).
    uid = user.get("user_id") or ""
    if not uid:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return uid


def _job_belongs(job: Job, user_id: str) -> bool:
    return getattr(job, "user_id", "") == user_id


def _normalize_lang_code(raw: str) -> str:
    """API canonical lang_code: lowercase, ISO `pt`→legacy `po`."""
    code = (raw or "").strip().lower()
    return LANG_CODE_ALIAS.get(code, code)


def _resolve_file_id(upload_dir: Path, user_id: str, file_id: str) -> Optional[Path]:
    """uploads/<user_id>/<file_id>_<filename> → возвращает Path или None."""
    user_dir = upload_dir / user_id
    if not user_dir.is_dir():
        return None
    for p in user_dir.iterdir():
        if p.is_file() and p.name.startswith(f"{file_id}_"):
            return p
    return None


def _job_to_summary(job: Job) -> Dict:
    """Краткое представление job для list endpoint."""
    try:
        langs = json.loads(getattr(job, "rewrite_languages", "[]") or "[]")
        files = json.loads(getattr(job, "rewrite_files", "[]") or "[]")
    except Exception:
        langs, files = [], []
    return redact_server_credentials({
        "job_id": job.id,
        "status": job.status,
        "progress_percent": getattr(job, "progress_percent", 0),
        "progress_detail": getattr(job, "progress_detail", ""),
        "model": getattr(job, "model", ""),
        "files_count": len(files),
        "languages_count": len(langs),
        "created_at": getattr(job, "created_at", 0.0),
        "started_at": getattr(job, "started_at", 0.0),
        "finished_at": getattr(job, "finished_at", 0.0),
    })


def _progress_by_lang(job: Job) -> Dict[str, Dict]:
    """
    Из PROGRESS_MANAGER собираем {lang_code: {chunks_done, chunks_total, status}}.
    Lang_code — короткий код (en, ru, po, ...). Если задача в PROGRESS_MANAGER не найдена — chunks 0/0.
    """
    from rewrite_core import PROGRESS_MANAGER, lang_name_to_code
    try:
        langs = json.loads(getattr(job, "rewrite_languages", "[]") or "[]")
        files = json.loads(getattr(job, "rewrite_files", "[]") or "[]")
    except Exception:
        return {}
    if not files:
        return {}
    base = os.path.basename(files[0])
    out: Dict[str, Dict] = {}
    for lang in langs:
        lang_name = lang.get("code") if isinstance(lang, dict) else str(lang)
        code = lang_name_to_code(lang_name)
        key = PROGRESS_MANAGER._task_key(
            base,
            lang_name,
            user_id=getattr(job, "user_id", "") or "",
            job_id=job.id,
        )
        task = PROGRESS_MANAGER.tasks.get(key)
        if not task:
            out[code] = {"chunks_done": 0, "chunks_total": 0, "status": "pending"}
            continue
        chunks = task.chunks or []
        done = sum(1 for c in chunks if c.get("status") == "completed")
        out[code] = {
            "chunks_done": done,
            "chunks_total": len(chunks),
            "status": task.status,
        }
    return out


def _resolve_output_path(job: Job, lang_code: str) -> Optional[Path]:
    """
    По lang_code (en, ru, po) → реальный путь outputs/<user_id>/<lang_code>_<base>.txt.
    Сверяется с job.output_files (только то, что реально сохранил worker).
    """
    from rewrite_core import lang_name_to_code, make_output_filename
    try:
        files = json.loads(getattr(job, "rewrite_files", "[]") or "[]")
        saved = json.loads(getattr(job, "output_files", "[]") or "[]")
    except Exception:
        return None
    if not files:
        return None
    base_name = os.path.splitext(os.path.basename(files[0]))[0][:50]

    # Найти lang_name из job.rewrite_languages, чей short-code совпадает с запрошенным
    try:
        langs = json.loads(getattr(job, "rewrite_languages", "[]") or "[]")
    except Exception:
        langs = []
    target_filename = None
    for lang in langs:
        lang_name = lang.get("code") if isinstance(lang, dict) else str(lang)
        if lang_name_to_code(lang_name) == lang_code:
            fname = make_output_filename(base_name, lang_name)
            if fname in saved:
                target_filename = fname
                break
    if not target_filename:
        return None
    out_dir = Path(getattr(job, "output_dir", "") or "")
    if not out_dir or not out_dir.is_dir():
        return None
    candidate = out_dir / target_filename
    return candidate if candidate.is_file() else None


def _job_to_detail(job: Job, request_base: str, include_log: bool = False) -> Dict:
    """Полное представление job для GET /jobs/{id}."""
    from rewrite_core import lang_name_to_code
    try:
        langs = json.loads(getattr(job, "rewrite_languages", "[]") or "[]")
        saved = json.loads(getattr(job, "output_files", "[]") or "[]")
    except Exception:
        langs, saved = [], []

    lang_codes: List[str] = []
    output_files: Dict[str, str] = {}
    for lang in langs:
        lang_name = lang.get("code") if isinstance(lang, dict) else str(lang)
        code = lang_name_to_code(lang_name)
        lang_codes.append(code)
        # URL присутствует только для тех языков, чьи файлы реально сохранены
        from rewrite_core import make_output_filename
        try:
            files = json.loads(getattr(job, "rewrite_files", "[]") or "[]")
            base_name = os.path.splitext(os.path.basename(files[0]))[0][:50] if files else ""
        except Exception:
            base_name = ""
        fname = make_output_filename(base_name, lang_name) if base_name else ""
        if fname and fname in saved:
            output_files[code] = f"{request_base}/api/v1/jobs/{job.id}/files/{code}"

    detail = {
        "job_id": job.id,
        "status": job.status,
        "progress_percent": getattr(job, "progress_percent", 0),
        "progress_detail": getattr(job, "progress_detail", ""),
        "progress_by_lang": _progress_by_lang(job),
        "error": getattr(job, "error", "") or "",
        "model": getattr(job, "model", ""),
        "preserve_paragraphs": bool(getattr(job, "rewrite_preserve_paragraphs", False)),
        "languages": lang_codes,
        "output_files": output_files,
        "created_at": getattr(job, "created_at", 0.0),
        "started_at": getattr(job, "started_at", 0.0),
        "finished_at": getattr(job, "finished_at", 0.0),
    }
    if include_log:
        log = getattr(job, "log", "") or ""
        detail["log_tail"] = log[-LANG_MAX_LEN_RETURNED:] if log else ""
    return redact_server_credentials(detail)


# --- Factory ---

def build_api_router(
    *,
    queue: JobQueue,
    upload_dir: Path,
    output_dir: Path,
    job_stop_events: Dict[str, Any],
    job_stop_events_lock: Lock,
    user_is_active: Optional[Callable[[str], bool]] = None,
) -> APIRouter:
    """Создаёт APIRouter с 7 endpoints. Все требуют api-key через Depends(get_api_user)."""

    router = APIRouter(
        prefix="/api/v1",
        tags=["API v1"],
        dependencies=[Depends(get_api_user)],
    )

    def _add_for_active_user(job: Job) -> str:
        user_id = job.user_id or ADMIN_USER_ID
        if user_is_active is not None and not user_is_active(user_id):
            raise HTTPException(status_code=403, detail="account is disabled")
        job_id = queue.add_job(job)
        if user_is_active is not None and not user_is_active(user_id):
            queue.cancel_jobs_for_user(user_id)
            raise HTTPException(status_code=403, detail="account is disabled")
        return job_id

    # ----- 0.1 GET /languages -----
    @router.get(
        "/languages",
        response_model=LanguagesResponse,
        summary="List supported languages",
        description=(
            "Returns the full list of 45 supported languages with all accepted input "
            "forms (canonical, Russian display, short code). Use this to discover valid "
            "`code` values for `languages[].code` in POST /jobs. "
            "Warnings call out legacy short codes (e.g. `po` for Portuguese) and "
            "ambiguous short codes (e.g. `ar` matches Arabic+Armenian)."
        ),
    )
    def list_languages() -> LanguagesResponse:
        items = [
            LanguageItem(
                canonical=canon,
                display_ru=ru,
                short_code=_CANONICAL_TO_SHORT.get(canon, ""),
            )
            for ru, canon in LANGUAGES.items()
        ]
        return LanguagesResponse(
            total=len(items),
            languages=items,
            warnings=LanguageWarnings(
                legacy_codes=_LEGACY_SHORT_CODES,
                ambiguous_short_codes=_AMBIGUOUS_SHORT_CODES,
            ),
            accepted_input_formats=[
                "canonical (e.g. 'English')",
                "display_ru (e.g. 'Английский', case-insensitive)",
                "short_code (e.g. 'en'; ambiguous codes rejected with hint)",
            ],
        )

    # ----- 1.1 POST /files -----
    @router.post(
        "/files",
        response_model=ApiFileUploadResponse,
        summary="Upload input file",
        description="Загрузка текстового файла для последующего использования в job. Max 50 MB.",
        responses={
            401: {"model": ApiErrorEnvelope},
            413: {"model": ApiErrorEnvelope},
            415: {"model": ApiErrorEnvelope},
            422: {"model": ApiErrorEnvelope},
        },
    )
    async def upload_input_file(
        file: UploadFile = File(...),
        user: Dict = Depends(get_api_user),
    ) -> ApiFileUploadResponse:
        user_id = _api_user_id(user)
        if not file.filename:
            raise HTTPException(status_code=422, detail="filename required")
        # file_id: 8-char nanoid (token_urlsafe(6) ~ 8 chars)
        file_id = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8] or secrets.token_hex(4)
        user_upload = upload_dir / user_id
        user_upload.mkdir(parents=True, exist_ok=True)
        safe_name = os.path.basename(file.filename).replace("/", "_")[:200]
        target = user_upload / f"{file_id}_{safe_name}"
        total = 0
        try:
            with open(target, "wb") as f:
                while True:
                    chunk = await file.read(8192)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD_SIZE_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"file too large (max {MAX_UPLOAD_SIZE_BYTES // 1024 // 1024} MB)"
                        )
                    f.write(chunk)
        except HTTPException:
            if target.exists():
                try:
                    target.unlink()
                except Exception:
                    pass
            raise
        return ApiFileUploadResponse(
            file_id=file_id,
            filename=safe_name,
            size_bytes=total,
            uploaded_at=time.time(),
        )

    # ----- 1.2 POST /jobs -----
    @router.post(
        "/jobs",
        response_model=ApiJobCreateResponse,
        status_code=201,
        summary="Create rewrite job",
        description="Создаёт новый rewrite-job для одного загруженного файла. webhook_url пока не поддерживается (422).",
        responses={
            400: {"model": ApiErrorEnvelope},
            401: {"model": ApiErrorEnvelope},
            422: {"model": ApiErrorEnvelope},
        },
    )
    def create_job(body: ApiJobCreate, user: Dict = Depends(get_api_user)) -> ApiJobCreateResponse:
        if body.webhook_url is not None:
            raise HTTPException(status_code=422, detail="webhook_url is reserved and not supported in v1 (planned for v1.1)")
        user_id = _api_user_id(user)
        file_id = body.files[0]
        resolved = _resolve_file_id(upload_dir, user_id, file_id)
        if not resolved:
            raise HTTPException(status_code=400, detail=f"file_id '{file_id}' not found for this user")

        user_output = output_dir / user_id
        user_output.mkdir(parents=True, exist_ok=True)

        provider = classify_provider(body.model)
        model_is_codex = provider == "codex"
        model_is_claude = provider == "claude"
        if not provider_user_allowed(provider, user_id):
            raise HTTPException(
                status_code=403, detail="provider is not enabled for this account"
            )
        if model_is_claude:
            if body.model not in get_allowed_claude_models():
                raise HTTPException(
                    status_code=422,
                    detail="Claude model is not allowed by server policy",
                )
            if not get_server_claude_oauth_tokens():
                raise HTTPException(
                    status_code=503,
                    detail="Claude server subscription is not configured.",
                )
        elif model_is_codex:
            if body.model not in get_allowed_codex_models():
                raise HTTPException(
                    status_code=422,
                    detail="Codex model is not allowed by server policy",
                )
        else:
            admin_custom = {
                str(v).strip()
                for v in (get_keys_config(ADMIN_USER_ID).get("custom_models") or [])
                if str(v).strip()
            }
            if body.model not in get_allowed_openai_models() and not (
                user_id == ADMIN_USER_ID and body.model in admin_custom
            ):
                raise HTTPException(
                    status_code=422,
                    detail="model is not allowed by server policy",
                )
            if not get_server_openai_keys():
                raise HTTPException(
                    status_code=503,
                    detail="OpenAI server key is not configured",
                )
        effective_use_subscription = model_is_claude
        # Task 3: для всех кроме админа (Володя) — принудительно preserve_paragraphs=True.
        effective_preserve_paragraphs = (
            True if user_id != ADMIN_USER_ID else body.preserve_paragraphs
        )

        job = Job(
            job_type=JobType.REWRITE.value,
            model=body.model,
            provider=provider,
            api_key="",
            # Inner concurrency is fixed by the server; request value is legacy.
            max_threads=API_MAX_THREADS_PER_JOB,
            output_dir=str(user_output),
            rewrite_files=json.dumps([str(resolved)], ensure_ascii=False),
            rewrite_languages=json.dumps(
                [
                    {
                        "display": lang.display or lang.code,
                        "code": lang.code,             # already canonical after validator
                        "channel": body.channel_name,  # job-level branding propagated per-lang
                        "host": body.host_name,
                        "custom_prompt": "",
                    }
                    for lang in body.languages
                ],
                ensure_ascii=False,
            ),
            rewrite_chunk_size=body.chunk_size,
            rewrite_address_style=body.address_style,
            rewrite_preserve_paragraphs=effective_preserve_paragraphs,
            use_subscription=effective_use_subscription,
            oauth_token="",
            oauth_tokens="[]",
            gen_safe_text=body.safe_text,
            parallel=False,
            user_id=user_id,
        )
        job_id = _add_for_active_user(job)
        return ApiJobCreateResponse(
            job_id=job_id,
            status=JobStatus.PENDING.value,
            created_at=time.time(),
            languages_count=len(body.languages),
        )

    # ----- 1.3 GET /jobs -----
    @router.get(
        "/jobs",
        summary="List jobs",
        description="Список jobs текущего пользователя с фильтром по статусу и pagination.",
    )
    def list_jobs(
        request: Request,
        user: Dict = Depends(get_api_user),
        status: Optional[str] = Query(None, description="CSV: pending,running,completed,failed,cancelled,partial,interrupted"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        created_after: Optional[float] = Query(None, description="Unix timestamp"),
    ) -> Dict:
        user_id = _api_user_id(user)
        all_jobs = queue.get_jobs_by_user(user_id, limit=2000)
        if status:
            allowed = {s.strip() for s in status.split(",") if s.strip()}
            all_jobs = [j for j in all_jobs if j.status in allowed]
        if created_after is not None:
            all_jobs = [j for j in all_jobs if getattr(j, "created_at", 0.0) >= created_after]
        total = len(all_jobs)
        page = all_jobs[offset:offset + limit]
        return {
            "items": [_job_to_summary(j) for j in page],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    # ----- 1.4 GET /jobs/{id} -----
    @router.get(
        "/jobs/{job_id}",
        summary="Get job details",
        responses={401: {"model": ApiErrorEnvelope}, 403: {"model": ApiErrorEnvelope}, 404: {"model": ApiErrorEnvelope}},
    )
    def get_job_detail(
        job_id: str,
        request: Request,
        user: Dict = Depends(get_api_user),
        include_log: bool = Query(False, description="Include last 5KB of log"),
    ) -> Dict:
        user_id = _api_user_id(user)
        job = queue.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if not _job_belongs(job, user_id):
            raise HTTPException(status_code=403, detail="access denied")
        if user_is_active is not None and not user_is_active(user_id):
            raise HTTPException(status_code=403, detail="account is disabled")
        base = str(request.base_url).rstrip("/")
        return _job_to_detail(job, base, include_log=include_log)

    # ----- 1.5 DELETE /jobs/{id} -----
    @router.delete(
        "/jobs/{job_id}",
        summary="Cancel job",
        description="Отменяет job. Для running запускает graceful interrupt (worker завершит после текущего chunk).",
        responses={
            401: {"model": ApiErrorEnvelope}, 403: {"model": ApiErrorEnvelope},
            404: {"model": ApiErrorEnvelope}, 409: {"model": ApiErrorEnvelope},
        },
    )
    def cancel_job_api(job_id: str, user: Dict = Depends(get_api_user)) -> Dict:
        user_id = _api_user_id(user)
        job = queue.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if not _job_belongs(job, user_id):
            raise HTTPException(status_code=403, detail="access denied")
        result = queue.request_cancel(job_id)
        if not result.changed:
            raise HTTPException(
                status_code=409,
                detail=f"cancel is a no-op ({result.code}, status={result.status})",
            )
        if result.code == "cancel_requested":
            with job_stop_events_lock:
                evt = job_stop_events.get(job_id)
            if evt:
                evt.set()
        return {
            "job_id": job_id,
            "status": result.status,
            "was": result.previous,
        }

    # ----- 1.6 GET /jobs/{id}/files/{lang_code} -----
    @router.get(
        "/jobs/{job_id}/files/{lang_code}",
        summary="Download output for a language",
        description="Скачать результат рерайта для одного языка. lang_code в проектной нотации (en, ru, po, ...). Алиас pt→po поддерживается.",
        response_class=FileResponse,
        responses={
            401: {"model": ApiErrorEnvelope}, 403: {"model": ApiErrorEnvelope},
            404: {"model": ApiErrorEnvelope}, 409: {"model": ApiErrorEnvelope},
        },
    )
    def download_output(
        job_id: str,
        lang_code: str,
        user: Dict = Depends(get_api_user),
    ):
        user_id = _api_user_id(user)
        job = queue.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if not _job_belongs(job, user_id):
            raise HTTPException(status_code=403, detail="access denied")
        if job.status not in DOWNLOADABLE_STATUSES:
            raise HTTPException(status_code=409, detail=f"job not ready ({job.status}); files available only when completed/partial/interrupted")
        code = _normalize_lang_code(lang_code)
        path = _resolve_output_path(job, code)
        if not path:
            raise HTTPException(status_code=404, detail=f"no output file for lang_code='{code}' in this job")
        return FileResponse(
            path=str(path),
            media_type="text/plain; charset=utf-8",
            filename=path.name,
        )

    # ----- 1.7 POST /jobs/{id}/retry -----
    @router.post(
        "/jobs/{job_id}/retry",
        summary="Retry job",
        description="Повторить незавершённые чанки. full=true → полная очистка прогресса и .txt, прогон с нуля.",
        responses={
            400: {"model": ApiErrorEnvelope}, 401: {"model": ApiErrorEnvelope},
            403: {"model": ApiErrorEnvelope}, 404: {"model": ApiErrorEnvelope},
        },
    )
    def retry_job_api(
        job_id: str,
        full: bool = Query(False, description="true → wipe progress + outputs, full re-run"),
        user: Dict = Depends(get_api_user),
    ) -> Dict:
        user_id = _api_user_id(user)
        job = queue.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if not _job_belongs(job, user_id):
            raise HTTPException(status_code=403, detail="access denied")
        if user_is_active is not None and not user_is_active(user_id):
            raise HTTPException(status_code=403, detail="account is disabled")
        cleared = 0
        if full:
            from rewrite_core import PROGRESS_MANAGER, make_output_filename
            ticket = queue.begin_retry(job_id, full=True)
            if not ticket:
                raise HTTPException(
                    status_code=409, detail="full retry requires a terminal job"
                )
            try:
                files = json.loads(ticket.job.rewrite_files or "[]")
                langs = json.loads(ticket.job.rewrite_languages or "[]")
                for f in files:
                    for lang in langs:
                        lang_name = (
                            lang.get("code")
                            if isinstance(lang, dict)
                            else str(lang)
                        )
                        if PROGRESS_MANAGER.delete_task(
                            os.path.basename(f),
                            lang_name,
                            user_id=user_id,
                            job_id=ticket.job.id,
                        ):
                            cleared += 1
                        base_name = os.path.splitext(os.path.basename(f))[0][:50]
                        out_path = (
                            Path(ticket.job.output_dir)
                            / make_output_filename(base_name, lang_name)
                        )
                        if out_path.is_file():
                            out_path.unlink()
                output_root = Path(ticket.job.output_dir).resolve()
                try:
                    reset_manifest = json.loads(
                        ticket.job.reset_manifest or "[]"
                    )
                except (json.JSONDecodeError, TypeError):
                    reset_manifest = []
                if isinstance(reset_manifest, dict):
                    reset_manifest = list(reset_manifest.values())
                if not isinstance(reset_manifest, list):
                    reset_manifest = []
                for filename in reset_manifest:
                    safe_name = os.path.basename(str(filename))
                    candidate = (output_root / safe_name).resolve()
                    if candidate.parent == output_root and candidate.is_file():
                        candidate.unlink()
                ticket.job.output_files = "[]"
                if not queue.complete_retry(ticket):
                    raise RuntimeError("retry ownership was lost")
                queue.append_log(job_id, "\n--- Полный повтор с нуля ---\n")
            except Exception as exc:
                queue.fail_retry(ticket, f"full retry cleanup failed: {exc}")
                raise HTTPException(
                    status_code=500, detail="full retry cleanup failed"
                )
        elif not queue.retry_job(job_id):
            raise HTTPException(
                status_code=409,
                detail="retry available only for failed/cancelled/partial/interrupted jobs",
            )
        return {"job_id": job_id, "status": JobStatus.PENDING.value, "full": full, "cleared_tasks": cleared}

    return router
