# -*- coding: utf-8 -*-
"""
Cover Server — REST API v1.

7 endpoints под /api/v1/* для программного доступа к генерации обложек.
Параллельная с session-id и shared-secret аутентификация: per-user ключи cvr_*.

Factory: build_api_router(queue, get_preset_manager, ...) -> APIRouter
"""

import io
import json
import os
import re
import secrets
import time
import uuid
import zipfile
from dataclasses import asdict, fields as dc_fields
from pathlib import Path
from typing import List, Dict, Optional, Callable, Any

from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal

from job_queue_cover import Job, JobStatus, PendingJobLimitExceeded
from cover_core import Preset, LANG_DISPLAY_TO_CODE, get_lang_code, GENERATION_OPTIONS
from api_cover_helpers import ensure_server_keys_for_job
from provider_policy import (
    InvalidGenerationProvider,
    canonical_generation_provider,
)


# ════════════════════════════════════════════════════════
#  Pydantic models
# ════════════════════════════════════════════════════════

class ApiPresetResponse(BaseModel):
    name: str
    languages: List[str]
    language: str = ""
    generation_provider: str = ""
    generation_model: str = ""
    fallback_mode: str = ""


class ApiCoverJobCreate(BaseModel):
    mode: Literal["url", "title"] = Field("url", description="url — генерация из YouTube; title — из названия + extra_prompt")
    # url-mode field
    urls: List[str] = Field(default_factory=list, max_length=50, description="YouTube URLs (только для mode=url)")
    # title-mode fields
    titles_per_lang: Dict[str, str] = Field(default_factory=dict,
                                             description="lang → текст обложки на этом языке (только для mode=title)")
    extra_prompt: str = Field("", max_length=2000, description="дополнительные детали для сцены (только для mode=title)")
    # общее
    preset_name: str = Field(..., min_length=1, description="имя пресета из /api/v1/presets")
    languages: List[str] = Field(..., min_length=1, max_length=10,
                                  description="языки в формате русских названий: Русский, English, ...")
    refs_per_language: Dict[str, str] = Field(default_factory=dict,
                                               description="lang -> file_id из POST /api/v1/files")

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_vendor_keys(cls, value):
        data = dict(value or {})
        secret_fields = ("openai_api_key", "fast_gen_api_key", "veo_api_key")
        if any(str(data.get(name) or "").strip() for name in secret_fields):
            raise ValueError(
                "Vendor API keys больше не принимаются: используются только серверные ключи."
            )
        for name in secret_fields + ("max_threads", "parallel"):
            data.pop(name, None)
        return data

    @field_validator("languages")
    @classmethod
    def _strip_langs(cls, v: List[str]) -> List[str]:
        out = [l.strip() for l in v if (l or "").strip()]
        if not out:
            raise ValueError("languages must contain at least one non-empty value")
        return out

    @model_validator(mode="after")
    def _check_mode(self):
        # url-mode валидация
        if self.mode == "url":
            urls = [u.strip() for u in self.urls if (u or "").strip()]
            if not urls:
                raise ValueError("urls required for mode=url")
            self.urls = urls
            if self.titles_per_lang:
                raise ValueError("titles_per_lang only allowed for mode=title")
            if self.extra_prompt:
                raise ValueError("extra_prompt only allowed for mode=title")
            return self
        # title-mode валидация
        if not self.titles_per_lang:
            raise ValueError("titles_per_lang required for mode=title")
        if self.urls:
            raise ValueError("urls only allowed for mode=url")
        # ключи titles_per_lang должны совпадать с languages — приведение к canonical через resolver
        resolved_langs = []
        for l in self.languages:
            r = _resolve_language(l)
            if r is None:
                raise ValueError(f"language '{l}' not supported")
            resolved_langs.append(r)
        # каждый ключ titles_per_lang тоже нормализуем
        # v1.0.7: пустой value разрешён → обложка будет сгенерирована без текста
        titles_resolved: Dict[str, str] = {}
        for k, v in self.titles_per_lang.items():
            txt = (v or "").strip()
            r = _resolve_language(k)
            if r is None:
                raise ValueError(f"titles_per_lang key '{k}' is not a supported language")
            titles_resolved[r] = txt  # пустая строка ОК
        missing = [l for l in resolved_langs if l not in titles_resolved]
        extra = [l for l in titles_resolved.keys() if l not in resolved_langs]
        if missing or extra:
            raise ValueError(
                f"titles_per_lang keys must match languages exactly. "
                f"missing for languages: {missing}, extra not in languages: {extra}"
            )
        # сохраняем нормализованные значения
        self.titles_per_lang = titles_resolved
        self.languages = resolved_langs
        return self


class ApiJobResponse(BaseModel):
    id: str
    status: str
    created_at: float
    started_at: float = 0.0
    finished_at: float = 0.0
    progress_percent: int = 0
    progress_detail: str = ""
    results_count: int = 0
    output_files: List[str] = Field(default_factory=list)
    fallback_files: List[str] = Field(default_factory=list)
    # Подмножество fallback_files: текст на обложке есть, но сверить его не удалось.
    unchecked_files: List[str] = Field(default_factory=list)
    # Что сказала сверка надписи и генератор — строки журнала задачи (04.10.2026: «как
    # мне понять, какие обложки не прошли проверку» — студия показывает их человеку).
    check_notes: List[str] = Field(default_factory=list)
    progress_by_language: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    error: str = ""


class ApiJobCreateResponse(BaseModel):
    job_id: str
    status: str


class ApiJobListItem(BaseModel):
    id: str
    status: str
    created_at: float
    progress_percent: int = 0
    results_count: int = 0


class ApiJobListResponse(BaseModel):
    items: List[ApiJobListItem]
    total: int
    limit: int
    offset: int


class ApiFileUploadResponse(BaseModel):
    file_id: str
    filename: str
    size: int


# ─── Preset CRUD models ───

# Допустимые provider/model для generation. Источник правды — GENERATION_OPTIONS из cover_core.
_VALID_GEN_PAIRS = {(p, m) for _label, p, m in GENERATION_OPTIONS}
_VALID_GEN_PROVIDERS = {p for p, m in _VALID_GEN_PAIRS}


class ApiPresetWrite(BaseModel):
    """Поля для создания/обновления пресета. Подмножество Preset dataclass.
    Часть полей опциональна — берутся дефолты из Preset()."""
    name: str = Field(..., min_length=1, max_length=80, description="имя пресета, уникально в рамках user_id")
    language: str = Field("Русский", description="основной язык (русское название из LANG_DISPLAY_TO_CODE)")
    languages: str = Field("", description="CSV дополнительных языков, напр. 'Русский,Английский'")
    style_prompt: str = ""
    use_auto_style: bool = True
    simple_mode: bool = Field(False, description="vS: img2img — превью редактируется моделью (перевод текста + смена поз), без vision-анализа")
    reference_image: str = Field("", max_length=200, description="vR: file_id референса стиля (из POST /files) — двухкартиночный style-transfer")
    fallback_mode: str = Field("auto", description="auto|always_overlay (always_overlay = пропустить проверку текста)")
    generation_provider: str = Field("flow", description="flow|grok|openai_img|flower")
    generation_model: str = Field("GEM_PIX_2", description="модель провайдера, см. GENERATION_OPTIONS")
    analysis_provider: str = Field("openai", description="openai (google deprecated, auto-migrated)")
    analysis_model: str = Field("gpt-4.1-mini")
    aspect_ratio: str = Field("16:9")
    max_generation_retries: int = Field(5, ge=1, le=20)
    debug_mode: bool = False

    @field_validator("fallback_mode")
    @classmethod
    def _validate_fallback(cls, v: str) -> str:
        allowed = {"auto", "always_overlay", "never_overlay"}
        if v not in allowed:
            raise ValueError(f"fallback_mode must be one of {sorted(allowed)}")
        return v


class ApiPresetFullResponse(BaseModel):
    """Полная структура пресета (подмножество Preset asdict; служебные поля скрыты)."""
    name: str
    language: str
    languages: str
    style_prompt: str
    use_auto_style: bool
    simple_mode: bool = False
    reference_image: str = ""
    fallback_mode: str
    generation_provider: str
    generation_model: str
    analysis_provider: str
    analysis_model: str
    aspect_ratio: str
    created_at: str
    updated_at: str
    max_generation_retries: int
    debug_mode: bool


# ════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════

# ─── Language resolver: принимает 3 формы ───
# 1. Русское название (canonical for cover_core): "Русский", "Английский"
# 2. ISO-код:        "ru", "en"
# 3. Английское название: "Russian", "English"
# Возвращает canonical (русское название), которое использует cover_core при генерации.

# ISO-код → английское название (для поддержки английских названий на входе API).
# Список синхронизирован с ключами LANG_DISPLAY_TO_CODE в cover_core.py.
_ISO_TO_EN_NAME = {
    "az": "Azerbaijani", "en": "English", "ar": "Arabic", "hy": "Armenian",
    "be": "Belarusian", "bg": "Bulgarian", "bs": "Bosnian", "hu": "Hungarian",
    "vi": "Vietnamese", "el": "Greek", "ka": "Georgian", "da": "Danish",
    "he": "Hebrew", "id": "Indonesian", "es": "Spanish", "it": "Italian",
    "kk": "Kazakh", "ca": "Catalan", "zh": "Chinese", "ko": "Korean",
    "lv": "Latvian", "lt": "Lithuanian", "mk": "Macedonian",
    "de": "German", "nl": "Dutch", "no": "Norwegian",
    "fa": "Persian", "pl": "Polish", "pt": "Portuguese",
    "ro": "Romanian", "ru": "Russian", "sr": "Serbian", "sk": "Slovak",
    "sl": "Slovenian", "th": "Thai", "tr": "Turkish", "uz": "Uzbek",
    "uk": "Ukrainian", "fi": "Finnish", "fr": "French", "hi": "Hindi",
    "hr": "Croatian", "cs": "Czech", "sv": "Swedish", "et": "Estonian",
    "ja": "Japanese",
}
_CODE_TO_RU = {code: ru for ru, code in LANG_DISPLAY_TO_CODE.items()}
_EN_LOWER_TO_CODE = {en.lower(): code for code, en in _ISO_TO_EN_NAME.items()}
_RU_LOWER_TO_CODE = {ru.lower(): code for ru, code in LANG_DISPLAY_TO_CODE.items()}
_VALID_CODES = set(_ISO_TO_EN_NAME.keys()) | set(LANG_DISPLAY_TO_CODE.values())


def _resolve_language(raw: str) -> Optional[str]:
    """Принимает строку в любой из 3 форм (ru-name / iso / en-name).
    Возвращает caconical русское название из LANG_DISPLAY_TO_CODE, или None если не распознано."""
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    sl = s.lower()
    # 1. ISO-код (приоритет — он самый короткий и однозначный)
    if sl in _VALID_CODES:
        return _CODE_TO_RU.get(sl)
    # 2. Русское название
    if sl in _RU_LOWER_TO_CODE:
        return _CODE_TO_RU[_RU_LOWER_TO_CODE[sl]]
    # 3. Английское название
    if sl in _EN_LOWER_TO_CODE:
        return _CODE_TO_RU[_EN_LOWER_TO_CODE[sl]]
    return None


# ─── v1.0.8: upload validation ───
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB, как обещано в доке


def _detect_image_mime(content: bytes) -> str:
    """Определяет MIME картинки по magic-bytes. Возвращает 'image/*' или 'application/octet-stream'.
    Не доверяет Content-Type — проверяет реальное содержимое."""
    if not content or len(content) < 4:
        return "application/octet-stream"
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if content[:2] == b"BM":
        return "image/bmp"
    return "application/octet-stream"


def _validate_languages(langs: List[str]) -> List[str]:
    """Принимает СЫРОЙ список (любая из 3 форм). Возвращает canonical русские названия.
    Бросает 422 если хоть один язык не распознан."""
    canonical = set(LANG_DISPLAY_TO_CODE.keys())
    resolved = []
    invalid = []
    for raw in langs:
        r = _resolve_language(raw)
        if r is None or r not in canonical:
            invalid.append(raw)
        else:
            resolved.append(r)
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unsupported languages: {invalid}. "
                f"Use canonical Russian name (Русский), English name (Russian), or ISO code (ru). "
                f"См. GET /api/v1/presets для списка поддерживаемых пресетом."
            ),
        )
    return resolved


def _compute_progress_by_language(job: Job, requested_languages: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """Из output_files считает прогресс по каждому языку.
    Формат имени файла: {lang_code}_{title}_{video_id}_{preset}.jpg (cover_core._output_filename).
    """
    try:
        urls = json.loads(job.urls or "[]")
    except Exception:
        urls = []
    try:
        out_files = json.loads(job.output_files or "[]")
    except Exception:
        out_files = []
    try:
        fb_files = set(json.loads(job.fallback_files or "[]"))
    except Exception:
        fb_files = set()
    try:
        job_langs = json.loads(job.languages or "[]")
    except Exception:
        job_langs = []

    if requested_languages is None:
        requested_languages = job_langs

    # Группируем output_files по lang_code (первая часть до _).
    # Файлы могут лежать в подпапках "<video_title>/<lang_code>_..." — парсим basename.
    by_code: Dict[str, List[str]] = {}
    by_code_fallback: Dict[str, int] = {}
    for rel in out_files:
        basename = Path(rel).name
        m = re.match(r"^([a-z]{2,3})_", basename)
        if not m:
            continue
        code = m.group(1)
        by_code.setdefault(code, []).append(rel)
        if rel in fb_files:
            by_code_fallback[code] = by_code_fallback.get(code, 0) + 1

    total_per_lang = max(1, len(urls))  # одно видео = одна обложка per language
    # Ключи progress_by_language — ISO-коды (как обещает docs/API_COVER_PUBLIC.md).
    # display оставляем как поле для backward-compat и UX.
    out = {}
    for lang_display in requested_languages:
        code = get_lang_code(lang_display)
        done_files = by_code.get(code, [])
        done = len(done_files)
        fb = by_code_fallback.get(code, 0)
        out[code] = {
            "code": code,
            "display": lang_display,
            "done": done,
            "total": total_per_lang,
            "fallback_count": fb,
            "files": done_files,
            "status": (
                "completed" if done >= total_per_lang and fb == 0 else
                "partial" if done > 0 else
                "pending"
            ),
        }
    return out


def _resolve_file_id(file_id: str, user_id: str, upload_dir: Path) -> Optional[Path]:
    """Поиск файла по file_id префиксу в UPLOAD_DIR/{user_id}/.
    Формат имени: {file_id}_{original_name}."""
    if not file_id:
        return None
    user_dir = upload_dir / user_id
    if not user_dir.exists():
        return None
    safe_id = re.sub(r"[^\w\-]", "", file_id)
    if not safe_id:
        return None
    for entry in user_dir.iterdir():
        if entry.is_file() and entry.name.startswith(safe_id + "_"):
            return entry
    return None


# Строки журнала задачи обложек, которые объясняют человеку итог: что сверка увидела в
# надписи, что сверить не удалось, что генератор не отдал картинку. Только они — весь
# журнал длинный и технический.
_CHECK_MARKS = ("❌ Проблема:", "проверка текста не выполнена", "проверка текста не пройдена",
                "Fast Gen не отдал картинку", "пустой результат от API",
                "Ошибка генерации изображения", "Оценки ИИ:", "Оценить кандидатов не удалось",
                # 17.10.2026: в режиме по названию сцена падала с 402 (лимит ключа), а задача
                # заканчивалась «completed_partial, 0» без слова о причине — студия тратила
                # раунды смягчения на сбой ключа. Теперь причина видна снаружи.
                "Ошибка генерации сцены")


def check_notes_of(log: str, limit: int = 8) -> List[str]:
    out = []
    for line in (log or "").splitlines():
        text = line.strip()
        if text and any(m in text for m in _CHECK_MARKS):
            out.append(text[:300])
    return out[-limit:]


def _job_to_response(job: Job) -> ApiJobResponse:
    try:
        out_files = json.loads(job.output_files or "[]")
    except Exception:
        out_files = []
    try:
        fb_files = json.loads(job.fallback_files or "[]")
    except Exception:
        fb_files = []
    try:
        unchecked = json.loads(getattr(job, "unchecked_files", "") or "[]")
    except Exception:
        unchecked = []
    return ApiJobResponse(
        check_notes=check_notes_of(job.log or ""),
        id=job.id,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        progress_percent=job.progress_percent,
        progress_detail=job.progress_detail,
        results_count=job.results_count,
        output_files=out_files,
        fallback_files=fb_files,
        unchecked_files=unchecked,
        progress_by_language=_compute_progress_by_language(job),
        error=job.error or "",
    )


# ════════════════════════════════════════════════════════
#  Router factory
# ════════════════════════════════════════════════════════

def build_api_router(
    *,
    queue,
    get_preset_manager: Callable[[str], Any],
    get_user_output_dir: Callable[[str], Path],
    job_belongs_to_user: Callable[[Job, str], bool],
    resolve_ref_paths: Callable[[Dict[str, str], str], Dict[str, str]],
    upload_dir: Path,
    output_dir: Path,
    queue_limit_resolver: Optional[Callable[[], int]] = None,
    max_threads_resolver: Optional[Callable[[], int]] = None,
) -> APIRouter:
    """Сборка APIRouter с зависимостями. Vendor keys/caps задаёт только сервер."""
    router = APIRouter(tags=["API v1"])

    def _user_id(request: Request) -> str:
        uid = getattr(request.state, "user_id", "")
        if not uid:
            raise HTTPException(status_code=401, detail="authentication required")
        return uid

    def _technical_queue_limit() -> int:
        return (
            max(1, int(queue_limit_resolver()))
            if queue_limit_resolver is not None
            else max(1, int(os.getenv("COVER_MAX_PENDING", "100")))
        )

    # ---- Presets ----

    @router.get("/presets", response_model=Dict[str, ApiPresetResponse])
    def list_presets(request: Request):
        """Список пресетов пользователя. Public для anonymous (вернёт пресеты 'default' пользователя)."""
        # Public endpoint: если auth не отработала, отдаём пресеты default user'а
        uid = getattr(request.state, "user_id", "default") or "default"
        mgr = get_preset_manager(uid)
        out = {}
        for name, preset in mgr._presets.items():
            primary = (preset.language or "").strip()
            extra = [x.strip() for x in (preset.languages or "").split(",") if x.strip()]
            langs = []
            if primary:
                langs.append(primary)
            for x in extra:
                if x not in langs:
                    langs.append(x)
            out[name] = ApiPresetResponse(
                name=name,
                languages=langs,
                language=primary,
                generation_provider=preset.generation_provider or "",
                generation_model=preset.generation_model or "",
                fallback_mode=preset.fallback_mode or "",
            )
        return out

    def _preset_to_full_response(preset: Preset) -> ApiPresetFullResponse:
        return ApiPresetFullResponse(**asdict(preset))

    def _validate_preset_body(body: ApiPresetWrite):
        """Дополнительная валидация (поверх Pydantic):
        - languages CSV → каждый элемент через _resolve_language
        - generation (provider, model) — в GENERATION_OPTIONS
        - analysis_provider auto-corrected (google→openai)
        """
        # languages CSV
        if body.languages.strip():
            invalid = []
            for part in [x.strip() for x in body.languages.split(",") if x.strip()]:
                if _resolve_language(part) is None:
                    invalid.append(part)
            if invalid:
                raise HTTPException(status_code=422, detail=f"languages contains unsupported: {invalid}")
        # primary language
        if body.language.strip() and _resolve_language(body.language) is None:
            raise HTTPException(status_code=422, detail=f"language '{body.language}' not supported")
        # generation pair
        try:
            gp = canonical_generation_provider(body.generation_provider)
        except InvalidGenerationProvider as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        gm = (body.generation_model or "").strip()
        if (gp, gm) not in _VALID_GEN_PAIRS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unsupported generation pair: provider={gp!r}, model={gm!r}. "
                    f"См. /api/v1/presets метаданные или допустимые: {sorted(_VALID_GEN_PROVIDERS)}"
                ),
            )

    def _has_active_job_with_preset(uid: str, preset_name: str) -> bool:
        for j in queue.get_all_jobs():
            if not job_belongs_to_user(j, uid):
                continue
            if j.status not in (JobStatus.PENDING.value, JobStatus.RUNNING.value):
                continue
            try:
                pd = json.loads(j.preset_data or "{}")
                if pd.get("name") == preset_name:
                    return True
            except Exception:
                continue
        return False

    @router.get("/presets/{name}", response_model=ApiPresetFullResponse)
    def get_preset_one(name: str, request: Request):
        uid = _user_id(request)
        preset = get_preset_manager(uid).get(name)
        if not preset:
            raise HTTPException(status_code=404, detail=f"preset '{name}' not found")
        return _preset_to_full_response(preset)

    @router.post("/presets", response_model=ApiPresetFullResponse, status_code=201)
    def create_preset(body: ApiPresetWrite, request: Request):
        uid = _user_id(request)
        _validate_preset_body(body)
        mgr = get_preset_manager(uid)
        if mgr.get(body.name) is not None:
            raise HTTPException(status_code=422, detail=f"preset '{body.name}' already exists. Use PUT to update.")
        # auto-migration analysis_provider
        analysis_provider = body.analysis_provider
        if analysis_provider in ("google", "gemini"):
            analysis_provider = "openai"
        preset = Preset(
            name=body.name,
            language=body.language,
            languages=body.languages,
            style_prompt=body.style_prompt,
            use_auto_style=body.use_auto_style,
            simple_mode=body.simple_mode,
            reference_image=body.reference_image,
            fallback_mode=body.fallback_mode,
            generation_provider=body.generation_provider,
            generation_model=body.generation_model,
            analysis_provider=analysis_provider,
            analysis_model=body.analysis_model,
            aspect_ratio=body.aspect_ratio,
            max_generation_retries=body.max_generation_retries,
            debug_mode=body.debug_mode,
        )
        mgr.save_preset(preset)
        return _preset_to_full_response(preset)

    @router.put("/presets/{name}", response_model=ApiPresetFullResponse)
    def update_preset(name: str, body: ApiPresetWrite, request: Request):
        uid = _user_id(request)
        mgr = get_preset_manager(uid)
        existing = mgr.get(name)
        if not existing:
            raise HTTPException(status_code=404, detail=f"preset '{name}' not found")
        # имя из URL — приоритет; body.name может отличаться, переименование запрещаем для простоты
        if body.name != name:
            raise HTTPException(status_code=422, detail=f"name mismatch: URL='{name}' vs body.name='{body.name}'. Rename via DELETE+POST.")
        _validate_preset_body(body)
        analysis_provider = body.analysis_provider
        if analysis_provider in ("google", "gemini"):
            analysis_provider = "openai"
        # Merge поверх существующего пресета: поля вне ApiPresetWrite (prompt_template,
        # seed_reference_image/-filename, seed_n_variants и будущие) сохраняются,
        # иначе партнёрский PUT молча стирал бы их (created_at тоже переживает merge).
        merged = asdict(existing)
        merged.update(
            name=body.name,
            language=body.language,
            languages=body.languages,
            style_prompt=body.style_prompt,
            use_auto_style=body.use_auto_style,
            simple_mode=body.simple_mode,
            reference_image=body.reference_image,
            fallback_mode=body.fallback_mode,
            generation_provider=body.generation_provider,
            generation_model=body.generation_model,
            analysis_provider=analysis_provider,
            analysis_model=body.analysis_model,
            aspect_ratio=body.aspect_ratio,
            max_generation_retries=body.max_generation_retries,
            debug_mode=body.debug_mode,
        )
        updated = Preset(**merged)
        mgr.save_preset(updated)
        return _preset_to_full_response(updated)

    @router.delete("/presets/{name}", status_code=204)
    def delete_preset(name: str, request: Request):
        uid = _user_id(request)
        mgr = get_preset_manager(uid)
        if mgr.get(name) is None:
            raise HTTPException(status_code=404, detail=f"preset '{name}' not found")
        if _has_active_job_with_preset(uid, name):
            raise HTTPException(
                status_code=409,
                detail=f"preset '{name}' is used by an active job (pending/running). Wait for completion or cancel the job first.",
            )
        mgr.delete_preset(name)
        # 204 No Content — без body

    # ---- Files (reference uploads) ----

    @router.post("/files", response_model=ApiFileUploadResponse, status_code=201)
    async def upload_reference_file(request: Request, file: UploadFile = File(...)):
        """Загрузить файл-референс. Возвращает file_id, который используется в refs_per_language.
        v1.0.8: добавлена валидация — size 1B–20MB, magic-bytes для image/*."""
        uid = _user_id(request)
        content = await file.read()
        size = len(content)
        # Валидация: непустой и не больше лимита
        if size == 0:
            raise HTTPException(status_code=422, detail="file is empty")
        if size > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"file too large: {size} bytes (max {_MAX_UPLOAD_BYTES} = 20 MB)",
            )
        # Magic-bytes: доверяем содержимому, не Content-Type заголовку
        mime = _detect_image_mime(content)
        if not mime.startswith("image/"):
            raise HTTPException(
                status_code=415,
                detail=f"unsupported file type: {mime or 'unknown'}, only image/* allowed (JPEG/PNG/WEBP/GIF)",
            )
        # OK — сохраняем
        user_dir = upload_dir / uid
        user_dir.mkdir(parents=True, exist_ok=True)
        file_id = secrets.token_urlsafe(6)[:8].replace("-", "_")
        file_id = re.sub(r"[^\w]", "", file_id) or uuid.uuid4().hex[:8]
        safe_name = re.sub(r"[^\w\-\.]", "_", (file.filename or "upload"))[:80]
        dest = user_dir / f"{file_id}_{safe_name}"
        with open(dest, "wb") as f:
            f.write(content)
        return ApiFileUploadResponse(
            file_id=file_id,
            filename=safe_name,
            size=size,
        )

    # ---- Jobs: create ----

    @router.post("/jobs", response_model=ApiJobCreateResponse, status_code=201)
    def create_job(body: ApiCoverJobCreate, request: Request):
        """Создать новое задание генерации обложек."""
        uid = _user_id(request)
        # v1.1.0: cap pending-джоб на юзера (анти-затопление очереди / cost-drain)
        _max_pending = _technical_queue_limit()
        _pending = queue.count_reserved_for_user(uid)
        if _pending >= _max_pending:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"too many pending/resetting jobs ({_pending}); "
                    f"max {_max_pending}"
                ),
            )
        mgr = get_preset_manager(uid)
        preset = mgr.get(body.preset_name)
        if not preset:
            raise HTTPException(
                status_code=422,
                detail=f"preset '{body.preset_name}' not found. См. GET /api/v1/presets",
            )

        # Валидация языков (нормализация + проверка)
        # _validate_languages принимает сырые входы (ru-name / ISO / en-name)
        # и возвращает canonical русские названия для cover_core.
        normalized = _validate_languages(body.languages)

        # Резолвим file_id → реальные пути
        refs_resolved: Dict[str, str] = {}
        for lang, file_id in (body.refs_per_language or {}).items():
            if not file_id:
                continue
            path = _resolve_file_id(file_id, uid, upload_dir)
            if path is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"file_id '{file_id}' for language '{lang}' not found. Upload via POST /api/v1/files",
                )
            refs_resolved[lang] = str(path)

        # v1.3.0: пол попыток снижен 5→2 (анти cost-drain; см. cover_server)
        if getattr(preset, "max_generation_retries", 0) < 1:
            preset.max_generation_retries = 2

        # v1.0.4 title-mode: titles_per_lang Pydantic уже привёл к canonical (русские названия).
        # Если mode=title, languages в body уже нормализованы через _resolve_language в model_validator,
        # поэтому второй вызов _validate_languages выше вернёт тот же list.
        title_urls = []
        titles_per_lang_json = "{}"
        extra_prompt = ""
        if body.mode == "title":
            titles_per_lang_json = json.dumps(body.titles_per_lang, ensure_ascii=False)
            extra_prompt = body.extra_prompt
            urls_json = "[]"
        else:
            urls_json = json.dumps(body.urls, ensure_ascii=False)

        user_output = get_user_output_dir(uid)
        job = Job(
            urls=urls_json,
            preset_data=json.dumps(asdict(preset), ensure_ascii=False),
            output_dir=str(user_output),
            languages=json.dumps(normalized, ensure_ascii=False),
            refs_per_language=json.dumps(refs_resolved, ensure_ascii=False),
            mode=body.mode,
            titles_per_lang=titles_per_lang_json,
            extra_prompt=extra_prompt,
            max_threads=(
                max(1, min(25, int(max_threads_resolver())))
                if max_threads_resolver is not None else 8
            ),
            parallel=False,
            user_id=uid,
            generation_provider=getattr(preset, "generation_provider", "") or "flow",
        )
        ensure_server_keys_for_job(job)
        try:
            job_id = queue.add_job(
                job,
                max_pending_for_user=_max_pending,
            )
        except PendingJobLimitExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"too many pending/resetting jobs ({exc.pending}); "
                    f"max {exc.limit}"
                ),
            ) from exc
        return ApiJobCreateResponse(job_id=job_id, status="pending")

    # ---- Jobs: list ----

    @router.get("/jobs", response_model=ApiJobListResponse)
    def list_jobs(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        status: str = Query("", description="фильтр по статусу"),
    ):
        uid = _user_id(request)
        all_jobs = queue.get_all_jobs()
        my = [j for j in all_jobs if job_belongs_to_user(j, uid)]
        if status:
            my = [j for j in my if j.status == status]
        my.sort(key=lambda j: getattr(j, "created_at", 0) or 0, reverse=True)
        total = len(my)
        page = my[offset:offset + limit]
        items = [
            ApiJobListItem(
                id=j.id,
                status=j.status,
                created_at=j.created_at,
                progress_percent=j.progress_percent,
                results_count=getattr(j, "results_count", 0),
            )
            for j in page
        ]
        return ApiJobListResponse(items=items, total=total, limit=limit, offset=offset)

    # ---- Jobs: detail ----

    @router.get("/jobs/{job_id}", response_model=ApiJobResponse)
    def get_job(job_id: str, request: Request):
        uid = _user_id(request)
        job = queue.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if not job_belongs_to_user(job, uid):
            raise HTTPException(status_code=403, detail="access denied")
        return _job_to_response(job)

    # ---- Jobs: cancel (DELETE) ----

    @router.delete("/jobs/{job_id}", status_code=202)
    def cancel_job(job_id: str, request: Request):
        """Graceful cancel: меняет статус на cancelled, не удаляет запись."""
        uid = _user_id(request)
        job = queue.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if not job_belongs_to_user(job, uid):
            raise HTTPException(status_code=403, detail="access denied")
        ok = queue.cancel_job(job_id)
        if not ok:
            return JSONResponse(
                status_code=409,
                content={"detail": f"cannot cancel job in status '{job.status}'"},
            )
        return {"status": "cancelled", "job_id": job_id}

    # ---- Jobs: download by language ----

    @router.get("/jobs/{job_id}/files/{lang_code}")
    def download_by_lang(job_id: str, lang_code: str, request: Request):
        """Скачать обложку(и) по коду языка. Если файлов несколько — ZIP."""
        uid = _user_id(request)
        job = queue.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if not job_belongs_to_user(job, uid):
            raise HTTPException(status_code=403, detail="access denied")

        # Принимаем и код (ru), и русское название (Русский)
        code = lang_code.strip()
        if code in LANG_DISPLAY_TO_CODE:
            code = LANG_DISPLAY_TO_CODE[code]
        code = code.lower()

        try:
            out_files = json.loads(job.output_files or "[]")
        except Exception:
            out_files = []
        matched = [rel for rel in out_files if Path(rel).name.lower().startswith(code + "_")]
        if not matched:
            raise HTTPException(status_code=404, detail=f"no files for language code '{code}'")

        out_dir = Path(job.output_dir) if job.output_dir else output_dir
        existing = [out_dir / rel for rel in matched if (out_dir / rel).exists()]
        if not existing:
            raise HTTPException(status_code=404, detail="files referenced but missing on disk")

        if len(existing) == 1:
            p = existing[0]
            return FileResponse(str(p), filename=p.name)

        # ZIP с несколькими
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in existing:
                zf.write(p, arcname=p.name)
        buf.seek(0)
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="job_{job_id}_{code}.zip"'},
        )

    # ---- Jobs: retry (full or incremental) ----

    @router.post("/jobs/{job_id}/retry", response_model=ApiJobResponse)
    def retry_job(
        job_id: str,
        request: Request,
        full: bool = Query(False, description="true = wipe (как /retry), false = resume (как /resume)"),
    ):
        uid = _user_id(request)
        job = queue.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if not job_belongs_to_user(job, uid):
            raise HTTPException(status_code=403, detail="access denied")

        terminal_or_partial = (
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
            JobStatus.COMPLETED_PARTIAL.value,
        )
        if job.status not in terminal_or_partial:
            # Resume также разрешён для completed с fallback'ами
            has_fb = False
            try:
                has_fb = len(json.loads(job.fallback_files or "[]")) > 0
            except Exception:
                pass
            if not (job.status == JobStatus.COMPLETED.value and has_fb and not full):
                raise HTTPException(
                    status_code=409,
                    detail=f"cannot retry job in status '{job.status}'",
                )
        previous_status = job.status

        if full:
            # Полный wipe — как POST /jobs/{id}/retry
            job.output_files = "[]"
            job.fallback_files = "[]"
            job.unchecked_files = "[]"
            job.results_count = 0

        ensure_server_keys_for_job(job)
        # Общие сбросы (для retry и resume)
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
                max_pending_for_user=_technical_queue_limit(),
            )
        except PendingJobLimitExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"too many pending/resetting jobs ({exc.pending}); "
                    f"max {exc.limit}"
                ),
            ) from exc
        if not updated:
            raise HTTPException(status_code=409, detail="job status changed; refresh and retry")
        return _job_to_response(job)

    return router
