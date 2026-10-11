# -*- coding: utf-8 -*-
"""
Cover Generator — ядро генерации обложек для YouTube.
Без GUI. Используется приложением и CLI.

Зависимости: pip install google-generativeai openai Pillow requests
"""

import os
import re
import json
import time
import base64
import hashlib
import warnings
import threading
import requests
from io import BytesIO
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from provider_policy import (
    canonical_generation_provider,
    effective_simple_mode_provider,
    provider_policy,
)

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# 03.09.2026: журнал платных вызовов. Клиент лежит в соседнем каталоге workers/,
# пакета нет — путь добавляем точечно и в КОНЕЦ sys.path, чтобы ничего не затенить.
# Импорт мягкий: движок обложек запускают и отдельно (CLI, ноутбук), там журнала может
# не быть вовсе, и его отсутствие не должно ронять генерацию.
try:
    import sys as _sys
    _WORKERS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workers")
    if os.path.isdir(_WORKERS_DIR) and _WORKERS_DIR not in _sys.path:
        _sys.path.append(_WORKERS_DIR)
    from call_journal import CallJournal
except Exception:                                                     # noqa: BLE001
    CallJournal = None


try:
    warnings.filterwarnings("ignore", message=".*Pydantic V1.*Python 3.14.*")
    import openai
    import httpx
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    httpx = None  # type: ignore


# ════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ════════════════════════════════════════════════════════

PRESETS_DIR = os.path.join(os.path.expanduser("~"), "cover_presets")
PRESETS_FILE = os.path.join(PRESETS_DIR, "presets.json")
REFS_DIR = os.path.join(PRESETS_DIR, "refs")
OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "cover_output")

for d in (PRESETS_DIR, REFS_DIR, OUTPUT_DIR):
    os.makedirs(d, exist_ok=True)

# GEMINI_MODEL удалён (v1.0.3) — Gemini SDK больше не используется
GPT_MODEL = "gpt-4.1-mini"

# Fast Gen API — несколько провайдеров (Flow, Whisk, Gemini)

# ─── событийный лог сервиса ───────────────────────────────────────────────────
# Ошибки провайдеров раньше жили только внутри лога конкретной задачи (в базе), а в
# файле сервера лежали одни HTTP-запросы. Когда обложки посыпались, причину пришлось
# выкапывать из базы задним числом, и тело ответа провайдера в ней не сохранялось.
# Пишем важное отдельной строкой в файл: его видно сразу и он переживает перезапуск.
import logging as _logging

_EVENTS_PATH = os.environ.get("COVER_EVENTS_LOG", "/root/cover_tool/cover_events.log")
_events_log = _logging.getLogger("cover.events")
if not _events_log.handlers:
    try:
        _h = _logging.FileHandler(_EVENTS_PATH, encoding="utf-8")
        _h.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _events_log.addHandler(_h)
        _events_log.setLevel(_logging.INFO)
        _events_log.propagate = False
    except Exception:
        pass


def log_event(level: str, msg: str) -> None:
    """Записать событие сервиса в общий файл. Молча переживает любую беду с файлом."""
    try:
        getattr(_events_log, level, _events_log.info)(msg)
    except Exception:
        pass


FAST_GEN_API_BASE = os.environ.get("FAST_GEN_API_BASE", "https://api.fast-gen.ai")

# v1.1.0 security: SSRF / key-exfil guard. base_url присылается в пресете юзером —
# разрешаем ТОЛЬКО официальный хост Fast Gen, иначе форсим FAST_GEN_API_BASE.
# Иначе тенант мог увести owner-ключ (он уходит в X-API-Key) на свой хост или в 169.254.169.254.
from urllib.parse import urlparse as _urlparse
_FASTGEN_ALLOWED_HOST = (_urlparse(FAST_GEN_API_BASE).hostname or "api.fast-gen.ai").lower()


def _safe_fast_gen_base(url: str) -> str:
    """Возвращает url только если это https на разрешённом хосте Fast Gen; иначе FAST_GEN_API_BASE."""
    if not url:
        return FAST_GEN_API_BASE
    try:
        p = _urlparse(str(url).strip())
        if p.scheme == "https" and (p.hostname or "").lower() == _FASTGEN_ALLOWED_HOST:
            return str(url).strip().rstrip("/")
    except Exception:
        pass
    return FAST_GEN_API_BASE

# Все варианты генерации для UI: (подпись, provider, model_code)
# Flow = разные модели (Nano, Nano Pro, Imagen 4, Nano Banana 2); Whisk = Imagen 4; Gemini = Imagen
GENERATION_OPTIONS = [
    # v1.0.18: убраны "Flow — Nano" (GEM_PIX) и "Flow — Imagen 4" (IMAGEN_3_5) —
    # Fast Gen отдаёт на них HTTP 422 (модели сняты). Остались только живые.
    ("Flow — Nano Pro", "flow", "GEM_PIX_2"),
    ("Flow — Nano Banana 2", "flow", "NARWHAL"),
    ("Nano Banana 2 Flower", "flower", ""),
    ("Grok — Quality", "grok", "quality"),
    ("Grok — Speed", "grok", "speed"),
    # Прямой xAI: платно с личного счёта, поэтому в интерфейсе только у админа
    # (фильтр ADMIN_ONLY_PROVIDERS в api_cover.py).
    ("Grok — официальный API", "grok_api", ""),
    ("OpenAI — gpt-image", "openai_img", ""),
    ("Nano Banana Pro (VeoNonStop)", "veo", "GEM_PIX_2"),
    ("Nano Banana 2 (VeoNonStop)", "veo", "NARWHAL"),
    ("Nano Banana 2 Lite (VeoNonStop)", "veo", "HARBOR_SEAL"),
    ("GPT Image 2.5 low (OpenAI)", "gpt_image2", ""),
]
THUMBNAIL_URLS = [
    "https://img.youtube.com/vi/{vid}/maxresdefault.jpg",
    "https://img.youtube.com/vi/{vid}/sddefault.jpg",
    "https://img.youtube.com/vi/{vid}/hqdefault.jpg",
]

# Код языка для имени файла (в начало): русское название -> короткий код
LANG_DISPLAY_TO_CODE = {
    "Азербайджанский": "az", "Английский": "en", "Арабский": "ar", "Армянский": "hy",
    "Белорусский": "be", "Болгарский": "bg", "Боснийский": "bs", "Венгерский": "hu",
    "Вьетнамский": "vi", "Греческий": "el", "Грузинский": "ka", "Датский": "da",
    "Иврит": "he", "Индонезийский": "id", "Испанский": "es", "Итальянский": "it",
    "Казахский": "kk", "Каталанский": "ca", "Китайский (упрощённый)": "zh",
    "Корейский": "ko", "Латышский": "lv", "Литовский": "lt", "Македонский": "mk",
    "Немецкий": "de", "Нидерландский": "nl", "Норвежский": "no",
    "Персидский (фарси)": "fa", "Польский": "pl", "Португальский": "pt",
    "Румынский": "ro", "Русский": "ru", "Сербский": "sr", "Словацкий": "sk",
    "Словенский": "sl", "Тайский": "th", "Турецкий": "tr", "Узбекский": "uz",
    "Украинский": "uk", "Финский": "fi", "Французский": "fr", "Хинди": "hi",
    "Хорватский": "hr", "Чешский": "cs", "Шведский": "sv", "Эстонский": "et",
    "Японский": "ja",
}


def get_lang_code(lang_display: str) -> str:
    """Возвращает короткий код языка для имени файла (ru, en, uk ...)."""
    code = LANG_DISPLAY_TO_CODE.get((lang_display or "").strip())
    if code:
        return code
    return re.sub(r"[^\w\-]", "_", (lang_display or "xx")[:8])


def _output_filename(lang_code: str, video_id: str, preset_name: str, video_title: str = "") -> str:
    """Имя файла: код_языка + (название ролика) + video_id + пресет."""
    safe_name = re.sub(r'[^\w\-]', '_', video_id)
    safe_title = ""
    if (video_title or "").strip():
        safe_title = re.sub(r'[^\w\-]', '_', video_title.strip())[:50].strip("_")
    if safe_title:
        return f"{lang_code}_{safe_title}_{safe_name}_{preset_name}.jpg"
    return f"{lang_code}_{safe_name}_{preset_name}.jpg"


def _safe_folder_name(video_title: str, video_id: str, fallback: str = "cover") -> str:
    """Имя папки для сохранения по названию ролика (без недопустимых символов)."""
    raw = (video_title or video_id or fallback).strip()
    safe = re.sub(r'[^\w\-]', '_', raw)[:80].strip("_")
    return safe or video_id or fallback


def _safe_video_name_for_file(video_title: str, video_id: str, fallback: str = "cover") -> str:
    """Короткое имя ролика для имени файла (No text_xxx.jpg)."""
    raw = (video_title or video_id or fallback).strip()
    safe = re.sub(r'[^\w\-]', '_', raw)[:50].strip("_")
    return safe or video_id or fallback


# ════════════════════════════════════════════════════════
#  ПРЕСЕТЫ
# ════════════════════════════════════════════════════════

@dataclass
class Preset:
    name: str = ""
    language: str = "Русский"
    languages: str = ""                    # несколько языков через запятую для массовой генерации, напр. "Русский,English,Українська"
    font_path: str = ""                    # путь к .ttf файлу
    text_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: int = 3
    text_position: str = "center"          # center, top, bottom, left, right
    text_size_pct: int = 15                # % от высоты картинки
    style_prompt: str = ""                 # промпт-шаблон стиля (только при ручном стиле)
    use_auto_style: bool = True            # True = ИИ анализирует превью и берёт стиль; False = использовать style_prompt
    fallback_mode: str = "auto"            # auto / always_overlay / never_overlay
    generation_provider: str = "flow"      # flow | whisk | gemini
    generation_model: str = "GEM_PIX_2"   # для flow: GEM_PIX | GEM_PIX_2 | IMAGEN_3_5 | NARWHAL; для whisk/gemini пусто
    fast_gen_base_url: str = ""           # пусто = FAST_GEN_API_BASE
    analysis_provider: str = "openai"     # google | openai — кто анализирует обложку и референс
    analysis_model: str = "gpt-4.1-mini"  # модель (gpt-4.1-mini, gpt-4o, gemini-2.0-flash и т.д.)
    aspect_ratio: str = "16:9"
    created_at: str = ""
    updated_at: str = ""
    max_generation_retries: int = 5   # число попыток генерации (API + проверка)
    debug_mode: bool = False          # полные логи ошибок в job.log
    simple_mode: bool = False         # vS: img2img — реальное превью → модель редактирует (перевод текста + смена поз), без vision-анализа и сборки промпта
    reference_image: str = ""         # vR: file_id/путь сохранённого референса стиля. Если задан — двухкартиночный style-transfer (контент+перевод из обложки, стиль из референса)
    prompt_template: str = ""         # seed-mode: шаблон-пресет ниши (делается ОДИН раз). {ЯЗЫК}→язык словом, маркер '>>> СЮДА ВСТАВЬ СЕМЯ <<<'/{СЕМЯ}/{SEED}→блок семени. Пусто = промпт берётся из семени как есть (passthrough)
    seed_reference_image: str = ""    # seed-mode: file_id общего референса (НЕ путать с reference_image url-режима)
    seed_reference_filename: str = "" # seed-mode: исходное имя файла референса (для отображения в UI)
    seed_n_variants: int = 3          # seed-mode: число вариантов на язык по умолчанию (1..8)


class PresetManager:
    """Управление пресетами: загрузка, сохранение, CRUD (потокобезопасно)."""

    def __init__(self, path: str = PRESETS_FILE):
        self.path = path
        self._presets: Dict[str, Preset] = {}
        self._lock = threading.RLock()
        self.load()

    def load(self):
        with self._lock:
            try:
                if os.path.exists(self.path):
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    default_preset = Preset()
                    defaults = {f.name: getattr(default_preset, f.name) for f in fields(Preset)}
                    field_names = {f.name for f in fields(Preset)}
                    self._presets = {}
                    migrated = False
                    for k, v in data.items():
                        merged = {**defaults, **{key: val for key, val in v.items() if key in field_names}}
                        # Авто-миграция (v1.0.3): analysis_provider="google" → "openai"
                        # Gemini SDK удалён, провайдер "google" больше не поддерживается.
                        if merged.get("analysis_provider") in ("google", "gemini"):
                            print(f"[PresetManager] auto-migration: '{k}'.analysis_provider={merged.get('analysis_provider')!r} → 'openai' (Gemini SDK removed in v1.0.3)")
                            merged["analysis_provider"] = "openai"
                            # gemini-модель тоже сбросить на дефолтную OpenAI-модель
                            if str(merged.get("analysis_model","")).startswith("gemini"):
                                merged["analysis_model"] = "gpt-4.1-mini"
                            migrated = True
                        # Авто-миграция: старые пресеты с устаревшей моделью → gpt-4.1-mini
                        if merged.get("analysis_provider") == "openai" and merged.get("analysis_model") == "gpt-4o-mini":
                            merged["analysis_model"] = "gpt-4.1-mini"
                            migrated = True
                        # Принудительно поднимаем retries до 5 если меньше (старый дефолт был 3)
                        if merged.get("max_generation_retries", 3) < 5:
                            merged["max_generation_retries"] = 5
                            migrated = True
                        self._presets[k] = Preset(**merged)
                    if migrated:
                        self._save_unlocked()
            except Exception:
                self._presets = {}

    def _save_unlocked(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({k: asdict(v) for k, v in self._presets.items()}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[PresetManager] Ошибка сохранения: {e}")

    def save(self):
        with self._lock:
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump({k: asdict(v) for k, v in self._presets.items()}, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[PresetManager] Ошибка сохранения: {e}")

    def get(self, name: str) -> Optional[Preset]:
        with self._lock:
            return self._presets.get(name)

    def list_names(self) -> List[str]:
        with self._lock:
            return list(self._presets.keys())

    def save_preset(self, preset: Preset):
        with self._lock:
            now = datetime.now().isoformat()
            if not preset.created_at:
                preset.created_at = now
            preset.updated_at = now
            self._presets[preset.name] = preset
            self.save()

    def delete_preset(self, name: str):
        with self._lock:
            if name in self._presets:
                del self._presets[name]
                self.save()

    def duplicate(self, src_name: str, new_name: str) -> Optional[Preset]:
        with self._lock:
            src = self._presets.get(src_name)
            if not src:
                return None
            new = Preset(**asdict(src))
            new.name = new_name
            new.created_at = ""
            self.save_preset(new)
            return new


# ════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ════════════════════════════════════════════════════════

# v1.3.0: in-process кэш анализа по video_id — не пере-платим за vision-вызовы
# при повторных задачах на тот же URL (TTL; keyed by provider/model/auto_style).
_ANALYSIS_CACHE_TTL = float(os.environ.get("COVER_ANALYSIS_CACHE_TTL", "21600"))  # 6 ч
_ANALYSIS_CACHE_MAX = 500
_analysis_cache: Dict[str, tuple] = {}
_analysis_cache_lock = threading.Lock()


def _analysis_cache_get(key: str):
    with _analysis_cache_lock:
        ent = _analysis_cache.get(key)
        if ent and (time.time() - ent[0]) < _ANALYSIS_CACHE_TTL:
            import copy
            return copy.deepcopy(ent[1]), copy.deepcopy(ent[2])
        if ent:
            _analysis_cache.pop(key, None)
    return None


def _analysis_cache_put(key: str, analysis: dict, style):
    if not analysis:
        return
    import copy
    with _analysis_cache_lock:
        if len(_analysis_cache) >= _ANALYSIS_CACHE_MAX:
            oldest = min(_analysis_cache.items(), key=lambda kv: kv[1][0])[0]
            _analysis_cache.pop(oldest, None)
        _analysis_cache[key] = (time.time(), copy.deepcopy(analysis), copy.deepcopy(style))


def extract_video_id(url: str) -> Optional[str]:
    """Извлекает video ID из любого формата YouTube ссылки."""
    patterns = [
        r"(?:v=|\/v\/)([\w-]{11})",
        r"(?:youtu\.be\/)([\w-]{11})",
        r"(?:embed\/)([\w-]{11})",
        r"(?:shorts\/)([\w-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    # Может быть просто ID
    if re.match(r"^[\w-]{11}$", url.strip()):
        return url.strip()
    return None


def get_youtube_video_title(url: str, timeout: int = 10) -> Optional[str]:
    """Получает название ролика через YouTube oEmbed (без API ключа)."""
    vid = extract_video_id(url)
    if not vid:
        return None
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"},
            timeout=timeout,
        )
        if r.status_code == 200:
            data = r.json()
            return (data.get("title") or "").strip() or None
    except Exception:
        pass
    return None


def download_thumbnail(url: str, timeout: int = 15) -> Optional[Image.Image]:
    """Скачивает превью YouTube видео. Пробует разные разрешения."""
    vid = extract_video_id(url)
    if not vid:
        return None
    for tpl in THUMBNAIL_URLS:
        try:
            r = requests.get(tpl.format(vid=vid), timeout=timeout)
            if r.status_code == 200 and len(r.content) > 5000:
                img = Image.open(BytesIO(r.content)).convert("RGB")
                if img.size[0] >= 200:
                    return img
        except Exception:
            continue
    return None


def image_to_base64(img: Image.Image, fmt: str = "JPEG") -> str:
    buf = BytesIO()
    img.save(buf, format=fmt, quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def load_image(path: str) -> Optional[Image.Image]:
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


# ════════════════════════════════════════════════════════
#  ШАГ 3: АНАЛИЗ ОБЛОЖКИ (OpenAI GPT; Gemini SDK удалён в v1.0.3)
# ════════════════════════════════════════════════════════

# Дефолтная модель для анализа (если в пресете пусто)
DEFAULT_ANALYSIS_MODEL_OPENAI = "gpt-4.1-mini"
# DEFAULT_ANALYSIS_MODEL_GOOGLE удалён (v1.0.3) — Gemini SDK больше не вызывается

# Промпты загружаются из default_prompts.json (не хардкод в коде)
_DEFAULT_PROMPTS_CACHE: Optional[Dict] = None
_DEFAULT_PROMPTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "default_prompts.json")


def _load_default_prompts() -> Dict:
    """Загружает промпты из default_prompts.json."""
    global _DEFAULT_PROMPTS_CACHE
    if _DEFAULT_PROMPTS_CACHE is not None:
        return _DEFAULT_PROMPTS_CACHE
    try:
        with open(_DEFAULT_PROMPTS_PATH, "r", encoding="utf-8") as f:
            _DEFAULT_PROMPTS_CACHE = json.load(f)
    except Exception:
        _DEFAULT_PROMPTS_CACHE = {}
    return _DEFAULT_PROMPTS_CACHE


def get_default_prompt(key: str) -> str:
    """Возвращает промпт по ключу из default_prompts.json."""
    return _load_default_prompts().get(key, "")


_prompts_local = threading.local()


def set_prompts_config(config: Dict) -> None:
    """Устанавливает кастомные промпты (thread-local, безопасно для многопоточности)."""
    _prompts_local.config = dict(config) if config else {}


def get_prompts_config() -> Dict:
    """Возвращает текущий конфиг промптов (thread-local)."""
    return dict(getattr(_prompts_local, 'config', {}))


def _active_prompts() -> Dict:
    """Возвращает промпты текущего потока."""
    return getattr(_prompts_local, 'config', {})


# _call_gemini_vision и _gemini_lock удалены (v1.0.3) — Gemini SDK больше не используется.
# Все вызовы _call_vision с provider="google" автоматически попадают в OpenAI-ветку с warning.


_openai_clients: Dict[str, object] = {}
_openai_clients_lock = threading.Lock()


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return max(1, default)


# Server-owned per-credential ceilings. Клиентский payload их не меняет.
OPENAI_INFLIGHT_LIMIT = _positive_env_int("COVER_OPENAI_INFLIGHT", 6)
VEO_INFLIGHT_LIMIT = _positive_env_int("COVER_VEO_INFLIGHT", 4)
GPT_IMAGE2_INFLIGHT_LIMIT = _positive_env_int("COVER_GPT_IMAGE2_INFLIGHT", 2)
XAI_INFLIGHT_LIMIT = _positive_env_int("COVER_XAI_INFLIGHT", 2)
_OPENAI_SEMAPHORE = threading.BoundedSemaphore(OPENAI_INFLIGHT_LIMIT)
_VEO_SEMAPHORE = threading.BoundedSemaphore(VEO_INFLIGHT_LIMIT)
_GPT_IMAGE2_SEMAPHORE = threading.BoundedSemaphore(GPT_IMAGE2_INFLIGHT_LIMIT)
_XAI_SEMAPHORE = threading.BoundedSemaphore(XAI_INFLIGHT_LIMIT)
_provider_key_semaphores = {
    "openai": {},
    "veo": {},
    "gpt_image2": {},
    "xai": {},
}
_provider_key_semaphores_lock = threading.Lock()


def _credential_id(api_key: str) -> str:
    """Stable non-secret map key; raw credentials stay only in call context."""
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()


def _provider_semaphore_for_key(kind: str, api_key: str):
    """One safe ceiling per unique credential; duplicate values share capacity."""
    defaults = {
        "openai": (_OPENAI_SEMAPHORE, OPENAI_INFLIGHT_LIMIT),
        "veo": (_VEO_SEMAPHORE, VEO_INFLIGHT_LIMIT),
        "gpt_image2": (_GPT_IMAGE2_SEMAPHORE, GPT_IMAGE2_INFLIGHT_LIMIT),
        "xai": (_XAI_SEMAPHORE, XAI_INFLIGHT_LIMIT),
    }
    default_semaphore, capacity = defaults[kind]
    credential_id = _credential_id(api_key)
    with _provider_key_semaphores_lock:
        semaphores = _provider_key_semaphores[kind]
        semaphore = semaphores.get(credential_id)
        if semaphore is None:
            # Preserve exact one-key/monkeypatch behavior for the first credential.
            semaphore = (
                default_semaphore
                if not semaphores
                else threading.BoundedSemaphore(capacity)
            )
            semaphores[credential_id] = semaphore
        return semaphore


def _acquire_cancelable(semaphore, cancel_check=None, step: float = 0.25) -> bool:
    """Ждёт provider slot короткими интервалами и не игнорирует cancel."""
    while True:
        if cancel_check and cancel_check():
            return False
        if semaphore.acquire(timeout=step):
            return True


def _chat_route(api_key: str, model: str):
    """Клиент, модель и фактический ключ для вызова модели у обложек.

    Одна точка на все три места (зрение, адаптация текста, сцена по названию): разойдясь,
    они молча дают разный маршрут, и починка одного не чинит остальные — ровно это и
    случилось 21.09.
    """
    base, alt_key, alt_model = _alt_chat_route()
    if base:
        # Модель берётся ЕГО: у стороннего провайдера свой список, и модели OpenAI там
        # может не быть вовсе — тогда каждый вызов вернул бы 400.
        return _openai_client(alt_key, base), (alt_model or model), alt_key
    return _openai_client(api_key), model, api_key


def _openai_client(api_key: str, base_url: str = ""):
    """Клиент OpenAI с кэшированием (один клиент на пару ключ+адрес, потокобезопасно)."""
    # 21.09.2026: пакета openai в окружении не было, и анализ обложки падал здесь
    # с `NameError: name 'openai' is not defined` — сообщением, по которому нельзя
    # понять ни что сломано, ни что делать. Флаг OPENAI_AVAILABLE при этом честно
    # выставлялся импортом и не читался НИГДЕ. Теперь читается.
    if not OPENAI_AVAILABLE:
        raise RuntimeError(
            "не установлен пакет openai — анализ и генерация обложек через OpenAI "
            "невозможны. Деньги не потрачены, повтор не поможет. "
            "Лечится установкой: .venv/bin/pip install openai"
        )
    with _openai_clients_lock:
        ck = (api_key, base_url or "")
        if ck not in _openai_clients:
            http_client = httpx.Client() if httpx is not None else None
            kw = {"api_key": api_key, "http_client": http_client}
            if base_url:
                kw["base_url"] = base_url
            _openai_clients[ck] = openai.OpenAI(**kw)
        return _openai_clients[ck]


def _alt_chat_route():
    """Куда слать ЛЮБОЙ вызов модели у обложек: (адрес, ключ, модель) либо пусто.

    Имя сперва было `_alt_vision_route`, и это оказалось ловушкой: маршрут применили
    только к зрению, а `adapt_text` и `generate_scene_from_title` остались ходить в
    OpenAI напрямую. 21.09 это стоило владельцу готовой обложки — анализ прошёл через
    прокси, распознал текст и стиль, а следующий же шаг «Адаптация текста» упал с
    429 «You have no credits remaining», и задача завершилась частично, без файла.

    21.09.2026: у счёта OpenAI ноль, а весь анализ обложки в этом форке ходит ТОЛЬКО
    в OpenAI — Gemini-SDK удалён в v1.0.3, и provider="google" молча падает обратно
    на него. То есть без денег обложка «новой сценой» невозможна в принципе.

    В бою эта развилка уже решена теми же тремя переменными (COVER_ALT_*): анализ
    уходит на сторонний OpenAI-совместимый прокси, который умеет зрение. Берём тот же
    путь, а не изобретаем свой.

    Ключ обязан ехать вместе с адресом. Задать чужой адрес и оставить ключ OpenAI —
    значит отправить секрет туда, где он не предназначен, и никогда об этом не узнать,
    поэтому адрес без ключа игнорируется целиком, а вызывающий об этом кричит.
    """
    base = (os.environ.get("COVER_ALT_BASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("COVER_ALT_API_KEY") or "").strip()
    model = (os.environ.get("COVER_ALT_MODEL") or "").strip()
    if not base:
        return ("", "", "")
    if not key:
        raise RuntimeError(
            "COVER_ALT_BASE_URL задан без COVER_ALT_API_KEY: ключ OpenAI чужому "
            "провайдеру не отправляется. Денег это не стоило, повтор не поможет. "
            "Задайте ключ этого провайдера или уберите адрес."
        )
    return (base, key, model)


# ─────────────── подписки Codex и Claude: вызов через сервис текста ───────────────
#
# Решение владельца 24.09.2026: подписки должны быть доступны везде, где есть OpenAI.
# У обложек своих кредиталов нет и заводиться не должно: распорядитель подписок в
# системе один — сервис текста. Там пулы, аренда, лимиты и замок вокруг файла входа
# Codex; второй распорядитель той же подписки означал бы потерянный одноразовый токен
# обновления и разлогиненную подписку. Поэтому обложки просят вызов, а не делают его.
SUBSCRIPTION_PROVIDERS = ("codex", "claude")
# 04.10.2026: 300 с не хватало. Подписка Codex общая с рерайтом, промптами картинок и
# названиями; под нагрузкой ответ из одного слова шёл 124 с, сверка двух картинок
# не укладывалась в 300 с — и каждую обложку студия перезаказывала за деньги.
SUBSCRIPTION_TIMEOUT = float(os.environ.get("COVER_SUBSCRIPTION_TIMEOUT", "900"))


def _subscription_call(provider: str, messages: list, model: str = "",
                       images: list | None = None) -> str:
    """Один вызов подпиской. Картинки принимает только Codex — так устроен его CLI."""
    import json as _json
    import urllib.error
    import urllib.request

    base = (os.environ.get("VS_REWRITE_BASE") or "http://127.0.0.1:8899").rstrip("/")
    key = os.environ.get("COVER_TEXT_SERVICE_KEY") or ""
    if not key:
        raise RuntimeError(
            "не задан ключ доступа к сервису текста (COVER_TEXT_SERVICE_KEY): "
            "подписками распоряжается он, а не обложки")
    payload = {"provider": provider, "model": model or "",
               "messages": messages, "images": images or []}
    req = urllib.request.Request(
        base + "/api/v1/chat-once",
        data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=SUBSCRIPTION_TIMEOUT) as resp:
            body = _json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str((_json.loads(exc.read() or b"{}") or {}).get("detail") or "")
        except Exception:                                          # noqa: BLE001
            pass
        raise RuntimeError(f"подписка {provider}: {detail or exc.reason}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"сервис текста не ответил: {exc.reason}")
    text = str((body or {}).get("text") or "")
    if not text.strip():
        raise RuntimeError(f"подписка {provider} вернула пустой ответ")
    return text


def _subscription_vision(images_list, prompt: str, provider: str, model: str) -> str:
    """Анализ картинки подпиской. Картинки уходят теми же PNG-base64, что и в OpenAI."""
    # 04.10.2026: две картинки PNG в полном размере (обложка 1376x768 + превью донора)
    # весят ~4 МБ в base64 — сервис текста отвечал 413 (REWRITE_MAX_JSON_BODY_BYTES), и
    # сверка надписи не выполнялась ни разу: каждая обложка студии шла в брак. Для чтения
    # надписи хватает 1024 точек по длинной стороне.
    blobs = []
    for im in images_list:
        small = im.copy()
        small.thumbnail((1024, 1024))
        blobs.append(image_to_base64(small, "PNG"))
    return _subscription_call(provider, [{"role": "user", "content": prompt}],
                              model=model, images=blobs)


def _call_openai_vision(image, prompt: str, api_key: str, model: str) -> str:
    """Вызов OpenAI (GPT-4o и др.) с картинкой. Возвращает текст ответа.
    image — одна PIL.Image ИЛИ список картинок (vS: 2-image сверка оригинал↔результат)."""
    images = list(image) if isinstance(image, (list, tuple)) else [image]
    content = [{"type": "text", "text": prompt}]
    for im in images:
        b64 = image_to_base64(im, "PNG")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    client, model, used_key = _chat_route(api_key, model)
    with _provider_semaphore_for_key("openai", used_key):
        resp = client.chat.completions.create(
            model=model or DEFAULT_ANALYSIS_MODEL_OPENAI,
            messages=[{"role": "user", "content": content}],
            max_tokens=1024,
        )
    return resp.choices[0].message.content or ""


_GOOGLE_PROVIDER_WARNED = False


def _call_vision(
    image: Image.Image,
    prompt: str,
    provider: str,
    model: str,
    gemini_api_key: str = "",   # legacy, игнорируется (v1.0.3)
    openai_api_key: str = "",
) -> str:
    """Анализ картинки через OpenAI (Gemini SDK удалён в v1.0.3).
    provider="google" сохранён для backward-compat — auto-fallback на OpenAI."""
    provider = (provider or "openai").strip().lower()
    if provider in SUBSCRIPTION_PROVIDERS:
        images = list(image) if isinstance(image, (list, tuple)) else [image]
        if provider == "claude":
            # Не притворяемся, что справились: у подписки Claude вход — одна строка,
            # инструменты ей запрещены, передать картинку нечем.
            raise RuntimeError(
                "подписка Claude не умеет смотреть картинки — для анализа превью "
                "выберите Codex или ключ провайдера")
        return _subscription_vision(images, prompt, provider, model)
    if provider == "google" or provider == "gemini":
        global _GOOGLE_PROVIDER_WARNED
        if not _GOOGLE_PROVIDER_WARNED:
            print("[cover_core] WARNING: provider='google' deprecated since v1.0.3 — using OpenAI instead", flush=True)
            _GOOGLE_PROVIDER_WARNED = True
        provider = "openai"
        model = ""  # принудительно дефолтная OpenAI-модель
    # всё кроме явного "openai" тоже идёт в OpenAI (никаких альтернатив больше нет)
    model_name = (model or "").strip() or DEFAULT_ANALYSIS_MODEL_OPENAI
    return _call_openai_vision(image, prompt, openai_api_key, model_name)


def _parse_json_response(text: str) -> Dict:
    """Извлекает JSON из ответа модели (может быть обёрнут в ```json)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Ищем первый сбалансированный JSON-объект
        depth = 0
        start = None
        for i, c in enumerate(text):
            if c == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        start = None
    return {}


def analyze_thumbnail(
    image: Image.Image,
    provider: str,
    model: str,
    gemini_api_key: str = "",   # legacy, игнорируется (v1.0.3)
    openai_api_key: str = "",
    video_title: str = "",
) -> Dict:
    """Анализ обложки: текст, шрифт, положение, сцена. provider: openai (google→openai с warning)."""
    prompt = _active_prompts().get("analyze_prompt") or get_default_prompt("analyze_prompt")
    video_title_val = (video_title or "").strip() or "(not provided)"
    prompt = prompt.replace("{video_title}", video_title_val)
    raw = _call_vision(image, prompt, provider, model, gemini_api_key, openai_api_key)
    return _parse_json_response(raw)


def analyze_style_reference(
    image: Image.Image,
    provider: str,
    model: str,
    gemini_api_key: str = "",   # legacy, игнорируется (v1.0.3)
    openai_api_key: str = "",
    video_title: str = "",
) -> Dict:
    """Анализ референса стиля. video_title — название ролика для контекста. provider: google | openai."""
    prompt = _active_prompts().get("style_analyze_prompt") or get_default_prompt("style_analyze_prompt")
    video_title_val = (video_title or "").strip() or "(not provided)"
    prompt = prompt.replace("{video_title}", video_title_val)
    raw = _call_vision(image, prompt, provider, model, gemini_api_key, openai_api_key)
    return _parse_json_response(raw)


# ════════════════════════════════════════════════════════
#  ШАГ 4: АДАПТАЦИЯ ТЕКСТА (GPT-4o-mini)
# ════════════════════════════════════════════════════════

def generate_scene_from_title(
    title: str,
    extra_prompt: str,
    openai_api_key: str,
    model: str = "",
) -> Dict:
    """v1.0.4 — генерация сцены для title-mode (без YouTube превью).
    На вход: название видео + опциональные доп.детали + openai ключ.
    Возвращает dict в том же формате что analyze_thumbnail (scene_description, mood,
    dominant_colors, characters, visual_tropes). Поле "text" пусто (юзер задаёт
    его per-lang отдельно).
    Все строковые поля ВСЕГДА на английском (для consistency с Fast Gen)."""
    if not title or not title.strip():
        raise ValueError("title must not be empty")
    if not openai_api_key:
        raise ValueError("openai_api_key required")

    model_name = (model or "").strip() or DEFAULT_ANALYSIS_MODEL_OPENAI
    if model_name.lower().startswith("gemini"):  # legacy preset с gemini-моделью → fallback
        model_name = DEFAULT_ANALYSIS_MODEL_OPENAI

    system = (
        "You are a YouTube thumbnail scene designer. Given a video title (in any language) "
        "and optional extra details, you generate a vivid visual scene for an AI image generator.\n\n"
        "Return ONLY JSON (no markdown, no preamble):\n"
        "{\n"
        '  "scene_description": "1-3 sentences in ENGLISH describing subject, setting, lighting, key objects, composition. MUST include: (a) overall image is BRIGHT or MID-tone (not dark/gloomy/black-dominated), (b) main subject has HIGH CONTRAST against background (use rim light, color separation, bright outline, or contrasting fill — never blend), (c) composition reserves clear EMPTY SPACE (top or side, ~30% of frame) for large title text — main subject must NOT occupy the area where text will be placed",\n'
        '  "mood": "one word in ENGLISH: dramatic / bright / vivid / cinematic / energetic / mysterious — AVOID purely dark/black moods",\n'
        '  "dominant_colors": ["#hex1", "#hex2", "#hex3"],\n'
        '  "characters": [{"role": "...", "gender": "...", "age": "...", "expression": "...", "pose": "..."}],\n'
        '  "visual_tropes": "ENGLISH comma-separated list of visual hooks (e.g. neon glow, sunlight rays, broken glass, vibrant colors)",\n'
        '  "composition": "rule-of-thirds",\n'
        '  "main_subject": "1-3 words in ENGLISH describing the focal point (e.g. face, person, object, animal, group)",\n'
        '  "subject_size_pct": 40,\n'
        '  "subject_position": "left-third / right-third — choose one, NEVER center",\n'
        '  "subject_focus": "sharp"\n'
        "}\n\n"
        "COMPOSITION QUANTITATIVE RULES (CRITICAL):\n"
        "- Main subject MUST occupy 35-50% of frame area (LARGE, dominant — not a background element). subject_size_pct in JSON MUST be an integer between 35 and 50.\n"
        "- Use rule of thirds: subject placed in LEFT-third OR RIGHT-third, NEVER dead-center. subject_position MUST be \"left-third\" or \"right-third\".\n"
        "- Subject in SHARP FOREGROUND focus; background soft / shallow depth-of-field. subject_focus MUST be \"sharp\".\n"
        "- Subject silhouette MUST contrast with background (rim light OR colour separation OR brightness gap).\n\n"
        "RULES:\n"
        "- scene_description, mood, characters fields, visual_tropes, main_subject — ALWAYS in English, "
        "regardless of input language (title and extra may be in any language; you understand them but output English).\n"
        "- dominant_colors — 3 hex codes, language-agnostic. Prefer BRIGHT/VIVID palette (avoid all-black/all-dark hex codes; at least one color should be light or vivid).\n"
        "- characters — empty list [] if no specific characters implied by the title.\n"
        "- Style: cinematic, photoreal, YouTube-thumbnail style. Avoid generic abstract scenes.\n"
        "- CRITICAL composition rule: the title text will be overlaid on top of the image as large bold letters. Design the scene so that text placement area (usually top-center or bottom-center) is visually CLEAR — sky, smooth wall, plain ground, etc. — NOT covered by faces, key objects or busy details. Main subject sits OFF-CENTER from the text area."
    )
    user = f"Title: {title.strip()}\n\nExtra details: {(extra_prompt or '').strip() or '(none)'}"

    client, model_name, used_key = _chat_route(openai_api_key, model_name)
    with _provider_semaphore_for_key("openai", used_key):
        resp = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=600,
            temperature=0.8,
        )
    raw = (resp.choices[0].message.content or "").strip()
    parsed = _parse_json_response(raw)
    # Гарантируем наличие обязательных полей
    parsed.setdefault("scene_description", "dramatic cinematic scene")
    parsed.setdefault("mood", "dramatic")
    parsed.setdefault("dominant_colors", [])
    parsed.setdefault("characters", [])
    parsed.setdefault("visual_tropes", "")
    # v1.0.12: композиционные поля (LLM ДОЛЖЕН вернуть, но fallback для надёжности)
    parsed.setdefault("composition", "rule-of-thirds")
    parsed.setdefault("main_subject", "the main subject")
    parsed.setdefault("subject_size_pct", 40)
    parsed.setdefault("subject_position", "left-third")
    parsed.setdefault("subject_focus", "sharp")
    parsed["text"] = ""  # в title-mode текст задаётся per-lang отдельно
    return parsed


def adapt_text(original_text: str, target_language: str, api_key: str, video_title: str = "",
               provider: str = "") -> str:
    """Адаптация текста обложки на целевой язык через GPT. video_title — название ролика для контекста.

    `provider` — подписка (codex или claude), когда человек выбрал её вместо ключа.
    Здесь картинок нет, поэтому обе подписки годятся одинаково.
    """
    if not original_text or not original_text.strip():
        return ""
    system_template = _active_prompts().get("adapt_text_system_template") or get_default_prompt("adapt_text_system_template")
    video_title_val = (video_title or "").strip() or "(not provided)"
    system_content = system_template.replace("{video_title}", video_title_val).replace("{target_language}", target_language)
    if (provider or "").strip().lower() in SUBSCRIPTION_PROVIDERS:
        return _subscription_call(provider.strip().lower(), [
            {"role": "system", "content": system_content},
            {"role": "user", "content": original_text},
        ]).strip()
    client, _model, used_key = _chat_route(api_key, GPT_MODEL)
    with _provider_semaphore_for_key("openai", used_key):
        resp = client.chat.completions.create(
            model=_model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": original_text}
            ],
            max_tokens=200,
            temperature=0.7,
        )
    return resp.choices[0].message.content.strip()


# ════════════════════════════════════════════════════════
#  ШАГ 5-6: СБОРКА ПРОМПТА + ГЕНЕРАЦИЯ
# ════════════════════════════════════════════════════════

def _str_from_analysis(val, default: str = "") -> str:
    """Приводит значение из JSON-анализа к строке (если пришёл list — склеивает)."""
    if val is None:
        return default
    if isinstance(val, list):
        return " ".join(str(x).strip() for x in val).strip() or default
    return str(val).strip() if str(val).strip() else default


def build_generation_prompt(
    analysis: Dict,
    adapted_text: str,
    preset: Optional[Preset] = None,
    style_from_analysis: Optional[Dict] = None,
    include_text: bool = True,
    mode: str = "url",
    has_reference: bool = False,
) -> str:
    """Собирает промпт для генерации. style_from_analysis — результат анализа стиля превью (при авто-стиле); иначе используется preset.style_prompt.
    v1.0.12: mode ("url"|"title") + has_reference влияют на directive композиции:
      - url: PRESERVE композицию оригинала, менять только specifics
      - title без ref: композиция из prompt без directive
      - title с ref: STYLE из ref, композиция из prompt"""

    scene = analysis.get("scene_description", "dramatic background")
    mood = analysis.get("mood", "dramatic")
    video_title = (analysis.get("video_title") or "").strip()
    video_title_line = f'Video: "{video_title}". ' if video_title else ""
    colors = analysis.get("dominant_colors", [])
    colors_str = f"Color palette: {', '.join(colors[:3])}." if colors else ""

    # Персонажи и визуальные триггеры (для манга/манхва ниши)
    chars = analysis.get("characters") or []
    if isinstance(chars, list) and chars:
        char_descs = []
        for c in chars[:4]:
            if not isinstance(c, dict):
                continue
            parts = []
            role = (c.get("role") or "").strip()
            gender = (c.get("gender") or "").strip()
            age = (c.get("age") or "").strip()
            hair = (c.get("hair") or "").strip()
            clothing = (c.get("clothing") or "").strip()
            expr = (c.get("expression") or "").strip()
            pose = (c.get("pose") or "").strip()
            if role: parts.append(role)
            if gender or age: parts.append(f"{gender} {age}".strip())
            if hair: parts.append(f"hair: {hair}")
            if clothing: parts.append(f"wearing {clothing}")
            if expr: parts.append(f"expression: {expr}")
            if pose: parts.append(f"pose: {pose}")
            if parts:
                char_descs.append("— " + ", ".join(parts))
        if char_descs:
            scene = scene + "\n\nCHARACTERS TO PRESERVE (keep same visual identity):\n" + "\n".join(char_descs)

    tropes = analysis.get("visual_tropes")
    if tropes:
        tropes_str = tropes if isinstance(tropes, str) else ", ".join(str(t) for t in tropes if t)
        if tropes_str.strip():
            scene = scene + f"\n\nVISUAL HOOKS to keep: {tropes_str.strip()}"
    if style_from_analysis and (style_from_analysis.get("style_description") or style_from_analysis.get("recommended_prompt_suffix")):
        parts = [_str_from_analysis(style_from_analysis.get("style_description"), "")]
        suffix = _str_from_analysis(style_from_analysis.get("recommended_prompt_suffix"), "")
        if suffix:
            parts.append(suffix.rstrip(". ") + ".")
        style_prompt_str = "Style (from original thumbnail): " + " ".join(parts).strip()
    else:
        style_prompt = (preset.style_prompt or "").strip()
        style_prompt_str = f"Style: {style_prompt}." if style_prompt else ""

    if include_text and adapted_text:
        pos = analysis.get("text_position", "center")
        font_style = analysis.get("font_style", "bold sans-serif")
        text_color = analysis.get("text_color", "white")
        text_size = (analysis.get("text_size") or "large").strip()
        text_layout = (analysis.get("text_layout") or "").strip()
        # v1.0.14: честный шрифт из анализа (без хардкода "Bold" — он ломал тонкие/нежирные шрифты).
        # Сохраняем семейство+вес+регистр + структуру (переносы строк, расположение лейблов).
        layout_clause = f" Text layout: {text_layout}." if text_layout else ""
        text_block = (
            f'Text "{adapted_text}" in {font_style}, {text_color} color, '
            f'positioned {pos}, {text_size} size, readable.'
            f'{layout_clause} '
            f'Preserve the original font family, weight and letter case; '
            f'keep the same line breaks and text layout as the source.'
        )
        cover_text = adapted_text
    else:
        text_block = "No text, no letters, no words on the image. Fill the entire frame with the scene — no empty spaces reserved for text."
        cover_text = ""

    # v1.0.12: композиционный блок (PRIMARY DIRECTIVE)
    composition = analysis.get("composition", "rule-of-thirds")
    main_subject = analysis.get("main_subject", "the main element")
    try:
        subject_size_pct = int(analysis.get("subject_size_pct", 40))
    except (TypeError, ValueError):
        subject_size_pct = 40
    subject_size_pct = max(5, min(95, subject_size_pct))
    subject_position = analysis.get("subject_position", "off-center")
    subject_focus = analysis.get("subject_focus", "sharp")

    # Mode-aware directive
    # v1.0.19: url-mode БЕЗ прикреплённой картинки — это text-to-image, НЕ edit.
    # Раньше фраза "preserve from the source thumbnail" заставляла модель (особенно gpt-image)
    # трактовать запрос как edit и требовать исходник → "Missing required source image".
    if mode == "url" and not has_reference:
        mode_directive = (
            "Generate a brand-new thumbnail image from scratch (text-to-image, NOT an edit — "
            "there is NO source image to modify). Follow the composition above: place the main "
            "subject at the given position/size/framing, do not default to dead-center."
        )
    elif mode == "url" and has_reference:
        mode_directive = (
            "Use the attached reference image for style and layout guidance; follow the "
            "composition above (subject position/size/framing)."
        )
    elif mode == "title" and has_reference:
        mode_directive = (
            "Take STYLE (palette, mood, rendering, art style) from reference image, "
            "but use composition from this prompt — do NOT copy reference's layout."
        )
    else:
        mode_directive = ""

    composition_block_lines = [
        f"- Main subject: {main_subject}",
        f"- Subject occupies ~{subject_size_pct}% of frame (LARGE, dominant)",
        f"- Subject position: {subject_position}",
        f"- Subject focus: {subject_focus} foreground; background soft",
        f"- Overall composition: {composition}",
    ]
    if mode_directive:
        composition_block_lines.append(f"- {mode_directive}")
    composition_block = "\n".join(composition_block_lines)

    template = _active_prompts().get("generation_prompt_template") or get_default_prompt("generation_prompt_template")
    # Backward-compat: если в template нет {composition_block} (юзер с overridden старым template),
    # — format не упадёт, мы добавим блок в начало вывода руками.
    has_composition_placeholder = "{composition_block}" in template
    format_kwargs = dict(
        scene=scene,
        mood=mood,
        colors=colors_str,
        style_prompt=style_prompt_str,
        style_suffix="",
        text_block=text_block,
        cover_text=cover_text,
        video_title_line=video_title_line,
        video_title=video_title,
    )
    if has_composition_placeholder:
        format_kwargs["composition_block"] = composition_block
    out = template.format(**format_kwargs).replace("  ", " ").strip()
    if not has_composition_placeholder:
        # старый template — допишем composition в начало
        out = "COMPOSITION & SUBJECT (PRIMARY DIRECTIVE):\n" + composition_block + "\n\n" + out
    if cover_text and cover_text not in out:
        out += f'\n\nTEXT ON COVER (render this exactly, NOT the video title): "{cover_text}". {text_block}'
    elif not cover_text:
        # Убираем остатки инструкций про текст если cover_text пуст
        out = out.replace('TEXT ON COVER (render this exactly, NOT the video title): ""', '')
        out = out.replace('The ONLY text must be: "".', 'Do NOT put any text or letters on the image.')
        out = out.replace('Do NOT write the video title on the image.', 'Do NOT write any text on the image.')
    return out


# Таймауты и повторы для Fast Gen API (502/timeout — частые временные сбои)
FAST_GEN_POLL_TIMEOUT = 60
FAST_GEN_POST_TIMEOUT = 90
FAST_GEN_MAX_RETRIES = 3
FAST_GEN_RETRY_BACKOFF = [5, 15, 40]

# v1.2.0 (Фаза 2): ВЗВЕШЕННЫЙ БЮДЖЕТ КОНКУРЕНТНОСТИ НА КЛЮЧ.
# Лимит у Fast Gen — на API-ключ (img_generation_threads_allowed из /api/v5/usage).
# Каждая операция «весит» сколько-то потоков (openapi x-rate-limit-count):
#   flow=4, openai=2, flower=1, grok=2. Держим сумму весов в полёте ≤ ёмкости ключа.
# Серверный ключ = одна корзина на всех free-юзеров; каждый личный ключ = СВОЯ корзина
# → серверный пул не бьётся в 429 и не дерётся с личными. Само-тюнинг: ёмкость читаем из API.
_PROVIDER_WEIGHT = {"flow": 4, "openai_img": 4, "flower": 1}


def _provider_weight(provider: str, model: str = "") -> int:
    if provider == "grok":
        return 1 if (model or "").strip().lower() == "speed" else 3
    return _PROVIDER_WEIGHT.get(provider, 2)
_KEY_BUDGET_MARGIN = float(os.environ.get("FAST_GEN_BUDGET_MARGIN", "0.8"))   # запас под чужую активность/бурсты
_KEY_BUDGET_FALLBACK = int(os.environ.get("FAST_GEN_BUDGET_FALLBACK", "12"))  # если /api/v5/usage недоступен
_KEY_BUDGET_OVERRIDE = int(os.environ.get("FAST_GEN_BUDGET_OVERRIDE", "0"))   # >0 = ручной потолок, игнор авто
FASTGEN_WEIGHTED_BUDGET_LIMIT = _positive_env_int(
    "COVER_FASTGEN_WEIGHTED_BUDGET", 8
)
_KEY_CAPS_TTL = 600
_key_caps: Dict[str, float] = {}
_key_caps_lock = threading.Lock()


def _fetch_key_capacity(api_key: str) -> int:
    """Ёмкость корзины ключа = img_generation_threads_allowed × margin (кэш 10 мин, фолбэк при ошибке)."""
    if _KEY_BUDGET_OVERRIDE > 0:
        return min(_KEY_BUDGET_OVERRIDE, FASTGEN_WEIGHTED_BUDGET_LIMIT)
    kid = _credential_id(api_key)
    now = time.time()
    with _key_caps_lock:
        ent = _key_caps.get(kid)
        if ent and (now - ent[1]) < _KEY_CAPS_TTL:
            return int(ent[0])
    threads = _KEY_BUDGET_FALLBACK
    try:
        r = requests.get(f"{FAST_GEN_API_BASE}/api/v6/usage",
                         headers={"X-API-Key": api_key}, timeout=10)
        if r.status_code == 200:
            t = int(((r.json() or {}).get("account_limits") or {}).get("img_generation_threads_allowed") or 0)
            if t > 0:
                threads = t
    except Exception:
        pass
    cap = min(
        FASTGEN_WEIGHTED_BUDGET_LIMIT,
        max(1, int(threads * _KEY_BUDGET_MARGIN)),
    )
    with _key_caps_lock:
        _key_caps[kid] = (cap, now)
    return cap


class _KeyBudget:
    """Взвешенный бюджет конкурентности на ключ: сумма весов в полёте ≤ capacity."""
    def __init__(self, capacity: int):
        self.capacity = max(1, capacity)
        self.used = 0
        self.cond = threading.Condition()

    def acquire(self, w: int, cancel_check=None) -> int:
        """Reserve and return the exact units owned by this caller (0=cancel)."""
        requested = max(1, int(w))
        with self.cond:
            while True:
                if cancel_check and cancel_check():
                    return 0
                reserved = max(1, min(requested, self.capacity))
                if self.used + reserved <= self.capacity:
                    break
                self.cond.wait(timeout=0.4)
            if cancel_check and cancel_check():
                return 0
            self.used += reserved
            return reserved

    def release(self, reserved_units: int):
        reserved_units = max(0, int(reserved_units))
        with self.cond:
            self.used = max(0, self.used - reserved_units)
            self.cond.notify_all()


_key_budgets: Dict[str, _KeyBudget] = {}
_key_budgets_lock = threading.Lock()


def _key_budget(api_key: str) -> _KeyBudget:
    cap = _fetch_key_capacity(api_key)
    kid = _credential_id(api_key or "server")
    with _key_budgets_lock:
        b = _key_budgets.get(kid)
        if b is None:
            b = _KeyBudget(cap)
            _key_budgets[kid] = b
        else:
            with b.cond:
                b.capacity = max(1, cap)  # подхватываем смену лимита ключа
                b.cond.notify_all()
        return b

# v1.0.11: thread-local для последней Fast Gen API ошибки.
# Используется чтобы в job-логе показать пользователю содержательную причину
# (content policy / violence guardrail / 5xx и т.п.) вместо безликого "пустой результат".
_last_fastgen_error = threading.local()


def _record_fastgen_error(err) -> None:
    """v1.0.15: записать ПОНЯТНУЮ причину сбоя Fast Gen в thread-local для job-лога.
    Раньше POST-level ошибки (429/таймаут/5xx) не попадали в лог — юзер видел только
    безликое 'пустой результат от API'. Теперь классифицируем по HTTP-коду/тексту."""
    s = str(err)
    status = None
    resp = getattr(err, "response", None)
    if resp is not None:
        status = getattr(resp, "status_code", None)
    if status == 429 or "429" in s or "Too Many Requests" in s:
        # Не выдумываем причину: 429 у Fast Gen приходит и на часовой лимит картинок
        # (6000/час на ключ), и на потоки. Прежний текст уверенно винил параллельность,
        # хотя у обложек всего один поток — и разбор уходил в ложную сторону.
        body = ""
        try:
            body = (resp.text or "")[:200] if resp is not None else ""
        except Exception:
            body = ""
        _last_fastgen_error.value = (
            "HTTP 429 Too Many Requests — Fast Gen отказал по лимиту"
            + (f": {body}" if body else " (часовой лимит картинок либо потоки; ответ пуст)")
        )
        log_event("error", "FastGen 429: " + (body or "тело ответа пустое"))
    elif status in (502, 503, 504) or "maintenance" in s.lower():
        _last_fastgen_error.value = f"сервис Fast Gen временно недоступен ({status or 'maintenance/5xx'})"
        log_event("error", f"FastGen недоступен: HTTP {status} {str(s)[:200]}")
    elif status == 401 or "401" in s or "api key" in s.lower():
        _last_fastgen_error.value = "HTTP 401 — неверный/пустой Fast Gen API ключ"
        log_event("error", "FastGen 401: ключ не принят")
    elif status == 413 or "413" in s or "too large" in s.lower():
        _last_fastgen_error.value = "HTTP 413 — запрос/реф-картинка слишком большие"
        log_event("warning", "FastGen 413: запрос слишком большой")
    elif status == 400 or "400" in s:
        # тело 400 содержит реальную причину (напр. операция не принимает inputs) — показываем её
        body = ""
        try:
            body = (resp.text or "").strip() if resp is not None else ""
        except Exception:
            body = ""
        detail = f"HTTP 400 Bad Request — {body}" if body else f"HTTP 400 Bad Request — {s}"
        _last_fastgen_error.value = (detail[:400] + "...") if len(detail) > 400 else detail
        log_event("error", "FastGen 400: " + detail[:300])
    else:
        _last_fastgen_error.value = (s[:240] + "...") if len(s) > 240 else s


def _image_path_to_data_uri(path: str, max_bytes: int = 5 * 1024 * 1024) -> Optional[str]:
    """Читает файл и возвращает 'data:image/{mime};base64,...' для передачи в Fast Gen reference_images.
    None если файл не существует / >5MB / не картинка."""
    import base64
    try:
        if not path or not os.path.exists(path):
            return None
        size = os.path.getsize(path)
        if size == 0 or size > max_bytes:
            return None
        with open(path, "rb") as f:
            content = f.read()
        # Magic-bytes для MIME (дублирует api_cover._detect_image_mime, но локально для cover_core)
        if content[:3] == b"\xff\xd8\xff": mime = "image/jpeg"
        elif content[:8] == b"\x89PNG\r\n\x1a\n": mime = "image/png"
        elif len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP": mime = "image/webp"
        elif content[:6] in (b"GIF87a", b"GIF89a"): mime = "image/gif"
        else:
            return None
        b64 = base64.b64encode(content).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


# --- Сторонние провайдеры генерации (порт из VideoStudio src/googler_worker.py) ---
VEO_BASE_URL = os.environ.get("VEO_BASE_URL", "https://veononstop.org/api/v1")  # VeoNonStop, auth X-API-Key: veo_...
VEO_BANANA_TIMEOUT = int(os.environ.get("VEO_BANANA_TIMEOUT", "600"))  # СИНХРОННЫЙ endpoint: POST держит соединение до готовности
VEO_RETRY_DELAY = 3            # плоская пауза между попытками (капча у провайдера не лечится backoff'ом)
GPT_IMAGE2_TIMEOUT = 180
# 17.09.2026: модель та же, что у кадров VideoStudio (`workers/googler_worker.py`,
# GPT_IMAGE_MODEL): "gpt-image-2.5" без суффикса у OpenAI нет, владелец выбрал sunburst.
# Внутренний id провайдера `gpt_image2` не меняется — он лежит в пресетах и задачах.
# Размеры 1536x1024 / 1024x1536 сняты под gpt-image-2 и под 2.5 не перепроверены.
GPT_IMAGE_MODEL = (os.environ.get("COVER_GPT_IMAGE_MODEL")
                   or os.environ.get("VS_GPT_IMAGE_MODEL") or "gpt-image-2.5-sunburst")

# xAI напрямую (официальный API Grok). Ключ xai-..., отдельный от Fast Gen.
XAI_BASE_URL = "https://api.x.ai/v1"
XAI_IMAGE_MODEL = os.environ.get("COVER_XAI_IMAGE_MODEL", "grok-imagine-image-2.0")
XAI_TIMEOUT = int(os.environ.get("COVER_XAI_TIMEOUT", "180"))


def _datauri_to_b64(s: str) -> str:
    """'data:image/...;base64,XXX' → 'XXX'; строка без префикса возвращается как есть."""
    if isinstance(s, str) and s.startswith("data:"):
        return s.split(",", 1)[-1]
    return s or ""


def _generate_via_veononstop(
    prompt: str, api_key: str, model: str = "GEM_PIX_2", aspect_ratio: str = "16:9",
    max_retries: int = None, log_fn=None,
    reference_images: Optional[List[str]] = None, cancel_check=None,
) -> Optional[Image.Image]:
    """VeoNonStop Nano Banana (veononstop.org). model → model_key: GEM_PIX_2 (Pro) |
    NARWHAL (Banana 2) | HARBOR_SEAL (Banana 2 Lite). Ответ синхронный:
    {success, data.media[0].fifeUrl}. Референсы: до 5, base64 без data:-префикса."""
    log = log_fn or (lambda m: None)
    ar = _v6_aspect(aspect_ratio)
    ar = ar if ar in ("16:9", "9:16") else "16:9"  # veo принимает только эти два
    payload = {"prompt": prompt, "aspect_ratio": ar}
    mk = (model or "").strip().upper()
    if mk:
        payload["model_key"] = mk
    refs = [r for r in (reference_images or []) if r][:5]
    if refs:
        payload["reference_images"] = [
            {"name": f"ref{i + 1}", "image_base64": _datauri_to_b64(r), "mime_type": "image/jpeg"}
            for i, r in enumerate(refs)
        ]
        payload["use_all_ref_images"] = True
    headers = {"X-API-Key": api_key, "Content-Type": "application/json; charset=utf-8"}
    # POST синхронный и не имеет idempotency key. После timeout неизвестно,
    # принял ли провайдер платную генерацию, поэтому автоматический повтор запрещён.
    attempts = 1
    for attempt in range(1, attempts + 1):
        if cancel_check and cancel_check():
            return None
        try:
            r = requests.post(f"{VEO_BASE_URL}/image/banana/generate",
                              headers=headers, json=payload, timeout=VEO_BANANA_TIMEOUT)
            if r.status_code in (401, 403):
                log(f"[veo] auth {r.status_code}: ключ VeoNonStop неверный/просрочен — ретраи бессмысленны")
                return None
            if r.status_code == 200:
                res = r.json()
                if isinstance(res, dict) and res.get("success"):
                    media = (res.get("data") or {}).get("media") or []
                    fife = media[0].get("fifeUrl") if media else None
                    if fife:
                        img_r = requests.get(fife, timeout=120)
                        img_r.raise_for_status()
                        return Image.open(BytesIO(img_r.content)).convert("RGB")
                log(f"[veo] попытка {attempt}/{attempts}: неуспешный ответ: {str(res)[:200]}")
            else:
                log(f"[veo] попытка {attempt}/{attempts}: HTTP {r.status_code}: {r.text[:200]}")
        except requests.exceptions.Timeout:
            log(f"[veo] попытка {attempt}/{attempts}: таймаут {VEO_BANANA_TIMEOUT}с")
        except Exception as e:
            log(f"[veo] попытка {attempt}/{attempts}: {e}")
        if attempt < attempts and _interruptible_sleep(VEO_RETRY_DELAY, cancel_check):
            return None
    return None


def _generate_via_gpt_image2(
    prompt: str, api_key: str, aspect_ratio: str = "16:9",
    max_retries: int = None, log_fn=None,
    reference_images: Optional[List[str]] = None, cancel_check=None,
) -> Optional[Image.Image]:
    """OpenAI GPT_IMAGE_MODEL НАПРЯМУЮ (платно, quality=low фиксирован; цена не перепроверена под 2.5).
    generate-only: референсы не поддерживаются — предупреждаем и генерим без них."""
    log = log_fn or (lambda m: None)
    if reference_images:
        log(f"[{GPT_IMAGE_MODEL}] референсы не поддерживаются (generate-only) — генерирую без референса")
    ar = _v6_aspect(aspect_ratio)
    size = {"16:9": "1536x1024", "9:16": "1024x1536"}.get(ar, "1024x1024")
    body = {"model": GPT_IMAGE_MODEL, "prompt": prompt, "size": size,
            "quality": "low", "n": 1, "output_format": "jpeg"}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    attempts = max(1, max_retries or 3)
    for attempt in range(1, attempts + 1):
        if cancel_check and cancel_check():
            return None
        try:
            openai_semaphore = _provider_semaphore_for_key("openai", api_key)
            if not _acquire_cancelable(openai_semaphore, cancel_check):
                return None
            try:
                r = requests.post("https://api.openai.com/v1/images/generations",
                                  headers=headers, json=body, timeout=GPT_IMAGE2_TIMEOUT)
            finally:
                openai_semaphore.release()
            if r.status_code in (401, 403):
                log(f"[{GPT_IMAGE_MODEL}] auth {r.status_code}: OpenAI ключ неверный/без доступа — ретраи бессмысленны")
                return None
            if r.status_code == 200:
                data = (r.json() or {}).get("data") or []
                b64 = data[0].get("b64_json") if data else None
                if b64:
                    return Image.open(BytesIO(base64.b64decode(b64))).convert("RGB")
                log(f"[{GPT_IMAGE_MODEL}] попытка {attempt}/{attempts}: пустой ответ")
                return None
            if r.status_code != 429:
                # 4xx/5xx мог прийти после принятия платной операции; без
                # idempotency key не создаём возможный дубль.
                log(f"[{GPT_IMAGE_MODEL}] попытка {attempt}/{attempts}: HTTP {r.status_code}: {r.text[:200]}")
                return None
            else:
                log(f"[{GPT_IMAGE_MODEL}] попытка {attempt}/{attempts}: HTTP {r.status_code}: {r.text[:200]}")
        except requests.exceptions.Timeout:
            log(f"[{GPT_IMAGE_MODEL}] попытка {attempt}/{attempts}: таймаут {GPT_IMAGE2_TIMEOUT}с")
            return None
        except Exception as e:
            log(f"[{GPT_IMAGE_MODEL}] попытка {attempt}/{attempts}: {e}")
            return None
        if attempt < attempts and _interruptible_sleep(3, cancel_check):
            return None
    return None


def _generate_via_xai(
    prompt: str, api_key: str, aspect_ratio: str = "16:9",
    max_retries: int = None, log_fn=None,
    reference_images: Optional[List[str]] = None, cancel_check=None,
) -> Optional[Image.Image]:
    """Grok НАПРЯМУЮ в xAI: то же, что делается руками в вебе.

    Есть референс → /v1/images/edits (правка присланной обложки, вход + выход
    ≈ $0.07). Нет референса → /v1/images/generations (≈ $0.04). Соотношение
    сторон у правки наследуется от входной картинки, поэтому его не шлём.

    Ответ отдаёт ССЫЛКУ на временный файл, а не base64: картинку докачиваем сами.
    """
    log = log_fn or (lambda m: None)
    refs = [r for r in (reference_images or []) if r][:3]     # API берёт до трёх
    body = {"model": XAI_IMAGE_MODEL, "prompt": prompt, "n": 1}
    if refs:
        картинки = []
        for r in refs:
            uri = r if str(r).startswith(("data:", "http://", "https://")) \
                else _image_path_to_data_uri(r)
            if uri:
                картинки.append({"type": "image_url", "url": uri})
        if картинки:
            body["image"] = картинки[0] if len(картинки) == 1 else картинки
    url = f"{XAI_BASE_URL}/images/{'edits' if body.get('image') else 'generations'}"
    if refs and not body.get("image"):
        log("[grok-api] референс не прочитался — генерирую без него")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    attempts = max(1, max_retries or 3)
    for attempt in range(1, attempts + 1):
        if cancel_check and cancel_check():
            return None
        try:
            r = requests.post(url, headers=headers, json=body, timeout=XAI_TIMEOUT)
            if r.status_code in (401, 403):
                log(f"[grok-api] auth {r.status_code}: ключ xAI неверный или без доступа — ретраи бессмысленны")
                return None
            if r.status_code == 200:
                data = (r.json() or {}).get("data") or []
                if not data:
                    log(f"[grok-api] попытка {attempt}/{attempts}: пустой ответ")
                    return None
                b64 = data[0].get("b64_json")
                if b64:
                    return Image.open(BytesIO(base64.b64decode(b64))).convert("RGB")
                ссылка = data[0].get("url") or ""
                if not ссылка:
                    log(f"[grok-api] попытка {attempt}/{attempts}: в ответе нет ни картинки, ни ссылки")
                    return None
                файл = requests.get(ссылка, timeout=XAI_TIMEOUT)
                if файл.status_code != 200 or not файл.content:
                    log(f"[grok-api] картинка не скачалась: HTTP {файл.status_code}")
                    return None
                return Image.open(BytesIO(файл.content)).convert("RGB")
            if r.status_code != 429:
                # Платная операция могла быть принята до ошибки: без ключа
                # идемпотентности повтор рискует оплатить вторую картинку.
                log(f"[grok-api] попытка {attempt}/{attempts}: HTTP {r.status_code}: {r.text[:200]}")
                return None
            log(f"[grok-api] попытка {attempt}/{attempts}: HTTP 429 (лимит) — жду")
        except requests.exceptions.Timeout:
            log(f"[grok-api] попытка {attempt}/{attempts}: таймаут {XAI_TIMEOUT}с")
            return None
        except Exception as e:
            log(f"[grok-api] попытка {attempt}/{attempts}: {e}")
            return None
        if attempt < attempts and _interruptible_sleep(3, cancel_check):
            return None
    return None


def generate_image_via_fast_gen(
    prompt: str,
    api_key: str,
    provider: str,
    model: str = "GEM_PIX_2",
    base_url: str = FAST_GEN_API_BASE,
    aspect_ratio: str = "16:9",
    max_retries: int = None,
    log_fn=None,
    reference_images: Optional[List[str]] = None,
    cancel_check=None,
    on_order=None,                       # 03.09.2026: журнал платных вызовов
    resume_order_id: str = "",           # 03.09.2026: журнал платных вызовов
) -> Optional[Image.Image]:
    """Единая точка: по provider вызывает Flow, Flower, Whisk, Gemini, Grok или OpenAI.
    reference_images (v1.0.9) — Flow (array). v1.0.10 — Flower (single, истинный img2img).
    v1.0.16 — глобальный семафор: не больше _FASTGEN_MAX_INFLIGHT генераций в полёте.
    v1.1.0 — base_url санитизируется (SSRF/key-exfil guard): owner-ключ уходит только на офиц. хост.
    v1.2.0 — взвешенный бюджет ПО КЛЮЧУ (ёмкость = реальный лимит ключа из /api/v5/usage):
    серверный и личные ключи = разные корзины, серверный пул не бьётся в 429.
    v1.4: провайдеры veo/gpt_image2 уходят в сторонние API мимо Fast Gen —
    api_key при этом уже ИХ ключ (подмену делает _execute_cover_job),
    бюджет/семафор Fast Gen к ним не применяется."""
    p = canonical_generation_provider(provider or "flow")
    dispatch = provider_policy(p).dispatch
    if dispatch == "veo":
        veo_semaphore = _provider_semaphore_for_key("veo", api_key)
        if not _acquire_cancelable(veo_semaphore, cancel_check):
            return None
        try:
            return _generate_via_veononstop(
                prompt, api_key, model=model, aspect_ratio=aspect_ratio,
                max_retries=max_retries, log_fn=log_fn,
                reference_images=reference_images, cancel_check=cancel_check)
        finally:
            veo_semaphore.release()
    if dispatch == "xai":
        xai_semaphore = _provider_semaphore_for_key("xai", api_key)
        if not _acquire_cancelable(xai_semaphore, cancel_check):
            return None
        try:
            return _generate_via_xai(
                prompt, api_key, aspect_ratio=aspect_ratio,
                max_retries=max_retries, log_fn=log_fn,
                reference_images=reference_images, cancel_check=cancel_check)
        finally:
            xai_semaphore.release()
    if dispatch == "gpt_image2":
        gpt_semaphore = _provider_semaphore_for_key("gpt_image2", api_key)
        if not _acquire_cancelable(gpt_semaphore, cancel_check):
            return None
        try:
            return _generate_via_gpt_image2(
                prompt, api_key, aspect_ratio=aspect_ratio,
                max_retries=max_retries, log_fn=log_fn,
                reference_images=reference_images, cancel_check=cancel_check)
        finally:
            gpt_semaphore.release()
    base_url = _safe_fast_gen_base(base_url)
    budget = _key_budget(api_key)
    weight = _provider_weight(p, model)
    reserved_units = budget.acquire(weight, cancel_check)
    if not reserved_units:
        return None
    try:
        return _generate_via_fast_gen_inner(
            prompt, api_key, p, model=model, base_url=base_url,
            aspect_ratio=aspect_ratio, max_retries=max_retries, log_fn=log_fn,
            reference_images=reference_images, cancel_check=cancel_check,
            # 03.09.2026: только у Fast Gen v6 есть id заказа — журналируем его
            on_order=on_order, resume_order_id=resume_order_id,
        )
    finally:
        budget.release(reserved_units)


def _v6_operation(provider: str, model: str, has_ref: bool) -> str:
    """v1.3.1: маппинг нашего provider/model → canonical v6 operation id (из /api/v6/capabilities)."""
    p = canonical_generation_provider(provider or "flow")
    m = (model or "").upper()
    if p == "flow":
        return "nano_banana_2_image_generate" if m == "NARWHAL" else "nano_banana_pro_image_generate"
    if p == "flower":
        return "flower_image_edit" if has_ref else "flower_image_generate"
    if p == "openai_img":
        return "openai_image_generate"
    if p == "grok":
        # grok_image_generate НЕ принимает inputs (capabilities: inputs 0..0) → при референсе
        # нужен grok_image_edit (inputs 1..5). Иначе POST /v6/generations → 400 Bad Request.
        return "grok_image_edit" if has_ref else "grok_image_generate"
    raise ValueError(f"provider {p!r} is not a FastGen dispatch provider")


def _v6_aspect(ar: str) -> str:
    allowed = {"16:9", "9:16", "1:1", "4:3", "3:4"}
    a = (ar or "16:9").strip().lower()
    a = {"landscape": "16:9", "portrait": "9:16", "square": "1:1"}.get(a, a)
    return a if a in allowed else "16:9"


def _interruptible_sleep(seconds: float, cancel_check=None, step: float = 0.4) -> bool:
    """Спит до `seconds`, но просыпается сразу, если cancel_check() стал True.
    Возвращает True если за время сна пришла отмена (чтобы вызывающий вышел быстро)."""
    if cancel_check and cancel_check():
        return True
    slept = 0.0
    while slept < seconds:
        time.sleep(min(step, seconds - slept))
        slept += step
        if cancel_check and cancel_check():
            return True
    return False


def generate_image_v6(
    prompt: str, api_key: str, operation: str,
    base_url: str = FAST_GEN_API_BASE, aspect_ratio: str = "16:9",
    reference_images: Optional[List[str]] = None, seed: Optional[int] = None,
    poll_interval: float = 2.0, poll_max_wait: float = 180.0,
    max_retries: int = None, log_fn=None, cancel_check=None,
    on_order=None, resume_order_id: str = "",
) -> Optional[Image.Image]:
    """v1.3.1: унифицированный Fast Gen v6. POST /api/v6/generations → poll /api/v6/generations/{id}.
    Старые /api/v4/{provider}/image/generate сняты провайдером (404). Картинка — в results[0]
    (data data-URI или download_url). reference_images → inputs (data URI / file id, до 10)."""
    base_url = (base_url or FAST_GEN_API_BASE).rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    body = {"operation": operation, "prompt": prompt, "aspect_ratio": _v6_aspect(aspect_ratio)}
    if reference_images:
        body["inputs"] = reference_images[:10]
    if seed is not None:
        body["seed"] = seed
    if max_retries is None:
        max_retries = FAST_GEN_MAX_RETRIES

    gen_id = None
    # 03.09.2026: журнал платных вызовов. resume_order_id — заказ уже создан и оплачен
    # (прошлый прогон умер между POST и скачиванием); POST не повторяем, сразу поллим.
    _resume = (resume_order_id or "").strip()
    for attempt in range(0 if _resume else max_retries):
        if cancel_check and cancel_check():
            _last_fastgen_error.value = "Отменено пользователем"
            return None
        try:
            r = requests.post(f"{base_url}/api/v6/generations", headers=headers, json=body, timeout=FAST_GEN_POST_TIMEOUT)
            if r.status_code in (502, 503, 504) and attempt < max_retries - 1:
                if _interruptible_sleep(FAST_GEN_RETRY_BACKOFF[min(attempt, len(FAST_GEN_RETRY_BACKOFF) - 1)], cancel_check):
                    return None
                continue
            r.raise_for_status()
            gen_id = (r.json() or {}).get("id")
            if not gen_id:
                _last_fastgen_error.value = "v6: ответ без generation id"
                return None
            break
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                if _interruptible_sleep(FAST_GEN_RETRY_BACKOFF[min(attempt, len(FAST_GEN_RETRY_BACKOFF) - 1)], cancel_check):
                    return None
            else:
                _record_fastgen_error(e)
                if log_fn:
                    log_fn(f"[generate_image_v6] POST error: {e}")
                return None
        except Exception as e:
            _record_fastgen_error(e)
            return None
    # 03.09.2026: id заказа известен — сообщаем журналу ДО ожидания результата,
    # иначе падение во время поллинга сотрёт след платной генерации.
    if _resume:
        gen_id = _resume
        if log_fn:
            log_fn(f"[generate_image_v6] resume {gen_id}: жду результат ранее созданного заказа")
    elif gen_id and on_order:
        try:
            on_order(gen_id)
        except Exception:                                             # noqa: BLE001
            pass
    if not gen_id:
        return None

    started = time.time()
    last_log = 0.0
    while time.time() - started < poll_max_wait:
        if _interruptible_sleep(poll_interval, cancel_check):
            _last_fastgen_error.value = "Отменено пользователем"
            return None
        try:
            r = requests.get(f"{base_url}/api/v6/generations/{gen_id}", headers=headers, timeout=FAST_GEN_POLL_TIMEOUT)
            if r.status_code in (502, 503, 504):
                continue
            r.raise_for_status()
            d = r.json()
        except requests.RequestException as e:
            if time.time() - last_log > 30:
                print(f"[generate_image_v6] poll {e}"); last_log = time.time()
            continue
        st = d.get("status")
        if st in ("succeeded", "success", "completed"):
            results = d.get("results") or []
            if not results:
                _last_fastgen_error.value = "v6: succeeded без results"
                return None
            res = results[0]
            data = res.get("data")
            if isinstance(data, str) and data.startswith("data:"):
                try:
                    return Image.open(BytesIO(base64.b64decode(data.split(",", 1)[-1]))).convert("RGB")
                except Exception as e:
                    _record_fastgen_error(e); return None
            url = res.get("download_url")
            if url:
                try:
                    ir = requests.get(url, timeout=FAST_GEN_POLL_TIMEOUT)
                    ir.raise_for_status()
                    return Image.open(BytesIO(ir.content)).convert("RGB")
                except Exception as e:
                    _record_fastgen_error(e); return None
            _last_fastgen_error.value = "v6: результат без data/download_url"
            return None
        if st in ("failed", "error", "canceled", "cancelled"):
            err = d.get("error") or "Unknown"
            _last_fastgen_error.value = str(err)
            return None
    _last_fastgen_error.value = "v6: timeout ожидания результата"
    return None


def _generate_via_fast_gen_inner(
    prompt: str,
    api_key: str,
    provider: str,
    model: str = "GEM_PIX_2",
    base_url: str = FAST_GEN_API_BASE,
    aspect_ratio: str = "16:9",
    max_retries: int = None,
    log_fn=None,
    reference_images: Optional[List[str]] = None,
    cancel_check=None,
    on_order=None,                       # 03.09.2026: журнал платных вызовов
    resume_order_id: str = "",           # 03.09.2026: журнал платных вызовов
) -> Optional[Image.Image]:
    """Диспетчер: все провайдеры → унифицированный v6 (v1.3.1). v4 per-provider сняты."""
    import random
    has_ref = bool(reference_images)
    operation = _v6_operation(provider, model, has_ref)
    # seed для Flow — слом кэша при одинаковом prompt (как было в v4)
    seed = random.randint(1, 2**31 - 1) if (provider or "").lower() == "flow" else None
    return generate_image_v6(
        prompt, api_key, operation, base_url=base_url, aspect_ratio=aspect_ratio,
        reference_images=reference_images, seed=seed, max_retries=max_retries, log_fn=log_fn,
        cancel_check=cancel_check, on_order=on_order, resume_order_id=resume_order_id,
    )


# ════════════════════════════════════════════════════════
#  ЖУРНАЛ ПЛАТНЫХ ВЫЗОВОВ (03.09.2026)
# ════════════════════════════════════════════════════════
# Генерация обложки — платный вызов, и на ролик их несколько (вариант на язык плюс
# повторы неудачных). Раньше от повторной оплаты защищала только проверка «файл уже
# есть»: падение или таймаут между «провайдер принял заказ» и «картинка скачана»
# оплачивал обложку второй раз. Порядок обязателен: reserve() до провайдера →
# ordered() как только известен id заказа → delivered() когда файл записан.
#
# Синхронные провайдеры (veononstop, gpt_image2, xai) id заказа не выдают: у них
# шаг ordered() пропускается, а «заказ создан» выводится из самого факта запроса.

# Единственный dispatch с id заказа и опросом статуса — Fast Gen v6. Всё остальное
# (veo, gpt_image2, xai) синхронно и id заказа не имеет; неизвестный dispatch считаем
# синхронным намеренно — так дороже не будет.
_JOURNAL_POLLABLE_DISPATCH = "fastgen"


def _journal_refs_digest(reference_images) -> List[str]:
    """Короткие хэши референсов для отпечатка: сами data URI весят мегабайты."""
    out = []
    for r in (reference_images or []):
        if not r:
            continue
        out.append(hashlib.sha256(str(r).encode("utf-8", "replace")).hexdigest()[:16])
    return out


def _journal_charged(err) -> bool:
    """Мог ли этот отказ означать списание денег.

    ИСПРАВЛЕНО 03.09.2026. Раньше для трёх синхронных провайдеров ЛЮБОЙ отказ помечался
    как «заказ был» (ordered=True). Последствие: одна опечатка в ключе давала 401, строка
    журнала застревала в «спросить провайдера», и следующая попытка отказывалась платить —
    обложки вставали насмерть до ручной разблокировки в кабинете. Отказ провайдера ДО
    приёма работы деньгами не пахнет и обязан разрешать повтор.

    Ложь в любую сторону стоит денег, поэтому правило простое: неизвестный отказ считаем
    оплаченным. Бесплатным считаем только то, что провайдер назвал явно.
    """
    text = str(err or "")
    low = text.lower()
    # Провайдер отказался ДО работы: неверный ключ, слишком большой запрос, отказ
    # валидации, документированный лимит. Денег нет, повтор безопасен.
    for mark in ("401", "403", "413", "400 bad request", "429",
                 "api key", "ключ не принят", "неверный/пустой"):
        if mark in low:
            return False
    # Соединение не состоялось — запрос до провайдера не доехал.
    for mark in ("connectionerror", "connection refused", "name or service not known",
                 "failed to establish", "nameresolutionerror"):
        if mark in low:
            return False
    return True


def _journaled_generate(
    prompt: str,
    api_key: str,
    *,
    provider: str,
    model: str = "",
    base_url: str = FAST_GEN_API_BASE,
    aspect_ratio: str = "16:9",
    max_retries: int = None,
    log_fn=None,
    reference_images: Optional[List[str]] = None,
    cancel_check=None,
    unit: str = "",
    lang: str = "",
    preset_name: str = "",
    result_path: str = "",
    note: str = "",
    log=None,
) -> Optional[Image.Image]:
    """generate_image_via_fast_gen + журнал платных вызовов.

    Без VS_CALLS_URL/VS_JOB_ID/VS_JOB_TOKEN клиент отдаёт action="off", may_call=True —
    поведение движка ровно прежнее (важно: обложки генерят и из CLI, без кабинета).
    """
    say = log or (lambda _m: None)
    _call = lambda **kw: generate_image_via_fast_gen(                 # noqa: E731
        prompt, api_key, provider=provider, model=model, base_url=base_url,
        aspect_ratio=aspect_ratio, max_retries=max_retries, log_fn=log_fn,
        reference_images=reference_images, cancel_check=cancel_check, **kw)

    if CallJournal is None:
        return _call()

    journal = CallJournal(log=say)
    if not journal.enabled:
        return _call()

    try:
        dispatch = provider_policy(canonical_generation_provider(provider or "flow")).dispatch
    except Exception:                                                 # noqa: BLE001
        dispatch = ""
    # Соотношение сторон обязано быть в отпечатке: 16:9 и 9:16 — разные картинки и разная
    # цена. Известный дефект gpt_image2 (16:9 → 1536x1024, то есть 3:2) здесь НЕ чиним —
    # пишем то соотношение, которое реально ушло в вызов.
    fingerprint = journal.fingerprint(
        provider=provider, model=model, prompt=prompt, lang=lang,
        preset=preset_name, aspect=_v6_aspect(aspect_ratio),
        refs=_journal_refs_digest(reference_images), note=note,
    )
    decision = journal.reserve(unit=unit, fingerprint=fingerprint, kind="cover",
                               provider=str(provider or ""), model=str(model or ""))

    if decision.action == "skip":
        # Обложка уже оплачена и получена — к провайдеру не идём.
        path = decision.result_path or ""
        if path and os.path.exists(path):
            try:
                say(f"   ⏭ журнал: обложка уже получена ранее — беру {path}")
                return Image.open(path).convert("RGB")
            except Exception as e:                                    # noqa: BLE001
                say(f"   ⚠ журнал: {path} не читается ({e}) — повторно не оплачиваю")
                return None
        say(f"   ⏭ журнал: вызов уже оплачен и доставлен (путь: {path or 'не записан'}) — "
            f"повторно не оплачиваю")
        return None

    if decision.action == "verify":
        # Заказ уже создавался. Второй создавать НЕЛЬЗЯ.
        order_id = decision.remote_order_id or ""
        if order_id and dispatch == _JOURNAL_POLLABLE_DISPATCH:
            say(f"   ↻ журнал: заказ {order_id} уже создан — дожидаюсь его результата без оплаты")
            image = _call(resume_order_id=order_id)
            if image is None:
                journal.failed(decision.call_id, "resume: заказ не отдал картинку", ordered=True)
                return None
            journal.delivered(decision.call_id, result_path or "")
            return image
        say("   ⏭ журнал: заказ уже создавался, а синхронного провайдера опросить нечем — "
            "пропускаю без оплаты")
        return None

    if not decision.may_call:
        # unknown: кабинет не ответил. Не знаем, оплачен ли вызов — платить нельзя.
        say(f"   ⏭ журнал: решение «{decision.action}» — платный вызов запрещён")
        return None

    ordered = {"v": False}

    def _on_order(order_id):
        if order_id and not ordered["v"]:
            ordered["v"] = True
            journal.ordered(decision.call_id, order_id)

    try:
        image = _call(on_order=_on_order)
    except Exception as e:                                            # noqa: BLE001
        journal.failed(decision.call_id, e,
                       ordered=ordered["v"] or (dispatch != _JOURNAL_POLLABLE_DISPATCH
                                                and _journal_charged(e)))
        raise
    if image is None:
        # Отмена пользователем — не отказ провайдера: если заказ так и не создан, денег
        # нет и повтор после перезапуска безопасен.
        if cancel_check and cancel_check():
            journal.failed(decision.call_id, "Отменено пользователем", ordered=ordered["v"])
            return None
        # Синхронные провайдеры без ключа идемпотентности: после таймаута/5xx неизвестно,
        # приняли ли платную генерацию → отмечаем как «заказ был», чтобы повтор не платил.
        err = getattr(_last_fastgen_error, "value", "") or "провайдер не отдал картинку"
        journal.failed(decision.call_id, err,
                       ordered=ordered["v"] or (dispatch != _JOURNAL_POLLABLE_DISPATCH
                                                and _journal_charged(err)))
        return None
    journal.delivered(decision.call_id, result_path or "")
    return image


# ════════════════════════════════════════════════════════
#  ШАГ 7: ПРОВЕРКА РЕЗУЛЬТАТА
# ════════════════════════════════════════════════════════


def check_result(
    image: Image.Image,
    expected_text: str,
    provider: str,
    model: str,
    gemini_api_key: str = "",   # legacy, игнорируется (v1.0.3)
    openai_api_key: str = "",
    video_title: str = "",
) -> Tuple[bool, Dict]:
    """Проверка сгенерированной обложки. provider: openai (google→openai с warning)."""
    template = _active_prompts().get("check_prompt_template") or get_default_prompt("check_prompt_template")
    video_title_val = (video_title or "").strip() or "(not provided)"
    prompt = template.replace("{video_title}", video_title_val).replace("{expected_text}", expected_text)
    raw = _call_vision(image, prompt, provider, model, gemini_api_key, openai_api_key)
    result = _parse_json_response(raw)
    passed = result.get("overall_pass", False)
    return passed, result


def check_simple_result(
    original: "Image.Image",
    generated: "Image.Image",
    language: str,
    provider: str = "openai",
    model: str = "",
    openai_api_key: str = "",
) -> Tuple[bool, Dict]:
    """vS QC: сверяет ДВЕ картинки — оригинальное превью и сгенерённый результат.
    Проверяет, что текст на результате — корректный перевод текста оригинала на {language},
    читаемый и без артефактов. Возвращает (passed, details)."""
    template = _active_prompts().get("simple_check_prompt_template") or get_default_prompt("simple_check_prompt_template")
    prompt = template.replace("{language}", language or "")
    try:
        raw = _call_vision([original, generated], prompt, provider, model, "", openai_api_key)
    except Exception as exc:                                       # noqa: BLE001
        # 21.09.2026: проверка НЕ СМОГЛА состояться — это не провал проверки.
        # Картинка к этому моменту уже сгенерирована и оплачена (FastGen), а контролёр
        # ходит в OpenAI. Когда у OpenAI кончились кредиты, исключение улетало наверх и
        # уносило с собой оплаченный результат — ровно то, против чего ниже в
        # `_generate_simple_edit` стоит подпись «анти cost-drain».
        #
        # Ниже по коду уже принято правило «QC не распарсился → не блокируем сохранение:
        # retry дороже, чем один неидеальный текст». Здесь тот же случай, только причина
        # другая, и ответ обязан быть тем же. Повтор тут бессмыслен вдвойне: от второй
        # попытки контролёр не починится, а каждая попытка — новая оплаченная генерация.
        #
        # `checked: False` отличает «проверили и всё хорошо» от «проверить не смогли» —
        # вызывающий обязан сказать человеку правду, а не нарисовать зелёную галочку.
        return True, {"overall_pass": True, "checked": False,
                      "issues": f"проверка текста не выполнена: {type(exc).__name__}: "
                                f"{exc}"[:300]}
    result = _parse_json_response(raw)
    if not result:
        # QC не распарсился — не блокируем сохранение (retry дороже, чем один неидеальный текст)
        return True, {"overall_pass": True, "issues": "QC unparseable — passed by default"}
    passed = bool(result.get("overall_pass", False))
    return passed, result



# ════════════════════════════════════════════════════════
#  ТРИ КАНДИДАТА И ОЦЕНКА 0–10 (владелец 06.10.2026)
# ════════════════════════════════════════════════════════
#
# «Делаем три обложки; если ни одна не прошла проверку — ИИ оценивает каждую от 0 до 10,
# выигрывает лучшая, при равной высшей оценке — первая». Раньше после неудачных попыток
# сохранялась ПОСЛЕДНЯЯ картинка с пометкой брака (или рисовался вариант без текста), и
# студия уводила выпуск в разбор: 06.10 так стояли 40 выпусков. Теперь все оплаченные
# картинки — кандидаты, и выбор между ними делает тот же контролёр, что их проверял.
#
# COVER_CANDIDATES=0 возвращает число попыток из пресета.
COVER_CANDIDATES = int(os.environ.get("COVER_CANDIDATES", "3") or 0)

_SCORE_PROMPT = (
    "Video title (context only): {video_title}\n\n"
    "Rate this YouTube cover from 0 to 10. The text ON THE COVER should be: "
    "\"{expected_text}\".\n"
    "10 = text is exactly right, readable, no artifacts, attractive composition. "
    "0 = text is missing, wrong or gibberish.\n"
    "Text correctness and readability matter most; composition breaks ties.\n\n"
    "Return ONLY JSON: {\"score\": <integer 0-10>, \"why\": \"one short sentence\"}")
_SIMPLE_SCORE_PROMPT = (
    "You are given TWO YouTube thumbnails. The FIRST image is the ORIGINAL. The SECOND "
    "image is a generated version whose text should be the ORIGINAL text translated "
    "into {language}.\n"
    "Rate the SECOND image from 0 to 10. 10 = text is a correct, readable {language} "
    "translation with no artifacts and the picture keeps the original's idea. 0 = text "
    "is missing, in the wrong language, gibberish or means something else.\n"
    "Text correctness and readability matter most; picture quality breaks ties.\n\n"
    "Return ONLY JSON: {\"score\": <integer 0-10>, \"why\": \"one short sentence\"}")


def _attempts_for(preset_lang) -> int:
    if COVER_CANDIDATES > 0:
        return COVER_CANDIDATES
    return max(1, getattr(preset_lang, "max_generation_retries", 5))


def _score_one(images, prompt, provider, model, openai_api_key):
    """Оценка 0–10 одной картинки. Не ответил или ответ не разобрался — None."""
    try:
        raw = _call_vision(images, prompt, provider, model, "", openai_api_key)
    except Exception:                                              # noqa: BLE001
        return None, ""
    data = _parse_json_response(raw or "") or {}
    try:
        score = float(data.get("score"))
    except (TypeError, ValueError):
        return None, ""
    return max(0.0, min(10.0, score)), str(data.get("why") or "")[:160]


def pick_best_candidate(candidates, score_fn, log) -> Optional[int]:
    """Номер лучшей картинки по оценке 0–10; при равенстве — первая из лучших.

    `score_fn(image)` → (оценка или None, пояснение). Кандидат без оценки не участвует.
    Ни одной оценки — None: вызывающий поступает по-старому.
    """
    best, best_score, parts = None, None, []
    for i, img in enumerate(candidates):
        score, why = score_fn(img)
        if score is None:
            parts.append(f"№{i + 1} — без оценки")
            continue
        parts.append(f"№{i + 1} — {score:g}/10" + (f" ({why})" if why else ""))
        if best_score is None or score > best_score:     # строго больше: при равенстве — первая
            best, best_score = i, score
    if best is None:
        log("   ⚠ Оценить кандидатов не удалось: " + "; ".join(parts))
        return None
    log("   🏁 Оценки ИИ: " + "; ".join(parts) + f" — беру №{best + 1}")
    return best

# ════════════════════════════════════════════════════════
#  ПАЙПЛАЙН: ПОЛНЫЙ ЦИКЛ ОДНОЙ ОБЛОЖКИ
# ════════════════════════════════════════════════════════

@dataclass
class CoverResult:
    success: bool = False
    image: Optional[Image.Image] = None
    image_no_text: Optional[Image.Image] = None  # картинка без текста (если генерировалась для fallback)
    adapted_text: str = ""
    used_fallback: bool = False
    # Текст на картинке есть, но сверить его не удалось (контролёр не ответил). Для
    # человека это тоже «не готово до конца» (used_fallback), но не брак: перерисовка
    # за деньги тут ничего не исправит — сломан контролёр, а не картинка.
    unchecked: bool = False
    # Проверку не прошёл ни один вариант, и картинку выбрал ИИ по оценке 0–10 из
    # нескольких оплаченных. Это не брак: выбор сделан, выпуск идёт дальше.
    scored: bool = False
    analysis: Dict = field(default_factory=dict)
    check_result: Dict = field(default_factory=dict)
    error: str = ""


def process_single(
    youtube_url: str,
    preset: Preset,
    gemini_api_key: str,
    openai_api_key: str,
    fast_gen_api_key: str = "",
    log_fn=None,
) -> CoverResult:
    """
    Полный пайплайн генерации одной обложки.
    Делегирует в _download_and_analyze_url + _generate_for_language.
    """
    thumbnail, analysis, style = _download_and_analyze_url(
        youtube_url, preset, gemini_api_key, openai_api_key, log_fn
    )
    if thumbnail is None:
        return CoverResult(error="Не удалось скачать превью")
    if analysis is None:
        return CoverResult(error="Не удалось проанализировать обложку")
    return _generate_for_language(
        thumbnail, analysis, style, preset,
        gemini_api_key, openai_api_key, fast_gen_api_key, log_fn,
        mode="url",
    )


def _download_and_analyze_url(
    youtube_url: str,
    preset: Preset,
    gemini_api_key: str,
    openai_api_key: str,
    log_fn=None,
) -> Tuple[Optional["Image.Image"], Optional[Dict], Optional[Dict]]:
    """
    Скачивает превью и анализирует обложку. При use_auto_style анализирует стиль превью.
    Возвращает (thumbnail, analysis, style_from_thumbnail или None).
    """
    def log(msg):
        if log_fn:
            log_fn(msg)
        else:
            print(msg)

    thumbnail = download_thumbnail(youtube_url)
    if not thumbnail:
        log("📥 Не удалось скачать превью")
        return None, None, None

    video_title = get_youtube_video_title(youtube_url)
    if video_title:
        log(f"   Название ролика: {video_title[:60]}{'…' if len(video_title) > 60 else ''}")

    # vS/vR: img2img — превью само уходит в модель на редактирование (simple_mode),
    # или двухкартиночный style-transfer при заданном референсе. В обоих случаях
    # vision-анализ обложки/стиля и адаптация текста НЕ нужны. Отдаём минимальный analysis.
    if bool(getattr(preset, "simple_mode", False)) or bool((getattr(preset, "reference_image", "") or "").strip()):
        log("⚡ img2img-режим: пропускаю vision-анализ (превью → редактирование моделью)")
        return thumbnail, {"text": "", "video_title": video_title or ""}, None

    # v1.3.0: кэш анализа по video_id — пропускаем платные vision-вызовы при повторе
    use_auto = bool(getattr(preset, "use_auto_style", True))
    vid = extract_video_id(youtube_url) or ""
    cache_key = f"{vid}|{preset.analysis_provider}|{preset.analysis_model}|{int(use_auto)}" if vid else ""
    if cache_key:
        cached = _analysis_cache_get(cache_key)
        if cached:
            analysis_c, style_c = cached
            analysis_c["video_title"] = video_title or ""
            log("🔍 Анализ обложки... (из кэша, vision-вызовы пропущены)")
            return thumbnail, analysis_c, style_c

    log("🔍 Анализ обложки...")
    analysis = analyze_thumbnail(
        thumbnail,
        preset.analysis_provider,
        preset.analysis_model,
        gemini_api_key,
        openai_api_key,
        video_title=video_title or "",
    )
    if not analysis:
        log("   Не удалось проанализировать обложку")
        return thumbnail, None, None

    analysis["video_title"] = video_title or ""
    log(f"   Текст: {analysis.get('text', '')}")

    style_from_thumbnail = None
    if use_auto:
        log("🎨 Анализ стиля превью...")
        style_from_thumbnail = analyze_style_reference(
            thumbnail,
            preset.analysis_provider,
            preset.analysis_model,
            gemini_api_key,
            openai_api_key,
            video_title=video_title or "",
        )
    if cache_key:
        _analysis_cache_put(cache_key, analysis, style_from_thumbnail)
    return thumbnail, analysis, style_from_thumbnail


# 05.10.2026: обложки в img2img (превью донора → модель редактирует) упирались в фильтр
# безопасности: 13 из 262 за три дня, и выпуск уходил в разбор «обложка не нарисовалась
# дважды» (Záhady Zvířat, донор «Why Do Sperm Whales TERRIFY Orca?»). Фильтр режет само
# превью — кровь, драка хищников. У моделей одного ключа FastGen фильтры разные, поэтому
# после первого такого отказа остальные попытки идут в запасную модель того же ключа.
#
# 11.10.2026, владелец: «Flower по качеству сильно хуже, я бы в последнюю очередь его
# ставил». Запасных теперь цепочка: Nano Banana 2 → Nano Banana Pro → Flower. Каждый
# следующий отказ фильтра переводит оставшиеся попытки на следующую модель цепочки;
# число попыток (и оплат) прежнее. Через запятую «провайдер/модель» в
# COVER_SAFETY_FALLBACK_PROVIDER.
COVER_SAFETY_FALLBACK_PROVIDER = (os.environ.get("COVER_SAFETY_FALLBACK_PROVIDER")
                                  or "flow/NARWHAL,flow/GEM_PIX_2,flower")


def _is_safety_block(err) -> bool:
    s = str(err or "").lower()
    return "safety" in s and ("block" in s or "filter" in s)


def safety_fallback_chain(current: str, current_model: str = "", n_refs: int = 1,
                          tried=()) -> tuple:
    """Следующая запасная модель того же ключа после отказа фильтра: (провайдер, модель)
    или ("", "") — запасной нет.

    flower принимает ровно одну картинку на вход — при двух (обложка + референс стиля)
    он пропускается. Другая семья ключа запрещена — платить не тем ключом нельзя.
    tried — уже испробованные (провайдер, модель) этой обложки.
    """
    def norm(prov, model):
        prov, model = canonical_generation_provider(prov), str(model or "").strip().upper()
        # flow без явной модели — это Nano Banana Pro (см. _v6_operation).
        return prov, (model or "GEM_PIX_2") if prov == "flow" else model

    seen = {norm(p, m) for p, m in tried}
    seen.add(norm(current, current_model))
    for item in str(COVER_SAFETY_FALLBACK_PROVIDER or "").split(","):
        prov, _, model = item.strip().partition("/")
        if not prov:
            continue
        try:
            prov = canonical_generation_provider(prov)
        except Exception:                                          # noqa: BLE001
            continue
        model = model.strip().upper()
        if prov == "flower" and n_refs > 1:
            continue
        if norm(prov, model) in seen:
            continue
        try:
            if effective_simple_mode_provider(current, prov) != prov:
                continue
        except Exception:                                          # noqa: BLE001
            continue
        return prov, model
    return "", ""


def safety_fallback_provider(current: str, n_refs: int = 1) -> str:
    """Первая запасная модель (только провайдер) — для старых вызовов."""
    return safety_fallback_chain(current, "", n_refs)[0]


def _generate_simple_edit(
    thumbnail: "Image.Image",
    preset_lang: Preset,
    fast_gen_api_key: str,
    openai_api_key: str,
    log_fn=None,
    cancel_check=None,
    result_path: str = "",      # 03.09.2026: куда ляжет файл — для журнала платных вызовов
) -> CoverResult:
    """vS: «простая генерация» — реальное превью уходит в модель на РЕДАКТИРОВАНИЕ
    (перевод текста на preset_lang.language + лёгкая смена поз/ракурса), без vision-анализа,
    adapt_text и build_generation_prompt. Промпт — simple_edit_prompt_template.
    Провайдер по умолчанию — из пресета (flow/Nano Banana Pro); можно форсить через
    env SIMPLE_MODE_PROVIDER (напр. 'flower' = гарантированный img2img flower_image_edit),
    если Nano Banana игнорирует reference inputs. QC — check_simple_result (сверка 2 картинок)."""
    result = CoverResult()

    def log(msg):
        (log_fn or print)(msg)

    def cancelled() -> bool:
        return bool(cancel_check and cancel_check())

    if not fast_gen_api_key:
        result.error = "Укажите Fast Gen API ключ"
        return result

    try:
        thumb_uri = "data:image/png;base64," + image_to_base64(thumbnail, "PNG")
    except Exception as e:
        result.error = f"Не удалось подготовить превью для редактирования: {e}"
        return result

    language = (preset_lang.language or "Русский").strip()
    base_provider = canonical_generation_provider(
        preset_lang.generation_provider or "flow"
    )

    # vR: референс стиля (второй кадр). reference_image здесь — уже РАЗРЕШЁННЫЙ путь
    # (file_id → абсолютный путь резолвит воркер внутри папки юзера, см. cover_server).
    ref_path = (getattr(preset_lang, "reference_image", "") or "").strip()
    ref_uri = _image_path_to_data_uri(ref_path) if ref_path else None

    if ref_uri:
        # Порядок = роли в промпте: [0] «the first image» = обложка (контент/смысл/текст),
        # [1] «the second image» = референс (только стиль). Простой порядок без имён —
        # ровно как в ручном тесте юзера, где сработало.
        reference_images = [thumb_uri, ref_uri]
        template = _active_prompts().get("style_ref_prompt_template") or get_default_prompt("style_ref_prompt_template")
        # 2-кадровый режим → нужен провайдер, принимающий ≥2 inputs (flow/Nano Banana=0..10,
        # grok_image_edit=1..5, openai=0..10). flower_image_edit принимает РОВНО 1 → переключаем на flow.
        # env SIMPLE_MODE_PROVIDER здесь НЕ применяем, чтобы не сломать 2-кадровый вход.
        provider = base_provider
        if provider == "flower":
            provider = "flow"
            log("   ⚠ flower не принимает 2 референса — для style-transfer переключаю на flow (Nano Banana)")
        log(f"🎨 Style-transfer (img2img): обложка + референс → {provider}, перевод текста на «{language}»")
    else:
        if ref_path:
            log(f"   ⚠ Референс не найден/не читается ({ref_path[:60]}) — генерирую без него")
        reference_images = [thumb_uri]
        template = _active_prompts().get("simple_edit_prompt_template") or get_default_prompt("simple_edit_prompt_template")
        provider = effective_simple_mode_provider(
            base_provider,
            os.environ.get("SIMPLE_MODE_PROVIDER", ""),
        )
        log(f"⚡ Простая генерация (img2img): превью → {provider}, перевод текста на «{language}»")

    prompt = template.replace("{language}", language)
    model = preset_lang.generation_model
    debug_log = log_fn if getattr(preset_lang, "debug_mode", False) else None
    max_attempts = _attempts_for(preset_lang)
    candidates = []         # оплаченные картинки, не прошедшие сверку — по порядку

    generated = None
    # 04.10.2026: последняя картинка, которую провайдер ОТДАЛ. Раньше пустой ответ на
    # второй попытке затирал оплаченную первую (не прошедшую сверку), и задача кончалась
    # «все попытки вернули пустой результат» — оплаченная картинка выбрасывалась.
    last_paid = None
    safety_tried = []       # (провайдер, модель), уже упёршиеся в фильтр
    for attempt in range(max_attempts):
        if cancelled():
            result.error = "Отменено пользователем"
            return result
        # 03.09.2026: платный вызов идёт через журнал (reserve → ordered → delivered).
        # Номер попытки в отпечатке: движок сознательно пересэмплирует один и тот же
        # промпт после провала QC — это РАЗНЫЕ платные вызовы, а не дубль.
        generated = _journaled_generate(
            prompt, fast_gen_api_key,
            provider=provider,
            model=model,
            base_url=preset_lang.fast_gen_base_url or FAST_GEN_API_BASE,
            aspect_ratio=preset_lang.aspect_ratio,
            max_retries=3,
            log_fn=debug_log,
            reference_images=reference_images,
            cancel_check=cancel_check,
            unit=get_lang_code(language),
            lang=language,
            preset_name=preset_lang.name,
            result_path=result_path,
            note=f"simple/attempt{attempt + 1}",
            log=log,
        )
        if generated:
            last_paid = generated
        if cancelled():
            result.error = "Отменено пользователем"
            return result
        if not generated:
            last_err = getattr(_last_fastgen_error, 'value', None)
            if last_err:
                short = last_err[:240] + ("..." if len(last_err) > 240 else "")
                log(f"   ⚠ Попытка {attempt + 1}/{max_attempts}: Fast Gen не отдал картинку — {short}")
                _last_fastgen_error.value = None
                if _is_safety_block(last_err):
                    safety_tried.append((provider, model))
                    alt, alt_model = safety_fallback_chain(provider, model,
                                                           len(reference_images), safety_tried)
                    if alt:
                        log(f"   🔀 Фильтр безопасности {provider}{'/' + model if model else ''} — "
                            f"дальше пробую моделью {alt}{'/' + alt_model if alt_model else ''} "
                            f"(тот же ключ FastGen, фильтр у неё свой)")
                        provider, model = alt, alt_model
            else:
                log(f"   ⚠ Попытка {attempt + 1}/{max_attempts}: пустой результат от API, повтор...")
            _interruptible_sleep(2 + attempt * 2, cancel_check)
            continue
        # QC: сверяем текст оригинала и результата (модель переводила сама)
        log(f"✅ Проверка текста (попытка {attempt + 1}/{max_attempts})...")
        passed, details = check_simple_result(
            thumbnail, generated, language,
            preset_lang.analysis_provider, preset_lang.analysis_model, openai_api_key,
        )
        result.check_result = details
        if passed:
            if details.get("checked") is False:
                # Зелёная галочка тут была бы враньём: картинку отдаём, но человек
                # должен знать, что текст на ней автоматически никто не сверял.
                log(f"   ⚠ {details.get('issues') or 'проверка текста не выполнена'}")
                log("   Картинка сохранена: она уже оплачена, и выбрасывать её "
                    "из-за неработающего контролёра нельзя.")
                result.used_fallback = True
                result.unchecked = True
            else:
                log("   ✅ Проверка пройдена")
            result.image = generated
            result.success = True
            return result
        log(f"   ❌ Проблема: {details.get('issues', '')}")
        candidates.append(generated)

    generated = generated or last_paid
    if not generated:
        result.error = f"Ошибка генерации изображения (все {max_attempts} попыток вернули пустой результат)"
        return result

    if candidates:
        log(f"   ⚖ Проверку не прошёл ни один из {len(candidates)} вариантов — "
            f"прошу ИИ оценить каждый от 0 до 10")
        score_prompt = ((_active_prompts().get("simple_score_prompt_template")
                         or get_default_prompt("simple_score_prompt_template")
                         or _SIMPLE_SCORE_PROMPT).replace("{language}", language))
        best = pick_best_candidate(
            candidates,
            lambda img: _score_one([thumbnail, img], score_prompt,
                                   preset_lang.analysis_provider,
                                   preset_lang.analysis_model, openai_api_key),
            log)
        if best is not None:
            result.image = candidates[best]
            result.success = True
            result.scored = True
            return result

    # Текст неидеален, но картинка есть — сохраняем последний вариант (анти cost-drain)
    log(f"   ⚠ После {max_attempts} попыток проверка текста не пройдена — сохраняю последний результат")
    result.image = generated
    result.success = True
    result.used_fallback = True
    return result


def _generate_for_language(
    thumbnail: "Image.Image",
    analysis: Dict,
    style_from_analysis: Optional[Dict],
    preset_lang: Preset,
    gemini_api_key: str,
    openai_api_key: str,
    fast_gen_api_key: str,
    log_fn=None,
    cancel_check=None,
    skip_adapt: bool = False,
    reference_images: Optional[List[str]] = None,
    mode: str = "url",
    result_path: str = "",      # 03.09.2026: куда ляжет файл — для журнала платных вызовов
) -> CoverResult:
    """
    Генерация одной обложки по уже готовым thumbnail и analysis (без повторного скачивания и анализа).
    cancel_check — callable, возвращает True если задание отменено; проверяется между шагами.
    skip_adapt (v1.0.4) — если True, не вызывает adapt_text: analysis['text'] уже на целевом языке
        (например в title-mode юзер задаёт текст per-lang сразу).
    reference_images (v1.0.9) — список data URI / 'file:...' для передачи в Fast Gen Flow
        как style/content guidance. Используется в title-mode вместо OpenAI vision style analysis.
    mode (v1.0.12) — "url" | "title"; влияет на directive композиции в build_generation_prompt.
    """
    def log(msg):
        if log_fn:
            log_fn(msg)
        else:
            print(msg)

    def cancelled() -> bool:
        return bool(cancel_check and cancel_check())

    result = CoverResult()
    result.analysis = analysis
    original_text = analysis.get("text", "")
    if cancelled():
        result.error = "Отменено пользователем"
        return result
    try:
        # vS/vR: img2img — отдельная ветка: превью редактирует модель (перевод текста +
        # смена поз), опц. со стилем из референса. Без adapt_text и build_generation_prompt.
        _has_ref = bool((getattr(preset_lang, "reference_image", "") or "").strip())
        if (bool(getattr(preset_lang, "simple_mode", False)) or _has_ref) and mode == "url" and thumbnail is not None:
            r = _generate_simple_edit(
                thumbnail, preset_lang, fast_gen_api_key, openai_api_key,
                log_fn=log_fn, cancel_check=cancel_check,
                result_path=result_path,        # 03.09.2026: журнал платных вызовов
            )
            r.analysis = analysis
            return r
        adapted_text = ""
        if original_text:
            if skip_adapt:
                # v1.0.4 title-mode: текст уже на нужном языке (юзер задал per-lang).
                log(f"✍️ Текст уже на {preset_lang.language} (skip_adapt) — пропускаю LLM-адаптацию")
                adapted_text = original_text
            elif openai_api_key:
                log(f"✍️ Адаптация текста на {preset_lang.language}...")
                adapted_text = adapt_text(
                    original_text, preset_lang.language, openai_api_key,
                    video_title=analysis.get("video_title", ""),
                )
                log(f"   Результат: {adapted_text}")
            else:
                adapted_text = original_text
        result.adapted_text = adapted_text

        # Наложение текста через PIL отключено — всегда просим модель нарисовать текст
        include_text = True
        has_reference = bool(reference_images)
        prompt = build_generation_prompt(
            analysis, adapted_text, preset_lang, style_from_analysis=style_from_analysis,
            include_text=include_text, mode=mode, has_reference=has_reference,
        )
        log(f"📝 Промпт: {prompt[:120]}...")

        if not fast_gen_api_key:
            result.error = "Укажите Fast Gen API ключ"
            return result

        generated = None
        last_paid = None          # см. _generate_simple_edit: пустой ответ не затирает оплаченный
        max_attempts = _attempts_for(preset_lang)
        candidates = []     # оплаченные картинки, не прошедшие проверку — по порядку
        debug_log = log_fn if getattr(preset_lang, "debug_mode", False) else None
        gen_failures = 0
        for attempt in range(max_attempts):
            if cancelled():
                result.error = "Отменено пользователем"
                return result
            # 03.09.2026: платный вызов идёт через журнал (reserve → ordered → delivered).
            # Номер попытки в отпечатке: пересэмплирование того же промпта после провала
            # проверки текста — это РАЗНЫЕ платные вызовы, а не дубль одного.
            generated = _journaled_generate(
                prompt, fast_gen_api_key,
                provider=preset_lang.generation_provider,
                model=preset_lang.generation_model,
                base_url=preset_lang.fast_gen_base_url or FAST_GEN_API_BASE,
                aspect_ratio=preset_lang.aspect_ratio,
                max_retries=3,
                log_fn=debug_log,
                reference_images=reference_images,
                cancel_check=cancel_check,
                unit=get_lang_code(preset_lang.language),
                lang=preset_lang.language,
                preset_name=preset_lang.name,
                result_path=result_path,
                note=f"{mode}/attempt{attempt + 1}",
                log=log,
            )
            if generated:
                last_paid = generated
            if cancelled():
                result.error = "Отменено пользователем"
                return result
            if not generated:
                gen_failures += 1
                # v1.0.11: показать содержательную причину если Fast Gen вернул API error
                last_err = getattr(_last_fastgen_error, 'value', None)
                if last_err:
                    short = last_err[:240] + ("..." if len(last_err) > 240 else "")
                    log(f"   ⚠ Попытка {attempt + 1}/{max_attempts}: Fast Gen не отдал картинку — {short}")
                    _last_fastgen_error.value = None
                else:
                    log(f"   ⚠ Попытка {attempt + 1}/{max_attempts}: пустой результат от API (причина не определена), повтор...")
                _interruptible_sleep(2 + attempt * 2, cancel_check)  # 2с/4с/6с backoff, прерывается отменой
                continue
            if not adapted_text or preset_lang.fallback_mode == "always_overlay":
                result.image = generated
                result.success = True
                return result
            log(f"✅ Проверка результата (попытка {attempt + 1}/{max_attempts})...")
            passed, check_details = check_result(
                generated, adapted_text,
                preset_lang.analysis_provider, preset_lang.analysis_model,
                gemini_api_key, openai_api_key,
                video_title=analysis.get("video_title", ""),
            )
            result.check_result = check_details
            if passed:
                log("   ✅ Проверка пройдена")
                result.image = generated
                result.success = True
                return result
            log(f"   ❌ Проблема: {check_details.get('issues', '')}")
            candidates.append(generated)

        # Все попытки исчерпаны
        generated = generated or last_paid
        if not generated:
            result.error = f"Ошибка генерации изображения (все {max_attempts} попыток вернули пустой результат)"
            return result

        if candidates:
            log(f"   ⚖ Проверку не прошёл ни один из {len(candidates)} вариантов — "
                f"прошу ИИ оценить каждый от 0 до 10")
            score_prompt = ((_active_prompts().get("score_prompt_template")
                             or get_default_prompt("score_prompt_template")
                             or _SCORE_PROMPT)
                            .replace("{video_title}",
                                     (analysis.get("video_title") or "").strip()
                                     or "(not provided)")
                            .replace("{expected_text}", adapted_text))
            best = pick_best_candidate(
                candidates,
                lambda img: _score_one(img, score_prompt,
                                       preset_lang.analysis_provider,
                                       preset_lang.analysis_model, openai_api_key),
                log)
            if best is not None:
                result.image = candidates[best]
                result.success = True
                result.scored = True
                return result

        # После max_attempts проверки текста — генерируем чистую версию без текста
        log(f"   ⚠ После {max_attempts} попыток проверка текста не пройдена — генерирую вариант без текста...")
        prompt_clean = build_generation_prompt(
            analysis, adapted_text, preset_lang, style_from_analysis=style_from_analysis,
            include_text=False, mode=mode, has_reference=has_reference,
        )
        clean_img = None
        # v1.3.0: clean-fallback ограничен 2 попытками (а не всеми max_attempts) — анти cost-drain
        for clean_attempt in range(min(2, max_attempts)):
            if cancelled():
                result.error = "Отменено пользователем"
                return result
            # 03.09.2026: clean-fallback — тоже платный вызов, тоже через журнал.
            clean_img = _journaled_generate(
                prompt_clean, fast_gen_api_key,
                provider=preset_lang.generation_provider,
                model=preset_lang.generation_model,
                base_url=preset_lang.fast_gen_base_url or FAST_GEN_API_BASE,
                aspect_ratio=preset_lang.aspect_ratio,
                max_retries=3,
                log_fn=debug_log,
                reference_images=reference_images,
                cancel_check=cancel_check,
                unit=get_lang_code(preset_lang.language),
                lang=preset_lang.language,
                preset_name=preset_lang.name,
                result_path=result_path,
                note=f"{mode}/clean{clean_attempt + 1}",
                log=log,
            )
            if clean_img:
                log(f"   ✅ Сохраняю вариант без текста (попытка {clean_attempt + 1})")
                result.image = clean_img
                result.success = True
                result.used_fallback = True
                return result
            log(f"   ⚠ Попытка {clean_attempt + 1}/{max_attempts} без текста — пусто, повтор...")
            _interruptible_sleep(2 + clean_attempt * 2, cancel_check)

        log(f"   ❌ Не удалось сгенерировать даже без текста за {max_attempts} попыток — сохраняю последний вариант с текстом")
        result.image = generated
        result.success = True
        result.used_fallback = True
        return result
    except Exception as e:
        import traceback
        result.error = str(e)
        log(f"❌ Ошибка: {e}")
        if getattr(preset_lang, "debug_mode", False) and log_fn:
            log_fn(traceback.format_exc())
        return result


def _process_batch_title_mode(
    *,
    preset: Preset,
    titles_per_lang: Dict[str, str],
    extra_prompt: str,
    openai_api_key: str,
    fast_gen_api_key: str,
    output_dir: str,
    refs_per_language: Optional[Dict[str, str]] = None,
    log_fn=None,
    out_files: Optional[List[str]] = None,
    skip_existing: bool = False,
    cancel_check=None,
    fallback_files: Optional[List[str]] = None,
    max_threads: int = 1,
    progress_cb=None,
    unchecked_files: Optional[List[str]] = None,
) -> List[CoverResult]:
    """v1.0.4 — title-mode пайплайн (без YouTube URL). Один scene на job, потом цикл по языкам.
    progress_cb(done, total) (v1.3.2) — прогресс по мере готовности языков."""
    def log(msg):
        if log_fn:
            log_fn(msg)
        else:
            print(msg)

    results: List[CoverResult] = []
    if not titles_per_lang:
        return results
    refs_per_language = refs_per_language or {}

    # base_title для генерации сцены: язык пресета приоритетнее, иначе первый НЕпустой,
    # иначе extra_prompt (v1.0.7 — пустые тайтлы разрешены для "no-text" обложек).
    non_empty_titles = [t.strip() for t in titles_per_lang.values() if (t or "").strip()]
    base_title = (titles_per_lang.get(preset.language) or "").strip() \
        or (non_empty_titles[0] if non_empty_titles else "") \
        or (extra_prompt or "").strip() \
        or "untitled scene"
    log(f"\n{'='*50}\n✏️ Title-mode: base_title={base_title!r}")
    if extra_prompt:
        log(f"   Extra: {extra_prompt[:120]}{'…' if len(extra_prompt) > 120 else ''}")
    log(f"   Языки: {list(titles_per_lang.keys())}")
    log(f"{'='*50}")

    # Папка для файлов: <safe_base_title> или fallback с sha256 если sanitize дал пусто
    safe_title = re.sub(r'[^\w\-]', '_', base_title)[:80].strip("_")
    if not safe_title:
        title_hash = hashlib.sha256(base_title.encode("utf-8")).hexdigest()[:6]
        safe_title = f"untitled_{title_hash}"
    folder_name = safe_title
    vid_synthetic = "title"   # для _output_filename позиции video_id

    if cancel_check and cancel_check():
        log("⏹ Задание отменено")
        return results

    # ШАГ 1: один раз генерируем сцену (на base_title + extra_prompt)
    log("🎬 Генерация сцены из названия...")
    try:
        analysis_base = generate_scene_from_title(
            base_title, extra_prompt, openai_api_key, preset.analysis_model
        )
        analysis_base["video_title"] = base_title
        log(f"   Scene: {(analysis_base.get('scene_description') or '')[:120]}")
    except Exception as e:
        import traceback
        log(f"❌ Ошибка генерации сцены: {e}")
        if getattr(preset, "debug_mode", False):
            log(traceback.format_exc())
        for _ in titles_per_lang:
            results.append(CoverResult(error=f"scene generation failed: {e}"))
        return results

    out_folder = os.path.join(output_dir, folder_name)
    os.makedirs(out_folder, exist_ok=True)

    # ШАГ 2: параллельная обработка языков (pattern из process_batch URL-mode generation pool, L2005)
    total = len(titles_per_lang)
    workers = max(1, min(int(max_threads or 1), total))
    log(f"[parallel] {workers} workers for {total} languages (max_threads={max_threads})")

    # thread-local prompts: захватываем конфиг текущего worker-потока, передаём в sub-потоки
    _current_prompts = get_prompts_config()
    def _thread_init():
        set_prompts_config(_current_prompts)

    # Pre-compute paths и skip_existing per язык (вне пула — быстро и безопасно)
    tasks = []   # list of (lang, lang_text, rel_path, full_path, preset_lang)
    skipped = {}  # lang -> rel_path (для skip_existing)
    for idx, (lang, lang_text) in enumerate(titles_per_lang.items()):
        preset_lang = Preset(**asdict(preset))
        preset_lang.language = lang
        lang_code = get_lang_code(lang)
        filename = _output_filename(lang_code, vid_synthetic, preset.name, lang_text)
        rel_path = folder_name + "/" + filename
        full_path = os.path.join(output_dir, rel_path)
        if skip_existing and os.path.exists(full_path):
            log(f"⏭ Уже есть: {rel_path}")
            skipped[lang] = rel_path
            continue
        tasks.append((lang, lang_text, rel_path, full_path, preset_lang))

    log(f"▶ К генерации: {len(tasks)} языков в пуле из {workers} потоков (skip_existing={len(skipped)})")

    def _do_one_lang(args):
        lang, lang_text, rel_path, full_path, preset_lang = args
        if cancel_check and cancel_check():
            return lang, rel_path, CoverResult(error="Отменено пользователем")

        def task_log(msg):
            if log_fn:
                log_fn(f"[{lang}] {msg}")

        # Per-lang analysis: тот же scene, но с целевым текстом (юзер ввёл)
        analysis_lang = dict(analysis_base)
        analysis_lang["text"] = lang_text

        # v1.0.9: ref-файл передаётся НАПРЯМУЮ в Fast Gen Flow как reference_images
        # (data URI). Старый путь через analyze_style_reference→OpenAI vision→текст убран —
        # на пустых/абстрактных ref он молча возвращал {} и стиль не применялся.
        ref_data_uri = None
        ref_path = refs_per_language.get(lang)
        if ref_path:
            ref_data_uri = _image_path_to_data_uri(ref_path)
            if ref_data_uri:
                task_log(f"🖼 Реф-картинка прикреплена ({os.path.basename(ref_path)}, {len(ref_data_uri)//1024} KB base64)")
            else:
                task_log(f"⚠ Реф-файл {os.path.basename(ref_path)} пропущен (не существует/>5MB/не картинка)")
        ref_list = [ref_data_uri] if ref_data_uri else None

        r = _generate_for_language(
            thumbnail=None,
            analysis=analysis_lang,
            style_from_analysis=None,  # текстовый style больше не нужен — ref идёт картинкой в Fast Gen
            preset_lang=preset_lang,
            gemini_api_key="",
            openai_api_key=openai_api_key,
            fast_gen_api_key=fast_gen_api_key,
            log_fn=task_log,
            cancel_check=cancel_check,
            skip_adapt=True,
            reference_images=ref_list,
            mode="title",
            result_path=full_path,          # 03.09.2026: журнал платных вызовов
        )
        if r.success and r.image:
            r.image.save(full_path, "JPEG", quality=92)
            task_log(f"💾 Сохранено: {full_path}")
        else:
            err = r.error or "не удалось сгенерировать/сохранить"
            task_log(f"❌ {err}")
        return lang, rel_path, r

    # Параллельное выполнение через ThreadPoolExecutor (одинаково с URL-mode generation pool)
    results_by_lang: Dict[str, tuple] = {}  # lang -> (rel_path, CoverResult)
    if tasks:
        _prog_total = len(tasks)
        _prog_done = 0
        with ThreadPoolExecutor(max_workers=workers, initializer=_thread_init) as executor:
            futures = {executor.submit(_do_one_lang, t): t[0] for t in tasks}
            cancelled_flag = False
            for future in as_completed(futures):
                if cancel_check and cancel_check() and not cancelled_flag:
                    cancelled_flag = True
                    log("⏹ Задание отменено. Отменяю незапущенные генерации...")
                    for fut in futures:
                        if not fut.done():
                            fut.cancel()
                try:
                    lang, rel_path, r = future.result()
                    results_by_lang[lang] = (rel_path, r)
                except Exception as e:
                    lang = futures[future]
                    log(f"❌ [{lang}] thread exception: {e}")
                    results_by_lang[lang] = ("", CoverResult(error=str(e)))
                _prog_done += 1
                if progress_cb:
                    try:
                        progress_cb(_prog_done, _prog_total)
                    except Exception:
                        pass

    # Собираем results + out_files в детерминированном порядке titles_per_lang.keys()
    for lang in titles_per_lang.keys():
        if lang in skipped:
            rel = skipped[lang]
            if out_files is not None and rel not in out_files:
                out_files.append(rel)
            results.append(CoverResult(success=True))
            continue
        if lang not in results_by_lang:
            # отменённый/незапущенный
            results.append(CoverResult(error="Отменено пользователем"))
            continue
        rel_path, r = results_by_lang[lang]
        if r.success and r.image and rel_path:
            if out_files is not None and rel_path not in out_files:
                out_files.append(rel_path)
            if getattr(r, "used_fallback", False) and fallback_files is not None and rel_path not in fallback_files:
                fallback_files.append(rel_path)
            if getattr(r, "unchecked", False) and unchecked_files is not None and rel_path not in unchecked_files:
                unchecked_files.append(rel_path)
        results.append(r)

    return results


# ════════════════════════════════════════════════════════
#  SEED-MODE: механизм «ПРЕСЕТ + СЕМЯ → N обложек» (niche-agnostic)
#  Финальный промпт = шаблон-пресет ⊕ семя → НАПРЯМУЮ в Fast Gen v6 с референсом.
#  Без generate_scene_from_title / build_generation_prompt / check_result.
# ════════════════════════════════════════════════════════

_SEED_MARKER_RE = re.compile(r">>>\s*СЮДА ВСТАВЬ СЕМЯ.*?<<<", re.S)
_SEED_TITLE_LABEL_RE = re.compile(
    r'^\s*(?:ИДЕЯ\s*/\s*НАЗВАНИЕ|НАЗВАНИЕ|ЗАГОЛОВОК|IDEA|TITLE)\s*[:\-]\s*(.+)$', re.I
)


def compose_seed_prompt(template: str, seed: str, lang_display: str) -> str:
    """Niche-agnostic сборка промпта обложки: финальный промпт = шаблон-пресет ⊕ семя.
    - {ЯЗЫК}/{LANG}/{ЯЗ} → язык словом (lang_display.lower(), напр. 'польский');
    - маркер семени '>>> СЮДА ВСТАВЬ СЕМЯ … <<<' или {СЕМЯ}/{SEED} → блок семени;
    - пустой шаблон → промпт = семя как есть (юзер принёс готовый промпт целиком).
    Вставка семени через re.sub(lambda) / str.replace — ЛИТЕРАЛЬНО (в семени бывают '\\' и группы-ссылки)."""
    seed = (seed or "").strip()
    tmpl = (template or "").strip()
    if not tmpl:
        return seed
    lang_word = (lang_display or "").strip().lower()
    out = tmpl
    for ph in ("{ЯЗЫК}", "{LANG}", "{ЯЗ}"):
        out = out.replace(ph, lang_word)
    if _SEED_MARKER_RE.search(out):
        out = _SEED_MARKER_RE.sub(lambda _m: seed, out)
    elif "{СЕМЯ}" in out or "{SEED}" in out:
        out = out.replace("{СЕМЯ}", seed).replace("{SEED}", seed)
    elif seed:
        out = out.rstrip() + "\n\n" + seed
    return out


def _seed_folder_name(seed_text: str) -> str:
    """Имя папки из первой непустой строки семени (снимаем известный лейбл ИДЕЯ/НАЗВАНИЕ и кавычки)."""
    line = next((l.strip() for l in (seed_text or "").splitlines() if l.strip()), "")
    m = _SEED_TITLE_LABEL_RE.match(line)
    core = (m.group(1) if m else line).strip().strip('"“”«»').strip()
    safe = re.sub(r'[^\w\-]', '_', core)[:60].strip("_")
    if not safe:
        safe = "seed_" + hashlib.sha256((seed_text or "x").encode("utf-8")).hexdigest()[:6]
    return safe


def _seed_output_filename(lang_code: str, preset_name: str, seed_text: str, variant: int) -> str:
    base = _seed_folder_name(seed_text)[:40].strip("_") or "seed"
    safe_preset = re.sub(r'[^\w\-]', '_', preset_name or "preset")
    return f"{lang_code}_{base}_v{variant}_{safe_preset}.jpg"


def _process_batch_seed_mode(
    *,
    preset: Preset,
    seeds_per_lang: Dict[str, str],
    n_variants: int,
    fast_gen_api_key: str,
    output_dir: str,
    refs_per_language: Optional[Dict[str, str]] = None,
    log_fn=None,
    out_files: Optional[List[str]] = None,
    skip_existing: bool = False,
    cancel_check=None,
    max_threads: int = 1,
    progress_cb=None,
) -> List[CoverResult]:
    """seed-mode пайплайн: для каждого языка compose_seed_prompt(preset.prompt_template, seed, lang) →
    N вариантов НАПРЯМУЮ через Fast Gen v6 с референсом. Задачи = языки × N вариантов, пул потоков."""
    def log(msg):
        if log_fn:
            log_fn(msg)
        else:
            print(msg)

    results: List[CoverResult] = []
    if not seeds_per_lang:
        return results
    refs_per_language = refs_per_language or {}
    n_variants = max(1, min(8, int(n_variants or 1)))

    if cancel_check and cancel_check():
        log("⏹ Задание отменено")
        return results

    first_seed = next((s for s in seeds_per_lang.values() if (s or "").strip()), "")
    folder_name = _seed_folder_name(first_seed)
    out_folder = os.path.join(output_dir, folder_name)
    os.makedirs(out_folder, exist_ok=True)
    log(f"\n{'='*50}\n🌱 Seed-mode: папка={folder_name!r}, языков={len(seeds_per_lang)}, вариантов={n_variants}")
    if not (preset.prompt_template or "").strip():
        log("   ℹ Шаблон пресета пуст — промпт берётся из семени как есть (passthrough).")
    log(f"{'='*50}")

    _current_prompts = get_prompts_config()
    def _thread_init():
        set_prompts_config(_current_prompts)

    # предвычислить задачи (lang, variant) + пути; финальный промпт на язык — один раз
    tasks = []   # (lang, v, final_prompt, rel_path, full_path, preset_lang)
    final_prompt_by_lang: Dict[str, str] = {}
    for lang, seed_text in seeds_per_lang.items():
        preset_lang = Preset(**asdict(preset))
        preset_lang.language = lang
        final_prompt = compose_seed_prompt(preset.prompt_template, seed_text, lang)
        final_prompt_by_lang[lang] = final_prompt
        lang_code = get_lang_code(lang)
        for v in range(1, n_variants + 1):
            filename = _seed_output_filename(lang_code, preset.name, seed_text or first_seed, v)
            rel_path = folder_name + "/" + filename
            full_path = os.path.join(output_dir, rel_path)
            if skip_existing and os.path.exists(full_path):
                log(f"⏭ Уже есть: {rel_path}")
                if out_files is not None and rel_path not in out_files:
                    out_files.append(rel_path)
                continue
            tasks.append((lang, v, final_prompt, rel_path, full_path, preset_lang))

    total = len(tasks)
    if total == 0:
        return [CoverResult(success=True) for _ in range(len(seeds_per_lang) * n_variants)]
    workers = max(1, min(int(max_threads or 1), total))
    log(f"▶ К генерации: {total} задач ({len(seeds_per_lang)} яз × {n_variants} вар) в пуле из {workers} потоков")

    def _do_one(args):
        lang, v, final_prompt, rel_path, full_path, preset_lang = args
        if cancel_check and cancel_check():
            return rel_path, CoverResult(error="Отменено пользователем")

        def task_log(msg):
            if log_fn:
                log_fn(f"[{lang} v{v}] {msg}")

        ref_list = None
        ref_path = refs_per_language.get(lang)
        if ref_path:
            uri = _image_path_to_data_uri(ref_path)
            if uri:
                ref_list = [uri]
            else:
                task_log(f"⚠ Реф-файл {os.path.basename(ref_path)} пропущен (не существует/>5MB/не картинка)")

        r = CoverResult()
        # 03.09.2026: платный вызов идёт через журнал (reserve → ordered → delivered).
        # unit = код языка + номер варианта: в seed-режиме вариантов на язык несколько.
        img = _journaled_generate(
            final_prompt, fast_gen_api_key,
            provider=preset_lang.generation_provider,
            model=preset_lang.generation_model,
            base_url=preset_lang.fast_gen_base_url or FAST_GEN_API_BASE,
            aspect_ratio=preset_lang.aspect_ratio,
            max_retries=max(1, getattr(preset_lang, "max_generation_retries", 2)),
            log_fn=(log_fn if getattr(preset_lang, "debug_mode", False) else None),
            reference_images=ref_list,
            cancel_check=cancel_check,
            unit=(get_lang_code(lang) if n_variants <= 1 else f"{get_lang_code(lang)}-v{v}"),
            lang=lang,
            preset_name=preset_lang.name,
            result_path=full_path,
            note="seed",
            log=task_log,
        )
        if img is not None:
            img.save(full_path, "JPEG", quality=92)
            r.image = img
            r.success = True
            task_log(f"💾 Сохранено: {full_path}")
        else:
            last_err = getattr(_last_fastgen_error, "value", None) or "Fast Gen не отдал картинку"
            r.error = str(last_err)[:240]
            _last_fastgen_error.value = None
            task_log(f"❌ {r.error}")
        return rel_path, r

    results_by_path: Dict[str, CoverResult] = {}
    _prog_done = 0
    with ThreadPoolExecutor(max_workers=workers, initializer=_thread_init) as executor:
        futures = {executor.submit(_do_one, t): t for t in tasks}
        cancelled_flag = False
        for future in as_completed(futures):
            if cancel_check and cancel_check() and not cancelled_flag:
                cancelled_flag = True
                log("⏹ Задание отменено. Отменяю незапущенные генерации...")
                for fut in futures:
                    if not fut.done():
                        fut.cancel()
            try:
                rel_path, r = future.result()
                results_by_path[rel_path] = r
            except Exception as e:
                t = futures[future]
                results_by_path[t[3]] = CoverResult(error=str(e))
            _prog_done += 1
            if progress_cb:
                try:
                    progress_cb(_prog_done, total)
                except Exception:
                    pass

    for lang, v, final_prompt, rel_path, full_path, preset_lang in tasks:
        r = results_by_path.get(rel_path) or CoverResult(error="не выполнено")
        if r.success and rel_path and out_files is not None and rel_path not in out_files:
            out_files.append(rel_path)
        results.append(r)

    # meta.json — сайдкар с точным промптом на язык + референс + N (для юзера/дебага)
    try:
        meta = {
            "mode": "seed",
            "preset": preset.name,
            "provider": preset.generation_provider,
            "model": preset.generation_model,
            "n_variants": n_variants,
            "languages": {
                lang: {
                    "final_prompt": final_prompt_by_lang.get(lang, ""),
                    "reference": os.path.basename(refs_per_language.get(lang, "")) if refs_per_language.get(lang) else "",
                }
                for lang in seeds_per_lang.keys()
            },
        }
        with open(os.path.join(out_folder, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return results


def process_batch(
    urls: List[str],
    preset: Preset,
    gemini_api_key: str,
    openai_api_key: str,
    fast_gen_api_key: str = "",
    output_dir: str = OUTPUT_DIR,
    languages: Optional[List[str]] = None,
    refs_per_language: Optional[Dict[str, str]] = None,
    log_fn=None,
    max_threads: int = 1,
    out_files: Optional[List[str]] = None,
    skip_existing: bool = False,
    cancel_check=None,
    fallback_files: Optional[List[str]] = None,
    titles_per_lang: Optional[Dict[str, str]] = None,
    extra_prompt: str = "",
    seeds_per_lang: Optional[Dict[str, str]] = None,
    n_variants: int = 3,
    progress_cb=None,
    unchecked_files: Optional[List[str]] = None,
) -> List[CoverResult]:
    """Пакетная генерация обложек. cancel_check — callable, возвращает True если задание отменено.
    progress_cb(done, total) (v1.3.2) — вызывается по мере готовности обложек для прогресс-бара.
    fallback_files — если передан, сюда добавляются rel_path обложек, сохранённых через fallback (без текста).
    v1.0.4 — если передан titles_per_lang, активируется title-mode (без YouTube URL):
      generate_scene_from_title(...) один раз, потом цикл по языкам с готовым текстом из titles_per_lang.
    seed-mode — если передан seeds_per_lang: механизм «пресет+семя → N обложек» (niche-agnostic)."""
    # ─── SEED-MODE BRANCH (пресет+семя, приоритетнее прочих) ───
    if seeds_per_lang:
        return _process_batch_seed_mode(
            preset=preset,
            seeds_per_lang=seeds_per_lang,
            n_variants=n_variants,
            fast_gen_api_key=fast_gen_api_key,
            output_dir=output_dir,
            refs_per_language=refs_per_language,
            log_fn=log_fn,
            out_files=out_files,
            skip_existing=skip_existing,
            cancel_check=cancel_check,
            max_threads=max_threads,
            progress_cb=progress_cb,
        )
    # ─── v1.0.4 TITLE-MODE BRANCH ───
    if titles_per_lang:
        return _process_batch_title_mode(
            preset=preset,
            titles_per_lang=titles_per_lang,
            extra_prompt=extra_prompt,
            openai_api_key=openai_api_key,
            fast_gen_api_key=fast_gen_api_key,
            output_dir=output_dir,
            refs_per_language=refs_per_language,
            log_fn=log_fn,
            out_files=out_files,
            skip_existing=skip_existing,
            cancel_check=cancel_check,
            fallback_files=fallback_files,
            max_threads=max_threads,
            progress_cb=progress_cb,
            unchecked_files=unchecked_files,
        )
    # ─── URL-MODE (существующая логика — не трогаю) ───
    if languages:
        lang_list = [l.strip() for l in languages if l.strip()]
    else:
        raw = (preset.languages or "").strip()
        if raw:
            lang_list = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        else:
            lang_list = [preset.language or "Русский"]
    if not lang_list:
        lang_list = [preset.language or "Русский"]

    results = []
    total_jobs = len(urls) * len(lang_list)

    # --- Общая функция сохранения результата ---
    def _save_result(r, u, lng, anal, url_index: int):
        vid = extract_video_id(u) or f"cover_{url_index+1}"
        video_title = (anal.get("video_title") or "").strip()
        folder_name = _safe_folder_name(video_title, vid)
        out_folder = os.path.join(output_dir, folder_name)
        os.makedirs(out_folder, exist_ok=True)
        lang_code = get_lang_code(lng)
        filename = _output_filename(lang_code, vid, preset.name, video_title)
        filepath = os.path.join(out_folder, filename)
        r.image.save(filepath, "JPEG", quality=92)
        rel = folder_name + "/" + filename
        if out_files is not None and rel not in out_files:
            out_files.append(rel)
        # Отмечаем fallback-обложки (сгенерированы без текста после провала проверки)
        if getattr(r, "used_fallback", False) and fallback_files is not None and rel not in fallback_files:
            fallback_files.append(rel)
        if getattr(r, "unchecked", False) and unchecked_files is not None and rel not in unchecked_files:
            unchecked_files.append(rel)
        if r.image_no_text:
            safe_name = _safe_video_name_for_file(video_title, vid)
            no_text_path = os.path.join(out_folder, f"No text_{safe_name}.jpg")
            r.image_no_text.save(no_text_path, "JPEG", quality=92)
            rel_nt = folder_name + "/" + f"No text_{safe_name}.jpg"
            if out_files is not None and rel_nt not in out_files:
                out_files.append(rel_nt)
            if log_fn:
                log_fn(f"💾 Сохранено (без текста): {no_text_path}")
        if log_fn:
            log_fn(f"💾 Сохранено: {filepath}")

    # ═══════════ ПОСЛЕДОВАТЕЛЬНЫЙ РЕЖИМ ═══════════
    if max_threads <= 1:
        job_idx = 0
        for i, url in enumerate(urls):
            if cancel_check and cancel_check():
                if log_fn:
                    log_fn("⏹ Задание отменено")
                break
            if log_fn:
                log_fn(f"\n{'='*50}")
                log_fn(f"📌 Ссылка {i+1}/{len(urls)}: {url}")
                log_fn(f"   Скачивание и анализ — один раз, затем {len(lang_list)} языков")
                log_fn(f"{'='*50}")
            thumbnail, analysis, style_from_thumbnail = _download_and_analyze_url(
                url, preset, gemini_api_key, openai_api_key, log_fn
            )
            if thumbnail is None:
                for _ in lang_list:
                    results.append(CoverResult(error="Не удалось скачать превью"))
                continue
            if analysis is None:
                for _ in lang_list:
                    results.append(CoverResult(error="Не удалось проанализировать обложку"))
                continue
            for j, lang in enumerate(lang_list):
                if cancel_check and cancel_check():
                    if log_fn:
                        log_fn("⏹ Задание отменено")
                    return results
                job_idx += 1
                preset_lang = Preset(**asdict(preset))
                preset_lang.language = lang
                if log_fn:
                    log_fn(f"\n--- [{job_idx}/{total_jobs}] {url} — {lang} ---")
                vid = extract_video_id(url) or f"cover_{i+1}"
                video_title = (analysis.get("video_title") or "").strip()
                folder_name = _safe_folder_name(video_title, vid)
                lang_code = get_lang_code(lang)
                filename = _output_filename(lang_code, vid, preset.name, video_title)
                rel_path = folder_name + "/" + filename
                full_path = os.path.join(output_dir, rel_path)
                if skip_existing and os.path.exists(full_path):
                    if log_fn:
                        log_fn(f"⏭ Уже есть: {rel_path}")
                    if out_files is not None and rel_path not in out_files:
                        out_files.append(rel_path)
                    results.append(CoverResult(success=True))
                    continue
                r = _generate_for_language(
                    thumbnail, analysis, style_from_thumbnail, preset_lang,
                    gemini_api_key, openai_api_key, fast_gen_api_key, log_fn,
                    cancel_check=cancel_check,
                    mode="url",
                    result_path=full_path,      # 03.09.2026: журнал платных вызовов
                )
                if r.success and r.image:
                    _save_result(r, url, lang, analysis, i)
                else:
                    if log_fn and (r.error or not r.success):
                        err = r.error or "не удалось сгенерировать/сохранить"
                        log_fn(f"❌ [{lang}]: {err}")
                results.append(r)
                if job_idx < total_jobs:
                    time.sleep(0.5)
        return results

    # ═══════════ ПАРАЛЛЕЛЬНЫЙ РЕЖИМ — ОДИН ГЛОБАЛЬНЫЙ ПУЛ ═══════════
    if log_fn:
        log_fn(f"\n{'='*50}")
        log_fn(f"🚀 Параллельный режим: {max_threads} потоков, {len(urls)} URL × {len(lang_list)} языков = {total_jobs} задач")
        log_fn(f"{'='*50}")

    # Захватываем промпты текущего (worker) потока, чтобы передать в sub-потоки пула
    _current_prompts = get_prompts_config()

    def _thread_init():
        """Инициализатор для sub-потоков ThreadPoolExecutor — копирует промпты в thread-local."""
        set_prompts_config(_current_prompts)

    # Этап 1: параллельно скачать+проанализировать все URL
    url_data = {}     # i -> (thumbnail, analysis, style)
    url_errors = {}   # i -> error message

    def _analyze_one(i_url):
        i, url = i_url
        if cancel_check and cancel_check():
            return i, None, None, None, "Отменено пользователем"
        if log_fn:
            log_fn(f"📌 [{i+1}/{len(urls)}] Анализ: {url}")
        thumb, anal, style = _download_and_analyze_url(
            url, preset, gemini_api_key, openai_api_key, log_fn
        )
        if thumb is None:
            return i, None, None, None, "Не удалось скачать превью"
        if anal is None:
            return i, None, None, None, "Не удалось проанализировать обложку"
        return i, thumb, anal, style, None

    analyze_workers = min(max_threads, len(urls)) if urls else 1
    with ThreadPoolExecutor(max_workers=analyze_workers, initializer=_thread_init) as analyze_pool:
        analyze_futures = {analyze_pool.submit(_analyze_one, (i, u)): i for i, u in enumerate(urls)}
        for fut in as_completed(analyze_futures):
            if cancel_check and cancel_check():
                for f2 in analyze_futures:
                    if not f2.done():
                        f2.cancel()
                break
            try:
                i, thumb, anal, style, err = fut.result()
                if err:
                    url_errors[i] = err
                else:
                    url_data[i] = (thumb, anal, style)
            except Exception as e:
                idx = analyze_futures[fut]
                url_errors[idx] = str(e)

    # Этап 2: собрать все (i, j) задачи, вычислить skip_existing, записать ошибки URL
    all_tasks = []
    results_by_key = {}
    skip_set = set()

    for i, url in enumerate(urls):
        if i in url_errors:
            err = url_errors[i]
            for j, lang in enumerate(lang_list):
                results_by_key[(i, j)] = (CoverResult(error=err), url, lang)
            continue
        if i not in url_data:
            for j, lang in enumerate(lang_list):
                results_by_key[(i, j)] = (CoverResult(error="Не обработано"), url, lang)
            continue
        thumb, anal, style = url_data[i]
        for j, lang in enumerate(lang_list):
            preset_lang = Preset(**asdict(preset))
            preset_lang.language = lang
            if skip_existing:
                vid = extract_video_id(url) or f"cover_{i+1}"
                video_title = (anal.get("video_title") or "").strip()
                folder_name = _safe_folder_name(video_title, vid)
                lang_code = get_lang_code(lang)
                filename = _output_filename(lang_code, vid, preset.name, video_title)
                rel_path = folder_name + "/" + filename
                full_path = os.path.join(output_dir, rel_path)
                if os.path.exists(full_path):
                    skip_set.add((i, j))
                    if out_files is not None and rel_path not in out_files:
                        out_files.append(rel_path)
                    if log_fn:
                        log_fn(f"⏭ [{i*len(lang_list)+j+1}/{total_jobs}] Уже есть: {rel_path}")
                    continue
            all_tasks.append((i, j, url, lang, preset_lang, thumb, anal, style))

    if log_fn and all_tasks:
        log_fn(f"▶ К генерации: {len(all_tasks)} задач в пуле из {max_threads} потоков")

    # Этап 3: один пул для всех (URL × lang)
    def _do_task(args):
        ii, jj, u, lng, p, thumb, anal, style_anal = args
        num = ii * len(lang_list) + jj + 1
        def task_log(msg):
            if log_fn:
                log_fn(f"[{num}/{total_jobs}] {msg}")
        if cancel_check and cancel_check():
            return (ii, jj, CoverResult(error="Отменено пользователем"), u, lng)
        res = _generate_for_language(
            thumb, anal, style_anal, p,
            gemini_api_key, openai_api_key, fast_gen_api_key, task_log,
            cancel_check=cancel_check,
            mode="url",
        )
        return (ii, jj, res, u, lng)

    cancelled_flag = False
    if all_tasks:
        if log_fn:
            n_workers = max(1, min(int(max_threads or 1), len(all_tasks)))
            log_fn(f"[parallel] {n_workers} workers for {len(all_tasks)} tasks (max_threads={max_threads})")
        _prog_total = len(all_tasks)
        _prog_done = 0
        with ThreadPoolExecutor(max_workers=max_threads, initializer=_thread_init) as executor:
            futures = {executor.submit(_do_task, t): t for t in all_tasks}
            for future in as_completed(futures):
                if cancel_check and cancel_check() and not cancelled_flag:
                    cancelled_flag = True
                    if log_fn:
                        log_fn("⏹ Задание отменено. Отменяю незапущенные генерации...")
                    for fut in futures:
                        if not fut.done():
                            fut.cancel()
                try:
                    ii, jj, r, u, lng = future.result()
                    results_by_key[(ii, jj)] = (r, u, lng)
                    t = futures[future]
                    anal = t[6]
                    if r.success and r.image:
                        _save_result(r, u, lng, anal, ii)
                except Exception as e:
                    t = futures[future]
                    results_by_key[(t[0], t[1])] = (CoverResult(error=str(e)), t[2], t[3])
                _prog_done += 1
                if progress_cb:
                    try:
                        progress_cb(_prog_done, _prog_total)
                    except Exception:
                        pass

    # Этап 4: собрать results в порядке (URL × lang)
    for i, url in enumerate(urls):
        for j, lang in enumerate(lang_list):
            if (i, j) in skip_set:
                results.append(CoverResult(success=True))
                continue
            entry = results_by_key.get((i, j))
            if entry:
                r, _, _ = entry
                if not (r.success and r.image) and log_fn and (r.error or not r.success):
                    err = r.error or "не удалось сгенерировать/сохранить"
                    log_fn(f"❌ [{lang}]: {err}")
                results.append(r)
            else:
                results.append(CoverResult(error="Не обработано"))

    return results


# ════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Cover Generator — генерация обложек YouTube")
    parser.add_argument("urls", nargs="*", help="YouTube ссылки")
    parser.add_argument("--preset", default="", help="Имя пресета")
    parser.add_argument("--lang", default="Русский", help="Язык текста")
    parser.add_argument("--gemini-key", default="", help="DEPRECATED (v1.0.3) — игнорируется, Gemini SDK удалён")
    parser.add_argument("--openai-key", default=os.environ.get("OPENAI_API_KEY", ""), help="OpenAI API key")
    parser.add_argument("--fast-gen-key", default=os.environ.get("FAST_GEN_API_KEY", ""), help="Fast Gen API key (обязателен для генерации)")
    parser.add_argument("--output", default=OUTPUT_DIR, help="Папка вывода")
    parser.add_argument("--list-presets", action="store_true", help="Показать пресеты")
    args = parser.parse_args()

    pm = PresetManager()

    if args.list_presets:
        names = pm.list_names()
        if names:
            print("Пресеты:", ", ".join(names))
        else:
            print("Нет сохранённых пресетов")
        exit(0)

    if not args.urls:
        parser.print_help()
        exit(1)

    preset = pm.get(args.preset) if args.preset else Preset(name="cli", language=args.lang)

    if args.gemini_key:
        import sys
        print("[DEPRECATED] --gemini-key ignored since v1.0.3 (Gemini SDK removed)", file=sys.stderr)

    results = process_batch(
        args.urls, preset,
        "", args.openai_key,   # gemini_key слот всегда пустой
        args.fast_gen_key,
        args.output, log_fn=print
    )

    ok = sum(1 for r in results if r.success)
    print(f"\n✅ Готово: {ok}/{len(results)} обложек")
