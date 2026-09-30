# -*- coding: utf-8 -*-
"""
Rewrite Core - бизнес-логика генерации и рерайта.
Без tkinter. Используется GUI и сервером.
"""

import os
import sys
import json
import re
import math
import threading
import queue
import time
import subprocess
import hashlib
import sqlite3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, List, Dict, Tuple, Optional, Set, Union
from dataclasses import dataclass, asdict
from pydantic import BaseModel, Field
from enum import Enum
import random
import unicodedata
import urllib.request
import urllib.error

try:
    import anthropic
    import httpx
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
try:
    import anyio
    from claude_code_sdk import query, ClaudeCodeOptions
    CLAUDE_CODE_SDK_AVAILABLE = True
except ImportError:
    CLAUDE_CODE_SDK_AVAILABLE = False
try:
    import docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False
try:
    import pypdf
    PYPDF_AVAILABLE = True
except ImportError:
    try:
        import PyPDF2 as pypdf
        PYPDF_AVAILABLE = True
    except ImportError:
        PYPDF_AVAILABLE = False
try:
    from odf import text, teletype
    from odf.opendocument import load as load_odt
    ODT_AVAILABLE = True
except ImportError:
    ODT_AVAILABLE = False
try:
    from striprtf.striprtf import rtf_to_text
    RTF_AVAILABLE = True
except ImportError:
    RTF_AVAILABLE = False

# ============== НАСТРОЙКИ ==============

# API Keys
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Модели
API_PROVIDER = "claude"  # "openai" или "claude"
DEFAULT_MODEL = "claude-sonnet-4-20250514"  # Sonnet 4 (Sonnet 4.5 может быть недоступен)
# Альтернативы:
# "claude-sonnet-4-20250514" - Sonnet 4
# "claude-3-5-haiku-20241022" - Haiku 3.5 (дешевле)
# "gpt-4o-mini" - для OpenAI
PROGRESS_FILE = os.path.join(os.path.expanduser("~"), "rewrite_progress_v2.json")
LAST_LANGUAGES_FILE = os.path.join(os.path.expanduser("~"), ".rewrite_last_languages.json")
LAST_COOKIES_FILE = os.path.join(os.path.expanduser("~"), ".rewrite_last_cookies.json")
LAST_OAUTH_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".rewrite_oauth_token.json")
KEYS_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".rewrite_keys_config.json")  # legacy / read-only fallback после миграции на user_keys

# Per-user keys storage (миграция user_keys_001, 2026-05-24).
# ADMIN_USER_ID должен совпадать с server.py / api.py / index.html / migrations/user_keys_001.py.
ADMIN_USER_ID = "292d249f2de0"
ADMIN_USER_IDS = {ADMIN_USER_ID}  # 2026-06-04: гейт ENV-ключа сервера

def env_openai_key_for(user_id: str) -> str:
    """ENV-ключ OpenAI сервера доступен ТОЛЬКО админу. Остальным — пусто (платят своим).

    Используется как ПОСЛЕДНИЙ fallback в путях, где user_id известен.
    На create-этапе (api.py / server.py) для не-админа без своего ключа должен
    срабатывать 422 ДО постановки в очередь — этот гейт защита-в-глубину."""
    return OPENAI_API_KEY if user_id in ADMIN_USER_IDS else ""
SESSIONS_DB_PATH = os.environ.get("SESSION_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.db"))
MODEL_CAPABILITIES_FILE = os.path.join(os.path.expanduser("~"), ".rewrite_model_capabilities.json")
PRESETS_FILE = os.path.join(os.path.expanduser("~"), ".rewrite_presets.json")
OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "rewrite_results")
DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "video_downloads")
# Генерация текстов с нуля
TEXT_GENERATION_PRESETS_FILE = os.path.join(os.path.expanduser("~"), "text_generation_presets.json")
JOB_QUEUE_DB = os.path.join(os.path.expanduser("~"), "job_queue.db")
QUEUE_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".rewrite_queue_config.json")
GEN_SETTINGS_PRESETS_FILE = os.path.join(os.path.expanduser("~"), ".generation_settings_presets.json")
GEN_OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "generated_texts")

def _detect_provider(model: str, use_subscription: bool = False) -> str:
    """
    Определяет провайдера API по имени модели.
    Возвращает: "subscription", "claude" или "openai".
    Правило: если модель содержит 'claude' — это Claude/subscription, всё остальное — OpenAI.
    """
    if "claude" in model.lower():
        return "subscription" if use_subscription else "claude"
    return "openai"


SAFE_TEXT_SUPPLEMENT = """
### РЕЖИМ БЕЗОПАСНОГО ТЕКСТА (YouTube-safe) ###

Цель: текст должен быть таким же захватывающим и интригующим, но НЕ должен выглядеть как попытка выдать вымысел за реальные документальные факты. YouTube демонетизирует каналы за "недостоверный контент", когда ИИ-текст подаётся как реальная документалистика.

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:

1. НАРРАТИВНАЯ ДИСТАНЦИЯ — подавай историю как пересказ/легенду/реконструкцию, а НЕ как документальный факт:
   - НЕЛЬЗЯ: "В 1982 году генерал Петров приказал расстрелять деревню. Это подтверждают рассекреченные документы."
   - МОЖНО: "Среди жителей тех мест до сих пор передают мрачную историю об одном офицере, который, как рассказывают старожилы, однажды отдал страшный приказ..."
   - Используй фразы: "рассказывают, что...", "по одной из версий...", "легенда гласит...", "существует история о том, как...", "говорят, что в те годы..."
   - НО! Не переусердствуй — достаточно 2-3 таких маркеров на каждые 1000-1500 слов. Текст не должен звучать неуверенно. Он должен звучать как РАССКАЗЧИК У КОСТРА — уверенный, но рассказывающий историю, а не зачитывающий протокол.

2. АНОНИМИЗАЦИЯ — НЕ придумывай конкретные ФИО + звания + даты + номера документов для вымышленных персонажей:
   - НЕЛЬЗЯ: "Майор Сергей Викторович Козлов, дело №4502-Б от 14 марта 1978 года, 3-й отдел КГБ г. Новосибирск"
   - МОЖНО: "Один офицер, чьё имя знали немногие", "человек, которого в тех кругах звали просто Капитан", "в документах он значился под оперативным псевдонимом"
   - Для РЕАЛЬНЫХ общеизвестных исторических личностей (Сталин, Наполеон, Генрих VIII, Елизавета II) — имена можно и нужно использовать, это повышает доверие
   - Для ВЫМЫШЛЕННЫХ персонажей — используй прозвища, архетипы, описательные конструкции вместо фейковых паспортных данных

3. ЗАПРЕЩЁННЫЕ МАРКЕРЫ "ФЕЙКОВОЙ ДОКУМЕНТАЛИСТИКИ":
   - Убери из текста: "рассекреченные документы", "секретный архив", "скрывали от народа", "вся правда о...", "учёные в шоке", "сенсационная находка доказывает"
   - Замени на: "забытая история", "малоизвестный эпизод", "загадка прошлого", "одна из тайн той эпохи"

4. АВТОРСКИЙ СКЕПСИС — добавляй элементы сомнения/размышления рассказчика:
   - "Звучит невероятно? Возможно. Но те, кто был рядом, рассказывают именно так."
   - "Правда это или городская легенда — каждый решает сам."
   - "В этой истории много неясного, и, может быть, именно поэтому она так завораживает."
   - Это НЕ ослабляет текст, а УСИЛИВАЕТ — зритель чувствует честность рассказчика.

5. ФИНАЛ — завершай вопросом к зрителю или моральной рефлексией, а НЕ "разоблачением":
   - НЕЛЬЗЯ: "Теперь вы знаете правду, которую скрывали 50 лет!"
   - МОЖНО: "Что бы ты сделал на его месте? Напиши в комментариях."
   - МОЖНО: "Может быть, эта история — всего лишь легенда. А может, нет. Но она заставляет задуматься."

ЧТО НЕ МЕНЯТЬ:
- Стиль повествования (рубленый, кинематографичный, атмосферный) — сохрани как есть
- Интригу и хуки — они должны остаться сильными
- Брендинг (имя ведущего, название канала) — без изменений
- Длину текста — не сокращай
- Сенсорику (запахи, звуки, ощущения) — это усиливает текст
- Диалоги и прямую речь — это делает текст живым

ВАЖНО: Этот режим — НЕ цензура. Текст должен оставаться таким же захватывающим, мрачным, интригующим. Меняется только ПОДАЧА: из "я зачитываю секретный документ" в "я рассказываю невероятную историю".
"""


def get_queue_config() -> Dict:
    """Загружает настройки очереди и параллельности."""
    default = {"max_concurrent_jobs": 5, "max_analysis_parallelism": 0, "max_parallel": 75}
    try:
        if os.path.exists(QUEUE_CONFIG_FILE):
            with open(QUEUE_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default["max_concurrent_jobs"] = max(1, min(200, int(data.get("max_concurrent_jobs", 5))))
                default["max_analysis_parallelism"] = max(0, min(500, int(data.get("max_analysis_parallelism", 0))))
                default["max_parallel"] = max(1, min(200, int(data.get("max_parallel", 75))))
    except Exception:
        pass
    return default


def save_queue_config(max_concurrent_jobs: int = None, max_analysis_parallelism: int = None, max_parallel: int = None):
    """Сохраняет настройки очереди. None — не менять."""
    try:
        data = get_queue_config()
        if max_concurrent_jobs is not None:
            data["max_concurrent_jobs"] = max(1, min(200, max_concurrent_jobs))
        if max_analysis_parallelism is not None:
            data["max_analysis_parallelism"] = max(0, min(500, max_analysis_parallelism))
        if max_parallel is not None:
            data["max_parallel"] = max(1, min(200, max_parallel))
        with open(QUEUE_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_effective_max_workers(requested: int) -> int:
    """Возвращает фактический лимит потоков (cap по max_parallel, >100 вызывает падения)."""
    cfg = get_queue_config()
    limit = int(cfg.get("max_parallel", 75))
    return min(requested, limit)


_user_id_ctx = threading.local()


def set_current_user_id(user_id: str) -> None:
    """Worker устанавливает user_id в thread-local context перед началом job/chunk.
    Нужно, чтобы get_keys_config() без явного user_id (в утилитах глубоко в стеке)
    читал ключи правильного партнёра, а не админа."""
    _user_id_ctx.user_id = user_id or ""


def clear_current_user_id() -> None:
    """Сбрасывает thread-local user_id (вызывать в finally после job/chunk)."""
    try:
        del _user_id_ctx.user_id
    except AttributeError:
        pass


def get_current_user_id() -> str:
    """Возвращает thread-local user_id или '' если не установлен."""
    return getattr(_user_id_ctx, "user_id", "") or ""


def _resolve_user_id(user_id: str) -> str:
    """Пустой / 'default' → thread-local ctx → ADMIN_USER_ID.

    Резолв-цепочка: явный аргумент > thread-local (установленный worker'ом) > admin.
    Это нужно для утилит вроде get_postprocess_openai_key() / debug_log() — у них
    нет signature для user_id, но они должны видеть ключи текущего партнёра."""
    if user_id and user_id != "default":
        return user_id
    ctx = get_current_user_id()
    if ctx and ctx != "default":
        return ctx
    return ADMIN_USER_ID


def get_keys_config(user_id: str = "") -> Dict:
    """Загружает настройки ключей для user_id из sessions.db.user_keys.

    Пустой / 'default' user_id → ADMIN_USER_ID. Если row нет — возвращает дефолты
    из ENV-констант (без INSERT — это сделает либо миграция, либо save_keys_config).
    """
    uid = _resolve_user_id(user_id)
    default = {
        "anthropic_api_key": ANTHROPIC_API_KEY,
        # 2026-06-04: ENV OPENAI_API_KEY возвращается ТОЛЬКО админу. Для не-админа default="".
        "openai_api_key": env_openai_key_for(uid),
        "oauth_tokens": [],
        "gpt_service_tier": "default",
        "gpt_use_cache": True,
        "postprocess_check_model": "gpt-5-mini",
        "custom_models": [],
        "debug_mode": False,
    }
    try:
        with sqlite3.connect(SESSIONS_DB_PATH, timeout=5.0) as conn:
            row = conn.execute(
                """
                SELECT anthropic_api_key, openai_api_key, oauth_tokens,
                       gpt_service_tier, gpt_use_cache, postprocess_check_model,
                       custom_models, debug_mode
                  FROM user_keys WHERE user_id = ?
                """,
                (uid,),
            ).fetchone()
        if row:
            anthr, openai_k, oauth_json, tier, use_cache, ppmodel, custom_json, dbg = row
            try:
                oauth = json.loads(oauth_json) if oauth_json else []
            except Exception:
                oauth = []
            if isinstance(oauth, str):
                oauth = [oauth] if oauth else []
            try:
                custom = json.loads(custom_json) if custom_json else []
            except Exception:
                custom = []
            if isinstance(custom, str):
                custom = [custom] if custom else []
            default["anthropic_api_key"] = anthr or default["anthropic_api_key"]
            default["openai_api_key"] = openai_k or default["openai_api_key"]
            default["oauth_tokens"] = oauth
            default["gpt_service_tier"] = tier or "default"
            default["gpt_use_cache"] = bool(use_cache)
            default["postprocess_check_model"] = ppmodel or "gpt-5-mini"
            default["custom_models"] = custom
            default["debug_mode"] = bool(dbg)
    except Exception:
        pass
    return default


def _load_model_capabilities() -> Dict[str, Dict[str, bool]]:
    """Загружает кэш неподдерживаемых возможностей моделей (flex, cache, temperature)."""
    try:
        path = os.path.expanduser(MODEL_CAPABILITIES_FILE)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_model_capability(model: str, key: str, value: bool) -> None:
    """Сохраняет в кэш: модель не поддерживает возможность (чтобы не повторять ошибку 100 раз)."""
    try:
        path = os.path.expanduser(MODEL_CAPABILITIES_FILE)
        caps = _load_model_capabilities()
        if model not in caps:
            caps[model] = {}
        caps[model][key] = value
        with open(path, "w", encoding="utf-8") as f:
            json.dump(caps, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def save_keys_config(
    anthropic_api_key: str,
    openai_api_key: str,
    oauth_tokens: List[str],
    gpt_service_tier: Optional[str] = None,
    gpt_use_cache: Optional[bool] = None,
    postprocess_check_model: Optional[str] = None,
    custom_models: Optional[List[str]] = None,
    debug_mode: Optional[bool] = None,
    user_id: str = "",
) -> None:
    """Сохраняет настройки ключей в sessions.db.user_keys для user_id (UPSERT).
    Пустой / 'default' user_id → ADMIN_USER_ID."""
    uid = _resolve_user_id(user_id)
    data = get_keys_config(uid)
    data["anthropic_api_key"] = anthropic_api_key
    data["openai_api_key"] = openai_api_key
    data["oauth_tokens"] = [t.strip() for t in oauth_tokens if t and t.strip()]
    if gpt_service_tier is not None:
        data["gpt_service_tier"] = gpt_service_tier
    if gpt_use_cache is not None:
        data["gpt_use_cache"] = gpt_use_cache
    if postprocess_check_model is not None:
        data["postprocess_check_model"] = postprocess_check_model.strip() or "gpt-5-mini"
    if custom_models is not None:
        data["custom_models"] = [m.strip() for m in custom_models if m and m.strip()]
    if debug_mode is not None:
        data["debug_mode"] = debug_mode
    try:
        with sqlite3.connect(SESSIONS_DB_PATH, timeout=5.0) as conn:
            conn.execute(
                """
                INSERT INTO user_keys
                  (user_id, anthropic_api_key, openai_api_key, oauth_tokens,
                   gpt_service_tier, gpt_use_cache, postprocess_check_model,
                   custom_models, debug_mode, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  anthropic_api_key = excluded.anthropic_api_key,
                  openai_api_key = excluded.openai_api_key,
                  oauth_tokens = excluded.oauth_tokens,
                  gpt_service_tier = excluded.gpt_service_tier,
                  gpt_use_cache = excluded.gpt_use_cache,
                  postprocess_check_model = excluded.postprocess_check_model,
                  custom_models = excluded.custom_models,
                  debug_mode = excluded.debug_mode,
                  updated_at = excluded.updated_at
                """,
                (
                    uid,
                    data["anthropic_api_key"],
                    data["openai_api_key"],
                    json.dumps(data["oauth_tokens"], ensure_ascii=False),
                    data["gpt_service_tier"],
                    1 if data["gpt_use_cache"] else 0,
                    data["postprocess_check_model"],
                    json.dumps(data["custom_models"], ensure_ascii=False),
                    1 if data["debug_mode"] else 0,
                    time.time(),
                ),
            )
    except Exception as e:
        print(f"[save_keys_config] failed for user_id={uid}: {e}")


# Максимум потоков на одну подписку (OAuth)
MAX_THREADS_PER_SUBSCRIPTION = 3

# Консервативные настройки для надёжности
DEFAULT_THREADS = 5  # Не 30!
DEFAULT_CHUNK_SIZE = 8000
MIN_THREADS = 1
MAX_THREADS = 10

# Повторная проверка перевода через GPT-4o-mini: ищет неестественные формулировки и правит под носителя
ENABLE_NATIVE_SPEAKER_CHECK = True
# Порог по длине: исправленный чанк не должен быть короче оригинала более чем на (1 - MIN_FIXED_VS_ORIGINAL_RATIO)
MIN_FIXED_VS_ORIGINAL_RATIO = 0.85  # если исправленный < 85% длины оригинала — правка отклоняется
# Минимальное отношение числа предложений (опционально): не принимать, если предложений сильно меньше
MIN_FIXED_SENTENCES_VS_ORIGINAL_RATIO = 0.80

CYRILLIC_LANGUAGES = {"Russian", "Ukrainian", "Bulgarian", "Belarusian", "Macedonian"}
# Сербский — на латинице (gajica) по нашему выбору, считается Latin для всех целей

# Per-model chunk-size defaults (max words per single LLM call).
# Берётся ~70% от заявленного max_output_tokens модели, чтобы оставить запас
# на JSON wrapper + preview/title/log_update и защититься от quality-degradation
# на максимальной длине output. 1 English word ≈ 1.3 tokens, 1 Russian ≈ 2.5.
# Источник output_tokens limits: Anthropic models/overview docs, 2026-05-23.
#   claude-opus-4-7   = 128k tokens (новый Opus)
#   claude-opus-4-6   = 128k tokens (legacy)
#   claude-sonnet-4-6 = 64k tokens
#   claude-haiku-4-5  = 64k tokens
# GPT — conservative (16k typical output ceiling в Chat Completions).
MODEL_CHUNK_DEFAULTS = {
    # Claude
    # 2026-06-18: РЕАЛИСТИЧНЫЙ размер одного ответа, а не ~70% теоретического
    # max_output. Модель за один ход надёжно пишет ~6-7к слов и "закругляется",
    # поэтому прежние 14-25к/чанк давали недобор (26к target → 2 чанка × ~8.5к = 17к).
    # Теперь чанк ≤ достижимого → words_per_chunk попадает в цель. Бонус: ещё ниже
    # риск stream-stall (#25979). Касается ТОЛЬКО генерации (не рерайта).
    "claude-opus-4-7":    {"latin": 7000, "cyrillic": 5000},
    "claude-opus-4-8":    {"latin": 7000, "cyrillic": 5000},
    "claude-fable-5":     {"latin": 7000, "cyrillic": 5000},
    "claude-opus-4-6":    {"latin": 7000, "cyrillic": 5000},
    "claude-sonnet-4-6":  {"latin": 6500, "cyrillic": 4500},
    "claude-haiku-4-5":   {"latin": 5000, "cyrillic": 3500},
    # GPT
    "gpt-4.1-nano": {"latin": 8000,  "cyrillic": 4500},
    "gpt-4.1-mini": {"latin": 10000, "cyrillic": 5500},
    "gpt-4o":       {"latin": 8000,  "cyrillic": 4500},
    "gpt-4o-mini":  {"latin": 6000,  "cyrillic": 3500},
}
# Fallback для неизвестных моделей (custom IDs / новые / старые pinned snapshots)
DEFAULT_CHUNK_WORDS_FALLBACK = {"latin": 5000, "cyrillic": 3500}

# DEPRECATED: legacy плоская таблица. Оставлено для обратной совместимости
# с любыми устаревшими вызовами. Реальный код использует get_chunk_size_words.
GENERATION_CHUNK_WORDS = DEFAULT_CHUNK_WORDS_FALLBACK


def get_chunk_size_words(model: str, lang_name: str) -> int:
    """Возвращает max words per chunk для (model, language).

    Lookup case-insensitive по канонической alias-форме (`claude-opus-4-7` etc).
    Для неизвестных моделей — fallback (5000 latin / 3500 cyrillic).
    """
    script = "cyrillic" if lang_name in CYRILLIC_LANGUAGES else "latin"
    # FIX-2026-06-29: env-override GEN_CHUNK_WORDS — меньше слов/чанк → больше чанков → ПОЛНАЯ длина.
    # Большой чанк → модель «закругляется» и недобирает (CZ-6: 5408/6200 на 1 чанке 7000). Tunable без редеплоя.
    # Раздельно latin/cyrillic: GEN_CHUNK_WORDS (latin) и GEN_CHUNK_WORDS_CYR (cyrillic), оба опциональны.
    _envk = "GEN_CHUNK_WORDS_CYR" if script == "cyrillic" else "GEN_CHUNK_WORDS"
    _env = os.environ.get(_envk)
    if _env:
        try:
            return max(500, int(_env))
        except Exception:
            pass
    model_key = (model or "").strip().lower()
    if model_key in MODEL_CHUNK_DEFAULTS:
        return MODEL_CHUNK_DEFAULTS[model_key][script]
    return DEFAULT_CHUNK_WORDS_FALLBACK[script]


def calculate_generation_chunks(
    total_words: int,
    lang_name: str,
    model: str = "",
    chunk_size_override: Optional[int] = None,
) -> Tuple[int, int]:
    """Рассчитывает количество chunks и слов на chunk для генерации.

    Args:
        total_words: целевая длина всего script.
        lang_name: canonical language name (English/Russian/...).
        model: model alias для per-model default. Если пусто — fallback.
        chunk_size_override: пользовательский override (UI поле). Если задан
            и >= 500 — используется вместо per-model default.

    Returns: (num_chunks, words_per_chunk).
    """
    if chunk_size_override and chunk_size_override >= 500:
        max_words = chunk_size_override
    else:
        max_words = get_chunk_size_words(model, lang_name)
    num_chunks = max(1, math.ceil(total_words / max_words))
    words_per_chunk = total_words // num_chunks
    # FIX-2026-06-29: GEN_LENGTH_FACTOR — модель отдаёт стабильно ~82% запрошенного (замерено на 14 джобах,
    # независимо от chunk_size). Просим в factor раз больше → отдаёт ~100%, БЕЗ повторных запросов (recovery).
    # Калибровка, не догадка: 1/0.82 ≈ 1.22. Env-tunable; 1.0 = выключено.
    _factor = float(os.environ.get("GEN_LENGTH_FACTOR", "1.0"))
    if _factor and _factor != 1.0:
        words_per_chunk = int(words_per_chunk * _factor)
    return num_chunks, words_per_chunk


# ============== Generation v2 helpers (Round 2 of refactor, 2026-05-23) ==============
# chunk-loop с JSON-output + rotation log. Постпроцесс (Pass A/B/C/D) применяется
# отдельной стадией ПОСЛЕ chunk-loop через существующий run_postprocess_pipeline().

ROTATION_LOG_DIR_BASENAME = "rotation_logs"
_GEN_V2_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)

# === Preset content split (variant B, 2026-05-23) ===
# Большие preset.content (~30K chars) при N chunk-вызовах подряд упираются
# в Anthropic rate-limit на крупных prompts. Решение: split на core directives
# (всегда передаём в каждый chunk) + reference data (только chunk 0).
REFERENCE_MARKER_RE = re.compile(r"\n[ \t]*---\s*REFERENCE\s*---[ \t]*\n", re.IGNORECASE)
LARGE_CONTENT_THRESHOLD = 15000
CORE_TRUNCATE_TO = 8000


def split_preset_content(content: str) -> Tuple[str, Optional[str]]:
    """Returns (core_for_all_chunks, reference_for_chunk0_only_or_None).

    Стратегии в порядке приоритета:
    (a) Explicit marker `---REFERENCE---` (юзер сам выбирает где split)
    (b) Length-based fallback при len(content) > LARGE_CONTENT_THRESHOLD
    (c) Иначе — возвращаем (content, None), split не нужен
    """
    if not content:
        return "", None
    m = REFERENCE_MARKER_RE.search(content)
    if m:
        return content[: m.start()].rstrip(), content[m.end():].lstrip()
    if len(content) > LARGE_CONTENT_THRESHOLD:
        return (
            content[:CORE_TRUNCATE_TO].rstrip() + "\n[... reference data only on first chunk ...]",
            content[CORE_TRUNCATE_TO:].strip(),
        )
    return content, None


def _chars_per_word(language: str) -> float:
    """PATCH-2026-06-27-chars: средняя длина слова в символах (для перевода слов→символы в промпте)."""
    l = (language or "").lower()
    if "german" in l:
        return 7.0
    if any(x in l for x in ("english", "spanish", "italian", "french", "portuguese", "dutch", "catalan")):
        return 6.0
    return 6.5  # славянские/кириллица по умолчанию (Polish, Czech, Russian, Ukrainian, ...)


def _char_length_line(target_words: int, language: str) -> str:
    """PATCH-2026-06-28-words: инструкция длины В СЛОВАХ («Слов», НЕ «слов буквенных»/символах) + само-перепроверка ПЕРЕД ответом."""
    lo = int(target_words * 0.95)
    hi = int(target_words * 1.15)
    return (
        f"LENGTH — measured in WORDS (слова): the 'script' MUST contain between {lo} and {hi} words; aim for ~{target_words}. "
        f"Count words the STANDARD way — tokens separated by spaces, exactly what a word counter reports as «Слов». "
        f"Do NOT count «слов буквенных» / letter-fragments and do NOT count characters: those give a BIGGER number and "
        f"would trick you into stopping too early, leaving the script too short. "
        f"SELF-CHECK BEFORE YOU ANSWER — mandatory: when you think the part is finished, re-read your 'script' and count its "
        f"WORDS («Слов»). If it is under {target_words}, you are NOT finished — keep developing the SAME material (more depth, a "
        f"concrete scene, fact, name or turn that genuinely belongs here) until you reach at least {target_words} words. "
        f"Never pad with filler, repetition, or meta-comments. Output the JSON only AFTER the word count («Слов») is in range."
    )


def build_funnel_prompt(
    preset_content: str,
    topic: str,
    language: str,
    target_words: int,
    num_chunks: int,
    words_per_chunk: int,
    host_name: str = "",
    channel_name: str = "",
    retry_hint: str = "",   # PATCH-2026-06-27-thinking: жёсткий хинт на ретрае после не-JSON ответа
) -> List[Dict[str, str]]:
    """PATCH-2026-06-27-funnelstep: отдельный шаг — Claude проектирует воронку ПЕРЕД генерацией.
    Возвращает messages для одного вызова, который выдаёт {preview, title, hook, plan}, где
    plan — массив из num_chunks строк, каждая = аутлайн части с БЮДЖЕТОМ слов на каждый бит
    (сумма ≈ words_per_chunk). План = бюджет длины, чтобы текст шёл нужной длины за один проход."""
    parts_n = max(1, int(num_chunks or 1))
    per = int(words_per_chunk) if words_per_chunk else (int(target_words) // parts_n if parts_n else int(target_words))
    # PATCH-2026-06-28-words: бюджет плана в СЛОВАХ («Слов», НЕ символах/буквенных — иначе текст выходит короче).
    system = (
        "You are a senior YouTube producer and long-form story architect. You design the engagement "
        "FUNNEL (preview -> title -> hook -> a length-budgeted PLAN) BEFORE any script is written. "
        "Output ONLY a single valid JSON object — first character '{', last '}'. No prose, no markdown fences."
    )
    chan_line = f"Channel name: {channel_name}.\n" if channel_name else ""
    host_line = f"Host / narrator: {host_name}.\n" if host_name else ""
    user = (
        f"CHANNEL PRESET (obey its style, audience, voice):\n{preset_content}\n\n"
        f"{chan_line}{host_line}"
        f"FINAL VIDEO LANGUAGE: {language}.\n"
        f"VIDEO IDEA / TOPIC:\n{topic}\n\n"
        f"Design the engagement FUNNEL for a narration video of about {target_words} words total "
        f"(count «Слов» — tokens separated by spaces, NOT «слов буквенных»/letter-fragments), split into "
        f"{parts_n} part(s) of about {per} words each.\n\n"
        "Output ONLY this JSON:\n"
        "{\n"
        '  "preview": "1-5 words for the thumbnail that grab the target audience (CAPS allowed)",\n'
        '  "title": "<=60 characters, explains the preview, concrete over clickbait, makes them click",\n'
        '  "hook": "1-2 sentences that confirm the preview+title but break off on an OPEN LOOP so the viewer cannot stop watching",\n'
        f'  "plan": ["<part 1 outline>", "... EXACTLY {parts_n} item(s) total ..."]\n'
        "}\n\n"
        "PLAN RULES — this is a LENGTH BUDGET in WORDS («Слов», NOT «слов буквенных» or characters), obey it:\n"
        f"- 'plan' MUST be a JSON array of EXACTLY {parts_n} string(s), one per part.\n"
        f"- Each part string breaks into 7-11 concrete BEATS. Write each beat as: 'concrete beat description (~N words)'. "
        f"The N values within one part MUST sum to AT LEAST {per} words (allocate generously — a too-thin plan "
        f"makes the final script too short). Give each beat enough concrete material (a real scene, fact, name, turn) to "
        f"genuinely fill its word budget.\n"
        "- The parts together form ONE complete arc: part 1 opens with the hook then a short host intro; middle parts "
        "develop with rising tension, concrete facts, names, scenes; the LAST part delivers the climax/payoff and ends "
        "with a short outro/sign-off naming the host and the channel. Put exactly ONE like+subscribe nudge in a middle part.\n"
        f"- Be SPECIFIC (real beats with real content, never 'introduction'/'conclusion'). Adapt to the {language} "
        f"audience mentality. Numbers and dates as words.\n"
        "- Inside beat descriptions do NOT use double-quote characters — write any quoted phrase with single quotes "
        "or none. Unescaped double quotes inside the JSON strings break parsing.\n"
        "\nCRITICAL — OUTPUT FORMAT (obey exactly):\n"
        "- Your ENTIRE response IS the JSON object and nothing else. The FIRST character you output is '{' and the LAST is '}'.\n"
        "- Do NOT think out loud, do NOT plan in prose, do NOT acknowledge. Write NOTHING before '{' — no 'I'll design...', "
        "no 'Let me...', no 'Here is...', no commentary. Emit the JSON directly.\n"
        "- Do NOT wrap in markdown fences. Do NOT add any text after '}'.\n"
    )
    if retry_hint:
        user = retry_hint.strip() + "\n\n" + user
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _repair_funnel_json(s: str) -> Optional[Dict[str, Any]]:
    """PATCH-2026-06-27-funnelrobust: чинит ОБОРВАННЫЙ JSON воронки — балансирует строки/скобки
    от первой '{' и дозакрывает. Лечит truncation (ответ обрезан на середине плана)."""
    start = s.find("{")
    if start < 0:
        return None
    frag = s[start:]
    stack = []
    in_str = False
    esc = False
    for ch in frag:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append(ch)
            elif ch == "}":
                if stack and stack[-1] == "{":
                    stack.pop()
            elif ch == "]":
                if stack and stack[-1] == "[":
                    stack.pop()
    repaired = frag
    if in_str:
        repaired += '"'  # закрыть оборванную строку
    repaired = re.sub(r",\s*$", "", repaired)  # убрать висящую запятую
    for opener in reversed(stack):
        repaired += "]" if opener == "[" else "}"
    try:
        obj = json.loads(repaired)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _salvage_funnel_fields(s: str) -> Optional[Dict[str, Any]]:
    """PATCH-2026-06-27-funnelrobust: посимвольный salvage полей воронки (когда JSON битый,
    напр. неэкранированная кавычка в польском описании). Тянет preview/title/hook + элементы plan."""
    out: Dict[str, Any] = {}
    for key in ("preview", "title", "hook"):
        m = re.search(r'"' + key + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', s)
        if m:
            try:
                out[key] = json.loads('"' + m.group(1) + '"')
            except Exception:
                out[key] = m.group(1)
    pm = re.search(r'"plan"\s*:\s*\[', s)
    plan: List[str] = []
    if pm:
        rest = s[pm.end():]
        for sm in re.finditer(r'"((?:[^"\\]|\\.)*)"', rest):
            seg = sm.group(1)
            try:
                plan.append(json.loads('"' + seg + '"'))
            except Exception:
                plan.append(seg)
            if re.match(r"\s*\]", rest[sm.end():sm.end() + 6]):
                break
    if plan:
        out["plan"] = plan
    return out if (out.get("hook") or out.get("plan") or out.get("preview")) else None


def parse_funnel_response(raw: str) -> Optional[Dict[str, Any]]:
    """PATCH-2026-06-27-funnelstep/funnelrobust: парсит {preview,title,hook,plan[]} из ответа шага воронки.
    Стратегии (как в parse_chunk_response): strict → raw_decode → truncation-repair → field-salvage."""
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    obj = None
    # 1) strict
    try:
        cand = json.loads(s)
        if isinstance(cand, dict):
            obj = cand
    except Exception:
        pass
    # 2) raw_decode от первой '{' (игнорит хвост/markdown/преамбулу)
    if obj is None:
        start = s.find("{")
        if start >= 0:
            try:
                cand, _ = json.JSONDecoder().raw_decode(s[start:])
                if isinstance(cand, dict):
                    obj = cand
            except Exception:
                pass
    # 3) truncation-repair (оборванный план)
    if obj is None:
        obj = _repair_funnel_json(s)
    # 4) field-salvage (битые кавычки)
    if obj is None:
        obj = _salvage_funnel_fields(s)
    if not isinstance(obj, dict):
        return None
    plan = obj.get("plan")
    if isinstance(plan, list):
        plan = [str(x).strip() for x in plan if x is not None and str(x).strip()]
    elif isinstance(plan, str) and plan.strip():
        plan = [plan.strip()]
    else:
        plan = []
    return {
        "preview": str(obj.get("preview", "") or "").strip(),
        "title": str(obj.get("title", "") or "").strip(),
        "hook": str(obj.get("hook", "") or "").strip(),
        "plan": plan,
    }


def build_chunk_prompt(
    preset_content: str,
    topic: str,
    chunk_idx: int,
    num_chunks: int,
    target_words_this_chunk: Optional[int],
    previous_tail: str,
    recent_log_phrases: List[str],
    language: str,
    host_name: Optional[str] = None,
    channel_name: Optional[str] = None,
    retry_hint: Optional[str] = None,
    plan_for_chunk: str = "",   # PATCH-2026-06-27-funnelstep: внешний план части (бюджет длины)
    hook: str = "",             # внешний хук из шага воронки (для первого чанка)
) -> List[Dict[str, str]]:
    """Сборка messages для одного chunk-вызова в новом generation flow.

    `retry_hint`: если задан — приписывается в конец user message ПЕРЕД повторной
    отправкой Claude. Это нужно потому что claude-code-sdk flattens messages list
    в одну строку (берёт только последний user message), поэтому простой append
    к messages теряет preset+topic контекст.
    """
    system = (
        "You are a YouTube script writer. Apply the channel preset strictly. "
        "Output ONLY a valid JSON object — no markdown wrapper, no prefix or suffix text, no comments."
    )

    # Split на core (всегда) + reference (только chunk 0).
    # Снижает prompt-size на chunks 1+ с ~30K до ~10K при больших preset'ах.
    core_content, reference_content = split_preset_content(preset_content)
    is_first_chunk = (chunk_idx == 0)

    user_parts: List[str] = [f"Channel preset (core directives):\n{core_content}\n"]
    if reference_content:
        if is_first_chunk:
            user_parts.append(
                "Channel preset (reference data — apply this style throughout the script):\n"
                f"{reference_content}\n"
            )
        else:
            user_parts.append(
                "(Reference data was provided in the first chunk — continue applying the same "
                "style and rotation_pool choices established there.)\n"
            )
    user_parts.append(f"Language: {language}")
    if host_name:
        user_parts.append(f"Host name: {host_name}")
    if channel_name:
        user_parts.append(f"Channel name: {channel_name}")
    user_parts.append(f"\nTopic / source material:\n{topic}")
    # Rotation log block убран 2026-06-03 — в conversation-mode модель сама помнит
    # ранее использованные фразы; для single-shot тоже признан мало-полезным.
    # Параметр recent_log_phrases оставлен в сигнатуре (backward compat callers),
    # но игнорируется. log_update в JSON-схеме оставлен (parse терпит пустой).
    _ = recent_log_phrases  # explicit ignore

    is_first = chunk_idx == 0
    is_last = chunk_idx == num_chunks - 1

    if num_chunks == 1:
        position_lines = ["Write the ENTIRE script as one piece."]
        if target_words_this_chunk:
            # PATCH-2026-06-27-chars: длина в СИМВОЛАХ + само-перепроверка перед ответом (один проход, без повторов).
            position_lines.append(_char_length_line(target_words_this_chunk, language))
        # PATCH-2026-06-27-funnel (item 4/5): сильный хук-обрыв + байт на подписку в середине + аутро.
        position_lines.append(
            "Open with a strong HOOK: 1-2 sentences, naturally embedded (NO separate header), that confirm "
            "the title/topic and break off on an OPEN LOOP so the viewer cannot stop watching "
            "(intrigue / mystery gap — never cheap clickbait)."
        )
        position_lines.append(
            "Around the MIDDLE of the script, naturally insert ONE short call-to-action (like + subscribe) "
            "mentioning the channel name — a single in-voice sentence, never jarring."
        )
        position_lines.append("END with a short outro / sign-off that names the host and the channel.")
        # PATCH-2026-06-27-funnel (item 3): inline-воронка — ТОЛЬКО если нет внешнего плана (funnelstep ниже).
        if not plan_for_chunk:
            position_lines.append(
                "FUNNEL — design it BEFORE writing and output it in the SAME JSON: 'preview' (1-5 words for the "
                "thumbnail), 'title' (<=60 chars, makes them click), 'hook' (your exact 1-2-sentence open-loop hook), "
                "'plan' (a short outline of the key beats/turns that keep the viewer to the very end). "
                "Then write the 'script' so it OPENS with your 'hook' and FOLLOWS your 'plan' throughout."
            )
        output_keys = '{"preview": "...", "title": "...", "hook": "...", "plan": "...", "script": "...", "log_update": [...]}'
    else:
        position_lines = [f"This is chunk {chunk_idx + 1} of {num_chunks}."]
        if target_words_this_chunk:
            # PATCH-2026-06-27-chars: длина в СИМВОЛАХ + само-перепроверка перед ответом.
            position_lines.append(_char_length_line(target_words_this_chunk, language))
        if is_first:
            # PATCH-2026-06-27-funnel (item 4): сильный хук-обрыв.
            position_lines.append(
                "OPEN with a strong HOOK: 1-2 sentences, naturally embedded (NO header), confirming the "
                "title/topic and breaking off on an OPEN LOOP so the viewer cannot stop watching "
                "(intrigue / mystery gap — never cheap clickbait)."
            )
            # item 3: inline-воронка в chunk 0 — ТОЛЬКО если нет внешнего плана (funnelstep).
            if not plan_for_chunk:
                position_lines.append(
                    "FUNNEL — design it BEFORE writing and output it in the SAME JSON: 'preview' (1-5 words), "
                    "'title' (<=60 chars), 'hook' (your exact open-loop hook above), 'plan' (key beats/turns that keep "
                    "the viewer to the very END of the WHOLE video). Open this chunk's 'script' with your 'hook' and "
                    "follow your 'plan'; later chunks will continue the SAME plan."
                )
        elif is_last:
            # PATCH-2026-06-27-chars: финальный чанк — символьный коридор + self-check, аутро только ПОСЛЕ.
            if target_words_this_chunk:
                position_lines.append(_char_length_line(target_words_this_chunk, language))
            position_lines.append(
                "This is the FINAL chunk: FIRST fully develop the remaining material and SELF-CHECK you are inside the "
                "character range above; only AFTER you have verified the count, close with a short outro/sign-off that "
                "NAMES the host and the channel. Do NOT rush, summarize, or wind down early."  # item 5: аутро
            )
        else:
            position_lines.append("CONTINUE the narrative at full length — do NOT wind down or conclude here.")
        # PATCH-2026-06-27-funnel (item 5): ровно ОДИН байт на подписку — в центральном чанке.
        if num_chunks >= 2 and chunk_idx == num_chunks // 2:
            position_lines.append(
                "In THIS chunk, naturally insert ONE short call-to-action (like + subscribe) mentioning the "
                "channel name — a single in-voice sentence, never jarring. (Only here, not in other chunks.)"
            )
        if previous_tail and not is_first:
            position_lines.append(f'Previous chunk ended with:\n"""{previous_tail}"""')
        if is_first:
            output_keys = '{"preview": "...", "title": "...", "hook": "...", "plan": "...", "script": "...", "log_update": [...]}'
        else:
            output_keys = '{"script": "...", "log_update": [...]}'

    # PATCH-2026-06-27-funnelstep: внешняя воронка — пишем по плану-бюджету части + готовым хуком (chunk 0).
    if plan_for_chunk:
        position_lines.append(
            "PLAN FOR THIS PART (designed in the funnel step) — follow it EXACTLY and write EACH beat to its "
            "~character budget so the total length lands on target without padding or rushing:\n" + plan_for_chunk
        )
        if chunk_idx == 0 and hook:
            position_lines.append(
                "Open the script with THIS exact hook (it was designed for this video; adapt it naturally into "
                + language + ", keep it an open loop, then a short host intro): " + hook
            )
        if chunk_idx == 0:
            output_keys = '{"preview": "...", "title": "...", "script": "...", "log_update": [...]}'

    user_parts.append("\n" + "\n".join(position_lines))
    user_parts.append(
        "\nCRITICAL OUTPUT RULES:\n"
        "- Output ONLY valid JSON matching the schema below.\n"
        "- Your ENTIRE response MUST be a single JSON object: the FIRST character is { and the LAST is }. Write NOTHING before { — no preamble, no plan, no acknowledgement sentence (do NOT start with phrases like I will / Let me).\n"
        "- Properly escape every double-quote and line break inside string values so the whole object stays valid JSON. The script must be ONE valid JSON string.\n"
        "- Do NOT ask clarifying questions. Make reasonable assumptions and proceed.\n"
        "- Do NOT wrap JSON in markdown fences (no ```json).\n"
        "- Do NOT add any text before or after the JSON object.\n"
        "- If the topic is ambiguous, pick the most plausible interpretation silently.\n"
        f"\nOutput STRICTLY this JSON shape:\n{output_keys}\n\n"
        "Rules:\n"
        f"- TTS-ready: no markdown, no brackets, numbers/dates/currencies/abbreviations as WORDS in {language}, no remarks like '(applause)'.\n"
        "- Apply preset's structure, voice anchors, anti-LLM bans, forbidden topics if present.\n"
        "- If preset has rotation_pools — pick phrases naturally; report which pool IDs used in log_update.\n"
        "- `script` must be a single string (no nested objects)."
    )

    if retry_hint:
        user_parts.append(f"\n⚠️ RETRY INSTRUCTION: {retry_hint}")

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def _parse_truncated_chunk_json(raw: str) -> Optional[Dict[str, Any]]:
    """Repair-стратегия для обрезанных ответов Claude.

    Когда Claude не дописал JSON (truncation), `"script": "..."` остаётся
    без закрывающей кавычки и `}`. Берём текст от первого `{` до последнего
    содержательного символа, дописываем `"}` и пытаемся распарсить.
    Шаг бэктрекинга 200 chars (компромисс speed/coverage).
    """
    if not raw:
        return None
    start = raw.find("{")
    if start < 0:
        return None
    tail = raw[start:]
    max_back = min(len(tail), 8000)  # repair тратит время, ограничиваем 8K
    for end in range(len(tail), max(0, len(tail) - max_back), -200):
        # repair-кандидат: обрезаем хвост, чистим висячую запятую, закрываем `"}`
        candidate = tail[:end].rstrip().rstrip(",")
        # Эвристика: если последний значимый кусок — незавершённая строка,
        # дописываем `"` и `}`. Иначе только `}`.
        if not candidate.endswith('"') and not candidate.endswith("}"):
            candidate = candidate + '"'
        candidate = candidate + "}"
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and isinstance(obj.get("script"), str) and len(obj["script"]) > 100:
                return obj
        except json.JSONDecodeError:
            continue
    return None


def parse_chunk_response(
    raw: str,
    expect_funnel: bool,
    job_id: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Парсит strict-JSON ответ. Возвращает dict с {preview,title,script,log_update}
    или None если парс упал. expect_funnel=True для первого chunk (preview+title обязательны).

    Стратегии:
      1) Strict json.loads.
      2) raw_decode — берёт ПЕРВЫЙ JSON-объект, игнорирует hvost (дубль `{...}{...}`,
         markdown wrapper).
      3) Truncation-repair (_parse_truncated_chunk_json) — добивает обрезанные `"}`.
      4) Trailing-garbage repair — чистит хвост вида `"]}"}}` (наблюдалось у Haiku 4.5).

    Forensics: если raw не похож на JSON (нет '{' в первых 200 chars), пишем
    /var/log/rewrite/clarifying_responses.log для будущего анализа паттерна.
    """
    if not raw or not raw.strip():
        return None
    parsed: Optional[Dict[str, Any]] = None
    raw_stripped = raw.strip()

    # Strategy 1: direct
    try:
        cand = json.loads(raw_stripped)
        if isinstance(cand, dict):
            parsed = cand
    except json.JSONDecodeError:
        pass

    # Strategy 2: raw_decode (берёт ПЕРВЫЙ JSON-объект, игнорирует hvost)
    if parsed is None:
        start = raw_stripped.find("{")
        if start >= 0:
            try:
                decoder = json.JSONDecoder()
                cand, _end = decoder.raw_decode(raw_stripped[start:])
                if isinstance(cand, dict):
                    parsed = cand
            except json.JSONDecodeError:
                pass

    # Strategy 3: truncation-repair
    if parsed is None:
        parsed = _parse_truncated_chunk_json(raw_stripped)

    # Strategy 4: trailing-garbage repair (Haiku 4.5 issue — мусорный хвост `"]}"}}`)
    if parsed is None:
        try:
            cleaned = re.sub(r'"\]\}"\}+\s*$', '"]}', raw_stripped)
            cleaned = re.sub(r'\}+\s*$', '}', cleaned)  # дублирующиеся закрывающие скобки
            cand = json.loads(cleaned)
            if isinstance(cand, dict):
                parsed = cand
        except (json.JSONDecodeError, re.error):
            pass

    if parsed is None:
        # Strategy 5 (salvage): строгие парсеры упали (битый JSON: преамбула перед "{",
        # кривое экранирование длинного script у opus-4-8). Достаём поля по имени
        # следующего ключа (терпит пробелы в разделителях). Без regex.
        # PATCH-2026-06-27-funnel: hook/plan — boundary-маркеры, чтобы title не захватывал их хвост на битом JSON.
        _GEN_KEYS = ("script", "title", "preview", "hook", "plan", "log_update")

        def _gen_field(name, s):
            i = s.find('"' + name + '"')
            if i == -1:
                return None
            j = s.find(":", i)
            if j == -1:
                return None
            k = s.find('"', j)
            if k == -1:
                return None
            start = k + 1
            end = len(s)
            for other in _GEN_KEYS:
                if other == name:
                    continue
                p = s.find('"' + other + '"', start)
                if p != -1 and p < end:
                    end = p
            seg = s[start:end].rstrip()
            if seg.endswith(","):
                seg = seg[:-1].rstrip()
            if seg.endswith('"'):
                seg = seg[:-1]
            return seg

        def _gen_unesc(x):
            try:
                return json.loads('"' + x + '"')
            except Exception:
                return x.strip()

        try:
            _sc = _gen_field("script", raw_stripped)
            if _sc is not None:
                _salv = {
                    "preview": _gen_unesc(_gen_field("preview", raw_stripped) or ""),
                    "title": _gen_unesc(_gen_field("title", raw_stripped) or ""),
                    "script": _gen_unesc(_sc),
                    "log_update": [],
                }
                _ok = bool(_salv["script"].strip()) and (
                    (not expect_funnel) or (_salv["preview"].strip() and _salv["title"].strip())
                )
                if _ok:
                    parsed = _salv
        except Exception:
            pass

    if parsed is None:
        # Forensics: если ответ не похож на JSON (модель ушла в clarifying вопросы / текст),
        # сохраняем для будущей диагностики паттерна — без падения worker'а.
        try:
            if "{" not in raw_stripped[:200]:
                from datetime import datetime as _dt
                from pathlib import Path as _P
                _log_dir = _P("/var/log/rewrite")
                _log_dir.mkdir(parents=True, exist_ok=True)
                _log_path = _log_dir / "clarifying_responses.log"
                with open(_log_path, "a", encoding="utf-8") as _lf:
                    _lf.write(
                        f"\n=== {_dt.utcnow().isoformat()}Z "
                        f"job={job_id or 'unknown'} model={model or 'unknown'} ===\n"
                    )
                    _lf.write(raw_stripped[:3000])
                    _lf.write("\n")
        except Exception:
            pass  # forensics best-effort, не ломаем worker
        return None

    required = {"script", "log_update"}
    if expect_funnel:
        required = required | {"preview", "title"}
    # Для repair-кандидата log_update может отсутствовать — допускаем пустой
    if "script" not in parsed:
        return None
    if expect_funnel and not all(k in parsed for k in ("preview", "title")):
        # Если repair восстановил только script но потерял preview/title — fail
        return None

    log_update_raw = parsed.get("log_update", [])
    if not isinstance(log_update_raw, list):
        log_update_raw = []

    return {
        "preview": str(parsed.get("preview", "") or "").strip(),
        "title": str(parsed.get("title", "") or "").strip(),
        "script": _normalize_literal_escapes(str(parsed.get("script", "") or "").strip()),  # FIX-2026-06-29: литеральные \n\n → реальные
        "log_update": [str(x).strip() for x in log_update_raw if x],
    }


def get_rotation_log_path(base_dir: str, user_id: str, preset_id: str) -> str:
    """Sanitized path для rotation log."""
    safe_uid = re.sub(r"[^\w-]", "_", user_id or "default")[:50]
    safe_pid = re.sub(r"[^\w-]", "_", preset_id or "default")[:80]
    return os.path.join(base_dir, ROTATION_LOG_DIR_BASENAME, safe_uid, f"{safe_pid}.log.json")


def load_rotation_log_phrases(
    base_dir: str, user_id: str, preset_id: str, max_entries: int
) -> List[str]:
    """Возвращает flat list фраз из последних max_entries entries (для anti-repeat в prompt)."""
    if max_entries <= 0:
        return []
    path = get_rotation_log_path(base_dir, user_id, preset_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f)
        if not isinstance(entries, list):
            return []
        recent = entries[-max_entries:]
        flat: List[str] = []
        for e in recent:
            if isinstance(e, dict):
                phrases = e.get("phrases") or []
                if isinstance(phrases, list):
                    flat.extend(str(p) for p in phrases if p)
        return flat
    except Exception:
        return []


def append_rotation_log(
    base_dir: str, user_id: str, preset_id: str, entry: Dict[str, Any], max_keep: int = 50
) -> None:
    """Atomic append entry в rotation log; trim до max_keep последних."""
    path = get_rotation_log_path(base_dir, user_id, preset_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entries: List[Any] = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                entries = loaded
        except Exception:
            entries = []
    entries.append(entry)
    if len(entries) > max_keep:
        entries = entries[-max_keep:]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _try_parse_content_as_json(content: str) -> Optional[Dict[str, Any]]:
    """Пытается распарсить preset.content как JSON. None если не JSON или не dict."""
    if not content:
        return None
    s = content.strip()
    # Если строка начинается с { — пробуем парс
    if not s.startswith("{"):
        return None
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def extract_skip_postprocess(content: str) -> bool:
    """preset.content может быть JSON с ключом skip_postprocess:true → отключает Pass A/B/C/D."""
    d = _try_parse_content_as_json(content)
    return bool(d and d.get("skip_postprocess") is True)


def extract_field_from_content(content: str, field: str, default: str = "") -> str:
    """Если content — JSON dict, читает поле; иначе default."""
    d = _try_parse_content_as_json(content)
    if d and isinstance(d.get(field), str):
        return d[field]
    return default


# ============== ЯЗЫКИ ==============

LANGUAGES = {
    "Русский": "Russian",
    "Украинский": "Ukrainian",
    "Белорусский": "Belarusian",
    "Польский": "Polish",
    "Чешский": "Czech",
    "Словацкий": "Slovak",
    "Болгарский": "Bulgarian",
    "Сербский": "Serbian",
    "Хорватский": "Croatian",
    "Словенский": "Slovenian",
    "Македонский": "Macedonian",
    "Боснийский": "Bosnian",
    "Английский": "English",
    "Немецкий": "German",
    "Французский": "French",
    "Испанский": "Spanish",
    "Итальянский": "Italian",
    "Каталанский": "Catalan",
    "Португальский": "Portuguese",
    "Нидерландский": "Dutch",
    "Шведский": "Swedish",
    "Норвежский": "Norwegian",
    "Датский": "Danish",
    "Финский": "Finnish",
    "Литовский": "Lithuanian",
    "Латышский": "Latvian",
    "Эстонский": "Estonian",
    "Греческий": "Greek",
    "Румынский": "Romanian",
    "Венгерский": "Hungarian",
    "Турецкий": "Turkish",
    "Казахский": "Kazakh",
    "Узбекский": "Uzbek",
    "Азербайджанский": "Azerbaijani",
    "Армянский": "Armenian",
    "Грузинский": "Georgian",
    "Арабский": "Arabic",
    "Иврит": "Hebrew",
    "Персидский (фарси)": "Persian",
    "Хинди": "Hindi",
    "Китайский (упрощённый)": "Simplified Chinese",
    "Японский": "Japanese",
    "Корейский": "Korean",
    "Вьетнамский": "Vietnamese",
    "Тайский": "Thai",
    "Индонезийский": "Indonesian",
}

LANGUAGES_SORTED = dict(sorted(LANGUAGES.items(), key=lambda x: x[0]))

# ============== СТАТУСЫ ЗАДАЧ ==============

class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"  # Частично завершено

@dataclass
class ChunkProgress:
    index: int
    total: int
    status: str  # pending, completed, failed
    result: str = ""
    error: str = ""
    attempts: int = 0

@dataclass  
class TaskProgress:
    file_path: str
    language: str
    lang_display: str
    status: str
    chunks: List[dict]
    output_file: str = ""
    started_at: str = ""
    updated_at: str = ""
    total_attempts: int = 0
    needs_review: bool = False   # детектор утечки чисел/англ-слов: флаг, НЕ автофикс
    review_note: str = ""

    def to_dict(self):
        return {
            "file_path": self.file_path,
            "language": self.language,
            "lang_display": self.lang_display,
            "status": self.status,
            "chunks": self.chunks,
            "output_file": self.output_file,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "total_attempts": self.total_attempts,
            "needs_review": self.needs_review,
            "review_note": self.review_note
        }
    
    @classmethod
    def from_dict(cls, d):
        return cls(**d)


# ============== АДАПТИВНЫЙ RATE LIMITER ==============

class AdaptiveRateLimiter:
    """
    Умный rate limiter:
    - Экспоненциальный backoff при 429
    - Автоматическое снижение потоков
    - Jitter для предотвращения thundering herd
    """
    def __init__(self):
        self._lock = threading.Lock()
        self.base_delay = 1.0
        self.current_delay = 1.0
        self.max_delay = 300.0  # 5 минут максимум
        self.consecutive_429s = 0
        self.last_429_time = 0
        self.recommended_threads = DEFAULT_THREADS
        
    def on_success(self):
        with self._lock:
            # Успех - уменьшаем задержку
            self.consecutive_429s = 0
            self.current_delay = max(self.base_delay, self.current_delay * 0.8)
            # Постепенно восстанавливаем потоки
            if self.recommended_threads < DEFAULT_THREADS:
                self.recommended_threads = min(DEFAULT_THREADS, self.recommended_threads + 1)
    
    def on_rate_limit(self) -> float:
        with self._lock:
            self.consecutive_429s += 1
            self.last_429_time = time.time()
            
            # Экспоненциальный backoff с jitter
            self.current_delay = min(
                self.max_delay,
                self.base_delay * (2 ** self.consecutive_429s) + random.uniform(0, 5)
            )
            
            # Снижаем потоки после 3 подряд 429
            if self.consecutive_429s >= 3:
                self.recommended_threads = max(MIN_THREADS, self.recommended_threads - 1)
            
            return self.current_delay
    
    def get_delay(self) -> float:
        with self._lock:
            return self.current_delay + random.uniform(0, self.current_delay * 0.1)
    
    def get_recommended_threads(self) -> int:
        with self._lock:
            return self.recommended_threads


# ============== МЕНЕДЖЕР ПРОГРЕССА ==============

class ProgressManager:
    """Сохраняет и загружает прогресс в JSON файл"""
    
    def __init__(self, progress_file: str = PROGRESS_FILE):
        self.progress_file = progress_file
        self._lock = threading.Lock()
        self.tasks: Dict[str, TaskProgress] = {}
        self.cache_stats = {
            "cache_hits": 0,
            "cache_creates": 0,
            "tokens_saved": 0
        }
        self.load()
    
    def _task_key(self, file_path: str, language: str, user_id: str = "") -> str:
        # B5 PATCH-2026-06-25-secfix: user_id в ключе -> два тенанта с одноимённым файлом
        # больше НЕ коллизят (раньше basename::lang давал межтенантный слив контента).
        return f"{user_id}::{os.path.basename(file_path)}::{language}"
    
    def load(self):
        """Загружает прогресс из файла"""
        try:
            if os.path.exists(self.progress_file):
                with open(self.progress_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for key, task_dict in data.get("tasks", {}).items():
                        self.tasks[key] = TaskProgress.from_dict(task_dict)
        except Exception as e:
            print(f"Ошибка загрузки прогресса: {e}")
            self.tasks = {}
    
    def save(self):
        """Сохраняет прогресс в файл"""
        with self._lock:
            try:
                data = {
                    "updated_at": datetime.now().isoformat(),
                    "tasks": {k: v.to_dict() for k, v in self.tasks.items()}
                }
                # Атомарная запись через временный файл
                temp_file = self.progress_file + ".tmp"
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(temp_file, self.progress_file)
            except Exception as e:
                print(f"Ошибка сохранения прогресса: {e}")
    
    def get_or_create_task(self, file_path: str, language: str, lang_display: str,
                           total_chunks: int, user_id: str = "") -> TaskProgress:
        """Получает существующую задачу или создаёт новую"""
        key = self._task_key(file_path, language, user_id)
        
        with self._lock:
            if key in self.tasks:
                task = self.tasks[key]
                # Проверяем, не изменилось ли количество чанков
                if len(task.chunks) != total_chunks:
                    # Файл изменился - сбрасываем
                    task = None
                elif task.status == TaskStatus.COMPLETED.value:
                    # Уже завершено
                    return task
            else:
                task = None
            
            if task is None:
                # Создаём новую задачу
                chunks = [
                    {"index": i, "status": "pending", "result": "", "error": "", "attempts": 0}
                    for i in range(total_chunks)
                ]
                task = TaskProgress(
                    file_path=file_path,
                    language=language,
                    lang_display=lang_display,
                    status=TaskStatus.PENDING.value,
                    chunks=chunks,
                    started_at=datetime.now().isoformat(),
                    updated_at=datetime.now().isoformat()
                )
                self.tasks[key] = task
            
            return task
    
    def update_chunk(self, file_path: str, language: str, chunk_index: int,
                     status: str, result: str = "", error: str = "", user_id: str = ""):
        """Обновляет статус чанка и сохраняет"""
        key = self._task_key(file_path, language, user_id)
        
        with self._lock:
            if key in self.tasks:
                task = self.tasks[key]
                if chunk_index < len(task.chunks):
                    task.chunks[chunk_index]["status"] = status
                    task.chunks[chunk_index]["result"] = result
                    task.chunks[chunk_index]["error"] = error
                    task.chunks[chunk_index]["attempts"] += 1
                    task.updated_at = datetime.now().isoformat()
                    task.total_attempts += 1
                    
                    # Обновляем статус задачи
                    self._update_task_status(task)
        
        self.save()
    
    def _update_task_status(self, task: TaskProgress):
        """Обновляет общий статус задачи на основе чанков"""
        statuses = [c["status"] for c in task.chunks]
        
        if all(s == "completed" for s in statuses):
            task.status = TaskStatus.COMPLETED.value
        elif any(s == "in_progress" for s in statuses):
            task.status = TaskStatus.IN_PROGRESS.value
        elif any(s == "completed" for s in statuses):
            task.status = TaskStatus.PARTIAL.value
        elif any(s == "failed" for s in statuses):
            task.status = TaskStatus.FAILED.value
        else:
            task.status = TaskStatus.PENDING.value
    
    def set_task_output(self, file_path: str, language: str, output_file: str, user_id: str = ""):
        """Устанавливает путь к выходному файлу"""
        key = self._task_key(file_path, language, user_id)
        with self._lock:
            if key in self.tasks:
                self.tasks[key].output_file = output_file
        self.save()

    def set_needs_review(self, file_path: str, language: str, note: str, user_id: str = ""):
        """Помечает задачу как требующую ручной вычитки (НЕ fail, НЕ автофикс).
        Используется детектором утечки цифр/английских слов-чисел."""
        key = self._task_key(file_path, language, user_id)
        with self._lock:
            if key in self.tasks:
                self.tasks[key].needs_review = True
                self.tasks[key].review_note = note
        self.save()
    
    def get_pending_tasks(self) -> List[Tuple[str, TaskProgress]]:
        """Возвращает список незавершённых задач"""
        with self._lock:
            return [
                (key, task) for key, task in self.tasks.items()
                if task.status != TaskStatus.COMPLETED.value
            ]
    
    def get_incomplete_chunks(self, task: TaskProgress) -> List[int]:
        """Возвращает индексы незавершённых чанков"""
        return [
            c["index"] for c in task.chunks 
            if c["status"] != "completed"
        ]
    
    def clear_completed(self):
        """Удаляет завершённые задачи"""
        with self._lock:
            self.tasks = {
                k: v for k, v in self.tasks.items()
                if v.status != TaskStatus.COMPLETED.value
            }
        self.save()

    def delete_task(self, file_path: str, language: str, user_id: str = "") -> bool:
        """Удаляет конкретную задачу (file+lang) из прогресса. Используется для полной перегенерации."""
        key = self._task_key(file_path, language, user_id)
        with self._lock:
            if key in self.tasks:
                del self.tasks[key]
                changed = True
            else:
                changed = False
        if changed:
            self.save()
        return changed
    
    def clear_all(self):
        """Очищает весь прогресс"""
        with self._lock:
            self.tasks = {}
        self.save()
    
    def get_summary(self) -> Dict:
        """Возвращает сводку по всем задачам"""
        with self._lock:
            total = len(self.tasks)
            completed = sum(1 for t in self.tasks.values() if t.status == TaskStatus.COMPLETED.value)
            partial = sum(1 for t in self.tasks.values() if t.status == TaskStatus.PARTIAL.value)
            failed = sum(1 for t in self.tasks.values() if t.status == TaskStatus.FAILED.value)
            pending = sum(1 for t in self.tasks.values() if t.status == TaskStatus.PENDING.value)
            
            return {
                "total": total,
                "completed": completed,
                "partial": partial,
                "failed": failed,
                "pending": pending,
                "in_progress": total - completed - partial - failed - pending
            }

    def get_chunks_summary(self, task_keys: Optional[Set[str]] = None) -> Dict:
        """Возвращает сводку по чанкам. task_keys — опциональный фильтр (basename::lang)."""
        with self._lock:
            total_chunks = 0
            completed_chunks = 0
            for key, task in self.tasks.items():
                if task_keys is not None and key not in task_keys:
                    continue
                for c in task.chunks:
                    total_chunks += 1
                    if c.get("status") == "completed":
                        completed_chunks += 1
            return {
                "total_chunks": total_chunks,
                "completed_chunks": completed_chunks,
            }
    
    def log_cache_usage(self, cache_hit_tokens: int = 0, cache_create_tokens: int = 0):
        """Логирует использование кэша"""
        with self._lock:
            if cache_hit_tokens > 0:
                self.cache_stats["cache_hits"] += 1
                self.cache_stats["tokens_saved"] += int(cache_hit_tokens * 0.9)  # 90% экономия
            if cache_create_tokens > 0:
                self.cache_stats["cache_creates"] += 1
    
    def get_cache_stats(self) -> Dict:
        """Возвращает статистику кэша"""
        with self._lock:
            return self.cache_stats.copy()


# ============== СТОП-ФЛАГ ==============

class StopFlag:
    """Глобальный флаг остановки. Поддерживает thread-local override для остановки только одной задачи в очереди."""
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = False
        self._thread_local = threading.local()

    def set_thread_stop_event(self, event):
        """Установить event для текущего потока. is_stopped() будет проверять event.is_set() вместо глобального флага."""
        self._thread_local.stop_event = event

    def clear_thread_stop_event(self):
        """Снять thread-local override."""
        if hasattr(self._thread_local, "stop_event"):
            del self._thread_local.stop_event

    def stop(self):
        with self._lock:
            self._stop = True

    def is_stopped(self):
        # Сначала проверяем thread-local (для задач очереди — останавливаем только эту задачу)
        if hasattr(self._thread_local, "stop_event") and self._thread_local.stop_event.is_set():
            return True
        with self._lock:
            return self._stop

    def reset(self):
        with self._lock:
            self._stop = False


STOP_FLAG = StopFlag()
RATE_LIMITER = AdaptiveRateLimiter()
PROGRESS_MANAGER = ProgressManager()

# Кэш анализа по файлу: один файл = один анализ для всех языков (вместо 4 дубликатов)
_ANALYSIS_CACHE: Dict[Tuple[str, str], Dict] = {}
_ANALYSIS_CACHE_LOCK = threading.Lock()
_CORRECTION_LOG_LOCK = threading.Lock()
# Семафоры анализа по лимиту из настроек (0 = без лимита)
_ANALYSIS_SEMAPHORES: Dict[int, threading.Semaphore] = {}
_ANALYSIS_SEMAPHORES_LOCK = threading.Lock()


def _get_analysis_semaphore() -> Optional[threading.Semaphore]:
    """Возвращает семафор для ограничения параллельных анализов (из настроек). 0 = без лимита."""
    cfg = get_queue_config()
    limit = int(cfg.get("max_analysis_parallelism", 0))
    if limit <= 0:
        return None
    with _ANALYSIS_SEMAPHORES_LOCK:
        if limit not in _ANALYSIS_SEMAPHORES:
            _ANALYSIS_SEMAPHORES[limit] = threading.Semaphore(limit)
        return _ANALYSIS_SEMAPHORES[limit]


def reset_analysis_stagger():
    """Сброс кэша анализа перед новым запуском (вызывать в start_rewrite)."""
    global _ANALYSIS_CACHE
    with _ANALYSIS_CACHE_LOCK:
        _ANALYSIS_CACHE.clear()


# ============== ГЕНЕРАЦИЯ ТЕКСТОВ (ПРЕСЕТЫ И ГЕНЕРАТОР) ==============

class Preset(BaseModel):
    """Пресет для генерации текста с нуля (v2 schema, Round 2 — shims удалены 2026-05-23)."""
    id: str
    name: str = Field(..., min_length=1, max_length=80)
    target_length_words: Optional[int] = Field(None, ge=300, le=100000)
    language: Optional[str] = None  # дефолт-язык пресета: воркер берёт его ДО "English" fallback (2026-06-27, language task)
    content: str = Field(..., min_length=1)
    created_at: float = 0.0
    last_used_at: Optional[float] = None

    class Config:
        extra = "ignore"  # старые поля в мигрированном JSON игнорируются без падения

    def to_dict(self) -> Dict[str, Any]:
        # Точный dump новой schema (без shim'ов). Для UI/CRUD.
        return {
            "id": self.id,
            "name": self.name,
            "target_length_words": self.target_length_words,
            "language": self.language,  # language task
            "content": self.content,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Preset":
        """Принимает мигрированный (v2) формат, дополняет defaults если поля отсутствуют."""
        return cls(
            id=str(d.get("id") or d.get("name", "preset")),
            name=str(d.get("name", "")),
            target_length_words=d.get("target_length_words") or d.get("total_words"),
            language=(d.get("language") or None),  # language task
            content=str(d.get("content") or d.get("prompt_template", "")),
            created_at=float(d.get("created_at") or 0.0) if not isinstance(d.get("created_at"), str) else 0.0,
            last_used_at=d.get("last_used_at"),
        )


class PresetManager:
    """Управление пресетами генерации (загрузка/сохранение в JSON v2)."""
    def __init__(self, presets_file: str = TEXT_GENERATION_PRESETS_FILE):
        self.presets_file = presets_file
        self._lock = threading.Lock()
        # ключ — id (slug); name отдельным полем в Preset. Изменено в v2 — было ключом name.
        self.presets: Dict[str, Preset] = {}
        self.load()

    def load(self):
        try:
            if os.path.exists(self.presets_file):
                with open(self.presets_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # В v2 storage остаётся {name → preset_dict} (так миграция мигрировала),
                # но в memory ключ — id. Резолвим оба для backward compat.
                raw = data.get("presets", {}) if isinstance(data, dict) else {}
                fresh: Dict[str, Preset] = {}
                for stored_key, preset_dict in raw.items():
                    if not isinstance(preset_dict, dict):
                        continue
                    p = Preset.from_dict(preset_dict)
                    fresh[p.id] = p
                self.presets = fresh
        except Exception as e:
            print(f"Ошибка загрузки пресетов генерации: {e}")
            self.presets = {}

    def save(self):
        with self._lock:
            try:
                # Сохраняем под именами для удобства чтения файла глазом (как сейчас юзер привык).
                data = {
                    "updated_at": time.time(),
                    "presets": {p.name: p.to_dict() for p in self.presets.values()},
                }
                temp_file = self.presets_file + ".tmp"
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(temp_file, self.presets_file)
            except Exception as e:
                print(f"Ошибка сохранения пресетов: {e}")

    def get_preset(self, identifier: str) -> Optional[Preset]:
        """Backward compat: ищет по id (новое) ИЛИ по name (старый UI/curl)."""
        if not identifier:
            return None
        # 1) try id (canonical)
        if identifier in self.presets:
            return self.presets[identifier]
        # 2) fallback: lookup by name
        for p in self.presets.values():
            if p.name == identifier:
                return p
        return None

    def get_preset_by_id(self, preset_id: str) -> Optional[Preset]:
        """Строго по id."""
        return self.presets.get(preset_id)

    def list_presets(self) -> List[Preset]:
        """Список Preset (Round 1 — изменено: возвращает объекты, не имена). Старые вызовы (server.py:692) переделаны."""
        return list(self.presets.values())

    def save_preset(self, preset: Preset):
        """Сохраняет preset (create или update). Ключ — id."""
        if not preset.created_at:
            preset.created_at = time.time()
        self.presets[preset.id] = preset
        self.save()

    def delete_preset(self, identifier: str) -> bool:
        """Удаляет preset по id (или name через get_preset). Returns True если был."""
        p = self.get_preset(identifier)
        if p is None:
            return False
        del self.presets[p.id]
        self.save()
        return True


def clean_chapter_text(text: str) -> str:
    """Очищает текст от маркеров глав (ГЛАВА ПЕРВАЯ и т.д.)."""
    patterns = [
        r"ГЛАВА\s+(ПЕРВАЯ|ВТОРАЯ|ТРЕТЬЯ|ЧЕТВЁРТАЯ|ПЯТАЯ|ШЕСТАЯ|СЕДЬМАЯ|ВОСЬМАЯ|ДЕВЯТАЯ|ДЕСЯТАЯ)",
        r"ГЛАВА\s+\d+",
        r"ЧАСТЬ\s+(ПЕРВАЯ|ВТОРАЯ|ТРЕТЬЯ|ЧЕТВЁРТАЯ|ПЯТАЯ|ШЕСТАЯ|СЕДЬМАЯ|ВОСЬМАЯ|ДЕВЯТАЯ|ДЕСЯТАЯ)",
        r"ЧАСТЬ\s+\d+",
        r"CHAPTER\s+\d+",
        r"PART\s+\d+",
    ]
    cleaned = text
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned)
    cleaned = cleaned.strip()
    # Фикс склеенных предложений: "слово.Следующее" → "слово. Следующее"
    cleaned = re.sub(r"([.!?])([A-ZА-ЯÀ-ÖØ-ÝĀ-ŽŐŰĐŁŃŚŹŻĆĄĘÅÆØÞÐÉÍÓÚÜŐŰŘŤŇĎŠŽČĚÖÜÄẞЁҐЄІЇȘȚĂ])", r"\1 \2", cleaned)
    # Фикс склеенных после запятой: "слово,Следующее"
    cleaned = re.sub(r",([A-ZА-ЯÀ-ÖØ-ÝĀ-ŽŐŰĐŁŃŚŹŻĆĄĘÅÆØÞÐÉÍÓÚÜŐŰŘŤŇĎŠŽČĚÖÜÄẞЁҐЄІЇȘȚĂ])", r", \1", cleaned)
    return cleaned


def get_system_style_prompt(language: str) -> str:
    """Системный промпт стиля для генерации (документальный триллер)."""
    return f"""Ты — сценарист документального YouTube-канала в жанре True Crime и исторических расследований. Язык: {language}.

ТВОИ ЖЕЛЕЗНЫЕ ПРАВИЛА:

1. СТИЛЬ: Рубленый ритм. Короткие предложения. (Пример: 'Было темно. Слишком темно для декабря').

2. ДЕТАЛИ: Фокусируйся на сенсорике (холод, запах гари, звук металла).

3. ФОРМАТ: Пиши цифры СЛОВАМИ. Текст предназначен для озвучки диктором.

4. ДРАМАТУРГИЯ: Не называй эмоции, а описывай действия.

5. СТРУКТУРА: Никаких вступлений типа 'Приветствую'. Сразу начинай историю с крючка.

6. ВЫВОД: Весь текст главы должен быть внутри квадратных скобок [Текст].

7. ЯЗЫК: Пиши ТОЛЬКО на {language}. Все цифры и даты пиши СЛОВАМИ.

8. СВЯЗНОСТЬ: Каждая глава должна логично продолжать предыдущую, используя контекст."""


class SmartNarrativeGenerator:
    """Генератор текстов с планированием и контекстным окном. Поддерживает ротацию OAuth токенов при лимитах."""
    def __init__(
        self,
        api_key: str,
        model: str,
        log_queue: queue.Queue,
        use_subscription: bool = False,
        oauth_token: str = "",
        oauth_tokens: Optional[List[str]] = None,
        start_token_index: int = 0,
    ):
        self.api_key = api_key
        self.model = model
        self.log_queue = log_queue
        self.use_subscription = use_subscription
        self.oauth_token = oauth_token
        self.oauth_tokens = (oauth_tokens or []) if oauth_tokens else []
        self.current_token_index = (
            [start_token_index % len(self.oauth_tokens)]
            if self.oauth_tokens and len(self.oauth_tokens) > 1
            else None
        )

    def _call_api(self, messages: List[Dict], task_info: str = "") -> Tuple[bool, str]:
        provider = _detect_provider(self.model, self.use_subscription)
        if provider == "subscription" and self.oauth_tokens and self.current_token_index and len(self.oauth_tokens) > 1:
            return call_api_universal_with_token_rotation(
                messages=messages,
                api_key=self.api_key,
                model=self.model,
                log_queue=self.log_queue,
                task_info=task_info,
                provider=provider,
                oauth_tokens=self.oauth_tokens,
                current_token_index=self.current_token_index,
            )
        token = self.oauth_tokens[0] if self.oauth_tokens else self.oauth_token
        return call_api_universal(messages, self.api_key, self.model, self.log_queue, task_info, provider, token)

    def generate_outline(self, topic: str, style_instruction: str, num_chapters: int,
                         language: str, custom_instructions: str = "",
                         preview_text: str = "", title_text: str = "", brief_text: str = "") -> List[str]:
        if STOP_FLAG.is_stopped():
            return [f"Часть {i+1}" for i in range(num_chapters)]
        outline_prompt = f"""Создай план повествования для YouTube-видео.

НАЗВАНИЕ ВИДЕО (зритель кликнул на это):
"{title_text or topic}"

ПРЕВЬЮ (текст на миниатюре):
"{preview_text or ''}"

ДОПОЛНИТЕЛЬНЫЕ УКАЗАНИЯ АВТОРА:
{brief_text or 'Нет. Используй свои знания по теме.'}

КРИТИЧЕСКИЕ ПРАВИЛА ДЛЯ ПЛАНА:
1. Часть 1 ОБЯЗАНА начинаться с момента, который ПРЯМО СВЯЗАН с названием видео.
   Не с предыстории, не с детства героя, не с описания города.
   С КОНКРЕТНОЙ СЦЕНЫ из обещания Title/Preview.
   
2. Если Title содержит реального человека — план должен следовать РЕАЛЬНЫМ событиям 
   его жизни (можно в нехронологическом порядке для драматизма).
   
3. Каждая часть — клиффхэнгер на конце.

4. Предыстория (детство, контекст эпохи) — НЕ РАНЬШЕ части 2-3. 
   Сначала захвати внимание конкретной сценой из Title.

{style_instruction[:1500]}
{custom_instructions or ''}

Формат: JSON массив из {num_chapters} элементов. Каждый — 2-3 предложения описания.
Язык плана: {language}

Верни ТОЛЬКО JSON массив, без комментариев."""
        system_prompt = """Ты профессиональный сценарист. Твоя задача - создать структурированный план повествования.
Верни результат ТОЛЬКО в формате JSON массива, без дополнительных комментариев."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": outline_prompt}
        ]
        success, response = self._call_api(messages, "Генерация плана")
        if STOP_FLAG.is_stopped():
            return [f"Часть {i+1}" for i in range(num_chapters)]
        if not success:
            log_msg(self.log_queue, "   ⚠️ Не удалось создать план, использую шаблон")
            return [f"Часть {i+1}" for i in range(num_chapters)]
        try:
            json_match = re.search(r"\[.*\]", response, re.DOTALL)
            if json_match:
                outline_json = json.loads(json_match.group())
                if isinstance(outline_json, list) and len(outline_json) >= num_chapters:
                    return outline_json[:num_chapters]
            outline_json = json.loads(response)
            if isinstance(outline_json, list):
                return outline_json[:num_chapters]
        except (json.JSONDecodeError, AttributeError) as e:
            log_msg(self.log_queue, f"   ⚠️ Ошибка парсинга плана: {e}, использую шаблон")
        return [f"Часть {i+1}" for i in range(num_chapters)]

    def generate_chapter(self, chapter_plan: str, prev_context: str, style_prompt: str,
                         chapter_num: int, total_chapters: int, language: str,
                         full_outline: Optional[List[str]] = None,
                         words_per_chunk: int = 700,
                         character_names: str = "") -> str:
        context_note = ""
        if prev_context:
            context_note = f"\n\nКОНТЕКСТ ПРЕДЫДУЩЕЙ ЧАСТИ (для связности):\n{prev_context}\n\nПродолжи с этого момента, но не повторяй эти предложения дословно."
        if character_names:
            context_note += f"""

════════════════════════════════════════
⚠️ ОБЯЗАТЕЛЬНЫЕ ИМЕНА ПЕРСОНАЖЕЙ:
════════════════════════════════════════
{character_names}
════════════════════════════════════════
Эти имена УЖЕ установлены в предыдущих частях текста.
Слушатель УЖЕ знает этих людей под этими именами.
Использование ДРУГОГО имени для того же персонажа = КРИТИЧЕСКАЯ ОШИБКА.
КОПИРУЙ эти имена точно. Не придумывай варианты, не переводи, не меняй фамилии.
════════════════════════════════════════
"""
        position_note = ""
        if chapter_num == 1:
            position_note = "Это ПЕРВАЯ часть. Начни с захватывающего вступления, которое сразу цепляет читателя."
        elif chapter_num == total_chapters:
            position_note = "Это ПОСЛЕДНЯЯ часть. Заверши историю сильным финалом с философской нотой."
        else:
            position_note = f"Это часть {chapter_num} из {total_chapters}. Продолжай развивать сюжет."
        user_prompt = f"""Напиши часть текста по следующему плану:

ПЛАН ЭТОЙ ЧАСТИ:
{chapter_plan}

{position_note}
{context_note}

ТРЕБОВАНИЯ К ДЛИНЕ:
- Целевой объём: примерно {words_per_chunk} слов (от {int(words_per_chunk * 0.9)} до {int(words_per_chunk * 1.15)} слов).
- НЕ превышай {int(words_per_chunk * 1.15)} слов — если текст длиннее, сократи его, убрав лишние описания или повторения.
- Если получается короче {int(words_per_chunk * 0.9)} — добавь детали, описания, рефлексию автора, конкретику.
- Язык: {language}

CRITICAL OUTPUT FORMAT (read carefully):
- Your ENTIRE response must be ONLY the story text wrapped in square brackets
- Format: [story starts here... story ends here]
- DO NOT write ANYTHING before the opening bracket [
- DO NOT write ANYTHING after the closing bracket ]
- NO preamble (no "Here is the text:", "Вот текст:", etc.)
- NO postamble (no "Hope this helps", "Надеюсь подходит", etc.)
- NO meta-commentary, NO explanations, NO questions
- Just pure narrative from first word to last word inside [ ]

CORRECT example:
[Warszawa, maj tysiąc dziewięćset dziewięćdziesiątego piątego roku. Restauracja Pod Złotym Lwem...]

WRONG examples (do NOT output like this):
❌ Вот часть 1: [Warszawa...]
❌ [Warszawa...] Надеюсь это подходит!
❌ Here is part 1: [Warszawa...] Let me know if you want changes.
"""
        if full_outline:
            outline_text = "\n".join([f"Часть {i+1}: {plan}" for i, plan in enumerate(full_outline)])
            extended_style = f"""{style_prompt}

ПЛАН ВСЕГО ТЕКСТА (для понимания общей арки):
{outline_text}

Каждая часть должна логично продолжать предыдущую и вписываться в общий сюжет.

⚠️ CRITICAL FORMATTING RULE:
Your response must contain ONLY the narrative wrapped in [ ].
NO text before bracket. NO text after bracket. NO explanations.
If you add ANY text outside brackets, output will be rejected and regenerated.

⚠️ CHARACTER NAME CONSISTENCY:
- Use the SAME main character name consistently throughout ALL parts of the story.
- If the main character is "Marek Wolański", use "Marek Wolański" (or "Marek", "Wolański") in EVERY part.
- Do NOT change the main character's name between parts.
- The author/host name (for intro/outro branding) is DIFFERENT from the main character name.
"""
            system_message = {
                "role": "system",
                "content": [{
                    "type": "text",
                    "text": extended_style,
                    "cache_control": {"type": "ephemeral"}
                }]
            }
            messages = [system_message, {"role": "user", "content": user_prompt}]
        else:
            messages = [
                {"role": "system", "content": style_prompt},
                {"role": "user", "content": user_prompt}
            ]
        success, response = self._call_api(messages, f"Часть {chapter_num}/{total_chapters}")
        if not success:
            return f"[ОШИБКА: {response}]"
        extracted = extract_from_brackets(response)
        cleaned = clean_chapter_text(extracted)
        return cleaned


PRESET_MANAGER = PresetManager()


def _extract_character_names(text: str) -> str:
    """
    Извлекает имена персонажей из сгенерированного текста (regex, без API-вызова).
    Находит все последовательности 2–4 слов с заглавной буквы (типичный паттерн имён).
    Возвращает строку для вставки в промпт.
    """
    # Ищем паттерн: 2-4 слова подряд с заглавной (латиница + кириллица + диакритика)
    name_pattern = re.compile(
        r'\b([A-ZА-ЯÀ-ÖØ-ÝĀ-ŽŐŰĐŁŃŚŹŻĆĄĘÅÆØÞÐÉÍÓÚÜŘŤŇĎŠŽČĚÖÜÄẞЁҐЄІЇ]'
        r'[a-zа-яà-öø-ýā-žőűđłńśźżćąęåæøþðéíóúüřťňďšžčěöüäßёґєіїŞşȘșȚțĂă]{2,})'
        r'(?:\s+[A-ZА-ЯÀ-ÖØ-ÝĀ-ŽŐŰĐŁŃŚŹŻĆĄĘÅÆØÞÐÉÍÓÚÜŘŤŇĎŠŽČĚÖÜÄẞЁҐЄІЇ]'
        r'[a-zа-яà-öø-ýā-žőűđłńśźżćąęåæøþðéíóúüřťňďšžčěöüäßёґєіїŞşȘșȚțĂă]{2,}){1,3}'
    )
    # Стоп-слова: начала предложений которые не являются именами
    stop_words = {
        'The', 'This', 'That', 'These', 'Those', 'When', 'Where', 'What', 'Which', 'While',
        'After', 'Before', 'During', 'Between', 'Through', 'Against', 'Without', 'However',
        'Although', 'Because', 'Perhaps', 'Maybe', 'Every', 'Each', 'Many', 'Some', 'Other',
        'Każdy', 'Każda', 'Kiedy', 'Gdzie', 'Jednak', 'Potem', 'Przez', 'Pewnego', 'Wtedy',
        'Fiecare', 'Când', 'Unde', 'Însă', 'Apoi', 'Prin', 'Atunci', 'După', 'Într',
        'Každý', 'Každá', 'Když', 'Kde', 'Pak', 'Přes', 'Tehdy', 'Potom',
    }
    
    found_names = {}
    for m in name_pattern.finditer(text):
        name = m.group(0)
        words = name.split()
        if words[0] in stop_words:
            continue
        if len(name) < 5:
            continue
        found_names[name] = found_names.get(name, 0) + 1
    
    # Оставляем имена, упомянутые хотя бы 2 раза (фильтрует случайные совпадения)
    significant = sorted(
        [(name, count) for name, count in found_names.items() if count >= 2],
        key=lambda x: -x[1]
    )
    if not significant:
        return ""
    
    lines = []
    for name, count in significant[:15]:  # Не больше 15 имён
        lines.append(f"- {name}")
    return "\n".join(lines)


# ============== УТИЛИТЫ ==============

def log_msg(log_queue: queue.Queue, message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_queue.put(f"[{timestamp}] {message}")


def debug_log(log_queue: Optional[queue.Queue], prefix: str, data: str):
    """Логирует данные в debug-режиме (если включён в настройках)."""
    if not log_queue or not get_keys_config().get("debug_mode", False):
        return
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_queue.put(f"[{timestamp}] [DEBUG] {prefix}")
    for line in data.split("\n"):
        log_queue.put(f"[{timestamp}] [DEBUG]   {line}")


def read_file_as_text(filepath: str) -> str:
    """
    Извлекает текст из файла. Поддерживаются:
    .txt — обычный текст;
    .docx, .doc — Word (нужен python-docx);
    .pdf — PDF (нужен pypdf или PyPDF2);
    .odt — OpenDocument (нужен odfpy);
    .rtf — RTF (нужен striprtf).
    """
    path_lower = filepath.lower()
    ext = os.path.splitext(path_lower)[1]

    if ext == ".txt":
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()

    if ext in (".docx", ".doc"):
        if not DOCX_AVAILABLE:
            raise ImportError(
                "Для чтения .docx/.doc установите: pip install python-docx"
            )
        if ext == ".doc":
            raise ValueError(
                "Формат .doc (старый Word) не поддерживается. Сохраните файл как .docx."
            )
        doc = docx.Document(filepath)
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" ".join(cell.text for cell in row.cells))
        return "\n\n".join(p for p in parts if p).strip()

    if ext == ".pdf":
        if not PYPDF_AVAILABLE:
            raise ImportError(
                "Для чтения PDF установите: pip install pypdf  (или PyPDF2)"
            )
        reader = pypdf.PdfReader(filepath)
        parts = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                parts.append(t)
        return "\n\n".join(parts).strip()

    if ext == ".odt":
        if not ODT_AVAILABLE:
            raise ImportError(
                "Для чтения .odt установите: pip install odfpy"
            )
        doc = load_odt(filepath)
        parts = []
        for elem in doc.getElementsByType(text.P):
            parts.append(teletype.extractText(elem))
        for elem in doc.getElementsByType(text.H):
            parts.append(teletype.extractText(elem))
        return "\n\n".join(p for p in parts if p).strip()

    if ext == ".rtf":
        if not RTF_AVAILABLE:
            raise ImportError(
                "Для чтения RTF установите: pip install striprtf"
            )
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return rtf_to_text(f.read()).strip()

    # Неизвестный формат — пробуем как текст
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        return f.read().strip()


def count_total_chunks_for_run(files: List[str], languages: List[Tuple[str, str]], chunk_size: int) -> Tuple[int, Set[str]]:
    """
    Предвычисляет общее число чанков для запуска и ключи задач.
    Returns: (total_chunks, task_keys для фильтрации в get_chunks_summary).
    """
    total = 0
    task_keys = set()
    for file_path in files:
        try:
            text = read_file_as_text(file_path)
            chunks = split_text_to_chunks(text, chunk_size)
            n = len(chunks) * len(languages)
            total += n
            for _lang_display, lang_code in languages:
                task_keys.add(f"{os.path.basename(file_path)}::{lang_code}")
        except Exception:
            pass  # файл не прочитался — будет 0 чанков
    return total, task_keys


_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def count_paragraphs(text: str) -> int:
    """Считает абзацы по разделителю blank line. Включая пустые между непустыми."""
    if not text:
        return 0
    return len(_PARAGRAPH_SPLIT_RE.split(text))


def normalize_paragraph_separators(text: str, expected_paragraphs: int) -> str:
    """
    Если модель забыла blank line и слила абзацы через одинарный \\n —
    восстанавливает структуру: при count_paragraphs(\\n\\n)==1 пытается
    split по одинарному \\n, и если число строк совпадает с ожидаемым,
    переформатирует обратно через \\n\\n.

    Возвращает нормализованный текст. Если эвристика не сработала — текст
    возвращается как есть (валидация дальше его всё равно отбракует).
    """
    if not text or expected_paragraphs <= 1:
        return text
    if count_paragraphs(text) >= expected_paragraphs:
        return text  # Уже ок
    # Пробуем по одинарному \n
    lines = text.split("\n")
    # Уберём только хвостовые пустые
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) == expected_paragraphs:
        return "\n\n".join(lines)
    # Иногда модель добавляет лишнюю пустую строку между некоторыми, так что
    # сжимаем кратные переносы: \n{2,} → \n\n; затем split по \n\n
    collapsed = re.sub(r"\n{2,}", "\n\n", text)
    if count_paragraphs(collapsed) == expected_paragraphs:
        return collapsed
    return text


def split_text_to_chunks_by_paragraphs(text: str, max_chars: int) -> Tuple[List[str], List[int]]:
    """
    Разбивает текст на чанки по полным абзацам (\\n\\n) — никогда не разрывает абзац.
    Возвращает (chunks, para_counts), где para_counts[i] — число абзацев в i-м чанке.
    Сохраняет пустые абзацы и порядок для последующего 1:1 alignment.
    """
    paragraphs = _PARAGRAPH_SPLIT_RE.split(text)
    chunks: List[str] = []
    para_counts: List[int] = []
    current: List[str] = []
    current_len = 0
    for p in paragraphs:
        added_len = len(p) + (2 if current else 0)
        if current and current_len + added_len > max_chars:
            chunks.append("\n\n".join(current))
            para_counts.append(len(current))
            current = [p]
            current_len = len(p)
        else:
            current.append(p)
            current_len += added_len
    if current:
        chunks.append("\n\n".join(current))
        para_counts.append(len(current))
    return chunks, para_counts


def _accept_retry_if_paragraphs_ok(
    current: str,
    candidate: str,
    preserve_paragraphs: bool,
    expected_paragraphs: int,
    log_queue,
    chunk_idx: int,
    where: str,
) -> str:
    """
    Принять candidate в качестве extracted ТОЛЬКО если paragraph structure совпадает.
    Иначе — оставить current (он уже прошёл paragraph-validation выше по потоку).

    Защищает ratio-retries (короткие/длинные ответы): новый ответ модели часто
    приходит single-line или с Markdown-line-breaks (`  \\n`), что ломает 1:1
    alignment абзацев.
    """
    if not (preserve_paragraphs and expected_paragraphs > 0):
        return candidate
    norm = normalize_paragraph_separators(candidate, expected_paragraphs)
    actual = count_paragraphs(norm)
    if actual == expected_paragraphs:
        return norm
    log_msg(
        log_queue,
        f"   ⚠️ Chunk {chunk_idx+1}: {where} даёт {actual}/{expected_paragraphs} абзацев — отклоняю, оставляю прежний"
    )
    return current


def split_text_to_chunks(text: str, max_chars: int) -> List[str]:
    """Разбивает текст на части по max_chars символов, не обрезая слова."""
    chunks = []
    current_chunk = ""
    
    sentences = re.split(r'(?<=[.!?])\s+', text)
    
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        
        if len(sentence) > max_chars:
            words = sentence.split()
            for word in words:
                if len(current_chunk) + len(word) + 1 <= max_chars:
                    current_chunk += (" " if current_chunk else "") + word
                else:
                    if current_chunk:
                        chunks.append(current_chunk.strip())
                    current_chunk = word
        else:
            if len(current_chunk) + len(sentence) + 1 <= max_chars:
                current_chunk += (" " if current_chunk else "") + sentence
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = sentence
    
    if current_chunk:
        chunks.append(current_chunk.strip())
    
    return chunks


def analyze_text_structure(
    text: str,
    log_queue: queue.Queue,
    use_subscription: bool = False,
    oauth_token: str = "",
    model: str = "claude-3-5-haiku-20241022",
    api_key: str = "",
    oauth_tokens: Optional[List[str]] = None,
    current_token_index: Optional[List[int]] = None,
) -> Dict:
    """
    Анализирует весь текст ПЕРЕД адаптацией.
    Создаёт структурную карту для качественной адаптации.
    Если use_subscription и передан oauth_token — анализ идёт по Pro подписке (та же модель или переданная).
    
    Возвращает:
    - genre: жанр текста
    - key_scenes: критичные сцены с типами
    - preservation_rules: что ОБЯЗАТЕЛЬНО сохранять
    - chunk_roles: роль каждого чанка в общей структуре
    - expected_ratios: ожидаемые соотношения длин для разных типов
    """
    # Для больших текстов берём начало + середину + конец
    sample_size = 30000  # ~7500 токенов
    if len(text) > sample_size:
        chunk_size = sample_size // 3
        sample = text[:chunk_size] + "\n\n[...]\n\n" + text[len(text)//2-chunk_size//2:len(text)//2+chunk_size//2] + "\n\n[...]\n\n" + text[-chunk_size:]
    else:
        sample = text
    
    analysis_prompt = f"""ANALYZE this text for high-quality adaptation planning.

TEXT LENGTH: {len(text)} characters
SAMPLE (beginning + middle + end):
{sample}

Your task: Understand this text deeply to guide perfect adaptation.

Output JSON ONLY:
{{
  "genre": "psychological_horror|thriller|scifi_documentary|narrative|other",
  "writing_style": "detailed_atmospheric|fast_paced|philosophical|technical|conversational",
  "key_characteristics": [
    "slow_burn tension building",
    "detailed environmental descriptions",
    "internal monologues",
    "philosophical reflections",
    "technical procedures"
  ],
  "critical_elements": [
    "Opening hook: detailed facility description",
    "Character X's internal crisis about Y",
    "Scientific procedure Z explained step-by-step",
    "Dialogue revealing key information",
    "Philosophical reflection on theme W"
  ],
  "content_breakdown": {{
    "dialogue_percentage": 30,
    "description_percentage": 40,
    "action_percentage": 15,
    "reflection_percentage": 15
  }},
  "pacing": "slow|medium|fast",
  "preservation_priority": [
    "CRITICAL: All dialogues word-for-word",
    "CRITICAL: All technical details",
    "CRITICAL: All atmosphere-building descriptions",
    "IMPORTANT: Character internal states",
    "IMPORTANT: Time transitions"
  ],
  "youtube_optimization": {{
    "target_retention": "Detailed content for 8+ minute watch time",
    "engagement_elements": ["suspense", "revelations", "character_depth"],
    "style_notes": "Maintain slow burn for binge-watching"
  }}
}}

Be SPECIFIC about critical_elements - not "dialogues exist" but "dialogue where X reveals Y to Z in lines 500-800"."""

    messages = [{"role": "user", "content": analysis_prompt}]
    
    keys_cfg = get_keys_config()
    has_oauth = bool(oauth_token or (oauth_tokens and len(oauth_tokens) > 0))
    provider = _detect_provider(model, use_subscription and has_oauth)
    if provider == "subscription":
        actual_key = ""
    elif provider == "claude":
        actual_key = api_key or keys_cfg.get("anthropic_api_key", ANTHROPIC_API_KEY)
    else:
        actual_key = api_key or keys_cfg.get("openai_api_key", OPENAI_API_KEY)
    
    def _do_analysis_api_call():
        if use_subscription and oauth_tokens and current_token_index is not None:
            return call_api_universal_with_token_rotation(
                messages=messages,
                api_key=actual_key,
                model=model,
                log_queue=log_queue,
                task_info="text_analysis",
                provider=provider,
                oauth_tokens=oauth_tokens,
                current_token_index=current_token_index,
            )
        return call_api_universal(
            messages=messages,
            api_key=actual_key,
            model=model,
            log_queue=log_queue,
            task_info="text_analysis",
            provider=provider,
            oauth_token=oauth_token,
        )
    
    sem = _get_analysis_semaphore()
    if sem:
        with sem:
            success, response = _do_analysis_api_call()
    else:
        success, response = _do_analysis_api_call()
    
    if not success:
        err_preview = (response[:200] + "…") if response and len(response) > 200 else (response or "пустой ответ")
        log_msg(log_queue, f"⚠️ Analysis failed, using defaults ({err_preview})")
        return {
            "genre": "unknown",
            "writing_style": "detailed",
            "critical_elements": ["Preserve all content completely"],
            "preservation_priority": ["All content equally important"],
            "content_breakdown": {"dialogue_percentage": 25, "description_percentage": 50, "action_percentage": 15, "reflection_percentage": 10},
            "pacing": "medium"
        }
    
    try:
        # Убираем markdown-обёртку ```json ... ```
        text = response.strip()
        if text.startswith("```"):
            first_newline = text.find("\n")
            if first_newline != -1 and ("json" in text[:first_newline].lower() or "```" in text[:first_newline]):
                text = text[first_newline + 1:]
            if text.endswith("```"):
                text = text[:-3].rstrip()
        # Извлекаем только первый JSON-объект (модель иногда добавляет текст после)
        json_start = text.find('{')
        if json_start == -1:
            raise ValueError("No JSON found")
        depth = 0
        json_end = -1
        for i in range(json_start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    json_end = i + 1
                    break
        if json_end == -1:
            # Ответ обрезан — пробуем починить, добавив закрывающие скобки
            snippet = text[json_start:]
            for add in ["}", "}]}", "}]}]}", "}" * min(depth, 10)]:
                try:
                    repaired = snippet + add
                    analysis = json.loads(repaired)
                    log_msg(log_queue, "   (восстановлен обрезанный JSON)")
                    break
                except json.JSONDecodeError:
                    continue
            else:
                raise ValueError("No complete JSON object found")
        else:
            analysis = json.loads(text[json_start:json_end])
        
        log_msg(log_queue, f"📊 Analysis complete:")
        log_msg(log_queue, f"   Genre: {analysis.get('genre', 'unknown')}")
        log_msg(log_queue, f"   Style: {analysis.get('writing_style', 'unknown')}")
        log_msg(log_queue, f"   Critical elements: {len(analysis.get('critical_elements', []))}")
        
        return analysis
        
    except Exception as e:
        log_msg(log_queue, f"⚠️ Failed to parse analysis: {e}, using defaults")
        return {
            "genre": "unknown",
            "writing_style": "detailed",
            "critical_elements": ["Preserve all content"],
            "preservation_priority": ["All content equally important"],
            "content_breakdown": {"dialogue_percentage": 25, "description_percentage": 50, "action_percentage": 15, "reflection_percentage": 10},
            "pacing": "medium"
        }


def _normalize_literal_escapes(text: str) -> str:
    """FIX-2026-06-29: модель иногда ДВАЖДЫ экранирует переносы (\\n\\n в JSON → литеральные \\n\\n
    остаются в тексте после одного json.loads). Narration-текст (TTS) НЕ содержит легитимных
    backslash-escape → конвертируем литеральные \\n \\r \\t в реальные. Быстрый выход если backslash нет."""
    if not text or "\\" not in text:
        return text
    return (text.replace("\\r\\n", "\n").replace("\\n", "\n")
                .replace("\\r", "\n").replace("\\t", " "))


def extract_from_brackets(text: str) -> str:
    """Извлекает текст из квадратных скобок, убирая все логи и служебные сообщения.

    Для основного рерайта мы жёстко просим модель возвращать ВЕСЬ текст
    в одних скобках, без заголовков и чек-листов. Эта функция:
    1) Если находит такие скобки — возвращает самый длинный фрагмент внутри них.
    2) Если скобок нет — делает мягкую очистку от служебных строк.
    """
    if not text:
        return ""
    text = _normalize_literal_escapes(text)   # FIX-2026-06-29: литеральные \n\n → реальные переносы

    # Ищем текст в квадратных скобках
    matches = re.findall(r'\[([^\]]+)\]', text, re.DOTALL)
    if matches:
        # Берём самый длинный матч (обычно это адаптированный текст)
        result = max(matches, key=len).strip()
        if len(result) > 50:  # Если результат разумной длины
            return result
    
    # Fallback: если скобок нет или результат слишком короткий, берём весь текст
    # но убираем возможные префиксы и логи
    cleaned = text.strip()
    
    # Убираем возможные префиксы
    for prefix in ["Output:", "Adapted text:", "Text:", "Result:", "✅", "⚠️", "❌", "🔍", "📊", "💾", "🔑"]:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    
    # Убираем строки, которые выглядят как логи (содержат эмодзи или паттерны логов)
    lines = cleaned.split('\n')
    cleaned_lines = []
    for line in lines:
        line = line.strip()
        # Пропускаем строки с эмодзи или паттернами логов
        if any(emoji in line for emoji in ["✅", "⚠️", "❌", "🔍", "📊", "💾", "🔑", "📈", "🔧"]):
            continue
        # Пропускаем строки с паттернами типа "Chunk X:", "Input:", "Output:"
        if re.match(r'^(Chunk|Input|Output|Ratio|Original|Your output|TASK|OUTPUT):', line, re.IGNORECASE):
            continue
        if line:
            cleaned_lines.append(line)
    
    cleaned = '\n'.join(cleaned_lines).strip()

    return cleaned


# === Эвристическая чистка Claude-output (2026-06-03) ===
# Claude иногда (особенно Sonnet/Opus при свободном рерайте) добавляет структурный мусор:
# «Блок 1», «Часть 2», markdown-заголовки, преамбулы «Вот переписанный текст:».
# Эта функция — НЕ LLM, чистый regex/стоп-лист. Консервативная: при любом сомнении НЕ режет.
# Применяется к каждому чанку Claude-rewrite (не для GPT-пути).

_SANITIZE_BLOCK_MARKER_RE = re.compile(
    r"^\s*(?:Блок|Часть|Глава|Раздел|Chunk|Block|Part|Section|Fragment|"
    r"Díl|Část|Część|Rozdział|Capitol|Capitolul|Kapitel|Bölüm|Kısım|"
    r"Capítulo|Capitolo|Capítol|Kapitola|Glava|Pasaje|Pasaj|Bagian|"
    r"Cabang|Bab|Sezione|Stück|Teil)\s*(?:[№#]\s*)?\d+(?:\s*из\s*\d+)?\s*[:.)]?\s*$",
    re.IGNORECASE | re.UNICODE,
)
_SANITIZE_MD_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")
_SANITIZE_MD_SEPARATOR_RE = re.compile(r"^\s*(?:[-=*_]\s*){3,}\s*$")
_SANITIZE_BOLD_ITALIC_MARKERS_RE = re.compile(r"(\*{1,3}|_{2,3})(.+?)\1")

# Префиксные/постфиксные meta-маркеры в стоп-листе (мультиязык, нижний регистр для match)
_SANITIZE_PREAMBLE_KEYWORDS = (
    "rewritten", "rewrite", "переписан", "переписал", "переписала",
    "version", "вариант", "перевод", "перевела", "перевёл", "translation", "translated",
    "here is", "here's", "вот", "below is", "ниже", "result", "результат",
    "adapted", "адаптир", "ось", "ось переписан",
    "přepsán", "přeložen", "wersja", "tłumacz", "versiune", "tradus",
)
_SANITIZE_TAIL_KEYWORDS = (
    "changes", "изменения", "i rewrote", "я переписал", "я переписала",
    "word count", "количество слов", "summary of changes", "сводка изменений",
    "note:", "примечание:", "footnotes", "сноск",
)


def sanitize_claude_output(text: str, lang_name: str = "") -> Tuple[str, int]:
    """Лёгкая структурная чистка Claude-rewrite output.

    Удаляет: markdown-заголовки/separators, явные block-маркеры (целая строка
    «Блок 1», «Part 3 of 5» и т.п.), короткие meta-преамбулы/хвосты со стоп-словами
    («Вот переписанный текст:» в начале / «Я изменил...» в конце).

    Снимает обёртку **bold** / __italic__ (оставляет текст).
    Схлопывает 3+ подряд пустых строк до 2.

    Returns: (cleaned_text, removed_lines_count).
    Консервативна: при любом сомнении строку оставляем (false-positive хуже false-negative).
    """
    if not text:
        return text, 0
    src = text.strip()
    if not src:
        return text, 0
    src = _normalize_literal_escapes(src)   # FIX-2026-06-29: литеральные \n\n → реальные переносы

    # 1. Если обёрнут [..] — снять (уже есть extract_from_brackets, переиспользуем)
    bracket_matches = re.findall(r'\[([^\]]+)\]', src, re.DOTALL)
    if bracket_matches:
        longest = max(bracket_matches, key=len).strip()
        if len(longest) > 100:  # уверены что это контент, не короткий tag
            src = longest

    lines = src.split("\n")
    removed = 0
    out_lines: List[str] = []

    for line in lines:
        stripped = line.strip()
        # Pure separator (---, ===, ***)
        if _SANITIZE_MD_SEPARATOR_RE.match(stripped):
            removed += 1
            continue
        # Markdown heading (#, ##, ###...)
        if _SANITIZE_MD_HEADING_RE.match(stripped):
            removed += 1
            continue
        # Block marker как отдельная строка
        if _SANITIZE_BLOCK_MARKER_RE.match(stripped):
            removed += 1
            continue
        out_lines.append(line)

    # 2. Снять **bold**/__italic__ маркеры (оставить текст)
    body = "\n".join(out_lines)
    body = _SANITIZE_BOLD_ITALIC_MARKERS_RE.sub(r"\2", body)

    # 3. Преамбула: первые 1-2 короткие meta-строки (<120 chars), заканчиваются ":",
    #    за ними пустая строка, содержат стоп-слово
    body_lines = body.split("\n")
    while body_lines:
        first = body_lines[0].strip()
        if not first:
            body_lines.pop(0)
            continue
        if (len(first) <= 120 and first.endswith(":")
            and len(body_lines) > 1 and not body_lines[1].strip()):
            low = first.lower()
            if any(kw in low for kw in _SANITIZE_PREAMBLE_KEYWORDS):
                body_lines.pop(0)  # remove preamble line
                removed += 1
                # consume blank line after
                if body_lines and not body_lines[0].strip():
                    body_lines.pop(0)
                continue
        break

    # 4. Хвостовые meta-строки: последние 1-2 короткие со стоп-словами
    while body_lines:
        last = body_lines[-1].strip()
        if not last:
            body_lines.pop()
            continue
        if len(last) <= 200:
            low = last.lower()
            if any(kw in low for kw in _SANITIZE_TAIL_KEYWORDS):
                body_lines.pop()
                removed += 1
                continue
        break

    cleaned = "\n".join(body_lines)

    # 5. Схлопнуть 3+ пустых строк → 2 (т.е. макс 1 пустая между блоками)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    return cleaned, removed


def find_sentences_with_cyrillic(text: str) -> List[Tuple[str, int, int]]:
    """
    Находит предложения с кириллицей в тексте.
    Возвращает список кортежей (предложение, начало_позиция, конец_позиция).
    """
    if not text:
        return []
    
    cyrillic_pattern = re.compile(r'[а-яёА-ЯЁ]')
    
    # Находим все позиции с кириллицей
    cyrillic_positions = []
    for match in cyrillic_pattern.finditer(text):
        cyrillic_positions.append(match.start())
    
    if not cyrillic_positions:
        return []
    
    # Находим границы предложений для каждой позиции с кириллицей
    result = []
    processed_ranges = set()  # Чтобы не дублировать предложения
    
    for pos in cyrillic_positions:
        # Ищем начало предложения (ищем назад до точки/восклицательного/вопросительного или начала текста)
        start = pos
        while start > 0 and text[start-1] not in '.!?\n':
            start -= 1
        
        # Ищем конец предложения (ищем вперед до точки/восклицательного/вопросительного или конца текста)
        end = pos
        while end < len(text) and text[end] not in '.!?\n':
            end += 1
        if end < len(text):
            end += 1  # Включаем знак препинания
        
        # Извлекаем предложение
        sentence = text[start:end].strip()
        
        # Проверяем, что в предложении действительно есть кириллица
        if cyrillic_pattern.search(sentence) and (start, end) not in processed_ranges:
            result.append((sentence, start, end))
            processed_ranges.add((start, end))
    
    # Сортируем по позиции начала
    result.sort(key=lambda x: x[1])
    
    return result


def fix_sentence_with_4o_mini(sentence: str, lang_name: str, api_key: str, 
                                log_queue: queue.Queue) -> Optional[str]:
    """
    Исправляет предложение с русскими словами через 4o-mini.
    Заменяет русские слова на правильные слова целевого языка.
    ВАЖНО: Возвращает ТОЛЬКО исправленное предложение в квадратных скобках.
    """
    if not sentence:
        return None
    
    # Очищаем предложение от лишних пробелов
    sentence = sentence.strip()
    
    system_prompt = f"""You are a translator. The input contains a MIXED sentence with Russian (Cyrillic) and {lang_name} fragments.

YOUR TASK: Output the ENTIRE sentence in 100% pure {lang_name}. Every Cyrillic character MUST be gone.

PROCESS:
1. Read the input sentence fully
2. Translate ALL Russian parts into natural {lang_name}, keeping meaning exact
3. Keep existing correct {lang_name} parts as they are
4. Combine into one fluent {lang_name} sentence

RULES:
- Zero Cyrillic in output
- Numbers/dates stay identical (1943, 88mm, etc. — unchanged)
- Proper names: use native {lang_name} spelling
- Return ONLY the result in [square brackets] — no explanations

Input: "Он смотрел na niebo и думал, что życie jest krótkie."
Output: [Patrzył na niebo i myślał, że życie jest krótkie.]

Input: "Every шотландский cat nie rozumie gospodara."
Output: [Każdy szkocki kot nie rozumie gospodarza.]"""

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Fix this sentence and return ONLY in square brackets:\n\n{sentence}"}
        ]
        
        # Используем модель проверки из настроек
        success, response = call_openai_api(
            messages=messages,
            model=get_postprocess_model(),
            api_key=api_key,
            log_queue=log_queue,
            task_info="fix_russian_words",
            service_tier=POSTPROCESS_SERVICE_TIER,
        )
        
        if success:
            fixed = extract_from_brackets(response)
            
            # Проверяем на артефакты (повторения, странные символы)
            if fixed and len(fixed) > 0:
                # CRITICAL: проверяем, что ВСЕ числа (годы, калибры и т.п.) совпадают
                orig_nums = re.findall(r'\d+', sentence)
                fixed_nums = re.findall(r'\d+', fixed)
                if orig_nums != fixed_nums:
                    if log_queue:
                        log_msg(
                            log_queue,
                            f"    ⚠️ Числа изменились при постобработке (orig={orig_nums}, fixed={fixed_nums}), откатываю фикс"
                        )
                    return None
                # Проверяем на повторения (например, "sebou sebou sebou")
                words = fixed.split()
                if len(words) > 3:
                    # Проверяем, нет ли повторяющихся слов подряд
                    for i in range(len(words) - 2):
                        if words[i] == words[i+1] == words[i+2]:
                            if log_queue:
                                log_msg(log_queue, f"    ⚠️ Обнаружены повторения в ответе, пропускаю")
                            return None
                
                # Проверяем, что кириллицы стало меньше или равно нулю
                cyrillic_in_original = len(re.findall(r'[а-яёА-ЯЁ]', sentence))
                cyrillic_in_fixed = len(re.findall(r'[а-яёА-ЯЁ]', fixed))
                
                # Проверяем, что длина не изменилась кардинально (не более чем в 2 раза)
                if len(fixed) > len(sentence) * 2 or len(fixed) < len(sentence) * 0.3:
                    if log_queue:
                        log_msg(log_queue, f"    ⚠️ Длина изменилась слишком сильно, пропускаю")
                    return None
                
                if cyrillic_in_fixed < cyrillic_in_original:
                    if log_queue:
                        log_msg(log_queue, f"    ✅ Исправлено: {cyrillic_in_original} → {cyrillic_in_fixed} русских символов")
                    return fixed
                elif cyrillic_in_fixed == 0:
                    if log_queue:
                        log_msg(log_queue, f"    ✅ Все русские слова заменены")
                    return fixed
                else:
                    if log_queue:
                        log_msg(log_queue, f"    ⚠️ Частично исправлено: {cyrillic_in_original} → {cyrillic_in_fixed} русских символов")
                    return fixed
        else:
            if log_queue:
                log_msg(log_queue, f"    ⚠️ Не удалось исправить через {get_postprocess_model()}: {response[:80]}")
    except Exception as e:
        if log_queue:
            log_msg(log_queue, f"    ⚠️ Ошибка при исправлении через {get_postprocess_model()}: {str(e)[:100]}")
    
    return None


def postprocess_remove_russian_words(text: str, lang_name: str, api_key: str,
                                     log_queue: queue.Queue) -> str:
    """
    Постобработка текста: находит предложения с кириллицей и заменяет их через 4o-mini.
    Это гарантирует отсутствие русских слов в финальном тексте.
    ВАЖНО: Обрабатывает весь текст ПОСЛЕ того как он готов, заменяет предложения корректно.
    Не вызывается для языков, где кириллица — целевой алфавит (русский/украинский/белорусский/болгарский/македонский).
    Сербский — пишется на латинице (целевой выбор), поэтому проверяется на общих основаниях.
    """
    if not text:
        return text
    # Для языков, где кириллица — норма, не «исправляем» её
    if lang_name and lang_name.lower() in ("russian", "ukrainian", "belarusian", "bulgarian", "macedonian"):
        return text

    # Находим все предложения с кириллицей (теперь с началом и концом)
    sentences_with_cyrillic = find_sentences_with_cyrillic(text)
    
    if not sentences_with_cyrillic:
        return text  # Нет русских слов, возвращаем как есть
    
    if log_queue:
        log_msg(log_queue, f"    🔍 Найдено {len(sentences_with_cyrillic)} предложений с кириллицей")
        log_msg(log_queue, f"    🔧 Запрашиваю исправление через {get_postprocess_model()}...")
    
    # Создаём список замен (позиция начала, позиция конца, исправленный текст)
    replacements = []
    
    # Обрабатываем каждое предложение (до 3 попыток при неудаче)
    MAX_RUSSIAN_FIX_RETRIES = 3
    for sentence, start_pos, end_pos in sentences_with_cyrillic:
        fixed = None
        for attempt in range(MAX_RUSSIAN_FIX_RETRIES):
            fixed = fix_sentence_with_4o_mini(sentence, lang_name, api_key, log_queue)
            if fixed and fixed != sentence:
                break
            if attempt < MAX_RUSSIAN_FIX_RETRIES - 1 and log_queue:
                log_msg(log_queue, f"    🔄 Повтор {attempt + 2}/{MAX_RUSSIAN_FIX_RETRIES} для предложения с кириллицей...")
        if fixed and fixed != sentence:  # Только если исправление успешно и отличается
            # Проверяем, что исправленный текст не содержит артефактов
            if len(fixed) > 0 and len(fixed) < len(sentence) * 3:  # Не более чем в 3 раза длиннее
                replacements.append((start_pos, end_pos, fixed))
    
    # Применяем замены в обратном порядке (чтобы позиции не сбивались)
    result_text = text
    for start_pos, end_pos, fixed in reversed(replacements):
        # Проверяем, что позиции валидны
        if 0 <= start_pos < len(result_text) and start_pos < end_pos <= len(result_text):
            # Заменяем предложение
            result_text = result_text[:start_pos] + fixed + result_text[end_pos:]
    
    # Финальная проверка на оставшуюся кириллицу
    remaining_cyr = len(re.findall(r'[а-яёА-ЯЁ]', result_text))
    if remaining_cyr > 0:
        if log_queue:
            log_msg(log_queue, f"    ⚠️ После обработки осталось {remaining_cyr} русских символов")
    else:
        if log_queue:
            log_msg(log_queue, f"    ✅ Все русские слова успешно заменены")

    # Дополнительная защита от артефактов: схлопываем безумные повторы одного и того же слова
    # Например: "превращались превращались превращались ..." → "превращались"
    def _collapse_repeats(match: re.Match) -> str:
        return match.group(1)

    # Три и более одинаковых слова подряд (регистр игнорируем)
    result_text = re.sub(r'\b(\w+)(?:\s+\1){2,}\b', _collapse_repeats, result_text, flags=re.IGNORECASE)
    
    return result_text


# ============== ПОВТОРНАЯ ПРОВЕРКА ПЕРЕВОДА (GPT-4o-mini) ==============

def _count_sentences(text: str) -> int:
    """Приблизительное число предложений по точкам, !, ?."""
    if not text or not text.strip():
        return 0
    return max(1, text.count(".") + text.count("!") + text.count("?"))


def _native_speaker_fix_chunk(
    chunk_text: str,
    lang_name: str,
    api_key: str,
    log_queue: queue.Queue,
    original_source_chunk: Optional[str] = None,
) -> str:
    """
    Отправляет один чанк перевода в GPT-4o-mini: находит некорректный/неестественный
    перевод и возвращает исправленный текст. Если передан original_source_chunk,
    в промпт добавляется оригинал с требованием не терять факты и объём.
    """
    if not chunk_text or len(chunk_text.strip()) < 20:
        return chunk_text

    system_prompt = f"""You are a native {lang_name} speaker and a professional editor. Your job is TRANSLATION NATIVENESS CHECK: verify the whole text and fix anything that is not native-quality.
- Preserve ALL facts, numbers, names, and meaning exactly.
- Fix awkward phrasing, calques, and machine-translation artifacts.
- Make it sound as a native {lang_name} speaker would write or say it.
- Do NOT add new content or remove existing content."""
    if original_source_chunk:
        system_prompt += """

CRITICAL: You will be given the ORIGINAL source text and the TRANSLATION. Your output must preserve ALL facts and the full volume of the ORIGINAL — do not shorten, omit, or summarize. Every fact and idea from the original must remain in your corrected translation."""
    system_prompt += """

EDITOR CHECKLIST (verify and fix):
1. NUMBERS AND DATES IN WORDS: All numbers, years, dates, and centuries must be written IN WORDS in """ + lang_name + """, not digits or "19th"/"XIX". If you see "19th century" or "1800s" — replace with the correct phrase in words (e.g. "nineteenth century" in """ + lang_name + """). "1800s" = nineteenth century, correctly translated in words (not 18th).
2. TECHNICAL (UNITS): Convert feet/inches to metric by RECALCULATING: 1 ft = 0.3048 m (1 m = 3.28 ft), 1 in = 2.54 cm. E.g. "8 feet" → 2.44 m → "about two and a half meters"; "10 feet" → ~3 m. Never keep the digit and only swap unit; never use "feet" as body parts.
3. TERMINOLOGY: Replace mistranslations: "Cabinet cards" → cabinet-format photos / cabinet portraits; "Tintype" / "color tint" → ferrotypes/tintypes where it means photo on metal.
4. AUDIO: Rephrase list-like or robotic sentences for natural flow and voice-over.
5. CULTURAL: Remove or fix misplaced local names/toponyms that don't belong to the target audience's country.
6. IDIOMS: Replace literal calques with natural """ + lang_name + """ equivalents.

After checking everything, output ONLY the corrected text, wrapped in square brackets [ ]. No explanations."""

    if original_source_chunk and original_source_chunk.strip():
        user_content = f"""You are given the ORIGINAL source text and its TRANSLATION into {lang_name}. Correct the translation for nativeness (units, terminology, dates in words, rhythm, idioms). Do NOT lose any facts or reduce the volume relative to the ORIGINAL. Output ONLY the corrected {lang_name} text in square brackets [ ].

ORIGINAL (source):
{original_source_chunk.strip()}

TRANSLATION (to correct):
{chunk_text}

Output ONLY the corrected translation in [ ]."""
    else:
        user_content = f"""Check this {lang_name} text for translation nativeness. Fix: dates/centuries in words (1800s → nineteenth century in {lang_name}), units, terminology, rhythm, cultural. Then output ONLY the corrected text in square brackets [ ].

Text:
{chunk_text}"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]

    success, response = call_openai_api(
        messages=messages,
        model=get_postprocess_model(),
        api_key=api_key,
        log_queue=log_queue,
        task_info="native_check",
        service_tier=POSTPROCESS_SERVICE_TIER,
    )

    if success and response:
        fixed = extract_from_brackets(response)
        if fixed and len(fixed.strip()) > 10:
            # При наличии оригинала проверка длины по оригиналу — в postprocess
            if original_source_chunk:
                return fixed
            # Без оригинала — не принимать сильное укорочение относительно перевода
            if len(fixed) >= len(chunk_text) * 0.85:
                return fixed
    return chunk_text


def postprocess_native_speaker_check_by_chunks(
    results_list: List[str],
    lang_name: str,
    api_key: str,
    log_queue: queue.Queue,
    original_chunks: Optional[List[str]] = None,
    log_file_path: Optional[str] = None,
) -> List[str]:
    """
    Повторная проверка сгенерированного текста по чанкам через GPT-4o-mini.
    Если передан original_chunks — исправленный текст сравнивается с оригиналом по длине/предложениям;
    при сильной потере правка отклоняется. Результаты пишутся в log_file_path.
    """
    if not ENABLE_NATIVE_SPEAKER_CHECK or not results_list:
        return results_list

    use_original = (
        original_chunks is not None
        and len(original_chunks) == len(results_list)
    )
    log_entries: List[Dict] = []

    fixed_list = []
    for i, chunk in enumerate(results_list):
        if STOP_FLAG.is_stopped():
            fixed_list.extend(results_list[len(fixed_list):])
            break
        if chunk.strip().startswith("[ОШИБКА:"):
            fixed_list.append(chunk)
            if log_file_path:
                log_entries.append({
                    "chunk": i + 1,
                    "status": "skipped",
                    "reason": "chunk is error placeholder",
                })
            continue

        orig_src = original_chunks[i] if use_original else None
        if log_queue:
            log_msg(log_queue, f"   🔎 {lang_name}: проверка чанка {i+1}/{len(results_list)} (GPT-4o-mini)...")
        corrected = _native_speaker_fix_chunk(
            chunk, lang_name, api_key, log_queue, original_source_chunk=orig_src
        )

        orig_len = len(orig_src) if orig_src else 0
        trans_len = len(chunk)
        fixed_len = len(corrected)
        orig_sent = _count_sentences(orig_src) if orig_src else 0
        fixed_sent = _count_sentences(corrected)

        accepted = True
        reason = "accepted"

        if use_original and orig_src and orig_len > 0:
            ratio_len = fixed_len / orig_len
            ratio_sent = fixed_sent / orig_sent if orig_sent > 0 else 1.0
            if ratio_len < MIN_FIXED_VS_ORIGINAL_RATIO:
                accepted = False
                reason = f"rejected: fixed length {fixed_len} is {ratio_len:.1%} of original {orig_len} (min {MIN_FIXED_VS_ORIGINAL_RATIO:.0%})"
                corrected = chunk
            elif orig_sent > 0 and ratio_sent < MIN_FIXED_SENTENCES_VS_ORIGINAL_RATIO:
                accepted = False
                reason = f"rejected: fixed sentences {fixed_sent} is {ratio_sent:.1%} of original {orig_sent} (min {MIN_FIXED_SENTENCES_VS_ORIGINAL_RATIO:.0%})"
                corrected = chunk
            else:
                reason = f"accepted (fixed {ratio_len:.1%} of original length, sentences {fixed_sent}/{orig_sent})"

        fixed_list.append(corrected)

        if log_file_path:
            entry = {
                "chunk": i + 1,
                "orig_len": orig_len,
                "trans_len": trans_len,
                "fixed_len": fixed_len,
                "orig_sentences": orig_sent,
                "fixed_sentences": fixed_sent,
                "accepted": accepted,
                "reason": reason,
            }
            if not accepted and chunk != corrected:
                entry["note"] = "kept translation (GPT edit rejected)"
            if accepted and chunk != corrected:
                # Превью «было → стало» для наглядности правок GPT
                trans_preview = (chunk.strip()[:200] + "…") if len(chunk) > 200 else chunk.strip()
                fixed_preview = (corrected.strip()[:200] + "…") if len(corrected) > 200 else corrected.strip()
                if trans_preview != fixed_preview:
                    entry["trans_preview"] = trans_preview
                    entry["fixed_preview"] = fixed_preview
            log_entries.append(entry)

    if log_file_path and log_entries:
        try:
            lines = [
                "",
                "=== Native speaker check (GPT-4o-mini) ===",
                f"Language: {lang_name}",
                f"Chunks: {len(results_list)}",
                f"Comparison with original: {'yes' if use_original else 'no'}",
                "",
            ]
            for e in log_entries:
                if e.get("status") == "skipped":
                    lines.append(f"Chunk {e['chunk']}: skipped ({e.get('reason', '')})")
                else:
                    lines.append(
                        f"Chunk {e['chunk']}: orig={e.get('orig_len', 0)} chars, "
                        f"trans={e.get('trans_len', 0)} chars, fixed={e.get('fixed_len', 0)} chars | "
                        f"sentences orig={e.get('orig_sentences', 0)} fixed={e.get('fixed_sentences', 0)} | "
                        f"{e.get('reason', '')}"
                    )
                    if e.get("note"):
                        lines.append(f"  -> {e['note']}")
                    if e.get("trans_preview") is not None:
                        lines.append("  [BEFORE] " + (e["trans_preview"].replace("\n", " ")))
                        lines.append("  [AFTER]  " + (e["fixed_preview"].replace("\n", " ")))
                lines.append("")
            with open(log_file_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            if log_queue:
                log_msg(log_queue, f"   📄 Лог проверки: {os.path.basename(log_file_path)}")
        except Exception as e:
            if log_queue:
                log_msg(log_queue, f"   ⚠️ Не удалось записать лог: {e}")

    return fixed_list


# ============== 3-СТУПЕНЧАТЫЙ ПОСТПРОЦЕСС (Pass A/B/C) ==============

# Модель для постобработки (Pass A/B/C/D) — по умолчанию gpt-5-mini, настраивается в «Настройки ключей»
POSTPROCESS_MODEL = "gpt-5-mini"
POSTPROCESS_SERVICE_TIER = "flex"


def get_postprocess_model() -> str:
    """Модель для проверки (Pass A/B/C/D, native speaker check). Читается из настроек ключей."""
    return get_keys_config().get("postprocess_check_model", POSTPROCESS_MODEL) or POSTPROCESS_MODEL


def get_postprocess_openai_key() -> str:
    """OpenAI API ключ для постпроцесса (Pass A/B/C/D).
    Берётся из per-user сохранённых настроек; env-fallback ТОЛЬКО для админа (env_openai_key_for).
    2026-06-04: ENV-ключ сервера больше НЕ достаётся не-админам."""
    cfg = get_keys_config()
    return (cfg.get("openai_api_key") or "").strip() or env_openai_key_for(_resolve_user_id(""))


# Лимит замен на пасс (избыток ведёт к ошибкам и конфликтам)
MAX_REPLACEMENTS = {"B": 15, "C": 12, "D": 8}

# Минимальная доля длины после Pass B/C — иначе откат к предыдущей версии
MIN_POSTPROCESS_LENGTH_RATIO = 0.85
# Верхний порог длины чанка: при ratio > этого значения — retry. Русский при рерайте с английского естественно расширяется на 15–25%, поэтому 1.40, а не 1.25.
MAX_CHUNK_LENGTH_RATIO = 1.40
# Per-paragraph safety-net (защита от спорадического раздувания одного абзаца ×10-20):
# если len(out_para) > LENGTH_RATIO_LIMIT * max(len(src_para), LENGTH_FLOOR) — length retry.
# LENGTH_FLOOR защищает короткие source-параграфы от ложных срабатываний.
# LENGTH_RATIO_LIMIT=2.0: ловит ×3+ катастрофы, не триггерит на естественное расширение
# RU/DE/TR (диакритики, длиннее английского — 1.3-1.6× нормально).
LENGTH_RATIO_LIMIT = 2.0
LENGTH_FLOOR = 30
MAX_LENGTH_RETRIES = 3

# Fallback intro/outro по языкам (если 4o-mini не вернул suggested_intro/outro)
INTRO_TEMPLATES = {
    "czech": "Jsem {host}, a toto je {channel}.",
    "polish": "Jestem {host}, a to jest {channel}.",
    "romanian": "Sunt {host}, iar acesta este {channel}.",
    "turkish": "Ben {host}, ve bu {channel}.",
    "russian": "Меня зовут {host}, и это {channel}.",
    "ukrainian": "З вами {host}, і це {channel}.",
    "english": "With you is {host}. This is {channel}.",
    "german": "Bei euch ist {host}. Das ist {channel}.",
    "french": "Avec vous, {host}. Voici {channel}.",
    "spanish": "Con vosotros, {host}. Esto es {channel}.",
    "italian": "Con voi è {host}. Questo è {channel}.",
}
OUTRO_TEMPLATES = {
    "czech": "S vámi byl {host}. Uvidíme se v příštím díle {channel}. Podpořte kanál lajkem.",
    "polish": "Z tobą był {host}. Do zobaczenia w kolejnym odcinku {channel}. Wesprzyj kanał lajkiem.",
    "romanian": "Cu tine a fost {host}. Ne vedem în episodul următor al {channel}. Sprijiniți canalul cu un like.",
    "turkish": "Sizinle birlikte olan {host}. {channel} bir sonraki bölümde görüşürüz. Beğeni ile destek olun.",
    "russian": "С вами был {host}. Увидимся в следующем выпуске {channel}. Поддержите канал лайком.",
    "ukrainian": "З вами був {host}. Побачимось у наступному випуску {channel}. Підтримайте канал лайком.",
    "english": "With you was {host}. See you in the next episode of {channel}. Support the channel with a like.",
    "german": "Bei euch war {host}. Bis zur nächsten Folge von {channel}. Unterstützt den Kanal mit einem Like.",
    "french": "Avec vous était {host}. À bientôt pour le prochain épisode de {channel}. Soutenez la chaîne avec un like.",
    "spanish": "Con vosotros estaba {host}. Nos vemos en el próximo episodio de {channel}. Apoya el canal con un like.",
    "italian": "Con voi c'era {host}. Ci vediamo nel prossimo episodio di {channel}. Sostieni il canale con un like.",
}
# Короткий CTA в середине текста (подписка/лайк)
MID_CTA_TEMPLATES = {
    "czech": "Přátelé, malá prosba: podpořte {channel} lajkem a odběrem, pomáhá to kanálu růst.",
    "polish": "Przyjaciele, mała prośba: wesprzyjcie {channel} lajkiem i subskrypcją, to pomaga kanałowi rosnąć.",
    "romanian": "Prieteni, o mică rugăminte: sprijiniți {channel} cu un like și abonare, ajută canalul să crească.",
    "turkish": "Arkadaşlar, küçük bir ricam var: {channel} kanalını beğeni ve abone olarak destekleyin.",
    "russian": "Друзья, небольшая просьба: поддержите канал {channel} лайком и подпиской, это помогает развиваться.",
    "ukrainian": "Друзі, невелика просьба: підтримайте канал {channel} лайком та підпискою.",
    "english": "Friends, a small request: support {channel} with a like and subscribe, it helps the channel grow.",
    "german": "Freunde, eine kleine Bitte: unterstützt {channel} mit einem Like und Abo.",
    "french": "Amis, une petite demande : soutenez {channel} avec un like et un abonnement.",
    "spanish": "Amigos, una pequeña petición: apoyad {channel} con un like y suscripción.",
    "italian": "Amici, una piccola richiesta: sostenete {channel} con un like e iscrizione.",
}


def _lang_key(lang_name: str) -> str:
    """Нормализует имя языка для ключа в INTRO/OUTRO_TEMPLATES."""
    return (lang_name or "").strip().lower().split("(")[0].strip()


def _length_guard(
    original: str,
    processed: str,
    pass_name: str,
    log_queue: queue.Queue,
    min_ratio: float = MIN_POSTPROCESS_LENGTH_RATIO,
) -> str:
    """Если processed короче original более чем на (1 - min_ratio), откат к original."""
    if not original or not processed:
        return original or processed
    ratio = len(processed) / len(original) if len(original) > 0 else 1.0
    if ratio < min_ratio:
        log_msg(log_queue, f"   ⚠️ {pass_name}: текст укоротился до {ratio:.0%}, откат")
        return original
    return processed


def merge_branding(
    text: str,
    audit_json: Dict,
    host_name: str,
    channel_name: str,
    lang_name: str,
    main_character_name: str = "",
    log_queue: Optional[queue.Queue] = None,
    skip_name_replacement: bool = False,
) -> str:
    """
    Применяет фиксы брендинга к тексту по результатам Pass A (Branding Audit).
    Замена старых имён/каналов, вставка intro/outro при отсутствии.
    
    ВАЖНО: Разграничение имён:
    - host_name: имя автора канала/диктора (используется в intro/outro для брендинга)
    - main_character_name: имя главного героя истории (НЕ заменяется на host_name)
    
    Args:
        main_character_name: Имя главного героя истории (НЕ заменяется на host_name).
                            Если не указано, определяется автоматически по частоте упоминаний
                            (имя, которое встречается >10 раз, считается именем главного героя).
    """
    if not audit_json:
        return text

    host = (host_name or "").strip()
    channel = (channel_name or "").strip()
    main_char = (main_character_name or "").strip()

    # 1. Замена старых имён автора/канала на host_name (только целые слова)
    # ДЛЯ ГЕНЕРАЦИИ: замену имен ОТКЛЮЧАЕМ полностью - имена персонажей не должны заменяться
    # ДЛЯ РЕРАЙТА: заменяем только редкие имена в intro/outro зонах
    if skip_name_replacement:
        if log_queue:
            log_msg(log_queue, "   ⚠️ Замена имен ОТКЛЮЧЕНА (это генерация, не рерайт)")
    elif host:
        for old_name in audit_json.get("old_names_found") or []:
            on = str(old_name).strip()
            if not on or on.lower() == host.lower():
                continue
            
            # Проверяем частоту упоминаний в тексте
            count = len(re.findall(r"\b" + re.escape(on) + r"\b", text, re.IGNORECASE))
            
            # КРИТИЧЕСКИЙ ФИЛЬТР: если имя встречается >3 раз, это скорее всего имя персонажа
            # Имя ведущего встречается максимум 1-2 раза (в intro/outro, которые еще не вставлены)
            # Если имя встречается 3+ раза - это точно персонаж истории, не заменяем!
            if count > 3:
                if log_queue:
                    log_msg(log_queue, f"   ⚠️ Пропущено имя '{on}' (встречается {count} раз - это имя ПЕРСОНАЖА, не ведущего)")
                continue
            
            # Если указано имя главного героя и оно совпадает - не заменяем
            if main_char and (on.lower() == main_char.lower() or main_char.lower() in on.lower() or on.lower() in main_char.lower()):
                if log_queue:
                    log_msg(log_queue, f"   ⚠️ Пропущено имя '{on}' (главный герой: {main_char})")
                continue
            
            # Проверяем контекст: если имя используется как персонаж (в диалогах, описаниях действий),
            # это НЕ имя ведущего. Имя ведущего должно быть только в предложениях от первого лица
            # типа "Я, [имя], расскажу..." или "Меня зовут [имя]"
            name_pattern = r"\b" + re.escape(on) + r"\b"
            matches = list(re.finditer(name_pattern, text, re.IGNORECASE))
            
            # Если имя встречается в контексте, где оно явно персонаж (перед глаголом действия,
            # в диалогах, в описаниях), пропускаем
            is_character_name = False
            for match in matches:
                start, end = match.span()
                # Берем контекст вокруг имени (50 символов до и после)
                context_start = max(0, start - 50)
                context_end = min(len(text), end + 50)
                context = text[context_start:context_end].lower()
                
                # Если имя используется как подлежащее с глаголом действия (персонаж делает что-то)
                # или в диалогах - это персонаж, не ведущий
                # Универсальные индикаторы для всех языков: глаголы действия, диалоги, описания действий
                action_indicators = [
                    # Диалоги (универсально для всех языков)
                    ' — ', ' "', ' „', ' "', ' „', '"', "'", '«', '»',
                    # Паттерны: имя + глагол (универсально)
                    on.lower() + ' said', on.lower() + ' says', on.lower() + ' told',
                    on.lower() + ' looked', on.lower() + ' looks', on.lower() + ' sat',
                    on.lower() + ' sits', on.lower() + ' stood', on.lower() + ' stands',
                    on.lower() + ' went', on.lower() + ' goes', on.lower() + ' came',
                    on.lower() + ' comes', on.lower() + ' took', on.lower() + ' takes',
                    on.lower() + ' put', on.lower() + ' puts', on.lower() + ' did',
                    on.lower() + ' does', on.lower() + ' made', on.lower() + ' makes',
                    # Русский
                    on.lower() + ' сказал', on.lower() + ' сказала', on.lower() + ' говорит',
                    on.lower() + ' смотрел', on.lower() + ' смотрела', on.lower() + ' сидел',
                    on.lower() + ' сидела', on.lower() + ' встал', on.lower() + ' встала',
                    # Польский
                    on.lower() + ' powiedział', on.lower() + ' powiedziała', on.lower() + ' mówi',
                    on.lower() + ' patrzy', on.lower() + ' patrzył', on.lower() + ' siedzi',
                    on.lower() + ' siedział', on.lower() + ' wstał', on.lower() + ' wstała',
                    # Чешский
                    on.lower() + ' řekl', on.lower() + ' řekla', on.lower() + ' říká',
                    on.lower() + ' seděl', on.lower() + ' seděla', on.lower() + ' vstal',
                    # Румынский
                    on.lower() + ' a spus', on.lower() + ' a zis', on.lower() + ' spune',
                    # Общие паттерны действий
                    ' ' + on.lower() + ' ',  # имя как отдельное слово в предложении (скорее персонаж)
                ]
                
                if any(indicator in context for indicator in action_indicators):
                    is_character_name = True
                    break
                
                # Дополнительная проверка: если имя стоит перед запятой/двоеточием и после него глагол - это персонаж
                # Паттерн: "[Name], [verb]" или "[Name]: [verb]"
                name_pos_in_context = context.find(on.lower())
                if name_pos_in_context > 0:
                    after_name = context[name_pos_in_context + len(on):name_pos_in_context + len(on) + 20]
                    if any(marker in after_name[:10] for marker in [',', ':', ' —', ' "', ' „']):
                        is_character_name = True
                        break
            
            if is_character_name:
                if log_queue:
                    log_msg(log_queue, f"   ⚠️ Пропущено имя '{on}' (используется как имя персонажа в действиях/диалогах)")
                continue
            
            # Если имя встречается редко (≤5 раз) и не в контексте персонажа,
            # заменяем его (это может быть старое имя ведущего в тексте)
            # НО заменяем ТОЛЬКО если оно в начале или конце текста (где может быть intro/outro)
            text_len = len(text)
            intro_zone_end = max(300, int(text_len * 0.08))  # Первые 8% или минимум 300 символов
            outro_zone_start = max(0, text_len - int(text_len * 0.08))  # Последние 8%
            
            intro_text = text[:intro_zone_end]
            middle_text = text[intro_zone_end:outro_zone_start]
            outro_text = text[outro_zone_start:]
            
            # Заменяем ТОЛЬКО в intro и outro зонах
            intro_replaced = re.sub(name_pattern, host, intro_text, flags=re.IGNORECASE)
            outro_replaced = re.sub(name_pattern, host, outro_text, flags=re.IGNORECASE)
            
            # Если были замены в intro/outro, обновляем текст
            if intro_replaced != intro_text or outro_replaced != outro_text:
                text = intro_replaced + middle_text + outro_replaced
                replaced_count = len(re.findall(r"\b" + re.escape(host) + r"\b", intro_replaced + outro_replaced, re.IGNORECASE)) - len(re.findall(r"\b" + re.escape(host) + r"\b", intro_text + outro_text, re.IGNORECASE))
                if log_queue:
                    log_msg(log_queue, f"   ✓ Заменено имя '{on}' → '{host}' в intro/outro зонах ({replaced_count} замен)")
            else:
                if log_queue:
                    log_msg(log_queue, f"   ⚠️ Имя '{on}' не найдено в intro/outro зонах, пропущено")

    # 2. Замена старых названий канала (целые слова / фразы)
    for old_channel in audit_json.get("old_channels_found") or []:
        oc = str(old_channel).strip()
        if oc and oc.lower() != channel.lower():
            text = re.sub(r"\b" + re.escape(oc) + r"\b", channel or "", text)

    # 3. Нет intro — вставляем около 30% текста (после ближайшего параграфа)
    if not audit_json.get("has_intro") and (host or channel):
        suggested = audit_json.get("suggested_intro", "").strip()
        if suggested:
            intro_line = suggested
        else:
            key = _lang_key(lang_name)
            tpl = INTRO_TEMPLATES.get(key) or INTRO_TEMPLATES.get("english", "With you is {host}. This is {channel}.")
            intro_line = tpl.format(host=host or "the host", channel=channel or "the channel")
        target_pos = min(2000, len(text))
        paragraphs = text.split("\n\n")
        if len(paragraphs) <= 1:
            text = intro_line + "\n\n" + text
        else:
            pos, cum = 0, 0
            for i, p in enumerate(paragraphs):
                next_cum = cum + len(p) + 2
                if next_cum <= target_pos:
                    pos = i + 1
                    cum = next_cum
                else:
                    pos = i
                    break
            else:
                pos = len(paragraphs)
            pos = max(1, pos)  # всегда после минимум 1 параграфа (хука)
            paragraphs.insert(pos, intro_line)
            text = "\n\n".join(paragraphs)

    # 4. Нет mid-CTA — вставляем в середину текста (около 40–50%, по границе параграфа)
    if not audit_json.get("has_mid_cta") and channel:
        suggested = audit_json.get("suggested_mid_cta", "").strip()
        if suggested:
            mid_cta_line = suggested
        else:
            key = _lang_key(lang_name)
            tpl = MID_CTA_TEMPLATES.get(key) or MID_CTA_TEMPLATES.get("english", "Friends, a small request: support {channel} with a like and subscribe, it helps the channel grow.")
            mid_cta_line = tpl.format(channel=channel)
        paragraphs = text.split("\n\n")
        if len(paragraphs) >= 3:
            # Вставляем после параграфа, ближайшего к 45% длины
            target_ratio = 0.45
            cum, pos = 0, 0
            total_len = len(text)
            for i, p in enumerate(paragraphs):
                cum += len(p) + 2
                if total_len > 0 and cum / total_len <= target_ratio:
                    pos = i + 1
                else:
                    break
            pos = max(1, min(pos, len(paragraphs) - 1))  # не в самое начало и не в конец
            paragraphs.insert(pos, mid_cta_line)
            text = "\n\n".join(paragraphs)

    # 5. Нет outro — добавляем в конец (fallback по языку)
    if not audit_json.get("has_outro") and (host or channel):
        suggested = audit_json.get("suggested_outro", "").strip()
        if suggested:
            outro_line = suggested
        else:
            key = _lang_key(lang_name)
            tpl = OUTRO_TEMPLATES.get(key) or OUTRO_TEMPLATES.get("english", "With you was {host}. See you in the next episode of {channel}. Support the channel with a like.")
            outro_line = tpl.format(host=host or "the host", channel=channel or "the channel")
        text = text.rstrip() + "\n\n" + outro_line

    return text


def _postprocess_pass_a_branding_audit(
    text: str, lang_name: str, host_name: str, channel_name: str,
    api_key: str, log_queue: queue.Queue,
) -> Tuple[bool, Optional[Dict]]:
    """Pass A: только анализ, возвращает JSON с брендингом."""
    prompt = f"""You are a branding compliance checker.

INPUT: A YouTube script in {lang_name}.
EXPECTED BRANDING:
- Host name: "{host_name or "(not set)"}"
- Channel name: "{channel_name or "(not set)"}"

CRITICAL RULES FOR "old_names_found":
1. Include ONLY names that appear in INTRO/OUTRO sections or in first-person narrator statements like:
   - "I am [name]" / "My name is [name]" / "I, [name], will tell you..."
   - Names in intro/outro sentences
   - Names used as the narrator/host identity

2. DO NOT include character names from the story, even if they appear frequently:
   - If "Tom" appears 10+ times as a character doing actions → DO NOT include "Tom"
   - If "Piotr Zawadzki" is mentioned as a person in the story → DO NOT include "Piotr Zawadzki"
   - If "Tomek" is used in dialogues or action descriptions → DO NOT include "Tomek"
   
3. How to identify CHARACTER names (DO NOT include):
   - Name appears with action verbs: "[Name] said", "[Name] looked", "[Name] sat"
   - Name appears in dialogues: "— [Name] said..."
   - Name appears multiple times (>3) throughout the text
   - Name is used as subject of action sentences

4. How to identify HOST names (DO include):
   - Name appears in intro/outro sections
   - Name appears in first-person statements: "I, [name]..."
   - Name appears 1-2 times total, likely in intro/outro context
   - Name is used as narrator identity, not as story character

Examples:
- Text: "Tom said hello. Tom sat down. Tom looked around." → DO NOT include "Tom" (character)
- Text: "I am Tom and welcome to the channel." → Include "Tom" (host name in intro)
- Text: "Piotr Zawadzki entered the room. Piotr looked at the table." → DO NOT include "Piotr Zawadzki" (character)
- Text: "Welcome, I'm Piotr Zawadzki." → Include "Piotr Zawadzki" (host name)

TASK: Scan the text and return ONLY a JSON object (no markdown, no code block):
{{
  "old_names_found": ["Tom", "Анатолий", ...],
  "old_channels_found": ["Old Channel Name", ...],
  "has_intro": true or false,
  "intro_position_percent": 0-100,
  "has_outro": true or false,
  "has_mid_cta": true or false,
  "mid_cta_position_percent": 30-70 if has_mid_cta else null,
  "issues": ["description of each issue"],
  "suggested_intro": "if has_intro is false, write one intro sentence IN {lang_name} using host name and channel name",
  "suggested_outro": "if has_outro is false, write one outro sentence IN {lang_name} using host name and channel name",
  "suggested_mid_cta": "if has_mid_cta is false, write ONE short sentence IN {lang_name} asking to like and subscribe, use channel name"
}}

Return ONLY valid JSON. No explanations."""

    messages = [
        {"role": "system", "content": "You output only valid JSON. No markdown, no explanation."},
        {"role": "user", "content": prompt + "\n\nTEXT:\n" + text[:30000]},
    ]
    success, response = call_openai_api(messages, get_postprocess_model(), api_key, log_queue, "Pass A (Branding Audit)", max_tokens=8192, service_tier=POSTPROCESS_SERVICE_TIER)
    if not success:
        return (False, None)
    response = response.strip()
    if response.startswith("```"):
        response = re.sub(r"^```\w*\n?", "", response)
        response = re.sub(r"\n?```\s*$", "", response)
    try:
        data = json.loads(response)
        return (True, data)
    except json.JSONDecodeError:
        log_msg(log_queue, "   ⚠️ Pass A: не удалось распарсить JSON")
        return (False, None)


def _normalize_whitespace(s: str) -> str:
    """Нормализует пробелы: множественные пробелы/переносы → один пробел."""
    return re.sub(r'\s+', ' ', s.strip())


# Маркеры тысяч в годах по языкам (для защиты от усечения дат в Pass D)
YEAR_MARKERS = {
    "ru": ["тысяча", "тысяч"],
    "cs": ["tisíc"],
    "pl": ["tysiąc", "tysięc"],
    "ro": ["mie ", "o mie"],
    "uk": ["тисяча", "тисяч"],
    "bg": ["хиляда", "хиляд"],
    "sk": ["tisíc"],
    "hu": ["ezer"],
    "tr": ["bin"],
    "de": ["tausend"],
    "fr": ["mille"],
    "es": ["mil"],
    "it": ["mille"],
    "en": ["thousand"],
}


def lang_name_to_code(name_or_code: str) -> str:
    """Возвращает код языка (ru, cs, ...) из имени или кода."""
    if not name_or_code:
        return ""
    s = (name_or_code or "").strip().lower()[:10]
    mapping = {"russian": "ru", "czech": "cs", "polish": "pl", "romanian": "ro", "ukrainian": "uk",
              "bulgarian": "bg", "slovak": "sk", "hungarian": "hu", "turkish": "tr", "german": "de",
              "french": "fr", "spanish": "es", "italian": "it", "english": "en", "dutch": "nl"}
    return mapping.get(s, s[:2] if len(s) >= 2 else s)


def make_output_filename(base_name: str, lang_code_or_display: str) -> str:
    """Имя выходного файла: {код}_{base_name}.txt (код = cs, pl, ro и т.д.)"""
    code = lang_name_to_code(lang_code_or_display)
    return f"{code}_{base_name}.txt" if code else f"{base_name}.txt"


def validate_date_replacement(old: str, new: str, lang_code: str) -> bool:
    """Отклоняет замены, которые усекают годы (удаляют маркер тысячи)."""
    code = lang_name_to_code(lang_code) if lang_code and len(str(lang_code)) > 2 else (lang_code or "")[:2].lower()
    markers = YEAR_MARKERS.get(code, [])
    if not markers:
        # Универсальная проверка: если new значительно короче и old содержит числительные
        if len(new) < len(old) * 0.5 and (re.search(r'\d{2,}', old) or any(m in old.lower() for m in ["тысяча", "tisíc", "tysiąc", "mie", "тисяча", "thousand"])):
            return False
        return True
    old_lower = old.lower()
    new_lower = new.lower()
    for marker in markers:
        if marker in old_lower and marker not in new_lower:
            return False
    return True


def validate_declutter_replacement(old: str, new: str) -> bool:
    """Базовые проверки замен Pass B (защита от канцелярита и раздувания)."""
    if len(new) > len(old) * 1.8 and len(old) > 15:
        return False
    old_words = old.split()
    new_words = new.split()
    if len(old_words) <= 4 and len(new_words) > len(old_words) * 2:
        return False
    bureaucratic = ["осуществлять", "испытывать", "являться", "производить", "осуществляет",
                    "испытывает", "является", "производит"]
    for word in bureaucratic:
        if word in new.lower() and word not in old.lower():
            return False
    return True


# Паттерны имён собственных (дома, парки, замки и т.д.) — не переводить
PROPER_NOUNS_PATTERNS = [
    r'\b\w+ House\b', r'\b\w+ Palace\b', r'\b\w+ Castle\b',
    r'\b\w+ Park\b', r'\b\w+ Street\b', r'\b\w+ Square\b',
    r'\b\w+ Lane\b', r'\b\w+ Road\b', r'\b\w+ Gardens\b',
    r'\b\w+ Abbey\b', r'\b\w+ Cathedral\b',
]


def is_proper_noun_change(old: str, new: str) -> bool:
    """Проверяет, не пытаемся ли перевести имя собственное."""
    for pattern in PROPER_NOUNS_PATTERNS:
        if re.search(pattern, old, re.IGNORECASE):
            if old.strip().lower() != new.strip().lower():
                return True
    return False


def _dominant_script(s: str) -> str:
    """Определяет доминирующий алфавит: cyrillic, latin или unknown."""
    scripts = {}
    for ch in s:
        if ch.isalpha():
            if '\u0400' <= ch <= '\u04FF':
                scripts['cyrillic'] = scripts.get('cyrillic', 0) + 1
            elif '\u0041' <= ch <= '\u007A' or '\u00C0' <= ch <= '\u024F':
                scripts['latin'] = scripts.get('latin', 0) + 1
    if not scripts:
        return 'unknown'
    return max(scripts, key=scripts.get)


def validate_script_match(old: str, text: str) -> bool:
    """Проверяет, что old написан на том же алфавите что и текст (кириллица vs латиница)."""
    if not old or not text or len(old) < 5:
        return True
    text_script = _dominant_script(text[:500])
    old_script = _dominant_script(old)
    if text_script == 'cyrillic' and old_script == 'latin':
        return False
    return True


def validate_replacement(old: str, new: str, pass_name: str, lang_code: str, text: str = "") -> Tuple[bool, str]:
    """
    Универсальный валидатор замен для всех пассов.
    Возвращает (is_valid, reason).
    """
    if old.strip() == new.strip():
        return False, "noop"
    if not new.strip():
        return False, "empty_replacement"
    if pass_name in ("D", "Pass D", "language_purity"):
        if not validate_date_replacement(old, new, lang_code):
            return False, "date_truncation"
        if text and not validate_script_match(old, text):
            return False, "script_mismatch"
    if pass_name in ("B", "Pass B", "declutter"):
        if not validate_declutter_replacement(old, new):
            return False, "bureaucratic_bloat"
    if pass_name in ("C", "D", "Pass C", "Pass D", "nativeness", "language_purity"):
        if is_proper_noun_change(old, new):
            return False, "proper_noun_translation"
    if len(old) > 10:
        ratio = len(new) / len(old)
        if ratio > 3.0 or ratio < 0.2:
            return False, f"extreme_length_change"
    return True, "ok"


def _find_fuzzy_match(text: str, pattern: str) -> Optional[str]:
    """
    Ищет pattern в text с гибким сопоставлением:
    1. Точное совпадение
    2. С нормализованными пробелами (игнорируя множественные пробелы/переносы)
    """
    # 1. Точное совпадение
    if pattern in text:
        return pattern
    
    # 2. Нормализация пробелов - создаём regex паттерн, который игнорирует множественные пробелы
    pattern_norm = _normalize_whitespace(pattern)
    pattern_words = pattern_norm.split()
    
    if len(pattern_words) < 2:
        return None
    
    # Создаём regex: каждое слово + один или более пробелов между словами
    regex_pattern = r'\s+'.join(re.escape(word) for word in pattern_words)
    regex_pattern = r'\s*' + regex_pattern + r'\s*'
    
    # Ищем в тексте
    match = re.search(regex_pattern, text, re.IGNORECASE)
    if match:
        matched_text = match.group(0)
        # Проверяем, что длина примерно соответствует (не слишком отличается)
        if abs(len(matched_text) - len(pattern)) <= len(pattern) * 0.5:
            return matched_text
    
    return None


def _apply_replacements(
    text: str, replacements: List[Dict], log_queue: queue.Queue, pass_name: str,
    lang_code: str = "",
) -> Tuple[str, List[Dict]]:
    """
    Применяет список замен к тексту с гибким сопоставлением.
    Unicode-нормализация NFC для совместимости (напр. румынские ț, ș).
    Валидирует каждую замену перед применением.
    Возвращает (результат, список деталей: [{old, new, status}]).
    """
    result = unicodedata.normalize("NFC", text)
    details: List[Dict] = []

    # Лимит замен на пасс
    pass_letter = pass_name.replace("Pass ", "").strip() if "Pass" in pass_name else pass_name
    limit = MAX_REPLACEMENTS.get(pass_letter, 15)
    if len(replacements) > limit:
        log_msg(log_queue, f"   ⚠️ {pass_name}: {len(replacements)} замен, обрезаю до {limit}")
        replacements = replacements[:limit]

    valid_replacements = []
    for item in replacements:
        old_val = item.get("old")
        new_val = item.get("new")
        if old_val is None:
            continue
        if not isinstance(old_val, str):
            old_val = str(old_val)
        if new_val is None:
            new_val = ""
        else:
            new_val = str(new_val)
        old_val = unicodedata.normalize("NFC", old_val)
        new_val = unicodedata.normalize("NFC", new_val)
        # Запрет удаления: "new" не должен быть пустым или только пробелы (Pass B не должен удалять предложения)
        if not new_val.strip():
            details.append({"old": (old_val[:80] + "…") if len(old_val) > 80 else old_val, "new": "(empty)", "status": "skipped_empty"})
            continue
        if len(old_val) < 3:
            details.append({"old": old_val[:50] + "…" if len(old_val) > 50 else old_val, "new": new_val[:30] + "…" if len(new_val) > 30 else new_val, "status": "skipped_short"})
            continue
        if old_val.strip() == new_val.strip():
            details.append({"old": old_val[:80] + "…" if len(old_val) > 80 else old_val, "new": new_val[:80] + "…" if len(new_val) > 80 else new_val, "status": "skipped_noop"})
            continue
        
        # ЗАЩИТА ИМЁН СОБСТВЕННЫХ: если old_val — имя собственное (с заглавной буквы, короткое),
        # и new_val — то же слово в другом падеже/форме, пропускаем замену
        if pass_name == "Pass C":  # Только для Pass C (Nativeness)
            old_stripped = old_val.strip().rstrip(".,!?;:")
            new_stripped = new_val.strip().rstrip(".,!?;:")
            
            # Проверяем: начинается с заглавной буквы, короткое (≤15 символов), одно слово
            if (len(old_stripped) <= 15 and 
                old_stripped and old_stripped[0].isupper() and
                len(old_stripped.split()) == 1 and
                len(new_stripped.split()) == 1):
                
                # Проверяем, что new_val — это то же слово, но в другой форме (склонение)
                # Убираем знаки препинания и сравниваем базовую форму
                old_base = old_stripped.lower().rstrip(".,!?;:")
                new_base = new_stripped.lower().rstrip(".,!?;:")
                
                # Если базовые формы похожи (одно слово превратилось в другое с изменением окончания),
                # и это не просто добавление/удаление знаков препинания — пропускаем
                # Также проверяем типичные окончания склонения (польский: -a, -em, -owi, -ie и т.д.)
                common_case_endings = ['a', 'em', 'owi', 'ie', 'ą', 'ego', 'emu', 'im', 'ym', 'ym', 'ej', 'ą']
                old_ends_with_case = any(old_base.endswith(e) for e in common_case_endings)
                new_ends_with_case = any(new_base.endswith(e) for e in common_case_endings)
                
                # Если одно слово склонено в другое (изменение окончания на типичное падежное)
                if (old_base != new_base and 
                    (old_base in new_base or new_base in old_base) and
                    abs(len(old_base) - len(new_base)) <= 5 and  # Изменение на ≤5 символов (окончание)
                    (old_ends_with_case or new_ends_with_case or 
                     any(old_base[:-2] == new_base[:-2] or old_base[:-3] == new_base[:-3] for _ in [1]))):  # Одинаковый корень
                    details.append({"old": old_val[:80] + "…" if len(old_val) > 80 else old_val, 
                                   "new": new_val[:80] + "…" if len(new_val) > 80 else new_val, 
                                   "status": "skipped_proper_noun"})
                    continue
                
                # Дополнительная проверка: если new_val просто добавляет точку/знак препинания к old_val
                if old_stripped == new_stripped.rstrip(".,!?;:"):
                    details.append({"old": old_val[:80] + "…" if len(old_val) > 80 else old_val, 
                                   "new": new_val[:80] + "…" if len(new_val) > 80 else new_val, 
                                   "status": "skipped_proper_noun"})
                    continue

        # Универсальная валидация замен
        is_valid, reason = validate_replacement(old_val, new_val, pass_name, lang_code, text=result)
        if not is_valid:
            details.append({"old": (old_val[:60] + "…") if len(old_val) > 60 else old_val,
                           "new": (new_val[:50] + "…") if len(new_val) > 50 else new_val,
                           "status": f"rejected:{reason}"})
            continue

        valid_replacements.append((old_val, new_val))
    
    # Сортируем по длине (от длинных к коротким)
    valid_replacements.sort(key=lambda x: len(x[0]), reverse=True)
    
    for old_val, new_val in valid_replacements:
        if old_val in result:
            result = result.replace(old_val, new_val)
            details.append({"old": old_val[:80] + "…" if len(old_val) > 80 else old_val, "new": new_val[:80] + "…" if len(new_val) > 80 else new_val, "status": "applied"})
            continue
        matched = _find_fuzzy_match(result, old_val)
        if matched:
            result = result.replace(matched, new_val, 1)
            details.append({"old": (old_val[:80] + "…") if len(old_val) > 80 else old_val, "new": (new_val[:80] + "…") if len(new_val) > 80 else new_val, "status": "applied"})
            continue
        details.append({"old": old_val[:80] + "…" if len(old_val) > 80 else old_val, "new": new_val[:80] + "…" if len(new_val) > 80 else new_val, "status": "skipped_not_found"})
    
    applied = sum(1 for d in details if d.get("status") == "applied")
    skipped_nf = sum(1 for d in details if d.get("status") == "skipped_not_found")
    skipped_short = sum(1 for d in details if d.get("status") == "skipped_short")
    skipped_noop = sum(1 for d in details if d.get("status") == "skipped_noop")
    skipped_empty = sum(1 for d in details if d.get("status") == "skipped_empty")
    skipped_proper = sum(1 for d in details if d.get("status") == "skipped_proper_noun")
    rejected = sum(1 for d in details if str(d.get("status", "")).startswith("rejected:"))
    log_msg(log_queue, f"   {pass_name}: применено {applied} замен" + (
        f", пропущено {skipped_nf} (не найдено)" if skipped_nf else ""
    ) + (f", {skipped_short} коротких" if skipped_short else "") + (f", {skipped_noop} no-op" if skipped_noop else "") + (f", {skipped_empty} пустых" if skipped_empty else "") + (f", {skipped_proper} имён собственных" if skipped_proper else "") + (f", отклонено {rejected}" if rejected else ""))
    return (result, details)


def _postprocess_pass_b_ai_declutter(text: str, lang_name: str, api_key: str, log_queue: queue.Queue, lang_code: str = "") -> Tuple[bool, str, Optional[str], List[Dict]]:
    """Pass B: модель возвращает только список замен (JSON); применяем их к тексту."""
    intensifiers_note = f"""
Intensifiers to limit (max 2 total) — examples: Czech: skutečně, opravdu; Polish: naprawdę, absolutnie; Romanian: cu adevărat; Turkish: gerçekten; Russian: по-настоящему; English: truly, really. Apply for {lang_name}."""
    prompt = f"""You are a style editor for YouTube scripts in {lang_name}. Do NOT return the full text.

Your task: list ONLY the exact replacements to make the text sound human-written (reduce AI patterns).

ПРАВИЛА СТИЛИСТИЧЕСКОЙ ПОЛИРОВКИ:
1. НЕ ЗАМЕНЯЙ короткие эмоциональные фразы длинными объяснениями. "Она боялась" лучше чем "Она испытывала страх" — краткость = сила для TTS.
2. НЕ МЕНЯЙ смысл. Если оригинал говорит "дом" (household), не заменяй на "семья" (family).
3. НЕ ДОБАВЛЯЙ канцеляризмы: "испытывала", "осуществляла", "являлась" — это убивает живой текст.
4. Каждая замена должна УЛУЧШАТЬ звучание при чтении вслух. Если не улучшает — не делай.
5. Максимум 15-20 замен на текст. Не нужно менять всё подряд.

RULES: Reduce repetitive intensifiers (max 2 total). Break formulaic transitions. Break question floods (4+ questions in a row). Vary sentence openings. Do not change facts, names, numbers, or meaning. Do not add content.

CRITICAL: NEVER change character names (protagonists, antagonists, side characters). Keep ALL character names exactly as written. Only change stylistic phrases, not names of people.

NARRATIVE VOICE PROTECTION:
- Do NOT remove dramatic repetitions if they build tension (e.g. "Dark. Too dark. Impossibly dark.").
- Do NOT flatten emotional escalation patterns (short → shorter → punch).
- Do NOT replace sensory language with abstract descriptions.
- If a phrase sounds like intentional style (staccato rhythm, questions for suspense, sentence fragments for pacing) — LEAVE IT.

OUTPUT FORMAT: A single JSON array. Each item: {{"old": "...", "new": "..."}}.

"new" MUST always contain replacement text — NEVER empty string or whitespace. REPHRASE, don't delete. "old" and "new" must differ (no identical pairs). Do NOT convert written-out numbers to digits. Do NOT change currencies or units. Maximum 15 replacements.

CRITICAL: For "old" you MUST copy the phrase CHARACTER-FOR-CHARACTER from the TEXT below—same spaces, punctuation, line breaks. If "old" is not found in the text, the replacement will be skipped.

Output ONLY the JSON array, no markdown, no explanation. Example: [{{"old": " a really ", "new": " a genuinely "}}, {{"old": "And here's the thing.", "new": "What matters is this."}}]

TEXT (copy "old" strings from here exactly):
""" + text[:50000]

    messages = [
        {"role": "system", "content": "You output only a JSON array of objects with keys 'old' and 'new'. No other text."},
        {"role": "user", "content": prompt},
    ]
    success, response = call_openai_api(messages, get_postprocess_model(), api_key, log_queue, "Pass B (AI Declutter)", max_tokens=8192, service_tier=POSTPROCESS_SERVICE_TIER)
    if not success:
        return (False, text, None, [])
    response = response.strip()
    if response.startswith("```"):
        response = re.sub(r"^```\w*\n?", "", response)
        response = re.sub(r"\n?```\s*$", "", response)
    try:
        replacements = json.loads(response)
        if not isinstance(replacements, list):
            log_msg(log_queue, "   ⚠️ Pass B: ответ не массив")
            return (False, text, None, [])
        result, details = _apply_replacements(text, replacements, log_queue, "Pass B", lang_code)
        return (True, result, response, details)
    except json.JSONDecodeError as e:
        log_msg(log_queue, f"   ⚠️ Pass B: не удалось распарсить JSON — {e}")
        return (False, text, None, [])


def _postprocess_pass_c_nativeness(text: str, lang_name: str, api_key: str, log_queue: queue.Queue, lang_code: str = "") -> Tuple[bool, str, Optional[str], List[Dict]]:
    """Pass C: модель возвращает только список замен (JSON); применяем их к тексту."""
    prompt = f"""You are a native {lang_name} speaker reviewing a YouTube script. Do NOT return the full text.

Your task: list ONLY the exact replacements to fix nativeness and correctness in {lang_name}.

ПРАВИЛА НАТУРАЛИЗАЦИИ:
1. НИКОГДА не переводи имена собственные: названия домов (York House, Clarence House), парков (Green Park, Hyde Park), замков и т.д. — это имена, не описания.
2. НИКОГДА не меняй глагол на его антоним или слово с противоположным значением. Если в тексте "устраивается" — не заменяй на "зарабатывается".
3. Если не уверен в замене — НЕ ДЕЛАЙ ЕЁ. Нулевое действие лучше ошибки.
4. Максимум 10-15 замен на текст. Концентрируйся на действительно неестественных оборотах.

FIX (via replacements): Calques; unnatural word order; wooden syntax; register inconsistency. Terminology: correct {lang_name} terms for the target audience. Cultural references: fix wrong country toponyms; translate foreign place names to {lang_name}.

ABSOLUTELY FORBIDDEN — NEVER CHANGE:
1. DATES AND YEARS: NEVER touch any year, date, or time reference. "в тысяча девятьсот третьем году" must stay EXACTLY as written. "nineteen hundred twenty-three" must stay EXACTLY as written. Do NOT abbreviate, shorten, reformat, or simplify ANY date. If a phrase contains a year — leave the ENTIRE phrase untouched.
2. NUMBERS: NEVER change written-out numbers. "восемь тысяч двести пятьдесят три" stays as is.
3. CURRENCIES: dollars stay dollars, NOT local currency.
4. PROPER NOUNS (ИМЕНА СОБСТВЕННЫЕ): NEVER change proper nouns — names of people, cities, nicknames, surnames. They are ALREADY in the correct form. Examples:
   - "Gruby" stays "Gruby" (NOT "Grubym", NOT "Grubego")
   - "Sokołowski" stays "Sokołowski" (NOT "Sokołowskiego", NOT "Sokołowskim")
   - "Warszawa" stays "Warszawa" (NOT "Warszawą", NOT "Warszawie")
   - "Piotr" stays "Piotr" (NOT "Piotra", NOT "Piotrem")
   - "Mokotów" stays "Mokotów" (NOT "Mokotowie", NOT "Mokotowa")
   - "Polska" stays "Polska" (NOT "Polska.", NOT "Polski")
   Do NOT decline proper nouns. Do NOT add punctuation to proper nouns. They are correct as written.
5. CHARACTER NAMES: NEVER change character names (protagonists, antagonists, side characters). Keep ALL character names exactly as written. "Karel Horáček" stays "Karel Horáček", "Sergej" stays "Sergej", etc.
6. IDENTICAL PAIRS: "old" and "new" MUST differ.

When in doubt about whether something is a date, number, or proper noun — DO NOT TOUCH IT.

CRITICAL: For "old" you MUST copy the phrase CHARACTER-FOR-CHARACTER from the TEXT below—same spaces, punctuation, line breaks. Do not paraphrase. Take the exact substring from TEXT and paste into "old". If "old" is not in the text, the replacement will be skipped. Do not change meaning. Maximum 12 replacements.

OUTPUT FORMAT: A single JSON array. Each item: {{"old": "exact phrase from text", "new": "replacement"}}. Output ONLY this JSON array, no markdown, no explanation.

TEXT (copy "old" strings from here exactly):
""" + text[:50000]

    messages = [
        {"role": "system", "content": "You output only a JSON array of objects with keys 'old' and 'new'. No other text."},
        {"role": "user", "content": prompt},
    ]
    success, response = call_openai_api(messages, get_postprocess_model(), api_key, log_queue, "Pass C (Nativeness)", max_tokens=8192, service_tier=POSTPROCESS_SERVICE_TIER)
    if not success:
        return (False, text, None, [])
    response = response.strip()
    if response.startswith("```"):
        response = re.sub(r"^```\w*\n?", "", response)
        response = re.sub(r"\n?```\s*$", "", response)
    try:
        replacements = json.loads(response)
        if not isinstance(replacements, list):
            log_msg(log_queue, "   ⚠️ Pass C: ответ не массив")
            return (False, text, None, [])
        result, details = _apply_replacements(text, replacements, log_queue, "Pass C", lang_code)
        return (True, result, response, details)
    except json.JSONDecodeError as e:
        log_msg(log_queue, f"   ⚠️ Pass C: не удалось распарсить JSON — {e}")
        return (False, text, None, [])


def _postprocess_pass_d_language_purity(text: str, lang_name: str, api_key: str, log_queue: queue.Queue, lang_code: str = "") -> Tuple[bool, str, Optional[str], List[Dict]]:
    """Pass D: Language Purity — находит слова не того языка и возвращает только реальные замены (JSON)."""
    prompt = f"""You are a {lang_name} language purity auditor for a TTS YouTube script.

Ты работаешь с текстом на {lang_name}. Все значения в поле "old" ДОЛЖНЫ быть фрагментами из текста на {lang_name}, а НЕ на английском или любом другом языке. Ищи в тексте фрагменты, которые нужно заменить, и копируй их ТОЧНО как они есть в тексте.

КРИТИЧЕСКОЕ ПРАВИЛО ДЛЯ ДАТ И ЧИСЛИТЕЛЬНЫХ:
- НИКОГДА не сокращай годы. Год "тысяча девятьсот пятнадцатый" должен оставаться полным.
- Допустимые форматы: "тысяча девятьсот пятнадцатый год" ИЛИ цифрами "1915".
- ЗАПРЕЩЕНО удалять "тысяча" или "девятьсот" из годов — это делает дату бессмысленной.
- Числительные в контексте адресов, возрастов и количеств тоже не трогать.
- Если не уверен — НЕ МЕНЯЙ. Лучше оставить как есть, чем сломать.

YOUR ONLY TASK: Find words/phrases that are NOT {lang_name} and need translation.

FIND these problems ONLY:
1. UNTRANSLATED ENGLISH phrases (e.g. "plain sight" → translate to {lang_name})
2. WORDS FROM WRONG LANGUAGES (e.g. Spanish "palacios" in Romanian, Czech "závoje" in Polish)
3. INVENTED/NONSENSE words that don't exist in any language
4. BARE DIGIT YEARS that should be written out (e.g. "1925" → words)

CRITICAL — DO NOT TOUCH:
- Number words ("bin", "yüz", "mie", "tysiąc") are PART OF {lang_name} — NEVER remove them
- Turkish "bin" = "thousand" and is REQUIRED in dates (bin dokuz yüz = 1900)
- Character names (protagonists, antagonists, side characters) — keep ALL character names exactly as written
- Proper nouns (city names, personal names, building names) — keep as-is
- Established foreign terms (Beaux-Arts, KGB, etc.) — keep as-is
- Words that ARE correct {lang_name} — do NOT list them

OUTPUT: JSON array. Each item: {{"old": "exact foreign/wrong phrase FROM THE TEXT", "new": "{lang_name} equivalent"}}

If text is clean (no foreign words found): return []

IMPORTANT: Return ONLY items that need ACTUAL changes. Maximum 8 items.

TEXT:
""" + text[:50000]

    messages = [
        {"role": "system", "content": "You output only a JSON array of objects with keys 'old' and 'new', or empty array []. No other text."},
        {"role": "user", "content": prompt},
    ]
    success, response = call_openai_api(messages, get_postprocess_model(), api_key, log_queue, "Pass D (Language Purity)", max_tokens=8192, service_tier=POSTPROCESS_SERVICE_TIER)
    if not success:
        return (False, text, None, [])
    response = response.strip()
    if response.startswith("```"):
        response = re.sub(r"^```\w*\n?", "", response)
        response = re.sub(r"\n?```\s*$", "", response)
    try:
        replacements = json.loads(response)
        if not isinstance(replacements, list):
            log_msg(log_queue, "   ⚠️ Pass D: ответ не массив")
            return (False, text, None, [])
        if len(replacements) == 0:
            log_msg(log_queue, "   Pass D: текст чистый, замен 0")
            return (True, text, response, [])
        result, details = _apply_replacements(text, replacements, log_queue, "Pass D", lang_code)
        return (True, result, response, details)
    except json.JSONDecodeError as e:
        log_msg(log_queue, f"   ⚠️ Pass D: не удалось распарсить JSON — {e}")
        return (False, text, None, [])


def _write_postprocess_log(
    output_dir: str,
    base_name: str,
    lang_code: str,
    pass_a_ok: bool,
    audit_json: Optional[Dict],
    pass_b_raw: Optional[str],
    pass_b_details: List[Dict],
    chars_before: int,
    chars_after_b: int,
    merge_actions: List[str],
    chars_after_merge: int,
    pass_c_raw: Optional[str],
    pass_c_details: List[Dict],
    chars_after_c: int,
    pass_d_raw: Optional[str] = None,
    pass_d_details: Optional[List[Dict]] = None,
    chars_after_d: Optional[int] = None,
) -> None:
    """Пишет постпроцесс-лог в {output_dir}/{base_name}_{lang_code}_postprocess.log."""
    if not output_dir or not base_name or not lang_code:
        return
    log_path = os.path.join(output_dir, f"{base_name}_{lang_code}_postprocess.log")
    pass_d_details = pass_d_details or []
    chars_after_d = chars_after_d if chars_after_d is not None else chars_after_c
    lines = [
        "=== Postprocess pipeline log ===",
        f"Chars: before={chars_before} → after B={chars_after_b} → after merge={chars_after_merge} → after C={chars_after_c} → after D={chars_after_d}",
        "",
    ]
    lines.append("--- Pass A (Branding Audit) ---")
    if pass_a_ok and audit_json:
        lines.append(json.dumps(audit_json, ensure_ascii=False, indent=2))
    else:
        lines.append("(failed or no data)")
    lines.append("")
    lines.append("--- Pass B (AI Declutter): raw JSON ---")
    lines.append(pass_b_raw if pass_b_raw else "(no data)")
    lines.append("")
    lines.append("--- Pass B: per-replacement status ---")
    for i, d in enumerate(pass_b_details):
        o, n = str(d.get("old", ""))[:70], str(d.get("new", ""))[:50]
        lines.append(f"  {i+1}. [{d.get('status', '?')}] old: {o} → new: {n}")
    lines.append("")
    lines.append("--- Merge (branding) ---")
    for a in merge_actions:
        lines.append(f"  {a}")
    lines.append("")
    lines.append("--- Pass C (Nativeness): raw JSON ---")
    lines.append(pass_c_raw if pass_c_raw else "(no data)")
    lines.append("")
    lines.append("--- Pass C: per-replacement status ---")
    for i, d in enumerate(pass_c_details):
        o, n = str(d.get("old", ""))[:70], str(d.get("new", ""))[:50]
        lines.append(f"  {i+1}. [{d.get('status', '?')}] old: {o} → new: {n}")
    lines.append("")
    lines.append("--- Pass D (Language Purity): raw JSON ---")
    lines.append(pass_d_raw if pass_d_raw else "(no data)")
    lines.append("")
    lines.append("--- Pass D: per-replacement status ---")
    for i, d in enumerate(pass_d_details):
        o, n = str(d.get("old", ""))[:70], str(d.get("new", ""))[:50]
        lines.append(f"  {i+1}. [{d.get('status', '?')}] old: {o} → new: {n}")
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass
    # Дополнительно дописываем в correction.log (общий лог постпроцесса для запуска)
    correction_path = os.path.join(output_dir, "correction.log")
    header = f"\n\n=== {base_name} / {lang_code} ===\n"
    try:
        with _CORRECTION_LOG_LOCK:
            with open(correction_path, "a", encoding="utf-8") as f:
                f.write(header + "\n".join(lines) + "\n")
    except Exception:
        pass


def _final_sanity_check(original_text: str, processed_text: str, lang_code: str, log_queue: queue.Queue) -> Tuple[str, List[str]]:
    """
    Финальная проверка после всех пассов.
    Возвращает (финальный_текст, список_предупреждений).
    """
    warnings: List[str] = []
    if not original_text or not processed_text:
        return processed_text or original_text, warnings
    ratio = len(processed_text) / len(original_text)
    if ratio < 0.85:
        warnings.append(f"Текст сократился на {(1 - ratio) * 100:.0f}%")
    if ratio > 1.20:
        warnings.append(f"Текст увеличился на {(ratio - 1) * 100:.0f}%")
    return processed_text, warnings


def run_postprocess_pipeline(
    full_text: str,
    lang_name: str,
    lang_display: str,
    host_name: str,
    channel_name: str,
    api_key: str,
    log_queue: queue.Queue,
    output_dir: str = "",
    base_name: str = "",
    lang_code: str = "",
    main_character_name: str = "",
    is_generated_text: bool = False,  # True для генерации, False для рерайта
) -> str:
    """
    Постпроцесс: Pass A+B параллельно → merge брендинга → Pass C (Nativeness) → Pass D (Language Purity).
    Если заданы output_dir, base_name, lang_code — пишет лог в {base_name}_{lang_code}_postprocess.log.
    """
    if STOP_FLAG.is_stopped():
        return full_text
    if not full_text or not full_text.strip():
        return full_text

    if full_text.strip().startswith("[ОШИБКА:"):
        return full_text

    log_msg(log_queue, f"🔎 {lang_display}: постпроцесс (Pass A+B → merge → Pass C → Pass D)...")

    pass_a_ok = False
    audit_json: Optional[Dict] = None
    pass_b_text = full_text
    pass_b_raw_json: Optional[str] = None
    pass_b_details: List[Dict] = []
    pass_c_raw_json: Optional[str] = None
    pass_c_details: List[Dict] = []
    pass_d_raw_json: Optional[str] = None
    pass_d_details: List[Dict] = []

    def run_a():
        return _postprocess_pass_a_branding_audit(
            full_text, lang_name, host_name, channel_name, api_key, log_queue
        )

    def run_b():
        return _postprocess_pass_b_ai_declutter(full_text, lang_name, api_key, log_queue, lang_code)

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_a = ex.submit(run_a)
        fut_b = ex.submit(run_b)
        try:
            pass_a_ok, audit_json = fut_a.result(timeout=180)
            # Программная проверка intro: если модель сказала has_intro, но host_name нет в первой четверти — отменяем
            if pass_a_ok and audit_json and audit_json.get("has_intro") and (host_name or "").strip():
                first_quarter = full_text[: max(1, len(full_text) // 4)]
                if (host_name or "").lower() not in first_quarter.lower():
                    audit_json["has_intro"] = False
                    issues = audit_json.get("issues") or []
                    issues.append(f"OVERRIDDEN: host '{host_name}' not found in first 25%")
                    audit_json["issues"] = issues
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            log_msg(log_queue, f"   ⚠️ Pass A failed: {err_msg}")
        try:
            res = fut_b.result(timeout=180)
            pass_b_ok, pass_b_text, pass_b_raw_json, pass_b_details = res[0], res[1], res[2], res[3]
            if not pass_b_ok:
                pass_b_text = full_text
        except Exception as e:
            pass_b_text = full_text
            err_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            log_msg(log_queue, f"   ⚠️ Pass B failed: {err_msg}")

    pass_b_text = _length_guard(full_text, pass_b_text, "Pass B", log_queue)
    len_after_b = len(pass_b_text)
    ratio_b = len_after_b / len(full_text) if full_text else 1.0
    log_msg(log_queue, f"   Pass B: {len(full_text)} → {len_after_b} chars ({ratio_b:.0%})")

    merged = pass_b_text
    merge_actions: List[str] = []
    if pass_a_ok and audit_json:
        merged = merge_branding(pass_b_text, audit_json, host_name or "", channel_name or "", lang_name, main_character_name="", log_queue=log_queue, skip_name_replacement=is_generated_text)
        for old_name in audit_json.get("old_names_found") or []:
            if old_name and str(old_name).strip():
                merge_actions.append(f"name: {old_name!r} → {host_name!r}")
        for old_ch in audit_json.get("old_channels_found") or []:
            if old_ch and str(old_ch).strip():
                merge_actions.append(f"channel: {old_ch!r} → {channel_name!r}")
        if not audit_json.get("has_intro") and (host_name or channel_name):
            merge_actions.append("inserted intro")
        if not audit_json.get("has_outro") and (host_name or channel_name):
            merge_actions.append("inserted outro")
        log_msg(log_queue, f"   ✅ {lang_display}: брендинг применён (merge)")

    if STOP_FLAG.is_stopped():
        _write_postprocess_log(output_dir, base_name, lang_code, pass_a_ok, audit_json,
                               pass_b_raw_json, pass_b_details, len(full_text), len(pass_b_text),
                               merge_actions, len(merged), None, [], len(merged))
        return merged

    pass_c_ok, final, pass_c_raw_json, pass_c_details = _postprocess_pass_c_nativeness(
        merged, lang_name, api_key, log_queue, lang_code
    )
    final = _length_guard(merged, final, "Pass C", log_queue)
    len_after_c = len(final)
    ratio_c = len_after_c / len(merged) if merged else 1.0
    log_msg(log_queue, f"   Pass C: {len(merged)} → {len_after_c} chars ({ratio_c:.0%})")
    if pass_c_ok:
        log_msg(log_queue, f"   ✅ {lang_display}: Pass C (nativeness) готов")
    else:
        final = merged

    # Pass D: Language Purity (после C, перед return)
    if STOP_FLAG.is_stopped():
        _write_postprocess_log(output_dir, base_name, lang_code, pass_a_ok, audit_json,
                               pass_b_raw_json, pass_b_details, len(full_text), len(pass_b_text),
                               merge_actions, len(merged), pass_c_raw_json, pass_c_details, len(final),
                               None, [], len(final))
        return final
    text_before_d = final
    pass_d_ok, pass_d_text, pass_d_raw_json, pass_d_details = _postprocess_pass_d_language_purity(
        final, lang_name, api_key, log_queue, lang_code
    )
    if pass_d_ok:
        final = _length_guard(text_before_d, pass_d_text, "Pass D", log_queue)
    else:
        final = text_before_d
    len_after_d = len(final)
    log_msg(log_queue, f"   Pass D: {len_after_c} → {len_after_d} chars")
    if pass_d_ok and pass_d_details:
        log_msg(log_queue, f"   ✅ {lang_display}: Pass D (language purity) готов")
    elif pass_d_ok:
        log_msg(log_queue, f"   Pass D: текст чистый")

    # Финальная проверка после всех пассов
    final, sanity_warnings = _final_sanity_check(full_text, final, lang_code, log_queue)
    if sanity_warnings:
        for w in sanity_warnings:
            log_msg(log_queue, f"   ⚠️ Финальная проверка: {w}")

    _write_postprocess_log(output_dir, base_name, lang_code, pass_a_ok, audit_json,
                           pass_b_raw_json, pass_b_details, len(full_text), len(pass_b_text),
                           merge_actions, len(merged), pass_c_raw_json, pass_c_details, len_after_c,
                           pass_d_raw_json, pass_d_details, len_after_d)
    return final


def _dedup_sentences(text: str) -> str:
    """Remove exact duplicate sentences, keeping first occurrence."""
    import hashlib
    # Split into paragraphs first (preserve structure)
    paragraphs = text.split('\n\n')
    result_paragraphs = []
    seen_hashes = set()
    
    for para in paragraphs:
        # Split paragraph into sentences
        parts = re.split(r'(?<=[.!?])\s+', para)
        kept = []
        for sentence in parts:
            stripped = sentence.strip()
            if len(stripped) < 40:
                # Short sentences (transitions, etc) - always keep
                kept.append(sentence)
                continue
            h = hashlib.md5(stripped.lower().encode()).hexdigest()
            if h not in seen_hashes:
                seen_hashes.add(h)
                kept.append(sentence)
            # else: skip duplicate
        
        rebuilt = ' '.join(kept)
        if rebuilt.strip():
            result_paragraphs.append(rebuilt)
    
    return '\n\n'.join(result_paragraphs)


def sanitize_final_text(text: str, lang_name: str) -> str:
    """
    Финальная очистка текста перед сохранением в .txt для TTS.
    - убирает явные служебные заголовки/чек-листы/логовые строки
    - правит некоторые типичные тех. артефакты (для чешского/польского/румынского)
    - удаляет случайные иероглифы и подобный мусор вне алфавита языка
    """
    if not text:
        return text

    # Удаляем типичные лог-заголовки и чек-листы постфактум (на случай старых моделей)
    lines: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        upper = stripped.upper()
        if stripped.startswith("#"):
            continue
        if upper.startswith(("KONTROLA", "VERIFICATION", "INPUT METRICS", "ANALÝZA", "ANALIZA", "CHECKLIST")):
            continue
        if "VERIFICATION CHECKLIST" in upper:
            continue
        if any(m in stripped for m in ["☑", "✓", "►"]):
            continue
        lines.append(line)

    cleaned = "\n".join(lines)

    # Удаляем редкие иероглифы / CJK (например, случайный '超')
    cleaned = re.sub(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]+", "", cleaned)
    # === ARTIFACT-STRIPPER (subtitle/SFX) ===
    # Удаление субтитровых/SFX-артефактов из исходных транскриптов, которые модель
    # иногда протаскивает в результат ('>>' смена диктора, [music]/[applause], ноты).
    # ВАЖНО: только [ \t] (не \s) — чтобы не задеть \n\n и структуру абзацев.
    # '>>' — субтитровая метка смены диктора
    cleaned = re.sub(r"[ \t]*>>+[ \t]*", " ", cleaned)
    # музыкальные ноты
    cleaned = re.sub(r"[\u266a\u266b\u266c\u2669]+", " ", cleaned)
    # скобочные теги: короткий тег без конечной пунктуации ([music],[applause],[aplauze]) -> удалить;
    # длинную фразу/предложение в скобках -> развернуть (текст сохраняем, убираем только скобки)
    def _strip_caption(_m):
        inner = _m.group(1).strip()
        if len(inner.split()) <= 3 and not re.search(r"[.!?\u2026]$", inner):
            return ""
        return inner
    cleaned = re.sub(r"\[([^\]\n]{1,200})\]", _strip_caption, cleaned)
    # косметика после удаления (двойные пробелы, пробел перед пунктуацией) — горизонтально
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([,.!?;:])", r"\1", cleaned)
    # # === ARTIFACT-STRIPPER (subtitle/SFX) === END


    lang_lower = lang_name.lower()

    # Языко-специфические быстрые фиксы известных артефактов
    if lang_lower == "polish":
        # 'działko' → 'działo' для серьёзного орудия
        cleaned = re.sub(r"\bdziałko\b", "działo", cleaned)
        # убрать возможный случайный иероглиф '超'
        cleaned = cleaned.replace("超", "")
    elif lang_lower == "czech":
        # Jagdtiger не должен становиться "Jaguár tygr" и подобными монстрами
        cleaned = re.sub(r"Jagu[aá]r[ -]?tygr", "Jagdtiger", cleaned)
        # IS-2 не должен становиться "Ioškin", "Joškin" и т.п.
        cleaned = re.sub(r"Io[šs]kin-?2", "IS-2", cleaned)
        cleaned = re.sub(r"Io[šs]kin", "IS-2", cleaned)
        # Рост гигантов: 8–10 футов = 2.5–3 м, не 4–5 м (аудит)
        cleaned = re.sub(r"stojící čtyři až někdy pět metrů vysoké", "stojící dva a půl až někdy tři metry vysoké", cleaned)
    elif lang_lower == "turkish":
        # Рост гигантов: 8–10 футов = 2.5–3 м, не 8–10 м (аудит)
        cleaned = re.sub(r"sekiz metre, bazen on metre boyunda duran", "iki buçuk, bazen üç metre boyunda duran", cleaned)
    elif lang_lower == "romanian":
        # Слишком частая галлюцинация 'pantof' вместо люка/двери → заменяем на нейтральный 'ușă'
        cleaned = re.sub(r"\bpantof\b", "ușă", cleaned)
        # На всякий случай приводим 'Jaguar tiger' к Jagdtiger
        cleaned = re.sub(r"Jaguar[ -]?tiger", "Jagdtiger", cleaned)

    # === UNIVERSAL FIXES (all languages) ===

    # 1. Fix stuck sentences: "word.Word" → "word. Word" — Unicode-aware через str.isupper(),
    #    ловит заглавную ЛЮБОГО скрипта (латиница с диакритикой, кириллица, греческий, …)
    #    БЕЗ перечисления алфавитов. Десятичные "3.14" не задеты (цифра не isupper).
    #    Известный минор: "U.S." → "U. S." (в этих текстах редкость, допустимо).
    def _space_after_sentence(s: str) -> str:
        out = []
        for i, ch in enumerate(s):
            out.append(ch)
            if ch in ".!?" and i + 1 < len(s) and s[i + 1].isupper():
                out.append(" ")
        return "".join(out)
    cleaned = _space_after_sentence(cleaned)

    # 1b. Fix stuck after comma: "word,Word" → "word, Word"
    cleaned = re.sub(
        r',([A-ZА-ЯÀ-ÖØ-ÝĀ-ŽŐŰĐŁŃŚŹŻĆÓĄĘÅÆØÞÐÝÉÍÓÚÜŐŰŘŤŇĎŠŽČĚÖÜÄẞЁҐЄІЇȘȚĂ])',
        r', \1',
        cleaned
    )

    # 1c. Fix double dots ".." → "." (but preserve "..." ellipsis)
    cleaned = re.sub(r'\.{4,}', '...', cleaned)  # 4+ dots → ellipsis
    cleaned = re.sub(r'(?<!\.)\.\.(?!\.)', '.', cleaned)  # exactly 2 dots → 1 dot

    # 1d. Remove Russian patronymics from non-Russian texts
    # Patronymics match pattern: word ending in -ewicz/-owicz/-jewicz/-owna/-jewna/-iewna (transliterated)
    # or Cyrillic -евич/-ович/-овна/-евна
    if lang_lower not in ("russian", "ukrainian", "belarusian"):
        # Transliterated patronymics (Latin script): "Siergiejewicz", "Aleksiejewna", "Nikołajewicz"
        # WARNING: \s+ съедает \n\n и ломает 1:1 структуру абзацев в preserve_paragraphs режиме —
        # используем [\t ]+ (только горизонтальный whitespace).
        cleaned = re.sub(r'[\t ]+[A-ZА-Я]\w*(?:jewicz|iewicz|owicz|jewna|iewna|owna)\b', '', cleaned, flags=re.IGNORECASE)
        # Cyrillic patronymics that leaked into non-Russian text
        cleaned = re.sub(r'[\t ]+[А-Я]\w*(?:евич|ович|евна|овна)\b', '', cleaned, flags=re.IGNORECASE)

    # 2. German: capitalize after period (after fix 1)
    if lang_lower in ("german", "deutsch"):
        def _cap_after_period(m):
            return m.group(1) + ' ' + m.group(2).upper()
        cleaned = re.sub(r'([.!?])\s+([a-zäöüß])', _cap_after_period, cleaned)

    # 3. Remove duplicate sentences
    cleaned = _dedup_sentences(cleaned)
    
    # 3.5. Fix Rewrite 1 issues (stuttering, repeated phrases)
    # Удаляем заикания типа "stworzony, stworzony" или "ukończony, ukończony"
    # Паттерн: слово, запятая, пробел, то же слово
    cleaned = re.sub(r'\b(\w+),\s+\1\b', r'\1', cleaned, flags=re.IGNORECASE)
    
    # Удаляем повторяющиеся фразы в одном предложении
    # Например: "w kościołach i pałacach... w kościołach i pałacach"
    # Разбиваем на предложения и проверяем каждое
    sentence_endings = re.finditer(r'[.!?]+\s*', cleaned)
    sentence_starts = [0] + [m.end() for m in sentence_endings]
    sentence_starts.append(len(cleaned))
    
    fixed_parts = []
    for i in range(len(sentence_starts) - 1):
        sentence = cleaned[sentence_starts[i]:sentence_starts[i+1]]
        
        # Ищем повторяющиеся фразы длиной 3-7 слов в предложении
        words = sentence.split()
        found_repeat = False
        if len(words) >= 6:  # Минимум 6 слов для поиска повторов
            # Ищем фразы из 3-7 слов, которые повторяются
            for phrase_len in range(7, 2, -1):  # От 7 до 3 слов
                if found_repeat:
                    break
                for start in range(len(words) - phrase_len * 2 + 1):
                    phrase = words[start:start + phrase_len]
                    phrase_text = ' '.join(phrase).lower().strip('.,;:!?')
                    if len(phrase_text) < 15:  # Слишком короткая фраза
                        continue
                    # Ищем повтор этой фразы дальше в предложении
                    for check_start in range(start + phrase_len, len(words) - phrase_len + 1):
                        check_phrase = words[check_start:check_start + phrase_len]
                        check_phrase_text = ' '.join(check_phrase).lower().strip('.,;:!?')
                        if phrase_text == check_phrase_text:
                            # Найден повтор - удаляем второй экземпляр
                            words = words[:check_start] + words[check_start + phrase_len:]
                            found_repeat = True
                            break
                    if found_repeat:
                        break
        
        if found_repeat:
            sentence = ' '.join(words)
        
        fixed_parts.append(sentence)
    
    cleaned = ''.join(fixed_parts)
    
    # Удаляем повторяющиеся фразы типа "zburzony w roku..." в разных предложениях
    # Паттерн: глагол + "w roku" + дата/текст
    date_verb_pattern = r'\b(zburzony|zbudowany|ukończony|stworzony|rozpoczęty|zakończony)\s+w\s+roku[^.!?]{0,50}'
    matches = list(re.finditer(date_verb_pattern, cleaned, re.IGNORECASE))
    if len(matches) > 1:
        seen_phrases = {}
        # Проходим в обратном порядке, чтобы удалять последние дубликаты
        for match in reversed(matches):
            phrase = match.group(0).lower().strip()
            # Нормализуем фразу (убираем лишние пробелы)
            phrase_normalized = re.sub(r'\s+', ' ', phrase)
            if phrase_normalized in seen_phrases:
                # Дубликат - удаляем этот экземпляр
                start, end = match.span()
                cleaned = cleaned[:start] + cleaned[end:]
            else:
                seen_phrases[phrase_normalized] = match

    # 4. Remove double outro
    last_paragraphs = cleaned.rstrip().split('\n\n')
    if len(last_paragraphs) >= 2:
        # Outro markers by language
        outro_markers = [
            # Thanks patterns
            r'(?i)\b(děkuj|merci|danke|grazie|dziękuj|obrigad|mulțumesc|спасибо|thanks|thank you)\b',
            # Subscribe patterns  
            r'(?i)\b(subskr|subscri|abonn|abonați|iscri|přihlás|подпис|odběr)\b',
            # "Good night" patterns
            r'(?i)\b(dobrou noc|bonne nuit|gute nacht|buona notte|dobranoc|boa noite|noapte bună|спокойной ночи|good night)\b',
            # "See you" patterns
            r'(?i)\b(nashledanou|au revoir|auf wiedersehen|arrivederci|do zobaczenia|até|ne vedem|до встречи|увидимся)\b',
        ]
        
        def _is_outro(paragraph: str) -> bool:
            matches = sum(1 for pattern in outro_markers if re.search(pattern, paragraph))
            return matches >= 2  # At least 2 different outro signals
        
        last = last_paragraphs[-1]
        second_last = last_paragraphs[-2]
        
        if _is_outro(last) and _is_outro(second_last):
            # Both are outros — remove the last one (added by merge_branding)
            cleaned = '\n\n'.join(last_paragraphs[:-1])

    # FINAL re-guard: блок удаления повторов (выше) делает ' '.join(sentence.split()),
    # что СЪЕДАЕТ граничный пробел предложения и пере-склеивает "wort.Wort". Date-dedup
    # и outro-removal тоже могут склеить. Повторно проставляем пробел после .!? перед
    # заглавной ЛЮБОГО скрипта — идемпотентно, гарантирует отсутствие склеек в выводе.
    cleaned = _space_after_sentence(cleaned)

    return cleaned


# ============== ПРОМПТЫ ==============

def get_system_prompt(lang_name: str, lang_display: str = "", analysis: Optional[Dict] = None, address_style: str = "", custom_prompt: str = "") -> str:
    """
    Возвращает системный промт для языка.
    address_style: "vy" = обращение на вы (formal), "ty" = на ты (informal), "" = не задано.
    custom_prompt: произвольные инструкции пользователя для адаптации контента (например, "Замени всех казахов на чехов").
    ВАЖНО: system_prompt должен быть одинаковым для всех чанков одного языка для кэширования!
    """
    # Определяем, нужно ли писать числа словами (для всех языков)
    use_words_for_numbers = True  # Всегда словами для всех языков
    
    # Определяем, нужна ли культурная адаптация для славянских/восточноевропейских языков
    needs_cultural_adaptation = lang_name.lower() in [
        "czech", "polish", "romanian", "ukrainian", "belarusian",
        "slovak", "bulgarian", "serbian", "croatian", "slovenian",
        "hungarian", "estonian", "latvian", "lithuanian",
        "portuguese",  # 2026-06-04: locale-lock pt-BR (см. lang_rules ниже)
    ]
    
    cultural_note = ""
    if needs_cultural_adaptation:
        lang_rules = {
            "polish": """
POLISH RULES:
- Officials: "Pan/Pani" (formal)
- Soviet: KGB→KGB, ZSRR, kosmonauta
- Dates: "14 marca 1978 roku"
- Complex sentences NATURAL - don't simplify!""",
            
            "czech": """
CZECH RULES:
- Officials: "vy" (formal)
- Soviet: KGB→KGB, SSSR, kosmonaut
- Dates: "14. března 1978"
- Long sentences NATURAL!""",

            "serbian": """
SERBIAN RULES — CRITICAL:
- WRITE IN LATIN SCRIPT (gajica) ONLY. NEVER use Cyrillic.
- ✅ CORRECT: "Kosmički aparat veličine manjeg automobila, sletio je na kometu."
- ❌ WRONG: "Космички апарат величине мањег аутомобила, слетио је на комету."
- Use Serbian Latin diacritics: č, ć, š, ž, đ, dž, lj, nj
- Soviet: KGB→KGB, SSSR, kosmonaut
- Officials: "vi" (formal)
- Dates: "14. marta 1978. godine"
- If the source text had Cyrillic — transliterate to Latin naturally (not letter-by-letter).""",

            "bulgarian": """
BULGARIAN RULES:
- Write in Cyrillic (native Bulgarian script).
- Officials: "Вие" (formal)
- Soviet: КГБ, СССР, космонавт
- Dates: "14 март 1978 г."
- Avoid Russian loanwords that differ from Bulgarian (проверяй окончания).""",

            "macedonian": """
MACEDONIAN RULES:
- Write in Macedonian Cyrillic (native script).
- Officials: "Вие" (formal)
- Soviet: КГБ, СССР, космонаут
- Dates: "14 март 1978 година"
- Macedonian-specific letters: ќ, ѓ, љ, њ, џ, ѕ, ј.""",
            
            "romanian": """
ROMANIAN RULES - CRITICAL FOR NATIVITY:

ARTICLES (DEFINITE/INDEFINITE):
- ALWAYS use definite articles when referring to specific nouns: "omul" (the man), "femeia" (the woman), "casa" (the house)
- Use indefinite articles for general/non-specific: "un om" (a man), "o femeie" (a woman)
- NEVER omit articles where they are grammatically required
- Examples: "etnograf" (ethnographer) → "etnograful" (the ethnographer), NOT "etnografic"

PRONOUNS AND REFLEXIVES - CRITICAL:
- "Să-l urmez" (follow him) NOT "Să mă urmez" (follow myself) - this is a catastrophic error!
- Check pronoun cases: accusative (îl, o, îi, le), dative (îi, le, li), reflexive (mă, te, se, ne, vă)
- "Să-l urmez" = "to follow him" (accusative)
- "Să mă urmez" = "to follow myself" (reflexive) - WRONG if meaning "follow him"
- Always verify pronoun reference matches the intended object

CASES AND DECLENSIONS:
- Accusative: "Văd omul" (I see the man), "Văd casa" (I see the house)
- Dative: "Îi dau cartea" (I give him the book), "Le spun" (I tell them)
- Genitive: "casa omului" (the man's house), "cartea femeii" (the woman's book)
- Vocative: "Ionule!" (John!), "doamnelor!" (ladies!)

PROFESSIONS AND NOUNS:
- "etnograf" (ethnographer) NOT "etnografic" (ethnographic - adjective)
- Professions are nouns, not adjectives: "arhitect", "medic", "profesor"
- Use correct gender: "etnograf" (m), "etnografă" (f)

NATURAL SPEECH PATTERNS:
- Use LIVING, MODERN Romanian, not old newspaper style
- Avoid literal translations from Russian - adapt sentence structure naturally
- Use natural Romanian word order, not Russian word order
- Prefer active voice: "El a spus" (He said) over passive constructions
- Use natural Romanian expressions and idioms when appropriate

SENTENCE STRUCTURE:
- Romanian allows flexible word order, but prefer natural patterns
- Don't force Russian sentence structure onto Romanian
- Use subordinating conjunctions naturally: "când" (when), "pentru că" (because), "deși" (although)
- Complex sentences are natural - don't oversimplify

VERB FORMS:
- Use correct verb conjugations for person, number, tense
- Imperative: "urmează-mă" (follow me), "urmează-l" (follow him)
- Subjunctive: "să urmez" (to follow), "să-l urmez" (to follow him)
- Past tense: "am urmărit" (I followed), "a urmărit" (he followed)

SOVIET/HISTORICAL TERMS:
- "tovarășul" (comrade), KGB, URSS
- Use Romanian historical terminology naturally

AVOID TRANSLATIONESE:
- Don't sound like a translator - sound like a native Romanian storyteller
- Don't use "dry" or "newspaper" style - use vivid, engaging narrative
- Make it feel like the story was originally written in Romanian
- Use natural Romanian rhythm and flow, not mechanical translation

WORD VERIFICATION - CRITICAL:
- Use ONLY real Romanian dictionary words. NEVER invent words.
- If unsure about a word, use a simpler synonym.
- Common errors to avoid:
  * "mătăsură" → CORRECT: "mătase" (silk)
  * "ultraginală" → CORRECT: "ultimă" (final/last)
  * "ultraginalimă" → CORRECT: "ultimă"
- "plain sight" = "la vedere" or "sub ochii tuturor", NEVER leave in English
- "Beaux-Arts" can stay as-is (it's the style name), but explain in Romanian if needed
""",
            "turkish": """
TURKISH RULES - CRITICAL:
- Formal: "siz" form throughout
- ALL YEARS MUST INCLUDE "bin" (thousand):
  ✅ CORRECT: "bin sekiz yüz kırk beş" (1845)
  ❌ WRONG: "sekiz yüz kırk beş" (845)
  Every year from 1000-1999 MUST start with "bin".
  "bin yedi yüz" (1700s), "bin sekiz yüz" (1800s), "bin dokuz yüz" (1900s).
  If you write a year without "bin", the TTS voice will say "845" instead of "1845".
  This is a CRITICAL FAILURE.
- Outro: "Sizinle birlikte Emre Kaya'ydı" (NOT "birlikte oydu Emre Kaya")
- Soviet: KGB→KGB, SSCB, kozmonot
- Complex sentences natural for Turkish
- NO repetition in opening sentence (don't repeat subject twice)
""",

            "portuguese": """
PORTUGUESE RULES — BRAZILIAN PORTUGUESE ONLY (pt-BR), CONSISTENTLY across the ENTIRE text:
- Lexis: celular NOT telemóvel, ônibus NOT autocarro, tela NOT ecrã, trem NOT comboio,
  gênio NOT génio, fato NOT facto, time NOT equipa, café da manhã NOT pequeno-almoço,
  banheiro NOT casa de banho, geladeira NOT frigorífico, suco NOT sumo, xícara NOT chávena,
  você NOT tu (verb forms tertia persona singular accordingly).
- Syntax: gerund "estar fazendo / está acontecendo" NOT European "estar a fazer / está a acontecer".
- Orthography: Brazilian spelling — gênio, ideia, neurônios, ônibus, econômico, fenômeno
  (NOT génio, ideia, neurónios, autocarro, económico, fenómeno).
- Common words: legal/bacana (cool), cara (guy), criança (child).
- NEVER mix European Portuguese (pt-PT) forms. If unsure, choose the Brazilian form.
- If the source text contains pt-PT artifacts (telemóvel, autocarro etc.), CONVERT them to pt-BR.
""",
        }
        
        # Используем lang_display если есть, иначе lang_name для консистентности
        display_name = lang_display if lang_display else lang_name
        cultural_note = "\n" + lang_rules.get(lang_name.lower(), 
            f"""
CULTURAL ADAPTATION FOR {display_name.upper()}:
- Adapt historical context to {display_name} perspective
- Use natural {display_name} phrasing, not word-by-word translation
- Consider local historical sensitivities and context
- Write as a native {display_name} speaker would, not as a translator""")
        # Запрет русских отчеств для всех нерусских языков
        if lang_name.lower() not in ("russian",):
            cultural_note += f"""

⚠️ PATRONYMICS BAN: Russian patronymics (отчества ending in -евич/-овна like "Сергеевич", "Алексеевна") 
do NOT exist in {display_name}. Use ONLY first name + surname (e.g. "Marek Wolański", NOT "Marek Siergiejewicz Wolański").
If the source text has patronymics — DROP them completely, keep only first name and surname."""
    
    
    numbers_rule = ("Render EVERY number, year, decade, ordinal, range and quantity as fully spelled-out words IN " + lang_name + ". The output must contain ZERO digits and ZERO English number-words. This includes decade phrases: 'nineteen-nineties', 'the 1990s', 'nineteen-twenties' MUST become the " + lang_name + " words for that decade — never left in English. If you are about to write 'nineteen', 'twenty', '-nineties', a digit, or '19th' — stop and spell it in " + lang_name + ".") if use_words_for_numbers else "Write numbers as DIGITS (e.g., 1943, 17:42, not words)"
    
    # Если есть анализ - включаем его в промпт
    context_section = ""
    if analysis:
        context_section = f"""
═══════════════════════════════════════════════════════════
TEXT CONTEXT (understand this BEFORE adapting):
═══════════════════════════════════════════════════════════

GENRE: {analysis.get('genre', 'unknown')}
STYLE: {analysis.get('writing_style', 'unknown')}
PACING: {analysis.get('pacing', 'unknown')}

CRITICAL ELEMENTS (you MUST preserve ALL):
{chr(10).join(f"  • {elem}" for elem in analysis.get('critical_elements', ['All content'])[:10])}

CONTENT BREAKDOWN:
  Dialogue: {analysis.get('content_breakdown', {}).get('dialogue_percentage', 25)}%
  Description: {analysis.get('content_breakdown', {}).get('description_percentage', 50)}%
  Action: {analysis.get('content_breakdown', {}).get('action_percentage', 15)}%
  Reflection: {analysis.get('content_breakdown', {}).get('reflection_percentage', 10)}%

PRESERVATION PRIORITY:
{chr(10).join(f"  {i+1}. {rule}" for i, rule in enumerate(analysis.get('preservation_priority', ['All content'])[:5]))}

YOUTUBE OPTIMIZATION:
  {analysis.get('youtube_optimization', {}).get('target_retention', 'Detailed content for engagement')}
  Style: {analysis.get('youtube_optimization', {}).get('style_notes', 'Natural pacing')}

═══════════════════════════════════════════════════════════
"""

    # Базовый промпт (расширенный для гарантии ≥4096 символов = ≥1024 токенов для кэширования)
    # ВАЖНО: Этот промпт должен быть одинаковым для всех чанков одного языка!
    base_prompt = f"""{context_section}You are an expert {lang_name} adaptation specialist for YouTube content.

═══════════════════════════════════════════════════════════
MISSION: PERFECT ADAPTATION (80+ quality score)
═══════════════════════════════════════════════════════════

Based on the TEXT CONTEXT above, you understand:
- What genre and style to maintain
- What elements are critical
- What pacing to preserve
- How to optimize for YouTube audience

═══════════════════════════════════════════════════════════
RETENTION — INTEREST HOOKS THROUGHOUT (critical for watch-time & re-watches)
═══════════════════════════════════════════════════════════
Keep the viewer hooked across the ENTIRE text, not only at the opening.
Roughly every 3000-5000 characters, place an interest trigger that makes
the listener want to keep going (and come back to re-watch):
  - open a curiosity loop / pose an intriguing question;
  - tease what is coming ("but the strangest part is still ahead...");
  - a gentle mini-cliffhanger or surprising turn taken FROM the source;
  - a forward-reference that pays off later in the text.
HOW (without breaking the no-invention rule): build these hooks ONLY by
FRAMING and emphasis of material that is ALREADY in the source — rephrase
for intrigue, foreground the most surprising existing fact, vary sentence
rhythm to build a small wave of curiosity. Do NOT invent new facts, events,
characters or details. Keep the calm, sleep-friendly tone — hooks are soft
pulls of curiosity, NEVER loud clickbait or jarring cliffhangers.

ABSOLUTE REQUIREMENTS:

1. LENGTH GUIDANCE
   Per-paragraph output length: 0.7×–1.3× of corresponding input paragraph (in characters).
   Do NOT pad to hit a length target. Do NOT shorten by omitting content.
   If source paragraph is short — output stays short. If source is long — output stays long.
   NEVER add new actions, descriptions, sensory details, atmosphere, or characters
   that are not present in the source. Faithful transfer of meaning is the goal,
   not enrichment.

2. CONTENT TYPE PRESERVATION
   Based on content_breakdown above:

   For DIALOGUE (if input has dialogue):
   - Every exchange must be present
   - Preserve speaking style and personality
   - Do not paraphrase away meaning, but do not embellish either

   For DESCRIPTION (if input has descriptions):
   - Preserve EXISTING details
   - Do NOT invent new visual/sensory details, do NOT enrich atmosphere

   For REFLECTION (philosophical/internal monologue):
   - Preserve the thoughts that ARE in the source
   - Do not extend or deepen them beyond what is written

   For ACTION:
   - All steps/events preserved
   - Pacing maintained

3. CRITICAL ELEMENTS CHECKLIST
   Before outputting, verify EACH element from preservation_priority is present
   and that NO new elements were invented.

4. COUNTING VERIFICATION (do in your head)
   Count before submitting:
   - Input paragraphs: ___ → Output paragraphs: ___ (must be EQUAL when 1:1 mode is on)
   - Input sentences per paragraph: roughly preserved
   If counts don't match — re-check; do NOT pad with new content.

5. GENRE-SPECIFIC RULES

   If PSYCHOLOGICAL_HORROR:
   - Preserve existing slow-burn beats; do not add new ones
   - Preserve existing atmospheric descriptions verbatim in spirit; do not invent

   If THRILLER:
   - Preserve existing pacing beats; do not add new ones

   If SCIFI_DOCUMENTARY:
   - Technical details = preserve precisely, do not simplify, do not invent
   - Procedures = step-by-step preservation, no extra steps

═══════════════════════════════════════════════════════════
TECHNICAL REQUIREMENTS ({lang_name}):
═══════════════════════════════════════════════════════════

CRITICAL RULES:

1. ZERO RUSSIAN: No Cyrillic, no Russian words in Latin script (vachtor, przeleścia, etc.)

2. NATIVE LANGUAGE ONLY:
   - Use natural {lang_name} grammar, word order, expressions
   - Sound like native {lang_name} writing, not translation
   - Use modern, living {lang_name}, not bureaucratic style
   - ADDRESS (you): {"Use formal 'you' (vy/вы/pan/Sie/vous/etc.) consistently throughout — no informal 'ty/ты'." if address_style == "vy" else "Use informal 'you' (ty/ты/du/tu/etc.) consistently throughout — no formal 'vy/вы'." if address_style == "ty" else "Use the natural address form for the genre and audience."}

3. TERMINOLOGY - Use EXISTING {lang_name} words, NEVER invent:
   CRITICAL EXAMPLES OF WRONG (DO NOT REPEAT THESE ERRORS):
   * Czech: "zájezdář" (tourist) for "вахтовик" → CORRECT: "turnusový pracovník"
   * Czech: "stážník" (intern) for "часовой" → CORRECT: "strážný" or "hlídka"
   * Czech: "poslechový znak" for "позывной" → CORRECT: "volací znak"
   * Czech: "ztlustlá" (fattened) for "осунувшееся" (emaciated) → use correct antonym
   * Czech: "pravda musí být řekena" → CORRECT: "řečena"
   * Polish: "pozywny" (nutritious) for "позывной" → CORRECT: "kryptonim" or "znak wywoławczy"
   * Polish: "cedrat" (citron) for "Cedr" (cedar) → CORRECT: "Cedr"
   * Polish: "przeleścia" (doesn't exist) for "пролески" → CORRECT: "przecinka" or "prześwit"
   * Polish: "Na wida" (calque) → CORRECT: "Z wyglądu"
   * Romanian: "vachtor" (doesn't exist) for "вахтовик" → CORRECT: "muncitor sezonier"
   * Romanian: "Polonerul" (doesn't exist) for "полковник" → CORRECT: "Colonelul"
   * Romanian: "un masă, o scaun" (wrong articles) → CORRECT: "o masă, un scaun"
   * Romanian: "un lămâi storc" (wrong form) → CORRECT: "o lămâie stoarsă"
   * Romanian: "rost la rești" (nonsense) → CORRECT: "cu gratii"

4. VERB MEANINGS - Use CORRECT meanings:
   * Romanian: "topi" = "to melt" (metal), NOT "to heat stove" → use "făcuse focul" or "încălzise"
   * Polish: "gaszić silnik" sounds wrong → use "zgasł" or "silnik zgasł"

5. CRITICAL TECHNICAL PROTECTION:
   - DO NOT TRANSLATE or distort tank/vehicle names, designations and project names (Jagdtiger, IS-2, IS-7, T-28, Maus, Tiger, Panther, T-34, KV, M103, T-95, etc.). Keep them EXACTLY as in the source. Never invent variants like "Ioškin", "Jaguar-tigr", "Ёшкин-2".
   - Military ranks MUST use proper {lang_name} equivalents (e.g. plukovník / pułkownik / colonel), NOT literal mixes.
   - CALIBERS & NUMBERS ARE SACRED: never change any numeric values (years, dates, calibers like 88 mm, 128 mm, 105 mm, weights, speeds). If you change a number, it is a CRITICAL FAILURE.
   - NO LANGUAGE MIXING: NEVER insert words from other target languages (no Czech in Romanian, no Polish in Czech, etc.).
   - NO HALLUCINATED TECHNICAL TERMS: if you don't know the precise term, use a safe generic native word ("door" = ușa/dveře/drzwi) instead of inventing nonsense ("shoe", "botek", etc.).

6. GRAMMAR - Follow {lang_name} grammar STRICTLY:
   - Articles: correct gender (Romanian: "o masă" not "un masă")
   - Cases: ALL nouns MUST decline correctly (Polish: "majorze" not "majorz", "boksie" not "bokse")
   - Pronouns: correct forms (avoid "Ja jestem" - drop pronouns when natural)
   - Verb forms: all correct
   - Word order: natural for {lang_name}, NOT Russian word order
   - Surnames: MUST decline in {lang_name} (Polish/ Czech surnames decline, not like English)

6. ZERO FOREIGN WORDS - NO LANGUAGE MIXING:
   - NO Russian words in ANY form (Cyrillic or Latin: "личное дело", "vachtor")
   - NO Spanish/English/other foreign words (e.g., "tiempo" in Polish text)
   - NO mixed languages - ONLY pure {lang_name}
   - If you see foreign word → find {lang_name} equivalent immediately

7. PROPER NOUNS - Preserve EXACTLY:
   - Space stations: "Salut 6" (NOT "Szałyt 6", "Szałt 6", "Salit 6")
   - Call signs: "Kedr" or "Cedr" (NOT "Kiełdr", "Cedru")
   - Historical names: preserve original spelling/transliteration
   - Technical terms: use standard {lang_name} terminology

8. NATIVE SYNTAX - NO CALQUES:
   - NO literal Russian word order
   - NO "I have fear" → use native expressions ("bałem się" not "miałem strach")
   - NO "podnosić się" for rocket launch → use "wystartował" or "poleciał"
   - Use natural {lang_name} idioms and expressions
   - Drop pronouns when natural (avoid "Ja jestem" if "Jestem" is natural)

9. NUMERALS AND DATES — ALWAYS IN WORDS:
   - ABSOLUTE RULE: render EVERY number, year, decade, ordinal, range and quantity as fully spelled-out words IN {lang_name}. The output must contain ZERO digits and ZERO English number-words. This is not a list of cases — it is a hard requirement: nothing numeric may remain as a digit or as an English word.
   - DECADES: phrases like "nineteen-nineties", "the 1990s", "nineteen-twenties" MUST become the {lang_name} words for that decade — never left in English. If you are about to write "nineteen", "twenty", "-nineties", a digit, or "19th" — stop and spell it in {lang_name}.
   - ALL numbers, years, dates, and centuries MUST be written in WORDS in {lang_name}, not digits or Roman numerals.
   - Years: e.g. 1943 → "thousand nine hundred forty-three" (or equivalent in {lang_name}); correct declension.
   - Centuries: e.g. "19th century" → "nineteenth century" in {lang_name} (e.g. Polish: "dziewiętnasty wiek", Czech: "devatenácté století", Romanian: "secolul al nouăsprezecelea"). NEVER output "19th", "XIX", "18th" — only words.
   - Dates: natural {lang_name} format, in words where appropriate for TTS.
   - Long numbers: in words, correct cases. Test: can a native speaker read it smoothly without stumbling?

9b. TRANSLATION/REWRITE ERROR PATTERNS (avoid machine-translation and neural-rewrite artifacts):
   - IDIOM CALQUES: Do not translate English (or source) idioms literally. Use equivalent idioms in {lang_name} (e.g. "advertised as such" → native phrasing for "presented as", not word-for-word).
   - FALSE FRIENDS IN TERMS: Use historically correct terms. Examples: "Cabinet cards" = cabinet-format photos / "cabinet portraits", NOT "cards for cabinet/filing". "Tintype" = ferrotypes/tintypes (photo on metal), NOT "color tint" or "color type images".
   - UNITS (CRITICAL): Convert imperial to metric by RECALCULATING the number, not just replacing the unit. Formula: 1 foot = 0.3048 m (or 1 m = 3.28 ft) → multiply feet by 0.305 to get meters. 1 inch = 2.54 cm. Example: "8 feet" or "8 ft" → 8 × 0.305 ≈ 2.44 m → write "about two and a half meters" (or "two point four meters"); "10 feet" → about 3 m. NEVER keep the same number and only change "feet" to "meters" (that is wrong). NEVER translate "feet" as body parts ("legs"). Output in metric, values in words where appropriate.
   - DATES IN WORDS: "1800s" = nineteenth century — write the century IN WORDS in {lang_name} (e.g. "nineteenth century" / "devatenácté století" / "secolul al nouăsprezecelea"), NOT "19th", "XIX" or "18th". Check context: 1800s = 19th century (1800–1899), NOT 18th. Mid-to-late 1800s = mid-to-late nineteenth century, in words.
   - RHYTHM: Avoid robotic, list-like syntax ("X matched, Y matched, Z fell correctly"). Rephrase for natural flow and voice-over (e.g. "Grain, light, shadows — everything was authentic").
   - REGISTER: Keep one consistent tone (e.g. engaging, slightly informal for video). No mixing formal/colloquial or switching "you" forms within the same text.

10. REPLACE FOREIGN BRANDING:
   - Remove old greetings/channel names/author names
   - Replace with new branding from instructions

11. Numbers and dates: {numbers_rule}. ALL dates and centuries IN WORDS in {lang_name} (e.g. nineteenth century, not "19th" or "XIX").
12. Length: Preserve all content (95-110% of input)
13. Remove markers: [музыка], [hudba], etc.
14. Output: [adapted text in {lang_name}]
15. SENTENCE START DIVERSITY:
   - No word should start more than 8% of sentences.
   - After writing, scan the first word of each sentence. If any word appears 10+ times, rephrase some sentences to start differently.
   - Techniques: invert clause order, start with time/place adverb, start with subordinate clause, start with participle.
   - Example: instead of 5x "Ale..." → vary with "Jenže...", "Přesto...", "Na druhou stranu...", "Naopak...", "Zato..."

16. VARIETY RULE (filler words):
   - Never use the same filler word more than 3 times per 1000 words.
   - Words to vary: "один/одна/одно", "jeden/jedna/jedno", "какой-то", "pewien", "某".
   - Use synonyms instead: "некий", "определённый", "тот самый", "pewien", "jakiś", "nějaký".
   - Scan your output: if you see "один" 5+ times in 1000 words, replace some with synonyms.

FINAL REMINDER: Your success metric is COMPLETENESS + QUALITY, not brevity.
YouTube viewers want FULL detailed stories, not compressed summaries."""
    
    # 2026-06-04 #12 fix: вклеиваем cultural_note в финальный prompt.
    # Раньше cultural_note собирался (Polish/Romanian/Portuguese rules), но НИКОГДА
    # не попадал в return base_prompt → все language-specific правила игнорировались.
    if cultural_note:
        base_prompt += f"""

═══════════════════════════════════════════════════════════
LANGUAGE-SPECIFIC RULES ({lang_name.upper()})
═══════════════════════════════════════════════════════════
{cultural_note}
═══════════════════════════════════════════════════════════
"""

    # Custom user instructions for content adaptation
    if custom_prompt:
        base_prompt += f"""

=== CUSTOM INSTRUCTIONS (HIGHEST PRIORITY) ===
The user has provided these specific instructions for this language version.
Apply them to EVERY chunk. These override any conflicting rules above.

{custom_prompt}

Apply these instructions naturally and consistently throughout the entire text.
=== END CUSTOM INSTRUCTIONS ===
"""

    return base_prompt

    # Проверяем что промпт достаточно длинный для кэширования
    # Для Haiku 4.5: минимум 2048 токенов = ~8200 символов, но лучше ~8800 для надёжности
    # Для Sonnet: минимум 1024 токена = ~4096 символов
    # Добавляем padding если нужно для гарантии кэширования
    current_length = len(base_prompt)
    min_length_for_haiku = 8800  # ~2200 токенов с запасом (минимум 2048)
    
    if current_length < min_length_for_haiku:
        # Добавляем дополнительный контент для достижения минимума
        additional_content = f"""

EXAMPLES OF CRITICAL ERRORS TO AVOID:

1. GRAMMATICAL HALLUCINATIONS:
Polish:
- WRONG: "majorz Krawcow" → CORRECT: "majorze Krawcowie" (correct declension)
- WRONG: "w izolowanym bokse" → CORRECT: "w izolowanym boksie" (correct case ending)
- WRONG: "Ja jestem" (robotic) → CORRECT: "Jestem" (drop pronoun when natural)

2. LANGUAGE MIXING (CRITICAL - ZERO TOLERANCE):
- WRONG: "jego личное дело spalono" (Russian in Polish text) → CORRECT: "jego osobiste akta spalono"
- WRONG: "Wasze tiempo nie jest" (Spanish in Polish) → CORRECT: "Wasz czas nie jest"
- WRONG: Any Cyrillic or foreign words → CORRECT: Pure {lang_name} only

3. PROPER NOUNS ERRORS:
- WRONG: "Szałyt 6", "Szałt 6", "Salit 6" → CORRECT: "Salut 6" (exact spelling)
- WRONG: "Kiełdr", "Cedru" → CORRECT: "Kedr" or "Cedr" (preserve original)
- Historical/technical terms: preserve EXACT spelling, don't invent

4. CALQUES AND WOODEN SYNTAX:
Polish:
- WRONG: "Oficjalnie Wołgin nie podnosił się w kosmos" → CORRECT: "Oficjalnie Wołgin nie wystartował w kosmos" (native verb)
- WRONG: "miałem strach" (literal "I had fear") → CORRECT: "bałem się" or "czułem lęk" (native expression)
- WRONG: Russian word order → CORRECT: Natural {lang_name} word order

5. UNITS — ADAPT THE VALUE, NOT JUST THE WORD:
- WRONG: "8 meters tall" when original was "8 feet" (you kept the number 8). 8 feet = 8 × 0.305 ≈ 2.44 m.
- CORRECT: "8 feet" → "about two and a half meters" or "two and a half metres" (value recalculated: 1 ft = 0.3048 m, 1 m = 3.28 ft).
- WRONG: "10 feet" → "10 meters". CORRECT: "10 feet" → "about three meters" (~3.05 m).

6. NUMERAL ERRORS:
Polish:
- WRONG: "tysiąc dziewięćset siedemdziesiątego siódmego roku" (wrong cases) → CORRECT: "tysiąc dziewięćset siedemdziesiątego siódmego roku" (verify all cases)
- Test: Can native speaker read date smoothly without stumbling?

Czech errors:
- "zájezdář" (tourist) for "вахтовик" → use "turnusový pracovník"
- "stážník" (intern) for "часовой" → use "strážný" or "hlídka"
- "poslechový znak" for "позывной" → use "volací znak"
- "ztlustlá" (fattened) for "осунувшееся" → use correct antonym

Romanian errors:
- "vachtor" (doesn't exist) for "вахтовик" → use "muncitor sezonier"
- "Polonerul" (doesn't exist) for "полковник" → use "Colonelul"
- "un masă, o scaun" (wrong articles) → use "o masă, un scaun"
- "topi" (to melt) for "топить печь" → use "făcuse focul" or "încălzise"

FINAL CHECKLIST BEFORE OUTPUT:
☐ NO foreign words (Russian, Spanish, English) - scan entire text
☐ Proper nouns spelled correctly (Salut 6, Kedr/Cedr)
☐ NO robotic phrases ("Ja jestem" → "Jestem")
☐ Surnames decline correctly (Polish/Czech requirement)
☐ NO calques - use native expressions
☐ Word order is natural for {lang_name}, not Russian
☐ Numerals and dates: all in WORDS in target language; correct cases
☐ DATES/CENTURIES: In words only (e.g. "nineteenth century"), not "19th"/"XIX"; 1800s = nineteenth century, correctly translated
☐ TECHNICAL: Feet/inches converted using 1 ft = 0.305 m (1 m = 3.28 ft), 1 in = 2.54 cm — number must change (e.g. 8 ft → ~2.5 m), not just the unit; no "feet" as body parts
☐ TERMINOLOGY: Cabinet cards → cabinet-format photos; Tintype → ferrotypes/tintypes (not "color tint")
☐ AUDIO: Read aloud mentally — no stumbling; natural rhythm for voice-over
☐ CULTURAL: No misplaced local names/toponyms for wrong country
☐ Test: Would native speaker read this smoothly? Full nativeness of translation.

REMEMBER:
- Use ONLY existing {lang_name} vocabulary
- Verify verb meanings match the action
- Check grammar: articles, cases, pronouns, verb forms
- Write as native {lang_name} speaker, not translator
- Preserve all details, maintain natural flow"""
        base_prompt += additional_content
    
    return base_prompt


def get_rewrite_prompt(lang_name: str, text: str, chunk_index: int, total_chunks: int,
                      lang_display: str = "", channel_name: str = "", host_name: str = "",
                      analysis: Optional[Dict] = None, prev_context: str = "",
                      preserve_paragraphs: bool = False, expected_paragraphs: int = 0,
                      strict_retry: bool = False) -> str:
    """Возвращает промт для рерайта одного чанка.

    ВАЖНО: просим модель вернуть ТОЛЬКО готовый текст чанка в КВАДРАТНЫХ СКОБКАХ,
    без каких‑либо логов, заголовков, чек-листов и метрик.

    preserve_paragraphs/expected_paragraphs: если True и >0, добавляется жёсткое требование
    выдать ровно столько же абзацев (для покадровой синхронизации 1:1).
    strict_retry: усиленная формулировка для повторной попытки после провала валидации.
    """
    
    # Подсчёт метрик
    input_length = len(text)
    input_sentences = text.count('.') + text.count('!') + text.count('?')
    input_paragraphs = len([p for p in text.split('\n\n') if p.strip()])
    
    # Определить роль чанка в общей структуре
    if total_chunks == 1:
        chunk_role = "COMPLETE TEXT - entire story in one chunk"
    elif chunk_index == 0:
        chunk_role = "OPENING - establish setting, introduce characters, create hook"
    elif chunk_index == total_chunks - 1:
        chunk_role = "ENDING - resolve story, maintain depth, satisfy viewer"
    else:
        chunk_role = f"MIDDLE PART {chunk_index+1}/{total_chunks} - develop story, maintain engagement"
    
    # Определить тип контента в чанке (простая эвристика)
    has_dialogue = text.count('"') > 3 or text.count('—') > 3
    has_description = len(text.split('\n\n')) > 3
    
    content_type_notes = []
    if has_dialogue:
        content_type_notes.append("Contains DIALOGUE - preserve every exchange completely")
    if has_description:
        content_type_notes.append("Contains DESCRIPTIONS - preserve existing details, do NOT add new ones or enrich")
    
    # Простые инструкции брендирования только для нужных чанков
    branding_instructions = ""
    actual_host_name = host_name if host_name and host_name.strip() else "Anatolij Eugenjanovič Kazancev"

    # STRICT NAMING block — добавляется во все ветки чтобы убить галлюцинации имён.
    _strict_naming = f"""

STRICT NAMING — NEVER INVENT, NEVER TRANSLATE, NEVER SUBSTITUTE:
- Host name MUST be EXACTLY: "{actual_host_name}" (character-for-character, including diacritics).
  Do NOT output "Pedro", "Carlos", "Anatolij" or any other name if host is "{actual_host_name}".
  Do NOT translate the name to a {lang_name} equivalent. Use the name AS GIVEN.
- Channel name MUST be EXACTLY: "{channel_name}" (character-for-character, including special chars).
  Do NOT output "Canal Café", "Channel X", or any variation if channel is "{channel_name}".
  Do NOT translate the channel name. Use the name AS GIVEN.
- If you write any host/channel name that differs from these exact strings — that is a FAILURE."""

    if total_chunks == 1 and (channel_name or host_name):
        # 2026-06-04 #11 fix: single-chunk случай — комбинируем FIRST (intro after hook)
        # И LAST (sign-off в конце). Раньше if/elif брал только FIRST → sign-off терялся.
        branding_instructions = f"""

CRITICAL BRANDING REPLACEMENT FOR SINGLE-CHUNK TEXT (BOTH intro AND sign-off):

1. SCAN AND REPLACE existing branding (if any in source):
   - Replace old greetings / old author names / old channel names with the new branding below.
   - If source has no branding — just add the new branding at the right places.

2. INTRO (after the hook, around 20-40% of text):
   - Start with the hook — NO branding in hook itself.
   - After the hook, insert a brief introduction (1-2 sentences) using:
     * Host: "{actual_host_name}"
     * Channel: "{channel_name}"
   - Format examples (adapt to {lang_name}):
     * Czech: "Jsem {actual_host_name}, a toto je {channel_name}, kde odkrýváme..."
     * Polish: "Jestem {actual_host_name}, a to jest {channel_name}, na którym odkrywamy..."
     * Portuguese (pt-BR): "Sou {actual_host_name}, e aqui no canal {channel_name} contamos..."

3. SIGN-OFF AS A SEPARATE FINAL SENTENCE (mandatory):
   - Keep the source's closing/reflective sentence INTACT. Do NOT rewrite its meaning.
     Do NOT make "{actual_host_name}" the grammatical subject of an author's reflective
     sentence (NEVER «{actual_host_name} thought about...»).
   - THEN append the sign-off as a NEW, SEPARATE final sentence using ONLY "{actual_host_name}"
     and "{channel_name}".
   - Format examples (adapt to {lang_name}):
     * English: "With you was {actual_host_name}. See you in the next episode of {channel_name}."
     * Russian: "С вами был {actual_host_name}. До встречи на канале {channel_name}."
     * Polish:  "Z wami był {actual_host_name}. Do zobaczenia na kanale {channel_name}."
     * Portuguese (pt-BR): "Aqui foi {actual_host_name}. Até o próximo episódio do canal {channel_name}."
   - Then ONE short call to subscribe and like (separate sentence).

4. APPLY CONSISTENTLY across languages — if host/channel are provided, sign-off MUST appear
   at the end. Never "in one language present, in another absent".{_strict_naming}"""

    elif chunk_index == 0 and (channel_name or host_name):
        # Первый чанк: хук без брендирования, затем интро после хука
        branding_instructions = f"""

CRITICAL BRANDING REPLACEMENT FOR FIRST CHUNK:
1. SCAN AND REPLACE existing branding/author introduction (MANDATORY):
   - Look for greetings at the start: "Добро пожаловать в закрытый архив" / "Vítejte v uzavřeném archivu" / "Witajcie w zamkniętym archiwum" / "Bun venit în arhiva închisă" / "Welcome to closed archive"
   - Look for author introductions: "Меня зовут Анатолий Евгеньевич Казанцев" / "Jmenuji se Anatolij Eugenjanovič Kazancev" / "Nazywam się Anatolij Eugenjanovič Kazancev" / "Mă numesc Anatolij Eugenjanovič Kazanțev" / "My name is Anatolij Eugenjanovič Kazancev"
   - Look for old channel names: "Uzavřený archiv", "Zamknięte archiwum", "Arhiva închisă", "Zamknięte archiwum", "closed archive"
   - These are from the ORIGINAL text - you MUST find and REPLACE them, not keep them
   - If text starts with any greeting or channel name - REMOVE it completely
   - If text contains old author name - REPLACE it with: "{actual_host_name}" (or {lang_name} equivalent)
   - If text contains old channel name - REPLACE it with: "{channel_name}" (if provided)

2. Structure:
   - Start with the hook (most intense moment from story) - NO branding in hook
   - After the hook (around 30-40% of text), add NEW introduction:
     * Use ONLY: "{actual_host_name}" (or {lang_name} equivalent) for author
     * Use ONLY: "{channel_name}" (if provided) for channel
     * Format examples:
       - Czech: "Jsem {actual_host_name}, a toto je {channel_name}, kde odkrýváme..."
       - Polish: "Jestem {actual_host_name}, a to jest {channel_name}, na którym odkrywamy..."
       - Romanian: "Sunt {actual_host_name}, iar acesta este {channel_name}, unde descoperim..."
     * Keep it brief (1-2 sentences), natural transition after hook
   - CRITICAL: Do NOT keep old greetings, old author names, or old channel names - they MUST be replaced{_strict_naming}"""
    
    elif chunk_index == total_chunks - 1 and (channel_name or host_name):
        # Последний чанк: аутро в конце
        branding_instructions = f"""

CRITICAL BRANDING REPLACEMENT FOR LAST CHUNK:
1. REMOVE AND REPLACE existing branding/author mentions:
   - If text contains old author name "Анатолий Евгеньевич Казанцев" / "Anatolij Eugenjanovič Kazancev" in outro - REPLACE with new name
   - If text contains old channel names in outro - REPLACE with new channel name
   - Do NOT keep old branding - REPLACE it

2. Structure — SIGN-OFF AS A SEPARATE FINAL SENTENCE (2026-06-04):
   - Keep the original closing/reflective sentence INTACT. Do NOT rewrite its meaning.
     Do NOT make "{actual_host_name}" the grammatical subject of an author's reflective
     sentence (e.g. NEVER «{actual_host_name} thought about...»). The author's closing
     belongs to the author, not the host.
   - THEN append the sign-off as a NEW, SEPARATE final sentence using ONLY "{actual_host_name}"
     and "{channel_name}".
     * Format examples (adapt to {lang_name}):
       - English: "With you was {actual_host_name}. See you in the next episode of {channel_name}."
       - Russian: "С вами был {actual_host_name}. До встречи на канале {channel_name}."
       - Polish:  "Z wami był {actual_host_name}. Do zobaczenia na kanale {channel_name}."
       - Portuguese (pt-BR): "Aqui foi {actual_host_name}. Até o próximo episódio do canal {channel_name}."
     * Then ONE short call to subscribe and like (separate sentence).
   - APPLY CONSISTENTLY in EVERY language — if host/channel are provided, the sign-off
     MUST appear at the end. Never "in one language present, in another absent".{_strict_naming}"""
    
    else:
        # Средний чанк: CTA в середине (если это средний чанк)
        middle_chunk = total_chunks // 2
        if chunk_index == middle_chunk and channel_name:
            branding_instructions = f"""

CRITICAL BRANDING REPLACEMENT FOR MIDDLE CHUNK:
1. REMOVE AND REPLACE existing CTA/branding:
   - If text contains old channel names in CTA sections - REPLACE with new channel name
   - Do NOT keep old channel mentions - REPLACE them

2. Structure:
   - Continue the story naturally
   - In the middle, after a natural pause, add NEW brief CTA:
     * Use ONLY the channel name: "{channel_name}"
     * Format: "Friends, a small request. Support [channel] with a like, it helps the channel grow." (adapt to {lang_name})
     * Keep it SHORT (1-2 sentences), then continue story
   - Replace any existing CTA with the new one"""
    
    # Всегда используем слова для чисел (пример для напоминания модели)
    if lang_name.lower() == "russian":
        numbers_ex = "1943→тысяча девятьсот сорок три"
    elif lang_name.lower() == "polish":
        numbers_ex = "1943→tysiąc dziewięćset czterdzieści trzy"
    elif lang_name.lower() == "czech":
        numbers_ex = "1943→tisíc devět set čtyřicet tři"
    elif lang_name.lower() == "romanian":
        numbers_ex = "1943→o mie nouă sute patruzeci și trei"
    else:
        numbers_ex = f"1943→write as words in {lang_name}"
    
    # Краткая текстовая подсказка по роли чанка (без служебных заголовков в ответе!)
    role_note = f"This part is the {chunk_role} of the overall story."

    content_note = ""
    if content_type_notes:
        content_note = "Content hints for this chunk:\n" + "\n".join(f"- {note}" for note in content_type_notes)

    # Блок жёстких требований к структуре абзацев (для покадровой синхронизации)
    paragraph_block = ""
    if preserve_paragraphs and expected_paragraphs > 0:
        prefix = "STRUCTURE FAILURE — RETRY. " if strict_retry else ""
        paragraph_block = f"""

═══════════════════════════════════════════════════════════
{prefix}HARD CONSTRAINT — paragraph length:
═══════════════════════════════════════════════════════════
- Output paragraphs must match input count exactly. Length 0.7×–2.0× per paragraph.
- Translation expansion 1.0×–1.4× is normal for Russian/German/Turkish (longer words, diacritics).
- Output <0.8× of input = under-compression — restore content; do NOT over-summarize.
- Output >2.0× of input = bloat — compress; do NOT enrich.
- NEVER add new actions, descriptions, sensory details, atmosphere, or characters not present in source.
- NEVER expand a short action like "X tips water" into a multi-sentence reflection.

═══════════════════════════════════════════════════════════
{prefix}PARAGRAPH STRUCTURE — NON-NEGOTIABLE (1:1 ALIGNMENT REQUIRED)
═══════════════════════════════════════════════════════════
- The input below has EXACTLY {expected_paragraphs} paragraphs separated by blank lines.
- Your output MUST have EXACTLY {expected_paragraphs} paragraphs in the SAME order.
- Mapping is strictly 1:1: input paragraph #N → output paragraph #N.
- DO NOT merge two input paragraphs into one output paragraph.
- DO NOT split one input paragraph into two output paragraphs.
- DO NOT reorder, omit, or insert extra paragraphs.
- An empty input paragraph → an empty output paragraph at the same position.

⚠️ PARAGRAPH SEPARATOR — CRITICAL FORMAT (most common failure mode):
- Between every two paragraphs put EXACTLY ONE COMPLETELY EMPTY LINE.
- That means the bytes between paragraphs are: <newline><newline> — a blank line.
- Single newline inside a paragraph keeps it as the SAME paragraph.
- A blank line (two consecutive newlines) starts a NEW paragraph.
- DO NOT join all paragraphs into one big block with single newlines — that produces 1 paragraph and FAILS the check.
- Visual layout your output must look like:
    Paragraph 1 text.
    <blank line>
    Paragraph 2 text.
    <blank line>
    Paragraph 3 text.
- This is required for frame-by-frame video alignment. Failure breaks the downstream pipeline.
═══════════════════════════════════════════════════════════"""

    # Очень жёстко задаём формат вывода: только текст в одних скобках
    prompt = f"""You are adapting CHUNK {chunk_index+1} of {total_chunks} from Russian into natural, high‑quality {lang_name}.

{role_note}
{content_note}
{paragraph_block}

Branding instructions (apply only inside the story text, not as separate headers):
{branding_instructions}

TASK:
- Rewrite the following source text into fluent, native‑sounding {lang_name}, preserving ALL facts, technical details, dates, names and narrative flow.
- Remove any existing greetings/outros and old channel/author names, and use ONLY these if provided:
  * Author name: "{actual_host_name}"
  * Channel name: "{channel_name}" (if not empty)
- Remove any subtitle markers like [музыка], [music], [muzika], [muzică], [muzyka], etc.
- Render EVERY number, year, decade, ordinal, range and quantity as fully spelled-out words IN {lang_name} (e.g. {numbers_ex}). The output must contain ZERO digits and ZERO English number-words. Decade phrases like "nineteen-nineties", "the 1990s", "nineteen-twenties" MUST become the {lang_name} words for that decade — never left in English. If you are about to write "nineteen", "twenty", "-nineties", a digit, or "19th" — stop and spell it in {lang_name}.
- Ensure ZERO Russian words remain (no Cyrillic, no Latin transliteration) – always replace them with natural {lang_name} words.
- Avoid literal calques, invented words and wooden syntax as described in the system instructions.
- UNITS: Convert feet/inches by RECALCULATING: 1 foot = 0.3048 m (1 m = 3.28 ft), 1 inch = 2.54 cm. E.g. 8 ft → ~2.5 m, 10 ft → ~3 m. Write the new value in metric (in words where natural); never keep the same number with "meters"; never use "feet" as body parts.
- DATES AND CENTURIES: Write ALL dates and centuries IN WORDS in {lang_name} (e.g. "nineteenth century", not "19th" or "XIX"). "1800s" = nineteenth century — translate correctly and in words.
- TERMINOLOGY: Use correct terms (e.g. Cabinet cards → cabinet-format photos; Tintype → ferrotypes/tintypes), not literal mistranslations.

OUTPUT FORMAT (STRICT, FOR TTS):
- Return ONLY the final adapted story text for this chunk, ready to be read by a voice over.
- NO headings, NO titles, NO section labels, NO \"analysis\", NO \"verification\", NO checklists, NO bullet lists, NO metrics, NO debug/log lines.
- Do NOT include things like: "KONTROLA PŘED VÝSTUPEM", "VERIFICATION CHECKLIST", "INPUT METRICS", "ANALÝZA", \"CHECKLIST\", \"PART 3/9\", etc.
- Do NOT include any Markdown syntax such as '#', '##', '---', '***', lists, checkboxes, emojis or decorative separators.
- Enclose the ENTIRE adapted story text in ONE SINGLE pair of square brackets, like:
  [тук идёт полный переписанный текст на {lang_name}]
- Do NOT use any other '[' or ']' characters inside the text. Only the outermost brackets are allowed.
- Do NOT add any text before or after the brackets.

{"CONTEXT FROM PREVIOUS CHUNK (use for continuity of names, style, and references — do NOT repeat this content):" + chr(10) + prev_context + chr(10) + chr(10) if prev_context else ""}SOURCE TEXT (Russian, keep all information, but fully rewrite in natural {lang_name}):
{text}

Now output ONLY the adapted {lang_name} text in ONE pair of square brackets, with no extra explanations.

CRITICAL: Do NOT describe what you will do. Do NOT write any plan or preface like "I need to" or "I will". Output ONLY the rewritten text in [square brackets], in full, starting immediately with the first word of the rewrite."""

    return prompt


# ============== API ==============

def call_openai_api(
    messages: List[Dict],
    model: str,
    api_key: str,
    log_queue: queue.Queue,
    task_info: str = "",
    max_tokens: int = 8192,
    service_tier: Optional[str] = None,
    use_caching: Optional[bool] = None,
) -> Tuple[bool, str]:
    """
    Вызов OpenAI API с бесконечными retry на rate limit.
    max_tokens: по умолчанию 8192 для длинных глав генерации и постпроцесса.
    service_tier: "default" (standard) или "flex" (50% дешевле, медленнее).
    use_caching: включить prompt cache retention 24h для длинных промптов (аналогично Anthropic).
    """
    import ssl

    if STOP_FLAG.is_stopped():
        return (False, "Остановлено пользователем")

    cfg = get_keys_config()
    tier = service_tier if service_tier is not None else cfg.get("gpt_service_tier", "default")
    caching = use_caching if use_caching is not None else cfg.get("gpt_use_cache", True)

    # Кэш неподдерживаемых возможностей — не повторять ошибку 100 раз
    caps = _load_model_capabilities().get(model, {})
    no_flex = caps.get("no_flex", False)
    no_cache = caps.get("no_cache", False)
    no_temperature = caps.get("no_temperature", False)

    url = "https://api.openai.com/v1/chat/completions"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    # gpt-5*, o*, o1*, o3*, o4* используют max_completion_tokens вместо max_tokens
    use_max_completion = any(x in model.lower() for x in ["gpt-5", "o3", "o4", "o1"])
    # gpt-5-mini: temperature только 1; prompt_cache_retention не поддерживается —
    # кэш работает автоматически (Cached input в прайсе), но явные параметры API отклоняет
    gpt5_mini_restrictions = "gpt-5-mini" in model.lower()
    data = {
        "model": model,
        "messages": messages,
    }
    if not gpt5_mini_restrictions and not no_temperature:
        data["temperature"] = 0.7
    if use_max_completion:
        data["max_completion_tokens"] = max_tokens
    else:
        data["max_tokens"] = max_tokens
    # Flex поддерживают не все модели (o3, o4-mini, gpt-5-mini и др.)
    if tier == "flex" and not no_flex:
        data["service_tier"] = "flex"
    if caching and not gpt5_mini_restrictions and not no_cache:
        data["prompt_cache_retention"] = "in_memory"
        system_content = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        if isinstance(system_content, str) and len(system_content) >= 1024:
            cache_key = hashlib.md5(system_content.encode()).hexdigest()[:16]
            data["prompt_cache_key"] = f"rewrite_{cache_key}"
    
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    attempt = 0
    max_network_retries = 10  # Сетевые ошибки - ограниченные retry
    
    while True:  # Бесконечный цикл для rate limit
        if STOP_FLAG.is_stopped():
            return (False, "Остановлено пользователем")
        
        try:
            # Добавляем базовую задержку между запросами
            delay = RATE_LIMITER.get_delay()
            if delay > 0.5:
                time.sleep(delay)
            
            # Debug: только summary (полный content перегружает CPU и GUI)
            debug_summary = {"url": url, "model": model, "messages": len(data.get("messages", [])), "content_lens": [len(str(m.get("content", ""))) for m in data.get("messages", [])]}
            debug_log(log_queue, "OpenAI REQUEST:", json.dumps(debug_summary, ensure_ascii=False))
            
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode('utf-8'),
                headers=headers,
                method='POST'
            )
            
            with urllib.request.urlopen(req, timeout=240, context=ssl_context) as response:
                result = json.loads(response.read().decode('utf-8'))
                content = result['choices'][0]['message']['content']
                RATE_LIMITER.on_success()
                if log_queue and caching:
                    usage = result.get("usage", {})
                    pt_details = usage.get("prompt_tokens_details", {})
                    cached = pt_details.get("cached_tokens", 0)
                    if cached > 0:
                        log_msg(log_queue, f"  💾 GPT cache hit: {cached} токенов")
                return (True, content)
                
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode('utf-8')
                error_json = json.loads(error_body)
                error_msg = error_json.get('error', {}).get('message', str(e))
            except:
                error_msg = str(e)
            debug_log(log_queue, "OpenAI HTTPError:", f"code={e.code} body={error_body} error_msg={error_msg}")
            
            if e.code == 429:
                # Rate limit - ждём и пробуем снова (БЕСКОНЕЧНО)
                wait_time = RATE_LIMITER.on_rate_limit()
                log_msg(log_queue, f"    ⏳ Rate limit{' ['+task_info+']' if task_info else ''}, жду {wait_time:.0f}с...")
                
                # Ждём с проверкой стоп-флага
                waited = 0
                while waited < wait_time:
                    if STOP_FLAG.is_stopped():
                        return (False, "Остановлено пользователем")
                    time.sleep(min(1, wait_time - waited))
                    waited += 1
                continue  # Пробуем снова
                
            elif e.code == 401:
                return (False, "Неверный API ключ")
            elif e.code == 404:
                return (False, f"Модель '{model}' не найдена")
            elif e.code == 400:
                err_lower = error_msg.lower()
                if "temperature" in err_lower and ("unsupported" in err_lower or "only the default" in err_lower):
                    if "temperature" in data:
                        del data["temperature"]
                        _save_model_capability(model, "no_temperature", True)
                        log_msg(log_queue, f"    ⚠️ Модель {model} поддерживает только temperature=1, запомнил")
                        continue
                if "invalid service_tier" in err_lower or ("service_tier" in err_lower and "unsupported" in err_lower):
                    if "service_tier" in data:
                        del data["service_tier"]
                        _save_model_capability(model, "no_flex", True)
                        log_msg(log_queue, f"    ⚠️ Модель {model} не поддерживает Flex, запомнил (standard)")
                        continue
                if "max_tokens" in err_lower and ("max_completion" in err_lower or "not supported" in err_lower):
                    if "max_tokens" in data:
                        data["max_completion_tokens"] = data.pop("max_tokens")
                        log_msg(log_queue, f"    🔄 Использую max_completion_tokens для {model}")
                        continue
                if "prompt_cache" in err_lower and "not supported" in err_lower:
                    for key in ["prompt_cache_retention", "prompt_cache_key"]:
                        if key in data:
                            del data[key]
                    _save_model_capability(model, "no_cache", True)
                    log_msg(log_queue, f"    ⚠️ Модель {model} не поддерживает кэш, запомнил")
                    continue
            elif e.code >= 500:
                # Ошибка сервера - retry с лимитом
                attempt += 1
                if attempt >= max_network_retries:
                    return (False, f"Ошибка сервера после {max_network_retries} попыток")
                log_msg(log_queue, f"    ⚠️ Ошибка сервера ({e.code}), повтор {attempt}...")
                time.sleep(3 * attempt)
                continue
            else:
                return (False, f"HTTP {e.code}: {error_msg[:80]}")
                
        except urllib.error.URLError as e:
            # Сетевая ошибка - retry с экспоненциальной задержкой (3, 6, 9, ... до 30 сек)
            debug_log(log_queue, "OpenAI URLError:", str(e))
            attempt += 1
            if attempt >= max_network_retries:
                return (False, f"Сетевая ошибка после {max_network_retries} попыток: {e.reason}")
            delay = min(3 * attempt, 30)
            log_msg(log_queue, f"    ⚠️ Сетевая ошибка, повтор {attempt} (пауза {delay}с)...")
            time.sleep(delay)
            continue
            
        except Exception as e:
            import traceback
            debug_log(log_queue, "OpenAI Exception:", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            attempt += 1
            if attempt >= max_network_retries:
                return (False, f"Ошибка после {max_network_retries} попыток: {str(e)[:80]}")
            delay = min(3 * attempt, 30)
            time.sleep(delay)
            continue


def _call_claude_api_once(
    messages: List[Dict],
    api_key: str,
    model: str,
    log_queue: queue.Queue,
    task_info: str = "",
    use_caching: bool = True
) -> Tuple[bool, str]:
    """Одиночный вызов Claude API (без ретраев). Возвращает __RETRY__:... при ошибках, пригодных для ретрая."""
    try:
        # Создаём кастомный HTTPX клиент с увеличенными таймаутами
        # Проблема: HTTPX по умолчанию имеет connect timeout 60 секунд, что вызывает "Client disconnected"
        # Решение: создаём HTTPX клиент с увеличенными таймаутами для всех операций
        # connect=300: время на установку соединения (5 минут)
        # read=300: время на чтение данных (5 минут) 
        # write=300: время на запись данных (5 минут)
        # pool=300: время на получение соединения из пула (5 минут)
        httpx_timeout = httpx.Timeout(
            connect=300.0,  # 5 минут на подключение (критично для создания кэша)
            read=300.0,     # 5 минут на чтение
            write=300.0,    # 5 минут на запись
            pool=300.0      # 5 минут на получение соединения
        )
        # Добавляем beta‑заголовок напрямую в HTTPX‑клиент, чтобы включить prompt caching
        httpx_client = httpx.Client(
            timeout=httpx_timeout,
            headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        
        # Передаём кастомный HTTPX клиент в Anthropic SDK
        # Это переопределяет дефолтные таймауты HTTPX (60 секунд)
        # ВАЖНО: не используем параметр beta в конструкторе (у тебя старая версия SDK)
        client = anthropic.Anthropic(api_key=api_key, http_client=httpx_client)
        
        # Конвертируем формат OpenAI → Claude
        system_content = None
        claude_messages = []
        
        for msg in messages:
            if msg["role"] == "system":
                content = msg["content"]
                
                # Если уже в формате Claude (список с cache_control)
                if isinstance(content, list):
                    system_content = content
                    log_msg(log_queue, f"  💾 Кэширование включено (формат Claude)")
                # Если строка - добавляем cache_control если достаточно длинная
                # Для Sonnet: минимум 1024 токена (~4096 символов)
                # Для Haiku 4.5: минимум 2048 токенов (~8192 символов)
                # Для Haiku 3.5: минимум 2048 токенов (~8192 символов)
                # Проверяем по символам с запасом
                elif isinstance(content, str):
                    # Приблизительная оценка: 1 токен ≈ 4 символа
                    estimated_tokens = len(content) // 4
                    # Определяем минимальный размер в зависимости от модели
                    if "haiku" in model.lower():
                        min_chars_for_cache = 8800  # ~2200 токенов (минимум для Haiku 4.5 - 2048, с запасом 2200)
                    else:
                        min_chars_for_cache = 4200  # ~1050 токенов (минимум для Sonnet - 1024 токена)
                    
                    if use_caching and len(content) >= min_chars_for_cache:
                        # Создаём с cache_control
                        # ВАЖНО: формат должен быть точно как в документации
                        # Используем 1h вместо 5m для более надёжного кэширования
                        system_content = [{
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral", "ttl": "1h"}
                        }]
                        log_msg(log_queue, f"  💾 Кэш включён: {len(content)} символов (~{estimated_tokens} токенов, TTL=1h)")
                    else:
                        # Слишком короткий для кэша
                        system_content = content
                        if use_caching:
                            log_msg(log_queue, f"  ⚠️  Кэш НЕ включён: {len(content)} символов < {min_chars_for_cache} минимума")
                else:
                    system_content = content
            else:
                claude_messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })
        
        # Если нет user message, добавляем пустой
        if not claude_messages:
            claude_messages.append({"role": "user", "content": "Please proceed."})
        
        # Создаём запрос (увеличен max_tokens для длинных глав генерации: 1500+ слов ≈ 6000 токенов)
        kwargs = {
            "model": model,
            "max_tokens": 8192,
            "messages": claude_messages
        }
        
        # Добавляем system (может быть строка или список с cache_control)
        if system_content:
            kwargs["system"] = system_content
            # Логируем формат system для отладки
            if isinstance(system_content, list):
                text_len = len(system_content[0].get('text', '')) if system_content and isinstance(system_content[0], dict) else 0
                has_cache = any('cache_control' in item for item in system_content if isinstance(item, dict))
                # Вычисляем хеш для проверки идентичности
                system_text = system_content[0].get('text', '') if system_content and isinstance(system_content[0], dict) else ''
                system_hash = hashlib.md5(system_text.encode()).hexdigest()[:8]
                log_msg(log_queue, f"  🔍 System: список, {text_len} символов, cache_control={'✅' if has_cache else '❌'}, hash={system_hash}")
            else:
                system_hash = hashlib.md5(system_content.encode()).hexdigest()[:8]
                log_msg(log_queue, f"  🔍 System: строка, {len(system_content)} символов (без кэша), hash={system_hash}")
        
        # Логируем параметры запроса для отладки
        if isinstance(system_content, list) and system_content:
            cache_control_info = system_content[0].get('cache_control', {})
            log_msg(log_queue, f"  🔍 Отправляю запрос с cache_control: {cache_control_info}")
        
        # Таймауты теперь управляются через HTTPX клиент (300 секунд для всех операций)
        # Не нужно передавать timeout в messages.create, так как он уже настроен в httpx_client
        debug_log(log_queue, "Claude REQUEST:", json.dumps({"model": model, "max_tokens": kwargs.get("max_tokens"), "messages_count": len(claude_messages), "system_len": len(str(system_content)) if system_content else 0}, ensure_ascii=False))
        response = client.messages.create(**kwargs)
        
        # Детальное логирование кэша
        usage = response.usage
        
        # Базовая статистика
        total_input = usage.input_tokens
        if log_queue:
            log_msg(log_queue, f"  📊 Input: {usage.input_tokens} tokens")
            log_msg(log_queue, f"  📊 Output: {usage.output_tokens} tokens")
        
        # Статистика кэша
        cache_created = 0
        cache_read = 0
        
        try:
            # Используем model_dump() если доступен (pydantic модель)
            if hasattr(usage, 'model_dump'):
                usage_dict = usage.model_dump()
                cache_created = usage_dict.get('cache_creation_input_tokens', 0) or 0
                cache_read = usage_dict.get('cache_read_input_tokens', 0) or 0
                
                # Также проверяем cache_creation.ephemeral_1h_input_tokens (для 1h TTL)
                if 'cache_creation' in usage_dict:
                    cache_creation_info = usage_dict['cache_creation']
                    if isinstance(cache_creation_info, dict):
                        ephemeral_1h = cache_creation_info.get('ephemeral_1h_input_tokens', 0) or 0
                        if ephemeral_1h > 0:
                            cache_created = ephemeral_1h  # Используем 1h кэш если он есть
                
                # Детальное логирование для отладки
                if log_queue and use_caching:
                    log_msg(log_queue, f"  🔍 Usage dict keys: {list(usage_dict.keys())}")
                    if 'cache_creation_input_tokens' in usage_dict:
                        log_msg(log_queue, f"  🔍 cache_creation_input_tokens = {usage_dict['cache_creation_input_tokens']}")
                    if 'cache_read_input_tokens' in usage_dict:
                        log_msg(log_queue, f"  🔍 cache_read_input_tokens = {usage_dict['cache_read_input_tokens']}")
                    if 'cache_creation' in usage_dict:
                        cache_creation_info = usage_dict['cache_creation']
                        if isinstance(cache_creation_info, dict):
                            ephemeral_1h = cache_creation_info.get('ephemeral_1h_input_tokens', 0) or 0
                            if ephemeral_1h > 0:
                                log_msg(log_queue, f"  🔍 cache_creation.ephemeral_1h_input_tokens = {ephemeral_1h}")
            # Или через атрибуты напрямую
            elif hasattr(usage, 'cache_creation_input_tokens'):
                cache_created = getattr(usage, 'cache_creation_input_tokens', 0) or 0
                cache_read = getattr(usage, 'cache_read_input_tokens', 0) or 0
            # Или через __dict__
            elif hasattr(usage, '__dict__'):
                usage_dict = usage.__dict__
                cache_created = usage_dict.get('cache_creation_input_tokens', 0) or 0
                cache_read = usage_dict.get('cache_read_input_tokens', 0) or 0
                if log_queue and use_caching:
                    log_msg(log_queue, f"  🔍 Usage __dict__ keys: {list(usage_dict.keys())}")
        except Exception as e:
            if log_queue:
                log_msg(log_queue, f"  ⚠️ Ошибка чтения cache статистики: {e}")
                import traceback
                log_msg(log_queue, f"  🔍 Traceback: {traceback.format_exc()}")
        
        # Логируем кэш создание и чтение
        if cache_created > 0:
            if log_queue:
                log_msg(log_queue, f"  💾 CACHE CREATED: {cache_created} tokens")
            PROGRESS_MANAGER.log_cache_usage(cache_create_tokens=cache_created)
        elif cache_read > 0:
            # Если кэш читается, но не создаётся - значит он был создан ранее
            if log_queue:
                log_msg(log_queue, f"  ℹ️  Кэш из предыдущего запроса (в пределах TTL)")
                log_msg(log_queue, f"  💡 Это нормально - кэш работает!")
            
        if cache_read > 0:
            if log_queue:
                savings = cache_read * 0.003 * 0.9 / 1000  # 90% экономия
                log_msg(log_queue, f"  ⚡ CACHE HIT: {cache_read} tokens (saved ${savings:.4f})")
            PROGRESS_MANAGER.log_cache_usage(cache_hit_tokens=cache_read)
        
        # Дополнительная информация о кэше
        if log_queue and use_caching:
            try:
                if hasattr(usage, 'model_dump'):
                    usage_dict = usage.model_dump()
                    if 'cache_creation' in usage_dict:
                        cache_creation_info = usage_dict['cache_creation']
                        if isinstance(cache_creation_info, dict):
                            ephemeral_5m = cache_creation_info.get('ephemeral_5m_input_tokens', 0)
                            ephemeral_1h = cache_creation_info.get('ephemeral_1h_input_tokens', 0)
                            if ephemeral_5m > 0:
                                log_msg(log_queue, f"  🔍 Cache creation (5m): {ephemeral_5m} tokens")
                            if ephemeral_1h > 0:
                                log_msg(log_queue, f"  🔍 Cache creation (1h): {ephemeral_1h} tokens")
            except:
                pass
        
        # Предупреждение если кэш не работает (только если оба = 0 И есть cache_control)
        if use_caching and cache_created == 0 and cache_read == 0:
            # Проверяем, был ли установлен cache_control
            has_cache_control = False
            if system_content and isinstance(system_content, list):
                has_cache_control = any('cache_control' in item for item in system_content if isinstance(item, dict))
            
            if has_cache_control:
                if log_queue:
                    log_msg(log_queue, f"  ⚠️  Кэш не использовался!")
                    log_msg(log_queue, f"  💡 Возможные причины:")
                    # Получаем estimated_tokens из system_content если доступен
                    est_tokens = '?'
                    if system_content and isinstance(system_content, list) and system_content:
                        text = system_content[0].get('text', '')
                        est_tokens = len(text) // 4 if text else '?'
                    elif system_content and isinstance(system_content, str):
                        est_tokens = len(system_content) // 4
                    log_msg(log_queue, f"     - System prompt <1024 токенов (у вас ~{est_tokens})")
                    log_msg(log_queue, f"     - Промпт изменился между запросами")
                    log_msg(log_queue, f"     - Модель {model} не поддерживает кэширование")
                    # Пробуем вывести полный usage для отладки
                    try:
                        if hasattr(usage, 'model_dump_json'):
                            usage_json = usage.model_dump_json()
                            log_msg(log_queue, f"  🔍 Полный usage JSON: {usage_json[:500]}")
                    except:
                        pass
        
        # Извлекаем текст
        text_content = ""
        for block in response.content:
            if block.type == "text":
                text_content += block.text
        
        return True, text_content
        
    except anthropic.APIError as e:
        error_str = str(e)
        error_code = getattr(e, 'status_code', None)
        debug_log(log_queue, "Claude APIError:", f"code={error_code} {error_str}")
        return False, f"__RETRY__:{error_code or 0}:{error_str}"
    except Exception as e:
        import traceback
        error_str = str(e)
        debug_log(log_queue, "Claude Exception:", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        return False, f"__RETRY__:EXC:{error_str}"


def _wait_with_stop_check(wait_time: float) -> bool:
    """Ждём wait_time секунд с проверкой STOP_FLAG. Возвращает True если остановлено."""
    waited = 0.0
    while waited < wait_time:
        if STOP_FLAG.is_stopped():
            return True
        time.sleep(min(1.0, wait_time - waited))
        waited += 1.0
    return False


def call_claude_api(
    messages: List[Dict],
    api_key: str,
    model: str,
    log_queue: queue.Queue,
    task_info: str = "",
    use_caching: bool = True
) -> Tuple[bool, str]:
    """
    Вызывает Claude API с итеративными ретраями (без рекурсии).
    Счётчики ретраев — локальные, не расшарены между потоками.
    """
    max_retries = {"disconnect": 3, "server": 5, "timeout": 3, "connection": 3, "rate_limit": 20}
    retries = {"disconnect": 0, "server": 0, "timeout": 0, "connection": 0, "rate_limit": 0}

    while True:
        success, response = _call_claude_api_once(messages, api_key, model, log_queue, task_info, use_caching)

        if success:
            RATE_LIMITER.on_success()
            return True, response

        # Обычная ошибка — не ретрай
        if not response.startswith("__RETRY__:"):
            return False, response

        # Разбираем тип ошибки
        _, error_code_str, error_str = response.split(":", 2)
        error_code = int(error_code_str) if error_code_str.isdigit() else None
        error_lower = error_str.lower()

        if STOP_FLAG.is_stopped():
            return False, "Остановлено пользователем"

        # 404 — модель не найдена, fallback на Sonnet 4.6 (актуальный pinned alias).
        # ВАЖНО: не fallback на claude-sonnet-4-20250514 — он deprecated и retire 2026-06-15.
        if error_code == 404 or "not_found" in error_lower:
            if "sonnet" in model.lower() and model != "claude-sonnet-4-6":
                log_msg(log_queue, f"    ⚠️ {model} недоступен, fallback на claude-sonnet-4-6")
                model = "claude-sonnet-4-6"
                continue
            return False, f"Модель '{model}' не найдена."

        # 499 / disconnect
        if error_code == 499 or "disconnected" in error_lower:
            if retries["disconnect"] >= max_retries["disconnect"]:
                return False, f"Client disconnected после {max_retries['disconnect']} попыток."
            retries["disconnect"] += 1
            backoff = 10 * retries["disconnect"]
            log_msg(log_queue, f"    🔄 Client disconnected, повтор {retries['disconnect']}/{max_retries['disconnect']} через {backoff}с...")
            time.sleep(backoff)
            continue

        # 429 / rate limit
        if error_code == 429 or "rate_limit" in error_lower:
            if retries["rate_limit"] >= max_retries["rate_limit"]:
                return False, f"Rate limit после {max_retries['rate_limit']} попыток."
            retries["rate_limit"] += 1
            wait_time = RATE_LIMITER.on_rate_limit()
            log_msg(log_queue, f"    ⏳ Rate limit{' ['+task_info+']' if task_info else ''}, жду {wait_time:.0f}с...")
            if _wait_with_stop_check(wait_time):
                return False, "Остановлено пользователем"
            continue

        # 500-599 server errors
        if error_code and 500 <= error_code < 600:
            if retries["server"] >= max_retries["server"]:
                return False, f"Ошибка сервера после {max_retries['server']} попыток (code {error_code})"
            retries["server"] += 1
            backoff = min(2 ** (retries["server"] - 1), 60)
            log_msg(log_queue, f"    ⚠️ Ошибка сервера ({error_code}), повтор {retries['server']}/{max_retries['server']} через {backoff}с...")
            time.sleep(backoff)
            continue

        # Timeout
        if "timeout" in error_lower or "timed out" in error_lower:
            if retries["timeout"] >= max_retries["timeout"]:
                return False, f"Timeout после {max_retries['timeout']} попыток"
            retries["timeout"] += 1
            backoff = 5 * retries["timeout"]
            log_msg(log_queue, f"    ⏳ Timeout, повтор {retries['timeout']}/{max_retries['timeout']} через {backoff}с...")
            time.sleep(backoff)
            continue

        # Connection error (generic Exception path)
        if "connection" in error_lower or "499" in error_str:
            if retries["connection"] >= max_retries["connection"]:
                return False, f"Connection error после {max_retries['connection']} попыток."
            retries["connection"] += 1
            backoff = 10 * retries["connection"]
            log_msg(log_queue, f"    🔄 Connection error, повтор {retries['connection']}/{max_retries['connection']} через {backoff}с...")
            time.sleep(backoff)
            continue

        # Неизвестная ошибка — не ретраим
        return False, f"Claude API error: {error_str[:200]}"


def _is_token_switch_signal(text_lower: str) -> bool:
    """FEATURE-2026-06-28-multitoken: токен ИСЧЕРПАН (лимит подписки) ИЛИ СДОХ (логаут/протух) →
    сигнал переключиться на следующий токен в ротации. Возвращает True если в выводе claude CLI
    есть признак того, что текущим токеном дальше ходить нельзя."""
    t = text_lower or ""
    return (
        # лимит подписки исчерпан
        "hit your limit" in t or "hit the limit" in t or ("limit" in t and "reset" in t)
        or "usage limit" in t or "rate limit" in t
        # токен сдох / разлогинен / протух → тоже «кончился», переключаемся
        or "not logged in" in t or "please run /login" in t or "/login" in t
        or "invalid bearer token" in t or "authentication" in t and "fail" in t
    )


def call_claude_subscription(
    messages: List[Dict],
    log_queue: queue.Queue,
    task_info: str = "",
    model: str = "claude-sonnet-4-5-20250929",
    oauth_token: str = ""
) -> Tuple[bool, str]:
    """
    Вызов Claude через Pro/Max подписку (claude-code-sdk).
    Использует oauth_token из параметра или переменной окружения.
    НЕ требует API ключа, НЕ тратит API credits.
    """
    if not CLAUDE_CODE_SDK_AVAILABLE:
        return False, "claude-code-sdk не установлен. Установите: pip install claude-code-sdk anyio"
    
    import os
    import io
    import subprocess
    
    # Используем токен из параметра или переменной окружения
    if not oauth_token:
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    
    if not oauth_token:
        return False, "OAuth токен не задан. Введите токен в GUI или установите CLAUDE_CODE_OAUTH_TOKEN"
    
    # Очищаем токен: убираем переносы строк, пробелы и префикс "Bearer "
    # Токен может содержать переносы строк и лишние пробелы
    oauth_token = "".join(oauth_token.split())  # Убираем все пробелы и переносы строк
    # Убираем префикс "Bearer" или "bearer" если есть
    if oauth_token.lower().startswith("bearer"):
        oauth_token = oauth_token[6:]  # Убираем "Bearer" (6 символов)
    
    if not oauth_token:
        return False, "OAuth токен пустой после очистки"
    
    # Логируем очищенный токен (первые и последние 10 символов для безопасности)
    if log_queue:
        # M6 PATCH-2026-06-25-secfix: не логируем хвост секрета и точную длину (раньше last10 + len).
        token_preview = (oauth_token[:7] + "…") if oauth_token else "(empty)"
        log_msg(log_queue, f"  🔍 Токен принят: {token_preview} (len~{(len(oauth_token) // 20) * 20}+)")

    # Извлекаем system prompt и user message из формата OpenAI
    system_prompt = ""
    user_content = ""
    for msg in messages:
        if msg["role"] == "system":
            content = msg["content"]
            if isinstance(content, list):
                if content and isinstance(content[0], dict):
                    system_prompt = content[0].get("text", "")
            else:
                system_prompt = str(content)
        elif msg["role"] == "user":
            user_content = str(msg["content"])
    
    if not user_content:
        user_content = "Please proceed."

    # Токен передаётся per-call через options.env (SDK 0.0.25+ поддерживает),
    # больше НЕ через global os.environ — это убрало serial-mutex и позволяет
    # реальный parallelism для subscription calls (раньше _env_lock сериализовал
    # все вызовы из любых worker-threads).

    # Перехватываем stderr для диагностики
    stderr_capture = io.StringIO()

    options = ClaudeCodeOptions(
        system_prompt=system_prompt if system_prompt else None,
        model=model,
        max_turns=1,  # запрещаем follow-up вопросы от Claude
        # REVERT-2026-06-28: MAX_THINKING_TOKENS убран — добавлял сегодня (недоказанный фикс нарратива),
        # подозрение в провоцировании #25979 на тяжёлых параллельных звонках. Вернул 17-дневно-стабильное состояние.
        env={"CLAUDE_CODE_OAUTH_TOKEN": oauth_token},
        # include_partial_messages=True (2026-06-03): StreamEvent partials = heartbeat
        # для watchdog'а. Без флага receive yields events редко (gap >70s),
        # длинные ответы Claude (rewrite больших кусков) ложно ловятся как stall.
        # Spike подтвердил: с флагом gap ~1.5s между events.
        include_partial_messages=True,
    )
    debug_log(log_queue, "Subscription REQUEST:", f"model={model} system_len={len(system_prompt)} user_len={len(user_content)}")

    result_text = ""

    async def _run():
        nonlocal result_text
        # Двухуровневый watchdog (Anthropic issue #25979 recommendation):
        #  - STREAM_DATA_TIMEOUT_SEC (120s): max тишина МЕЖДУ chunks стрима — ловит
        #    mid-turn freeze (когда API замолкает и CLI ждёт бесконечно).
        #  - SUBSCRIPTION_CALL_TIMEOUT_SEC (600s): hard cap — на случай slow-stream.
        last_data_time = [time.monotonic()]
        iterate_done = anyio.Event()

        async def _stream_watchdog():
            # Каждые ≤15s проверяем не превышен ли silence-timeout. Выходим как только
            # iterate_done.set() (нормальное завершение стрима).
            while not iterate_done.is_set():
                with anyio.move_on_after(15):
                    await iterate_done.wait()
                    return  # стрим завершился — выходим
                # 15s прошло без события — проверяем тишину
                silence = time.monotonic() - last_data_time[0]
                if silence > STREAM_DATA_TIMEOUT_SEC:
                    raise TimeoutError(
                        f"stream-data watchdog: no data for {int(silence)}s "
                        f"(known CLI hang — anthropics/claude-code#25979)"
                    )

        # got_assistant: чтобы избежать дубля AssistantMessage+ResultMessage —
        # ResultMessage используем ТОЛЬКО как fallback, если AssistantMessage не пришёл.
        got_assistant = [False]

        async def _do_iter():
            nonlocal result_text
            try:
                async for message in query(prompt=user_content, options=options):
                    last_data_time[0] = time.monotonic()  # любая активность (включая StreamEvent partial) ресетает silence
                    msg_type = type(message).__name__

                    if msg_type == "AssistantMessage":
                        content = getattr(message, "content", None)
                        if isinstance(content, str):
                            result_text += content
                            got_assistant[0] = True
                        elif isinstance(content, list):
                            for block in content:
                                text = getattr(block, "text", None)
                                if text and isinstance(text, str):
                                    result_text += text
                                    got_assistant[0] = True

                    elif msg_type == "ResultMessage" and not got_assistant[0]:
                        # fallback: AssistantMessage не пришёл, используем ResultMessage
                        result = getattr(message, "result", None)
                        if result and isinstance(result, str):
                            result_text += result
                    # StreamEvent / прочее — игнорируем для контента, но watchdog уже обновлён выше
            finally:
                iterate_done.set()

        with anyio.fail_after(SUBSCRIPTION_CALL_TIMEOUT_SEC):
            async with anyio.create_task_group() as tg:
                tg.start_soon(_stream_watchdog)
                await _do_iter()

    try:
        if log_queue:
            log_msg(log_queue, f"  🔑 Использую Pro подписку (модель: {model})")
        # Preflight CLI check — one-time cache (2026-06-04). Раньше делалось на каждый
        # вызов с timeout=5 → лишний node-spawn + 'version timed out' под параллелью.
        _ensure_cli_ready(log_queue)

        # Semaphore: лимит одновременных claude subprocess'ов между всеми джобами.
        # Держим внутри forensics+pkill чтобы зомби-CLI не занимал слот после release.
        with _claude_sema:
            # Inner retry на stream-stall: 1 retry до того как стрелять наверх в server.py
            # (там external retry с 30s backoff). Inner retry освобождает новое TCP — без
            # backoff, потому что подвисший TCP не разлепит сам, а новый subprocess сразу
            # открывает свежее соединение.
            # PATCH-2026-06-12-transient-inner-retry
            # Inner retry: stream-stall (timeout) НЕ ретраим (outer retry в server.py),
            # а транзиентные НЕ-timeout ошибки SDK/API (overloaded/rate-limit/decode/
            # process — приходят как TaskGroup sub-exception) РЕТРАИМ свежим subprocess.
            # Вернул transient inner-retry, потерянный при #13 hard-cap 2026-06-04.
            _OPUS_INNER_RETRIES = 0        # timeout stalls: без inner retry (как было)
            _TRANSIENT_INNER_RETRIES = 2   # транзиентные не-timeout: 2 повтора
            _timeout_attempt = 0
            _transient_done = 0
            _attempts_done = 0
            while True:
                _attempts_done += 1
                result_text = ""  # reset на каждом attempt
                try:
                    anyio.run(_run)
                    break  # success
                except BaseException as _attempt_err:
                    if isinstance(_attempt_err, (KeyboardInterrupt, SystemExit)):
                        raise
                    _to = _find_timeout_error(_attempt_err)
                    if _to is None:
                        # Транзиентная не-timeout ошибка — ретраим свежим subprocess (новый TCP).
                        if _transient_done < _TRANSIENT_INNER_RETRIES:
                            _transient_done += 1
                            # PATCH-2026-06-12-no-pkill-transient: НЕ pkill здесь. Под параллелью
                            # pkill -P <pid> -f claude убил бы ВСЕ соседние claude-субпроцессы
                            # (общий PID процесса, до CLAUDE_MAX_PARALLEL детей одновременно).
                            # Транзиентная ошибка = query() уже упал, subprocess закрыт
                            # SDK-контекстом; свежий retry откроет новый. Чистить нечего.
                            if log_queue:
                                log_msg(
                                    log_queue,
                                    f"  🔁 [transient] inner retry {_transient_done}/{_TRANSIENT_INNER_RETRIES} "
                                    f"after: {str(_attempt_err)[:140]}"
                                )
                            time.sleep(3 * _transient_done)  # короткий backoff
                            continue
                        raise  # транзиентные ретраи исчерпаны — outer handler разберётся
                    # timeout path (как было): forensics + kill, inner retry по _OPUS_INNER_RETRIES
                    try:
                        _collect_hung_cli_forensics(log_queue, f"{task_info or 'unknown'}_a{_attempts_done}")
                    except Exception:
                        pass
                    try:
                        subprocess.run(
                            ["pkill", "-9", "-P", str(os.getpid()), "-f", "claude"],
                            capture_output=True, timeout=5,
                        )
                    except Exception:
                        pass
                    if _timeout_attempt < _OPUS_INNER_RETRIES:
                        _timeout_attempt += 1
                        if log_queue:
                            log_msg(
                                log_queue,
                                f"  🔁 [opus-stall] inner retry {_timeout_attempt}/{_OPUS_INNER_RETRIES} "
                                f"after stream stall ({_to})"
                            )
                        time.sleep(2)
                        try:
                            import gc as _gc
                            _gc.collect()
                        except Exception:
                            pass
                        continue
                    raise TimeoutError(f"{_to} (after {_attempts_done} inner attempts)") from _attempt_err

        # Логируем результат перед возвратом
        if log_queue:
            if result_text:
                log_msg(log_queue, f"  ✅ Успешно получен ответ ({_attempts_done} attempt): {len(result_text)} символов")
                log_msg(log_queue, f"  🔍 Превью ответа: {result_text[:200]}...")
            else:
                log_msg(log_queue, f"  ⚠️ Ответ пустой! Проверьте структуру сообщения от SDK")

        if not result_text:
            return False, "Ответ от Claude пустой. Проверьте токен и модель."

        return True, result_text
    except BaseException as outer_e:
        # Распаковываем (Base)ExceptionGroup от anyio.create_task_group, чтобы
        # TimeoutError от stream-watchdog или anyio.fail_after был пойман корректно.
        _to_err = _find_timeout_error(outer_e)
        if _to_err is not None:
            # Forensics + kill УЖЕ сделаны в inner-retry loop (на каждом attempt).
            # Здесь только формируем error для server.py (наружный retry).
            timeout_reason = str(_to_err) if str(_to_err) else f"hard cap {SUBSCRIPTION_CALL_TIMEOUT_SEC}s"
            msg = (
                f"Claude Code CLI stream stalled ({timeout_reason}). "
                f"Inner retries exhausted. Known upstream bug — anthropics/claude-code#25979. "
                f"Forensics: /tmp/cli_hang_<task>_a<N>_<ts>/; prompt ~{len(user_content)} chars."
            )
            debug_log(log_queue, "Subscription TIMEOUT:", msg)
            if log_queue:
                log_msg(log_queue, f"  ⏱️  Subscription timeout: {msg}")
            return False, SUBSCRIPTION_TIMEOUT_PREFIX + msg
        # Не timeout: KeyboardInterrupt/SystemExit пропускаем, остальное обрабатываем как Exception.
        if not isinstance(outer_e, Exception):
            raise
        e = outer_e
        # PATCH-2026-06-12-transient-inner-retry: распаковываем реальную sub-exception из (Base)ExceptionGroup,
        # чтобы лог/классификация видели причину, а не "unhandled errors in a TaskGroup".
        _flat_subs = []
        def _flatten_eg(_x):
            _ss = getattr(_x, "exceptions", None)
            if _ss:
                for _y in _ss:
                    _flatten_eg(_y)
            elif _x is not None:
                _flat_subs.append(_x)
        try:
            _flatten_eg(outer_e)
        except Exception:
            pass
        if _flat_subs:
            e = _flat_subs[0]
        import traceback
        error_msg = str(e) or type(e).__name__
        stderr_output = stderr_capture.getvalue()
        is_limit_error = False
        debug_log(log_queue, "Subscription Exception:", f"{type(e).__name__}: {error_msg}\nstderr: {stderr_output}\n{traceback.format_exc()}")
        if log_queue:
            log_msg(log_queue, f"  ❌ Subscription error: {error_msg[:300]}")
            if stderr_output:
                log_msg(log_queue, f"  ❌ Stderr: {stderr_output[:500]}")
            else:
                log_msg(log_queue, f"  ❌ Stderr пустой — проверь: claude -p 'test' в терминале")
                try:
                    test_result = subprocess.run(
                        ["claude", "-p", "test"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": oauth_token},  # FEATURE-2026-06-28-multitoken: проба ИМЕННО упавшим токеном
                    )
                    if test_result.stderr:
                        log_msg(log_queue, f"  ❌ Test stderr: {test_result.stderr[:500]}")
                    if test_result.stdout:
                        log_msg(log_queue, f"  ℹ️ Test stdout: {test_result.stdout[:200]}")
                    out_lower = ((test_result.stdout or "") + " " + (test_result.stderr or "")).lower()
                    if _is_token_switch_signal(out_lower):   # лимит ИЛИ логаут/протух → переключить токен
                        is_limit_error = True
                except Exception as test_e:
                    log_msg(log_queue, f"  ❌ Не удалось запустить тест: {test_e}")
        if not is_limit_error and ("exit code 1" in error_msg.lower() or "command failed" in error_msg.lower()):
            try:
                test_result = subprocess.run(
                    ["claude", "-p", "test"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": oauth_token},  # FEATURE-2026-06-28-multitoken: проба упавшим токеном
                )
                out_lower = ((test_result.stdout or "") + " " + (test_result.stderr or "")).lower()
                if _is_token_switch_signal(out_lower):   # лимит ИЛИ логаут/протух → переключить токен
                    is_limit_error = True
            except Exception:
                pass
        if is_limit_error:
            return False, "SUBSCRIPTION_LIMIT:" + error_msg
        return False, error_msg
    # finally блок c restore os.environ + _env_lock.release() удалён 2026-05-24:
    # токен теперь идёт per-call через options.env, глобальная env не трогается → нечего восстанавливать.


SUBSCRIPTION_LIMIT_PREFIX = "SUBSCRIPTION_LIMIT:"
SUBSCRIPTION_TIMEOUT_PREFIX = "SUBSCRIPTION_TIMEOUT:"
# Watchdog для Claude Code CLI hang — known upstream bug, regression в 2.1.42.
# Anthropic issue: https://github.com/anthropics/claude-code/issues/25979
# Симптом: CLI streaming замолкает mid-turn, ждёт следующий пакет вечно, висит
# в epoll_wait с открытым TCP к Anthropic. Anthropic сами рекомендуют external
# watchdog daemon с timeout 5 мин и kill -9 — наша реализация именно это.
#
# Двухуровневый watchdog:
#  STREAM_DATA_TIMEOUT_SEC — макс тишина между chunks (ловит mid-turn freeze)
#  SUBSCRIPTION_CALL_TIMEOUT_SEC — hard cap на весь вызов (на случай slow-stream)
STREAM_DATA_TIMEOUT_SEC = 780  # was 120, bumped 2026-05-24 — Opus/Sonnet streaming pacing pauses up to ~130s between complete JSON events; re-applied after userKeys-refactor clobber
SUBSCRIPTION_CALL_TIMEOUT_SEC = 600
# Conversation-mode (ClaudeSDKClient) использует include_partial_messages=True →
# StreamEvent'ы летят каждые ~0.5-1.5с (heartbeat для watchdog'а). Поэтому жёстче, чем
# one-shot path, но с запасом — 420s, чтобы реальный hang не висел 10 мин.
CONV_STREAM_DATA_TIMEOUT_SEC = 780

# === Global claude-subprocess semaphore (2026-06-04) ===
# Лимитирует ОБЩЕЕ количество одновременных claude CLI subprocess'ов между всеми
# джобами/языками/юзерами. Защита от RAM-OOM (каждый claude ≈ 200MB).
# Дефолт 3 RAM-safe для 957MB VDS; на 12GB Contabo systemd ставит =15 через Environment.
_CLAUDE_MAX_PARALLEL = int(os.environ.get("CLAUDE_MAX_PARALLEL", "3"))
_claude_sema = threading.Semaphore(_CLAUDE_MAX_PARALLEL)

# === Preflight CLI check — ОДИН раз на процесс (2026-06-04) ===
# До этого `claude --version` спавнил лишний node-процесс на КАЖДЫЙ вызов
# и таймаутил под параллелью (timeout=5 → 'version timed out' в логах).
# Теперь — module-level cache + timeout=30 на единственную проверку.
_cli_checked: Dict[str, Any] = {"ok": None, "lock": threading.Lock()}


def _ensure_cli_ready(log_queue=None) -> bool:
    """One-time `claude --version` check (cached на процесс). Возвращает True если CLI OK."""
    if _cli_checked["ok"] is not None:
        return _cli_checked["ok"]
    with _cli_checked["lock"]:
        if _cli_checked["ok"] is not None:
            return _cli_checked["ok"]  # double-check после lock
        try:
            result = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=30,
            )
            _cli_checked["ok"] = (result.returncode == 0)
            if not _cli_checked["ok"] and log_queue:
                log_msg(log_queue, f"  ⚠️ claude CLI вернул код {result.returncode}: {(result.stderr or '')[:200]}")
        except FileNotFoundError:
            _cli_checked["ok"] = False
            if log_queue:
                log_msg(log_queue, "  ⚠️ claude CLI не найден")
        except Exception as e:
            _cli_checked["ok"] = False
            if log_queue:
                log_msg(log_queue, f"  ⚠️ claude preflight failed: {e}")
        return _cli_checked["ok"]


# Python 3.10 не имеет BaseExceptionGroup в builtins (только 3.11+). anyio.create_task_group
# оборачивает internal exceptions в (Base)ExceptionGroup — нужно распаковать чтобы
# `except TimeoutError` корректно сработал.
try:
    _BaseExceptionGroup = BaseExceptionGroup  # Python 3.11+
except NameError:
    try:
        from exceptiongroup import BaseExceptionGroup as _BaseExceptionGroup
    except ImportError:
        _BaseExceptionGroup = None


def _find_timeout_error(exc):
    """Возвращает TimeoutError если он есть в exc (включая вложенные ExceptionGroup), иначе None."""
    if isinstance(exc, TimeoutError):
        return exc
    if _BaseExceptionGroup and isinstance(exc, _BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _find_timeout_error(sub)
            if found is not None:
                return found
    return None


def _collect_hung_cli_forensics(log_queue, job_id):
    """Сохраняет диагностический snapshot всех claude-процессов перед kill'ом.

    Вызывается из TimeoutError handler в call_claude_subscription. Собирает
    в /tmp/cli_hang_<job_id>_<ts>/pid_<PID>/:
      - cmdline.txt — точные argv (\\0 → пробелы)
      - status.txt, stack.txt, wchan.txt, syscall.txt — где CLI заклинило (без ptrace)
      - fds.txt — какие файловые дескрипторы открыты (особенно stdin)
      - lsof.txt — full FD list с типами / pipes / sockets
      - tcp.txt — есть ли TCP к api.anthropic.com (через `ss -tnp`)
      - strace.txt — syscalls 3 сек (может упасть permission denied при ptrace_scope=1)

    Returns: (base_dir, pids) или None если ничего не найдено.
    """
    import subprocess as _sp
    import os as _os
    import time as _time

    ts = int(_time.time())
    safe_jid = re.sub(r"[^\w-]", "_", str(job_id or "unknown"))[:40]
    base_dir = f"/tmp/cli_hang_{safe_jid}_{ts}"
    try:
        _os.makedirs(base_dir, exist_ok=True)
    except Exception:
        return None

    # Найти PID(s) claude процессов — дети текущего worker процесса
    pids = []
    try:
        out = _sp.run(
            ["pgrep", "-P", str(_os.getpid()), "-f", "claude"],
            capture_output=True, text=True, timeout=2,
        )
        pids = [p.strip() for p in out.stdout.splitlines() if p.strip()]
    except Exception:
        pass
    # Fallback: все claude процессы на хосте
    if not pids:
        try:
            out = _sp.run(
                ["pgrep", "-f", "/usr/bin/claude"],
                capture_output=True, text=True, timeout=2,
            )
            pids = [p.strip() for p in out.stdout.splitlines() if p.strip()]
        except Exception:
            pass

    try:
        with open(f"{base_dir}/_summary.txt", "w", encoding="utf-8") as f:
            f.write(f"forensics_ts={ts}\njob_id={job_id}\nfound_pids={pids}\n")
    except Exception:
        pass

    for pid in pids:
        pid_dir = f"{base_dir}/pid_{pid}"
        try:
            _os.makedirs(pid_dir, exist_ok=True)
        except Exception:
            continue

        # 1. cmdline (\\0 → space)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as src:
                content = src.read().replace(b"\x00", b" ")
            with open(f"{pid_dir}/cmdline.txt", "wb") as dst:
                dst.write(content)
        except Exception as e:
            try:
                with open(f"{pid_dir}/cmdline.err", "w") as f:
                    f.write(str(e))
            except Exception:
                pass

        # 2-5. /proc snapshots без ptrace
        for fname in ("status", "stack", "wchan", "syscall"):
            try:
                _sp.run(
                    ["cp", f"/proc/{pid}/{fname}", f"{pid_dir}/{fname}.txt"],
                    capture_output=True, timeout=2,
                )
            except Exception:
                pass

        # 6. FDs
        try:
            with open(f"{pid_dir}/fds.txt", "w", encoding="utf-8") as f:
                _sp.run(
                    ["ls", "-la", f"/proc/{pid}/fd/"],
                    stdout=f, stderr=_sp.STDOUT, timeout=2,
                )
        except Exception:
            pass

        # 7. lsof
        try:
            with open(f"{pid_dir}/lsof.txt", "w", encoding="utf-8") as f:
                _sp.run(["lsof", "-p", pid], stdout=f, stderr=_sp.STDOUT, timeout=5)
        except Exception:
            pass

        # 8. TCP via ss
        try:
            with open(f"{pid_dir}/tcp.txt", "w", encoding="utf-8") as f:
                _sp.run(["ss", "-tnp"], stdout=f, stderr=_sp.STDOUT, timeout=2)
        except Exception:
            pass

        # 9. strace 3 сек (может упасть permission denied при ptrace_scope=1)
        try:
            with open(f"{pid_dir}/strace.txt", "w", encoding="utf-8") as f:
                _sp.run(
                    ["timeout", "3", "strace", "-p", pid,
                     "-e", "trace=read,write,close,connect,recvfrom,sendto,epoll_wait"],
                    stdout=f, stderr=_sp.STDOUT, timeout=5,
                )
        except Exception:
            pass

    if log_queue is not None:
        try:
            log_msg(log_queue, f"  🔬 Hang forensics saved: {base_dir} (pids: {pids or 'none found'})")
        except Exception:
            pass
    return base_dir, pids


def _gen_count_script_words(raw: str) -> int:
    """PATCH-2026-06-27-length: слова в script из JSON-ответа генерации. -1 если не распарсить."""
    p = parse_chunk_response(raw, expect_funnel=False)
    if not p:
        return -1
    return len((p.get("script") or "").split())


def _gen_merge_continuation(main_raw: str, cont_raw: str) -> str:
    """PATCH-2026-06-27-length: мерджит script из cont_raw в конец script main_raw,
    сохраняя preview/title/hook/plan/log_update. При неудаче — main_raw без изменений."""
    pm = parse_chunk_response(main_raw, expect_funnel=False)
    pc = parse_chunk_response(cont_raw, expect_funnel=False)
    if not pm or not pc or not (pc.get("script") or "").strip():
        return main_raw
    merged_script = (pm.get("script") or "").rstrip() + "\n\n" + (pc.get("script") or "").lstrip()
    obj = {}
    try:
        raw_obj = json.loads(main_raw[main_raw.find("{"): main_raw.rfind("}") + 1])
        if isinstance(raw_obj, dict):
            obj = dict(raw_obj)
    except Exception:
        obj = {}
    if not obj:
        obj = {"preview": pm.get("preview", ""), "title": pm.get("title", ""), "log_update": pm.get("log_update", [])}
    obj["script"] = merged_script
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return main_raw


def call_claude_conversation(
    turn_prompts: List[str],
    system_prompt: str,
    model: str,
    oauth_token: str,
    log_queue: queue.Queue,
    task_info: str = "",
    target_words_per_turn: int = 0,   # PATCH-2026-06-27-length: >0 включает in-conversation recovery до коридора
    max_recovery_turns: int = 2,
) -> Tuple[bool, List[str], Optional[str]]:
    """Multi-turn разговор с Claude через ClaudeSDKClient (continue-conversation).

    Открывает ОДИН ClaudeSDKClient → проходит turn_prompts последовательно. Модель
    видит ВЕСЬ предыдущий разговор сама — никаких previous_tail или re-вставки preset.
    Кэш промпта (system + первый turn) переиспользуется на turn 2+ → дешевле.

    Защиты (как в call_claude_subscription):
      - per-turn stream-data watchdog STREAM_DATA_TIMEOUT_SEC (тишина внутри turn'а)
      - per-turn hard cap SUBSCRIPTION_CALL_TIMEOUT_SEC
      - env per-call через ClaudeCodeOptions.env (не os.environ)
      - forensics _collect_hung_cli_forensics при stall
      - TaskGroup unwrap через _find_timeout_error

    Стратегия retry: **abandon-to-partial** (см. отчёт 2026-06-03):
      если turn K заглох — НЕ replay (сессия может быть в битом состоянии),
      закрываем client, возвращаем done_count=K-1 ответов + error для caller'а.

    Returns:
      (success_all, raw_responses, error_msg_if_any)
      success_all=True  → len(raw_responses) == len(turn_prompts)
      success_all=False → len(raw_responses) = число завершённых turn'ов (0..N-1), error_msg описывает причину
    """
    if not CLAUDE_CODE_SDK_AVAILABLE:
        return False, [], "claude-code-sdk не установлен"
    if not turn_prompts:
        return False, [], "no turn_prompts"

    oauth_token = "".join((oauth_token or "").split())
    if oauth_token.lower().startswith("bearer"):
        oauth_token = oauth_token[6:]
    if not oauth_token:
        return False, [], "OAuth токен пустой"

    if log_queue:
        log_msg(log_queue, f"  💬 Conversation: {len(turn_prompts)} turn(s), model={model}")
        # M6 PATCH-2026-06-25-secfix: не логируем хвост секрета и точную длину (раньше last10 + len).
        token_preview = (oauth_token[:7] + "…") if oauth_token else "(empty)"
        log_msg(log_queue, f"  🔍 Токен принят: {token_preview} (len~{(len(oauth_token) // 20) * 20}+)")

    from claude_code_sdk import ClaudeSDKClient, ClaudeCodeOptions

    options = ClaudeCodeOptions(
        system_prompt=system_prompt if system_prompt else None,
        model=model,
        env={"CLAUDE_CODE_OAUTH_TOKEN": oauth_token},  # REVERT-2026-06-28: MAX_THINKING убран (см. call_claude_subscription)
        # БЕЗ max_turns=1 — multi-turn нужен
        # include_partial_messages: StreamEvent partials = heartbeat для watchdog'а.
        # Без флага receive_response() выдаёт events редко (gap >70s на 2500w turn,
        # ломает watchdog на длинных turns). Spike подтвердил: с флагом gap ~1.5s.
        include_partial_messages=True,
    )

    raw_responses: List[str] = []
    error_msg: Optional[str] = None

    async def _run():
        nonlocal error_msg
        client = ClaudeSDKClient(options=options)
        try:
            await client.connect()
        except Exception as e:
            error_msg = f"connect failed: {type(e).__name__}: {e}"
            if log_queue:
                log_msg(log_queue, f"  ❌ Conversation connect failed: {error_msg}")
            return

        try:
            # PATCH-2026-06-27-length: один отправитель turn'а (переиспользуется для recovery-turn'ов).
            async def _send_one(prompt_text, turn_label):
                last_data_time = [time.monotonic()]
                iterate_done = anyio.Event()
                turn_text: List[str] = []

                async def _stream_watchdog():
                    while not iterate_done.is_set():
                        with anyio.move_on_after(15):
                            await iterate_done.wait()
                            return
                        silence = time.monotonic() - last_data_time[0]
                        if silence > CONV_STREAM_DATA_TIMEOUT_SEC:
                            raise TimeoutError(
                                f"stream-data watchdog {turn_label}: no data for {int(silence)}s "
                                f"(known CLI hang — anthropics/claude-code#25979)"
                            )

                async def _do_turn():
                    try:
                        await client.query(prompt_text)
                        async for message in client.receive_response():
                            last_data_time[0] = time.monotonic()
                            msg_type = type(message).__name__
                            if msg_type == "AssistantMessage":
                                content = getattr(message, "content", None)
                                if isinstance(content, str):
                                    turn_text.append(content)
                                elif isinstance(content, list):
                                    for block in content:
                                        text = getattr(block, "text", None)
                                        if text and isinstance(text, str):
                                            turn_text.append(text)
                            elif msg_type == "ResultMessage":
                                result = getattr(message, "result", None)
                                if result and isinstance(result, str):
                                    turn_text.append(result)
                    finally:
                        iterate_done.set()

                # per-turn watchdog + hard cap
                with anyio.fail_after(SUBSCRIPTION_CALL_TIMEOUT_SEC):
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(_stream_watchdog)
                        await _do_turn()
                return "".join(turn_text)

            for turn_idx, prompt in enumerate(turn_prompts):
                turn_no = turn_idx + 1
                if log_queue:
                    log_msg(log_queue, f"  💬 Turn {turn_no}/{len(turn_prompts)} → query (prompt_len={len(prompt)})")

                raw = await _send_one(prompt, f"turn {turn_no}")
                raw_responses.append(raw)
                if log_queue:
                    _scl = len(raw)
                    try:
                        import json as _json
                        _obj = _json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
                        if isinstance(_obj, dict) and isinstance(_obj.get("script"), str):
                            _scl = len(_obj["script"])
                    except Exception:
                        pass
                    log_msg(log_queue, f"  ✅ Turn {turn_no}/{len(turn_prompts)} done: {_scl} chars (script) / {len(raw)} raw")

                # PATCH-2026-06-28-words: in-conversation recovery — дожать ТОЛЬКО если короче >20% (floor 0.8, как рерайт). Меряем «Слов».
                if target_words_per_turn and target_words_per_turn > 0:
                    _lo = int(target_words_per_turn * 0.8)
                    _hi = int(target_words_per_turn * 1.15)
                    _is_last_turn = (turn_idx == len(turn_prompts) - 1)
                    _rec = 0
                    while _rec < max_recovery_turns:
                        _w = _gen_count_script_words(raw)
                        if _w < 0 or _w >= _lo:
                            break
                        _rec += 1
                        _deficit = _lo - _w
                        # outro-aware: последний turn должен ЗАКРЫТЬ аутро в конце продолжения (иначе аутро уедет в середину).
                        if _is_last_turn:
                            _close = ("If your previous part already ended with a sign-off, treat that as PREMATURE — keep "
                                      "developing the story, and only at the VERY END write the final short sign-off that "
                                      "names the host and the channel.")
                        else:
                            _close = "Do NOT write any closing or sign-off yet."
                        _rp = (
                            f"Your previous part is only {_w} words, but it MUST reach at least {_lo} words "
                            f"(target band {_lo}-{_hi}). CONTINUE the SAME part, picking up seamlessly right where it "
                            f"ended. Add about {_deficit} more words of real substance — more depth, concrete detail, an "
                            f"extra scene or development that belongs here. Do NOT repeat anything already written, do NOT "
                            f"start a new section/chunk. {_close} "
                            f"Output ONLY JSON: {{\"script\": \"<the additional continuation text only>\"}}."
                        )
                        if log_queue:
                            log_msg(log_queue, f"  ↪ length recovery {_rec}/{max_recovery_turns} (turn {turn_no}): {_w}<{_lo} words → +{_deficit}")
                        # FIX-2026-06-29: дожим НЕ-фатальный — если _send_one/merge падают (в т.ч. SDK
                        # «cannot unpack non-iterable NoneType» на recovery-turn'е) → оставляем текущую длину,
                        # НЕ роняем всю джобу. Ловим BaseException (anyio оборачивает в ExceptionGroup).
                        try:
                            _cont = await _send_one(_rp, f"turn {turn_no} recovery {_rec}")
                            raw = _gen_merge_continuation(raw, _cont)
                            raw_responses[-1] = raw
                        except (KeyboardInterrupt, SystemExit):
                            raise
                        except BaseException as _rec_e:
                            if log_queue:
                                log_msg(log_queue, f"  ⚠️ recovery {_rec} (turn {turn_no}) сбой ({type(_rec_e).__name__}: {str(_rec_e)[:70]}) — оставляю текущую длину, джобу НЕ роняю")
                            break
                    if log_queue and _rec > 0:
                        log_msg(log_queue, f"  ✅ after recovery (turn {turn_no}): {_gen_count_script_words(raw)} words / band {_lo}-{_hi}")
        finally:
            try:
                await client.disconnect()
            except Exception as e:
                if log_queue:
                    log_msg(log_queue, f"  ⚠️ disconnect: {e}")

    # Preflight CLI check — one-time cache (2026-06-04)
    _ensure_cli_ready(log_queue)

    # Semaphore: общий лимит claude subprocess'ов. Держим вокруг ВСЕГО цикла включая
    # forensics+pkill при stall — иначе зомби-CLI жив, а слот свободен → лишний CLI.
    outer_e = None
    with _claude_sema:
        try:
            anyio.run(_run)
        except BaseException as e:
            outer_e = e
            _to_inner = _find_timeout_error(e)
            if _to_inner is not None:
                # forensics+pkill INSIDE семафора (зомби не занимает свободный слот)
                try:
                    _collect_hung_cli_forensics(log_queue, f"{task_info or 'conv'}_turn{len(raw_responses) + 1}")
                except Exception:
                    pass
                try:
                    subprocess.run(
                        ["pkill", "-9", "-P", str(os.getpid()), "-f", "claude"],
                        capture_output=True, timeout=5,
                    )
                except Exception:
                    pass

    # вышли из семафора — обрабатываем исход
    if outer_e is None:
        if error_msg:
            return False, raw_responses, error_msg
        if len(raw_responses) != len(turn_prompts):
            return False, raw_responses, f"completed {len(raw_responses)}/{len(turn_prompts)} turns"
        return True, raw_responses, None

    _to_err = _find_timeout_error(outer_e)
    done_count = len(raw_responses)
    if _to_err is not None:
        # abandon-to-partial: НЕ пытаемся replay (сессия может быть битой)
        reason = str(_to_err) if str(_to_err) else f"hard cap {SUBSCRIPTION_CALL_TIMEOUT_SEC}s"
        msg = (
            f"Conversation stalled at turn {done_count + 1}/{len(turn_prompts)} ({reason}). "
            f"Completed {done_count} turns. abandon-to-partial."
        )
        if log_queue:
            log_msg(log_queue, f"  ⏱️  {msg}")
        return False, raw_responses, SUBSCRIPTION_TIMEOUT_PREFIX + msg
        # не timeout — пробрасываем как обычное исключение
        if not isinstance(outer_e, Exception):
            raise
        msg = f"Conversation error at turn {done_count + 1}: {type(outer_e).__name__}: {outer_e}"
        if log_queue:
            log_msg(log_queue, f"  ❌ {msg}")
        return False, raw_responses, msg


def call_api_universal(
    messages: List[Dict],
    api_key: str,
    model: str,
    log_queue: queue.Queue,
    task_info: str = "",
    provider: str = "claude",
    oauth_token: str = ""
) -> Tuple[bool, str]:
    """
    Универсальная функция вызова API (Claude, OpenAI или Pro подписка).
    
    Args:
        provider: "claude", "openai" или "subscription"
        oauth_token: OAuth токен для подписки (только для provider="subscription")
    """
    debug_log(log_queue, "API ROUTE:", f"provider={provider}, model={model}, task={task_info}")
    if provider == "subscription":
        return call_claude_subscription(messages, log_queue, task_info, model, oauth_token)
    elif provider == "claude":
        return call_claude_api(messages, api_key, model, log_queue, task_info, use_caching=True)
    else:
        return call_openai_api(messages, model, api_key, log_queue, task_info)


_TOKEN_LIMIT_CACHE: Dict[str, Tuple[bool, float]] = {}   # token -> (на_лимите?, ts) — кэш пред-проверки
_TOKEN_LIMIT_TTL_SEC = int(os.environ.get("TOKEN_LIMIT_TTL_SEC", "90"))  # сколько доверять пробе (лимит не меняется секунда-в-секунду)


def _token_is_limited(token: str, log_queue=None) -> bool:
    """PROACTIVE-2026-06-28: дешёвая проба `claude -p test` ИМЕННО этим токеном — на лимите/мёртв ли он.
    Кэш на TTL чтоб не пробивать каждый чанк. Проба упала по другой причине → считаем OK (пусть реальный звонок решит)."""
    if not token:
        return False
    now = time.time()
    c = _TOKEN_LIMIT_CACHE.get(token)
    if c and (now - c[1]) < _TOKEN_LIMIT_TTL_SEC:
        return c[0]
    limited = False
    try:
        r = subprocess.run(
            ["claude", "-p", "test"], capture_output=True, text=True, timeout=15,
            env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token},
        )
        out = ((r.stdout or "") + " " + (r.stderr or "")).lower()
        limited = _is_token_switch_signal(out)
    except Exception:
        limited = False
    _TOKEN_LIMIT_CACHE[token] = (limited, now)
    return limited


def call_api_universal_with_token_rotation(
    messages: List[Dict],
    api_key: str,
    model: str,
    log_queue: queue.Queue,
    task_info: str,
    provider: str,
    oauth_tokens: List[str],
    current_token_index: List[int],
) -> Tuple[bool, str]:
    """
    Вызов API с переключением на другой OAuth-токен при лимите подписки.
    current_token_index — список из одного int (мутабельный), обновляется при переключении.
    """
    if not oauth_tokens or provider != "subscription" or len(oauth_tokens) <= 1:
        token = oauth_tokens[0] if oauth_tokens else ""
        return call_api_universal(messages, api_key, model, log_queue, task_info, provider, token)
    n = len(oauth_tokens)
    tried = 0
    last_response = ""
    # ROUND-ROBIN-2026-06-28: КАЖДЫЙ звонок берёт СЛЕДУЮЩИЙ токен по кругу — нагрузка распределяется по ВСЕМ токенам,
    # а не сидим на одном пока не лопнет. Лимитный/мёртвый — пропускаем (проба + кэш, большой запрос не шлём).
    while tried < n:
        idx = current_token_index[0] % n
        current_token_index[0] = (idx + 1) % n        # сразу сдвигаем — следующий звонок пойдёт на следующий токен
        token = oauth_tokens[idx]
        if _token_is_limited(token, log_queue):        # лимит/мёртв → пропускаем
            if log_queue:
                log_msg(log_queue, f"  ⏭️  Токен {idx + 1}/{n} на лимите — пропускаю (round-robin → следующий)")
            tried += 1
            continue
        success, response = call_api_universal(messages, api_key, model, log_queue, task_info, provider, token)
        if success:
            return True, response
        last_response = response
        if response.startswith(SUBSCRIPTION_LIMIT_PREFIX):
            _TOKEN_LIMIT_CACHE[token] = (True, time.time())   # запомнить лимит → следующие звонки пропустят
            if log_queue:
                log_msg(log_queue, f"  🔄 Токен {idx + 1}/{n} лимит подписки — пробую следующий")
            tried += 1
        else:
            return False, response
    return False, last_response or "SUBSCRIPTION_LIMIT: все токены на лимите/недоступны"




def translate_paragraphs_recursive(
    paragraphs: List[str],
    lang_name: str,
    lang_display: str,
    chunk_index: int,
    total_chunks: int,
    channel_name: str,
    host_name: str,
    analysis: Optional[Dict],
    prev_context: str,
    system_prompt: str,
    api_call_fn,
    log_queue: queue.Queue,
    depth: int = 0,
    max_depth: int = 6,
) -> str:
    """
    Рекурсивно переводит блок из нескольких абзацев с гарантией 1:1.

    Логика:
    - Один API-вызов на весь блок. Если число абзацев совпало — возвращаем.
    - Если не совпало — режем pополам по абзацам и рекурсивно переводим каждую половину.
    - Базовый случай: 1 абзац — переводим как есть и возвращаем (даже если что-то странное,
      это будет ровно 1 абзац для alignment).

    Возвращает строку с абзацами через \\n\\n.
    """
    n = len(paragraphs)
    if n == 0:
        return ""
    indent = "      " + "  " * depth
    chunk_text = "\n\n".join(paragraphs)
    user_prompt = get_rewrite_prompt(
        lang_name, chunk_text, chunk_index, total_chunks,
        lang_display, channel_name, host_name, analysis, prev_context,
        preserve_paragraphs=True,
        expected_paragraphs=n,
        strict_retry=(depth > 0),
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    success, response = api_call_fn(messages)
    if success:
        text = extract_from_brackets(response)
        text = normalize_paragraph_separators(text, n)
        if count_paragraphs(text) == n:
            if depth > 0:
                log_msg(log_queue, f"{indent}✅ split d={depth}: {n} абзацев OK")
            return text
        actual = count_paragraphs(text)
        log_msg(log_queue, f"{indent}⚠️ split d={depth}: {actual}/{n} — режу пополам")
    else:
        log_msg(log_queue, f"{indent}⚠️ split d={depth}: API error — режу пополам")

    # Базовый случай: 1 абзац — обработать как есть, даже если получилось плохо
    if n == 1 or depth >= max_depth:
        # n == 1 — корректный лист, гарантируем 1 абзац (без \n\n внутри).
        # n > 1 при исчерпанной глубине — degraded fallback: возвращаем n маркеров
        # через \n\n, чтобы вверх по рекурсии count_paragraphs == n и chunk
        # пометился failed валидатором, а не молча терял разделители.
        if n == 1:
            if success:
                text = extract_from_brackets(response).strip()
                if not text:
                    text = paragraphs[0]
                text = re.sub(r"\n\s*\n+", " ", text).strip()
                return text
            return paragraphs[0]
        # n > 1: depth исчерпан
        return "\n\n".join(f"[FAIL_para_{i}]" for i in range(n))

    # Делим пополам и рекурсивно
    half = n // 2
    left = translate_paragraphs_recursive(
        paragraphs[:half], lang_name, lang_display, chunk_index, total_chunks,
        channel_name, host_name, analysis, prev_context,
        system_prompt, api_call_fn, log_queue, depth + 1, max_depth,
    )
    right = translate_paragraphs_recursive(
        paragraphs[half:], lang_name, lang_display, chunk_index, total_chunks,
        channel_name, host_name, analysis, "",  # prev_context уже использован в left
        system_prompt, api_call_fn, log_queue, depth + 1, max_depth,
    )
    return left + "\n\n" + right


# ============== ОСНОВНАЯ ФУНКЦИЯ РЕРАЙТА ==============

def rewrite_single_language(
    source_file: str,
    lang_display: str,
    lang_name: str,
    chunk_size: int,
    model: str,
    api_key: str,
    output_dir: str,
    log_queue: queue.Queue,
    channel_name: str = "",
    host_name: str = "",
    address_style: str = "",
    custom_prompt: str = "",
    use_subscription: bool = False,
    oauth_token: str = "",
    oauth_tokens: Optional[List[str]] = None,
    start_token_index: int = 0,
    preserve_paragraphs: bool = False,
) -> Tuple[str, bool]:
    """
    Рерайтит файл на один язык с сохранением прогресса.
    При лимите подписки переключается на следующий токен из oauth_tokens.

    preserve_paragraphs=True: разбиение по абзацам (\\n\\n), на каждом чанке
    проверяется что число абзацев на выходе == входу, до 3 retry; при провале —
    язык помечается failed.
    """
    if STOP_FLAG.is_stopped():
        return (lang_display, False)
    
    tokens = (oauth_tokens or []) if oauth_tokens is not None else ([oauth_token] if oauth_token else [])
    if use_subscription and not tokens:
        tokens = [oauth_token] if oauth_token else []
    current_token_index = [start_token_index % len(tokens)] if tokens else [0]
    
    try:
        # Читаем файл (поддержка txt, docx, pdf, odt, rtf)
        text = read_file_as_text(source_file)
        
        # Анализ текста: кэш по (файл, модель) — один анализ на файл для всех языков
        cache_key = (source_file, model)
        with _ANALYSIS_CACHE_LOCK:
            analysis = _ANALYSIS_CACHE.get(cache_key)
        if analysis is None:
            log_msg(log_queue, f"📊 {lang_display}: Analyzing text structure...")
            analysis = analyze_text_structure(
                text, log_queue,
                use_subscription=use_subscription,
                oauth_token=tokens[current_token_index[0]] if tokens else oauth_token,
                model=model,
                api_key=api_key,
                oauth_tokens=tokens if use_subscription and len(tokens) > 1 else None,
                current_token_index=current_token_index if use_subscription and len(tokens) > 1 else None,
            )
            with _ANALYSIS_CACHE_LOCK:
                _ANALYSIS_CACHE[cache_key] = analysis
        else:
            log_msg(log_queue, f"📊 {lang_display}: анализ из кэша (файл уже обработан)")

        # === Claude rewrite mode (2026-06-03) ===
        # Для Claude — крупный chunk (60-80K chars ≈ один кусок на ≤14K слов) и
        # natural reroll (preserve_paragraphs=False) уже передаётся snippet сверху.
        # GPT-путь (preserve=True, мелкий chunk) не трогаем.
        _is_claude = ("claude" in (model or "").lower()) or use_subscription
        if _is_claude and not preserve_paragraphs:
            original_chunk_size = chunk_size
            chunk_size = int(os.environ.get("CLAUDE_CHUNK_SIZE", "20000"))  # 2026-06-11: 60000->20000, промпт ~35K (opus стабилен; на 65K падал stream-stall #25979)
            if chunk_size != original_chunk_size:
                log_msg(log_queue, f"🔧 {lang_display}: chunk_size bump для Claude {original_chunk_size}→{chunk_size} chars (меньше швов = лучше связность)")

        # PATCH-2026-06-13-e2big-guard: preserve режет по абзацам (\n\n) и НЕ дробит один
        # огромный абзац. Текст без структуры абзацев (1 гигантский абзац) ушёл бы
        # одним промптом-аргументом → CLI 'Argument list too long' (E2BIG, >128КБ/арг).
        # В таком случае preserve бессмыслен — откатываемся на size-chunking ≤20К.
        if preserve_paragraphs:
            _paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
            _maxpara = max((len(p) for p in _paras), default=0)
            if _maxpara > 30000:
                log_msg(log_queue, f"\u26a0\ufe0f {lang_display}: \u043c\u0430\u043a\u0441. \u0430\u0431\u0437\u0430\u0446 {_maxpara} \u0441\u0438\u043c\u0432 > 30000 \u2014 \u043d\u0435\u0442 \u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u044b \u0430\u0431\u0437\u0430\u0446\u0435\u0432, preserve \u0432\u044b\u043a\u043b\u044e\u0447\u0435\u043d, \u0440\u0435\u0436\u0443 \u043f\u043e \u0440\u0430\u0437\u043c\u0435\u0440\u0443 (\u0437\u0430\u0449\u0438\u0442\u0430 \u043e\u0442 E2BIG)")
                preserve_paragraphs = False
                chunk_size = int(os.environ.get("CLAUDE_CHUNK_SIZE", "20000"))

        if preserve_paragraphs:
            chunks, chunk_para_counts = split_text_to_chunks_by_paragraphs(text, chunk_size)
            total_input_paragraphs = sum(chunk_para_counts) if chunk_para_counts else count_paragraphs(text)
            log_msg(log_queue, f"📐 {lang_display}: режим сохранения абзацев — {total_input_paragraphs} абзацев → {len(chunks)} чанков")
        else:
            chunks = split_text_to_chunks(text, chunk_size)
            chunk_para_counts = []
            total_input_paragraphs = 0
        total_chunks = len(chunks)
        
        # B5 PATCH-2026-06-25-secfix: user_id для tenant-scoped ключа прогресса.
        # output_dir = outputs/<user_id> -> basename = user_id (== job.user_id).
        _uid = os.path.basename(os.path.normpath(output_dir)) if output_dir else ""
        # Получаем или создаём задачу в менеджере прогресса
        task = PROGRESS_MANAGER.get_or_create_task(
            source_file, lang_name, lang_display, total_chunks, user_id=_uid
        )
        
        # Проверяем, не завершена ли уже
        if task.status == TaskStatus.COMPLETED.value:
            log_msg(log_queue, f"✅ {lang_display}: уже завершено ранее")
            return (lang_display, True)
        
        # Определяем какие чанки нужно обработать
        incomplete_chunks = PROGRESS_MANAGER.get_incomplete_chunks(task)
        
        if not incomplete_chunks:
            log_msg(log_queue, f"✅ {lang_display}: все части готовы")
            return (lang_display, True)
        
        completed_count = total_chunks - len(incomplete_chunks)
        if completed_count > 0:
            log_msg(log_queue, f"🔄 {lang_display}: продолжаю с части {completed_count + 1}/{total_chunks}")
        else:
            log_msg(log_queue, f"🌍 {lang_display}: начинаю ({total_chunks} частей)")
        
        # Определяем провайдера из модели и флага подписки
        keys_cfg = get_keys_config()
        provider = _detect_provider(model, use_subscription)
        if provider == "subscription":
            actual_api_key = ""
        elif provider == "claude":
            actual_api_key = api_key or keys_cfg.get("anthropic_api_key", ANTHROPIC_API_KEY)
        else:
            actual_api_key = api_key or keys_cfg.get("openai_api_key", OPENAI_API_KEY)
        log_msg(log_queue, f"🔧 {lang_display}: model='{model}', provider='{provider}', use_sub={use_subscription}, key={'***'+actual_api_key[-4:] if actual_api_key else 'EMPTY'}")

        # Обрабатываем только незавершённые чанки
        for chunk_idx in incomplete_chunks:
            if STOP_FLAG.is_stopped():
                log_msg(log_queue, f"⏹️ {lang_display}: остановлено, прогресс сохранён")
                return (lang_display, False)
            
            chunk_text = chunks[chunk_idx]
            
            # Контекст из предыдущих чанков: последние 3 предложения для согласованности имён и стиля
            prev_chunk_context = ""
            if chunk_idx > 0:
                prev_task_key = f"{_uid}::{os.path.basename(source_file)}::{lang_name}"  # B5
                prev_task = PROGRESS_MANAGER.tasks.get(prev_task_key)
                if prev_task:
                    prev_chunk = prev_task.chunks[chunk_idx - 1] if chunk_idx - 1 < len(prev_task.chunks) else None
                    if prev_chunk and prev_chunk.get("status") == "completed" and prev_chunk.get("result"):
                        prev_text = prev_chunk["result"]
                        prev_sentences = re.split(r"(?<=[.!?])\s+", prev_text)
                        prev_chunk_context = " ".join(prev_sentences[-3:]) if len(prev_sentences) > 3 else prev_text[-500:]

            # Отмечаем как in_progress
            PROGRESS_MANAGER.update_chunk(source_file, lang_name, chunk_idx, "in_progress", user_id=_uid)
            
            # Системный промт (для кэширования!)
            # ВАЖНО: system_prompt должен быть одинаковым для всех чанков одного языка!
            # ВАЖНО: lang_display должен быть одинаковым для всех чанков!
            system_prompt = get_system_prompt(lang_name, lang_display, analysis, address_style, custom_prompt)
            
            # Логируем хеш system prompt для проверки идентичности
            system_hash = hashlib.md5(system_prompt.encode()).hexdigest()[:8]
            system_length = len(system_prompt)
            estimated_tokens = system_length // 4
            
            if chunk_idx == 0:
                log_msg(log_queue, f"  🔑 System prompt: hash={system_hash}, длина={system_length} символов (~{estimated_tokens} токенов)")
                log_msg(log_queue, f"  🔑 lang_name='{lang_name}', lang_display='{lang_display}' (должны быть одинаковыми для всех чанков)")
                if custom_prompt:
                    log_msg(log_queue, f"  📝 Custom prompt: {custom_prompt[:80]}{'...' if len(custom_prompt) > 80 else ''}")
            else:
                log_msg(log_queue, f"  🔑 System prompt: hash={system_hash} (должен совпадать с первым: {system_hash})")
            
            # Проверяем минимальную длину для кэширования
            if system_length < 4096:
                log_msg(log_queue, f"  ⚠️ ВНИМАНИЕ: System prompt слишком короткий ({system_length} < 4096), кэш не будет работать!")
            
            # Ожидаемое число абзацев в этом чанке (если включён режим)
            expected_paragraphs = (
                chunk_para_counts[chunk_idx]
                if preserve_paragraphs and chunk_idx < len(chunk_para_counts)
                else 0
            )

            # Формируем сообщения
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": get_rewrite_prompt(
                    lang_name, chunk_text, chunk_idx, total_chunks,
                    lang_display, channel_name, host_name, analysis, prev_chunk_context,
                    preserve_paragraphs=preserve_paragraphs,
                    expected_paragraphs=expected_paragraphs,
                )}
            ]
            
            # Вызываем API (с ротацией токенов при лимите подписки)
            task_info = f"{lang_display} {chunk_idx+1}/{total_chunks}"
            if use_subscription and tokens:
                success, response = call_api_universal_with_token_rotation(
                    messages, actual_api_key, model, log_queue, task_info, provider, tokens, current_token_index
                )
            else:
                success, response = call_api_universal(
                    messages, actual_api_key, model, log_queue, task_info, provider,
                    tokens[current_token_index[0]] if tokens else oauth_token,
                )
            
            if STOP_FLAG.is_stopped():
                log_msg(log_queue, f"⏹️ {lang_display}: остановлено")
                return (lang_display, False)
            
            if not success:
                # Критическая ошибка (не rate limit)
                log_msg(log_queue, f"❌ {lang_display}: часть {chunk_idx+1} - {response}")
                PROGRESS_MANAGER.update_chunk(source_file, lang_name, chunk_idx, "failed", error=response, user_id=_uid)
                
                if "API ключ" in response or "не найдена" in response:
                    return (lang_display, False)
                continue
            
            # Успех!
            extracted = extract_from_brackets(response)

            # ---- Валидация структуры абзацев (1:1 alignment) ----
            if preserve_paragraphs and expected_paragraphs > 0:
                # Эвристика: если модель использовала одинарный \n вместо \n\n — восстанавливаем
                extracted = normalize_paragraph_separators(extracted, expected_paragraphs)
                actual_paragraphs = count_paragraphs(extracted)
                paragraph_retries = 0
                MAX_PARAGRAPH_RETRIES = 3
                while actual_paragraphs != expected_paragraphs and paragraph_retries < MAX_PARAGRAPH_RETRIES:
                    paragraph_retries += 1
                    log_msg(
                        log_queue,
                        f"   ⚠️ Chunk {chunk_idx+1}: paragraphs mismatch ({actual_paragraphs}/{expected_paragraphs}), retry {paragraph_retries}/{MAX_PARAGRAPH_RETRIES}"
                    )
                    para_retry_user = get_rewrite_prompt(
                        lang_name, chunk_text, chunk_idx, total_chunks,
                        lang_display, channel_name, host_name, analysis, prev_chunk_context,
                        preserve_paragraphs=True,
                        expected_paragraphs=expected_paragraphs,
                        strict_retry=True,
                    )
                    # Дополняем явным указанием что предыдущая попытка дала {actual_paragraphs}
                    para_retry_user += (
                        f"\n\nPrevious attempt produced {actual_paragraphs} paragraphs — that is WRONG. "
                        f"Output EXACTLY {expected_paragraphs}."
                    )
                    para_messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": para_retry_user},
                    ]
                    if use_subscription and tokens:
                        ok_p, resp_p = call_api_universal_with_token_rotation(
                            para_messages, actual_api_key, model, log_queue,
                            f"{task_info}_para_retry{paragraph_retries}", provider, tokens, current_token_index
                        )
                    else:
                        ok_p, resp_p = call_api_universal(
                            para_messages, actual_api_key, model, log_queue,
                            f"{task_info}_para_retry{paragraph_retries}", provider,
                            tokens[current_token_index[0]] if tokens else oauth_token,
                        )
                    if ok_p:
                        extracted_p = extract_from_brackets(resp_p)
                        extracted_p = normalize_paragraph_separators(extracted_p, expected_paragraphs)
                        actual_p = count_paragraphs(extracted_p)
                        if actual_p == expected_paragraphs:
                            extracted = extracted_p
                            actual_paragraphs = actual_p
                            log_msg(
                                log_queue,
                                f"   ✅ Chunk {chunk_idx+1}: paragraph structure OK ({expected_paragraphs}/{expected_paragraphs}) after {paragraph_retries} retries"
                            )
                            break
                        else:
                            actual_paragraphs = actual_p
                if actual_paragraphs != expected_paragraphs:
                    # Fallback: рекурсивный split — режем чанк пополам по абзацам и
                    # переводим каждую половину отдельно. На малом размере модель
                    # справляется лучше; в худшем случае дойдём до 1 абзаца.
                    log_msg(
                        log_queue,
                        f"   🪓 Chunk {chunk_idx+1}: 3 retries не помогли ({actual_paragraphs}/{expected_paragraphs}), запускаю рекурсивный split"
                    )

                    def _api_call(messages_):
                        if use_subscription and tokens:
                            return call_api_universal_with_token_rotation(
                                messages_, actual_api_key, model, log_queue,
                                f"{task_info}_split", provider, tokens, current_token_index
                            )
                        return call_api_universal(
                            messages_, actual_api_key, model, log_queue,
                            f"{task_info}_split", provider,
                            tokens[current_token_index[0]] if tokens else oauth_token,
                        )

                    input_paragraphs = chunk_text.split("\n\n")
                    fallback_text = translate_paragraphs_recursive(
                        input_paragraphs, lang_name, lang_display,
                        chunk_idx, total_chunks, channel_name, host_name,
                        analysis, prev_chunk_context, system_prompt,
                        _api_call, log_queue, depth=0,
                    )
                    fallback_count = count_paragraphs(fallback_text)
                    if fallback_count == expected_paragraphs:
                        extracted = fallback_text
                        actual_paragraphs = fallback_count
                        log_msg(
                            log_queue,
                            f"   ✅ Chunk {chunk_idx+1}: paragraphs восстановлены через split → {fallback_count}/{expected_paragraphs}"
                        )
                    else:
                        log_msg(
                            log_queue,
                            f"   ⚠️ {lang_display}: chunk {chunk_idx+1} paragraphs FAILED ({fallback_count}/{expected_paragraphs}) даже после split — помечаю чанк как failed, продолжаю остальные"
                        )
                        PROGRESS_MANAGER.update_chunk(
                            source_file, lang_name, chunk_idx, "failed",
                            error=f"paragraphs {fallback_count}/{expected_paragraphs}", user_id=_uid
                        )
                        continue
                else:
                    log_msg(
                        log_queue,
                        f"   ✅ Chunk {chunk_idx+1}: paragraphs preserved {expected_paragraphs}/{expected_paragraphs}"
                    )

            # Проверка длины с retry
            orig_len = len(chunk_text)
            extracted_len = len(extracted)
            ratio = extracted_len / orig_len if orig_len > 0 else 1.0

            # Детальное логирование для отладки
            if extracted_len < 100:  # Подозрительно короткий результат
                log_msg(log_queue, f"   ⚠️ WARNING: Extracted text very short ({extracted_len} chars), response length: {len(response)}")
                log_msg(log_queue, f"   🔍 Response preview: {response[:200]}...")

            if ratio < 0.90:  # короткий ИЛИ нарратив-преамбула («I need to… Let me first…»)
                log_msg(log_queue, f"   ⚠️ Chunk {chunk_idx+1}: SHORT ({ratio:.1%}, {extracted_len}/{orig_len} chars) — анти-нарратив retry...")
                # FIX-2026-06-28-narration: нарратив opus-4-6 интермиттентный (~17%, детерминированно не убрать).
                # Лечим ПОВТОРАМИ с жёстким анти-нарратив-промптом (старт с '[', запрет плана/«I need to»/«Let me»).
                # До N попыток: 0.17^N → при 4 ≈ 0.08% что все нарративят. Берём лучший (самый длинный) результат.
                _NARR_MAX = int(os.environ.get("NARRATION_MAX_RETRIES", "4"))
                best_extracted, best_len = extracted, extracted_len
                for _ratt in range(1, _NARR_MAX + 1):
                    retry_prompt = f"""STOP. Your previous reply was NOT the translation — it was planning / a preamble (like "I need to...", "Let me first...") or far too short. That is a FAILURE.

Output ONLY the COMPLETE translation/adaptation of the text below, inside [square brackets].
- Your VERY FIRST character MUST be '[' — start translating IMMEDIATELY.
- Do NOT write ANY analysis, planning, thinking, notes, or commentary.
- NEVER begin with "I need to", "Let me", "I will", "First", "Here", "I'll", "Okay", "The".
- Translate EVERY sentence. Length must be >= the original (>={int(orig_len * 0.95)} characters). Numbers and dates as words. Keep ALL facts.

TEXT TO TRANSLATE:
{chunk_text}

Now output the full translation, starting with '[':"""
                    retry_messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": retry_prompt},
                    ]
                    success_retry, response_retry = call_api_universal(
                        retry_messages, actual_api_key, model, log_queue,
                        f"{task_info}_nretry{_ratt}", provider,
                        tokens[current_token_index[0]] if tokens else oauth_token,
                    )
                    if not success_retry:
                        log_msg(log_queue, f"   ⚠️ анти-нарратив retry {_ratt}/{_NARR_MAX}: вызов упал, дальше")
                        continue
                    extracted_retry = extract_from_brackets(response_retry)
                    retry_len = len(extracted_retry)
                    retry_ratio = retry_len / orig_len if orig_len > 0 else 1.0
                    if retry_len > best_len:
                        best_extracted, best_len = extracted_retry, retry_len
                    if retry_ratio >= 0.90:
                        log_msg(log_queue, f"   ✅ анти-нарратив retry {_ratt}/{_NARR_MAX} OK: {retry_len} chars ({retry_ratio:.1%})")
                        break
                    log_msg(log_queue, f"   ⚠️ retry {_ratt}/{_NARR_MAX}: всё ещё {retry_ratio:.1%} ({retry_len}/{orig_len}) — повтор")
                extracted = _accept_retry_if_paragraphs_ok(
                    extracted, best_extracted,
                    preserve_paragraphs, expected_paragraphs,
                    log_queue, chunk_idx, "narration-retry-loop",
                )
            elif ratio > MAX_CHUNK_LENGTH_RATIO:  # Раздувание сверх порога — retry без добавления воды
                log_msg(log_queue, f"   ⚠️ Chunk {chunk_idx+1}: TOO LONG ({ratio:.1%}, {extracted_len}/{orig_len} chars), retrying...")
                max_allowed = int(orig_len * MAX_CHUNK_LENGTH_RATIO)
                retry_long_prompt = f"""Your output is TOO LONG. You added {extracted_len - orig_len} extra characters.

Original: {orig_len} characters. Your output: {extracted_len} (max allowed: {max_allowed}).

Do NOT add new content, examples, or padding. Preserve meaning and style but stay within 95-{int(MAX_CHUNK_LENGTH_RATIO*100)}% of original length.

Original text:
{chunk_text}

TASK: Return a complete adaptation that is NOT longer than {int(MAX_CHUNK_LENGTH_RATIO*100)}% of the original. Output ONLY the adapted text in square brackets."""

                retry_long_messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": retry_long_prompt}
                ]
                success_retry, response_retry = call_api_universal(
                    retry_long_messages, actual_api_key, model, log_queue,
                    f"{task_info}_retry_long", provider,
                    tokens[current_token_index[0]] if tokens else oauth_token,
                )
                if success_retry:
                    extracted_retry = extract_from_brackets(response_retry)
                    retry_len = len(extracted_retry)
                    retry_ratio = retry_len / orig_len if orig_len > 0 else 1.0
                    if retry_ratio <= MAX_CHUNK_LENGTH_RATIO and retry_ratio >= 0.90:
                        log_msg(log_queue, f"   ✅ Retry LONG success: {retry_len} chars ({retry_ratio:.1%})")
                        extracted = _accept_retry_if_paragraphs_ok(
                            extracted, extracted_retry,
                            preserve_paragraphs, expected_paragraphs,
                            log_queue, chunk_idx, "long-retry-success",
                        )
                    elif retry_len < extracted_len:
                        log_msg(log_queue, f"   📉 Using retry version (shorter: {retry_len} vs {extracted_len})")
                        extracted = _accept_retry_if_paragraphs_ok(
                            extracted, extracted_retry,
                            preserve_paragraphs, expected_paragraphs,
                            log_queue, chunk_idx, "long-retry-shorter",
                        )
                else:
                    log_msg(log_queue, f"   ⚠️ Retry LONG failed, keeping current output")
            else:
                log_msg(log_queue, f"   ✅ Chunk {chunk_idx+1}: {extracted_len} chars ({ratio:.1%})")

            # === Per-paragraph length safety-net ===
            # Defence-in-depth поверх prompt-rules: если хоть один out_para раздут >LENGTH_RATIO_LIMIT
            # от src_para — length retry с явным указанием модели на проблемный абзац.
            # Лимит MAX_LENGTH_RETRIES; после исчерпания принимаем как есть с warning (не блокируем).
            if preserve_paragraphs and expected_paragraphs > 0:
                length_retry_count = 0
                while length_retry_count < MAX_LENGTH_RETRIES:
                    input_paras = chunk_text.split("\n\n")
                    output_paras = re.split(r"\n\s*\n", extracted)
                    violation = None
                    for _i, (src_p, out_p) in enumerate(zip(input_paras, output_paras)):
                        if len(out_p) > LENGTH_RATIO_LIMIT * max(len(src_p), LENGTH_FLOOR):
                            violation = (_i, len(src_p), len(out_p))
                            break
                    if violation is None:
                        break
                    _idx, _src_len, _out_len = violation
                    length_retry_count += 1
                    log_msg(
                        log_queue,
                        f"   ⚠️ Chunk {chunk_idx+1}: paragraph {_idx+1} раздут {_out_len}/{_src_len} ({_out_len/max(_src_len,1):.1f}×) — length retry {length_retry_count}/{MAX_LENGTH_RETRIES}"
                    )
                    len_retry_user = get_rewrite_prompt(
                        lang_name, chunk_text, chunk_idx, total_chunks,
                        lang_display, channel_name, host_name, analysis, prev_chunk_context,
                        preserve_paragraphs=True,
                        expected_paragraphs=expected_paragraphs,
                        strict_retry=True,
                    )
                    len_retry_user += (
                        f"\n\nPrevious attempt produced paragraph #{_idx+1} with {_out_len} chars from "
                        f"a source paragraph of {_src_len} chars (ratio {_out_len/max(_src_len,1):.1f}×). "
                        f"That is INFLATION. Output ALL paragraphs within 0.7×–1.5× of source length. "
                        f"Do NOT add sensory details, descriptions, or actions absent from source."
                    )
                    len_retry_messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": len_retry_user},
                    ]
                    if use_subscription and tokens:
                        ok_l, resp_l = call_api_universal_with_token_rotation(
                            len_retry_messages, actual_api_key, model, log_queue,
                            f"{task_info}_len_retry{length_retry_count}", provider, tokens, current_token_index
                        )
                    else:
                        ok_l, resp_l = call_api_universal(
                            len_retry_messages, actual_api_key, model, log_queue,
                            f"{task_info}_len_retry{length_retry_count}", provider,
                            tokens[current_token_index[0]] if tokens else oauth_token,
                        )
                    if ok_l:
                        extracted_l = extract_from_brackets(resp_l)
                        extracted_l = normalize_paragraph_separators(extracted_l, expected_paragraphs)
                        if count_paragraphs(extracted_l) == expected_paragraphs:
                            extracted = extracted_l
                        else:
                            log_msg(
                                log_queue,
                                f"   ⚠️ Length retry дал {count_paragraphs(extracted_l)}/{expected_paragraphs} абзацев — отклоняю, оставляю прежний"
                            )
                    else:
                        log_msg(log_queue, f"   ⚠️ Length retry API failed — оставляю прежний")
                if length_retry_count >= MAX_LENGTH_RETRIES and violation is not None:
                    log_msg(
                        log_queue,
                        f"   ❌ Chunk {chunk_idx+1}: {MAX_LENGTH_RETRIES} length retries провалены, принимаю с warning (не блокирую сохранение)"
                    )

            # Снимок до постобработки — на случай отката если структура абзацев сломается
            extracted_before_postproc = extracted

            # ПОСТОБРАБОТКА: Удаляем русские слова через 4o-mini
            # Находим предложения с кириллицей и заменяем их через дешевую модель
            extracted = postprocess_remove_russian_words(
                extracted,
                lang_name,
                get_postprocess_openai_key(),
                log_queue
            )

            # Финальная санитарная очистка перед записью в .txt для TTS
            extracted = sanitize_final_text(extracted, lang_name)

            # Нормализация: trailing/leading blank на границе чанка после "\n\n".join
            # сливается с межчанковым разделителем и теряет абзац — приводим к канону.
            extracted = extracted.strip("\n")
            extracted = re.sub(r"\n\s*\n", "\n\n", extracted)

            # Если включена защита абзацев — постобработка не должна изменить их количество
            if preserve_paragraphs and expected_paragraphs > 0:
                final_para = count_paragraphs(extracted)
                if final_para != expected_paragraphs:
                    log_msg(
                        log_queue,
                        f"   ⚠️ Chunk {chunk_idx+1}: postprocess сломал абзацы ({final_para}/{expected_paragraphs}), откатываю на до-постобработочный вариант"
                    )
                    extracted = extracted_before_postproc
                    extracted = extracted.strip("\n")
                    extracted = re.sub(r"\n\s*\n", "\n\n", extracted)

                # Двойная страховка: если после rollback структура всё ещё битая —
                # помечаем чанк failed и пропускаем его (errors>0 не даст сохранить файл).
                final_para_after = count_paragraphs(extracted)
                if final_para_after != expected_paragraphs:
                    log_msg(
                        log_queue,
                        f"   ❌ Chunk {chunk_idx+1}: после rollback структура абзацев всё ещё битая ({final_para_after}/{expected_paragraphs}) — помечаю как FAILED"
                    )
                    PROGRESS_MANAGER.update_chunk(
                        source_file, lang_name, chunk_idx, "failed",
                        error=f"paragraph structure broken after rollback: {final_para_after}/{expected_paragraphs}", user_id=_uid
                    )
                    continue

            # Anti-plan ratio-guard (2026-06-03): после всех retry — если финал катастрофически
            # короткий, это план/преамбула («I need to adapt...»), а не рерайт. НЕ сохраняем
            # как completed — помечаем failed, чтобы партнёр НЕ получил огрызок 0.7% в COMPLETED.
            # Порог 0.30 — НЕ ронять компрессирующие языки (cs/de часто 50-60%), но ловить мусор.
            _final_ratio = len(extracted) / orig_len if orig_len > 0 else 1.0
            if _final_ratio < 0.30:
                log_msg(
                    log_queue,
                    f"   ❌ {lang_display}: chunk {chunk_idx+1} вернул {_final_ratio:.1%} от оригинала "
                    f"({len(extracted)}/{orig_len} chars) — FAILED (не сохраняю огрызок: похоже на план/преамбулу)"
                )
                PROGRESS_MANAGER.update_chunk(
                    source_file, lang_name, chunk_idx, "failed",
                    error=f"output too short: {_final_ratio:.1%} ({len(extracted)}/{orig_len} chars) — likely plan/preface instead of rewrite", user_id=_uid
                )
                continue

            PROGRESS_MANAGER.update_chunk(source_file, lang_name, chunk_idx, "completed", result=extracted, user_id=_uid)
            log_msg(log_queue, f"   ✅ {lang_display}: часть {chunk_idx+1}/{total_chunks}")
        
        # Собираем результат
        task = PROGRESS_MANAGER.tasks.get(f"{_uid}::{os.path.basename(source_file)}::{lang_name}")  # B5
        
        if task:
            # Собираем все завершённые чанки
            results = []
            errors = 0
            for chunk in task.chunks:
                if chunk["status"] == "completed":
                    results.append(chunk["result"])
                else:
                    results.append(f"[ОШИБКА: {chunk.get('error', 'не обработано')}]")
                    errors += 1

            # 3-ступенчатый постпроцесс: Pass A (Branding) + Pass B (AI Declutter) параллельно → merge → Pass C (Nativeness)
            base_name = os.path.splitext(os.path.basename(source_file))[0][:50]
            lang_code = lang_name.lower().replace(" ", "_")[:15]  # для логов/постпроцесса
            # При preserve_paragraphs пропускаем pipeline: intro/outro/merge ломают 1:1 структуру
            if preserve_paragraphs:
                log_msg(log_queue, f"📐 {lang_display}: постпроцесс Pass A/B/C/D пропущен (режим сохранения абзацев)")
            elif _is_claude:
                # Claude rewrite (2026-06-03): OpenAI Pass A/B/C/D отключён (gpt-5-mini
                # на сильной модели → synonym swaps + factual errors). Вместо него —
                # лёгкая эвристическая чистка структурного мусора (block markers,
                # markdown headings, преамбулы «Вот переписанный текст:»).
                total_removed = 0
                cleaned_results = []
                for r in results:
                    if r and not str(r).startswith("[ОШИБКА:"):
                        c, removed = sanitize_claude_output(r, lang_name)
                        cleaned_results.append(c)
                        total_removed += removed
                    else:
                        cleaned_results.append(r)
                results = cleaned_results
                log_msg(log_queue, f"🧹 {lang_display}: sanitize Claude output — removed {total_removed} meta lines (postprocess Pass A/B/C/D пропущен для Claude)")
            elif ENABLE_NATIVE_SPEAKER_CHECK and results:
                full_text = "\n\n".join(results)
                full_text = run_postprocess_pipeline(
                    full_text,
                    lang_name,
                    lang_display,
                    host_name,
                    channel_name,
                    get_postprocess_openai_key(),
                    log_queue,
                    output_dir=output_dir,
                    base_name=base_name,
                    lang_code=lang_code,
                    is_generated_text=True,  # Это генерация, не рерайт
                )
                results = [full_text]

            # M5 PATCH-2026-06-25-secfix: НЕ сохраняем файл при ЛЮБЫХ ошибках чанков.
            # Раньше гард был только для preserve_paragraphs, а Claude/non-preserve путь писал
            # брак с маркерами [ОШИБКА:...] и отдавал как PARTIAL-downloadable. Теперь любой провал
            # чанка -> файл не пишем, failed chunks дозальются через "🔁 Повторить незавершённые".
            if errors > 0:
                log_msg(
                    log_queue,
                    f"⚠️ {lang_display}: {errors} чанков провалились — файл НЕ сохранён (не отдаём брак со вставками [ОШИБКА:…]). "
                    f"Жми «🔁 Повторить незавершённые» чтобы дозалить только их."
                )
                return (lang_display, False)

            # Сохраняем файл: код языка в начале (cs_, pl_, ro_...)
            output_file = os.path.join(output_dir, make_output_filename(base_name, lang_name))
            os.makedirs(os.path.dirname(output_file), exist_ok=True)
            final_text = "\n\n".join(results)

            # Финальная проверка структуры абзацев — должно быть РОВНО как в оригинале
            if preserve_paragraphs and total_input_paragraphs > 0:
                final_paragraphs = count_paragraphs(final_text)
                if final_paragraphs != total_input_paragraphs:
                    log_msg(
                        log_queue,
                        f"❌ {lang_display}: финальная структура абзацев не совпадает "
                        f"({final_paragraphs}/{total_input_paragraphs}). Файл НЕ сохранён."
                    )
                    return (lang_display, False)
                log_msg(
                    log_queue,
                    f"📐 {lang_display}: paragraphs preserved {final_paragraphs}/{total_input_paragraphs}"
                )

            # ШАГ 4: детектор утечки чисел/англ-слов-чисел — ФЛАГ needs_review, НЕ автофикс.
            # digit-часть универсальна (любой target: все числа должны быть словами).
            # English-word-часть валидна, т.к. пайплайн всегда English→X (для не-английских
            # источников не сработает — но абсолютное правило чисел в промпте защитит на
            # уровне модели). Исключены слова-коллизии: 'one' (pl "one"=они), 'ten'
            # (pl/cs "ten"=этот), 'million'/'billion' (de "Million"/"Billion" — родные) —
            # иначе постоянные ложные срабатывания на нормальном тексте этих языков.
            if lang_name.lower() != "english":
                _digits = re.search(r"\d", final_text)
                _eng_numwords = re.search(
                    r"\b(two|three|four|five|six|seven|eight|nine|"
                    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
                    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|"
                    r"twenties|thirties|forties|fifties|sixties|seventies|eighties|nineties)\b",
                    final_text, re.IGNORECASE
                )
                if _digits or _eng_numwords:
                    _what = []
                    if _digits:
                        _what.append(f"digit '{_digits.group(0)}'")
                    if _eng_numwords:
                        _what.append(f"EN number-word '{_eng_numwords.group(0)}'")
                    _note = "leftover " + " + ".join(_what) + " — числа спелить словами вручную (вычитка)"
                    log_msg(log_queue, f"⚠️ {lang_display}: {_note} → флаг needs_review (не автофикс)")
                    PROGRESS_MANAGER.set_needs_review(source_file, lang_name, _note, user_id=_uid)

            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(final_text)

            PROGRESS_MANAGER.set_task_output(source_file, lang_name, output_file, user_id=_uid)

            if errors == 0:
                log_msg(log_queue, f"✅ {lang_display}: готово → {os.path.basename(output_file)}")
                return (lang_display, True)
            else:
                log_msg(log_queue, f"⚠️ {lang_display}: готово с {errors} ошибками")
                return (lang_display, False)
        
        return (lang_display, False)
        
    except Exception as e:
        log_msg(log_queue, f"❌ {lang_display}: {e}")
        return (lang_display, False)


def generate_and_postprocess_single_text(*args, **kwargs) -> Tuple[str, bool]:
    """DEPRECATED (Round 2, 2026-05-23): старая all-in-one generation+postprocess function.
    Заменена в новом worker (_execute_generation_job в server.py) chunk-loop через build_chunk_prompt()
    + run_postprocess_pipeline() как отдельной стадией. Любой её вызов теперь — явная ошибка,
    не silent корруптом со старой schema preset'ов.
    """
    raise NotImplementedError(
        "generate_and_postprocess_single_text removed in Round 2 (2026-05-23). "
        "New flow: server.py:_execute_generation_job + rewrite_core:build_chunk_prompt + run_postprocess_pipeline."
    )

# ============== СКАЧИВАНИЕ СУБТИТРОВ И ОБЛОЖЕК ==============

