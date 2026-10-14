"""
Googler Worker v2.1 - Генерация изображений и видео через Googler API
+ Генерация промтов через GPT-4.1-nano

Обновлено для API v2:
- Асинхронная генерация видео с polling
- Поддержка новых операций (generate_video_from_image, generate_video_start_end)
- Улучшенная обработка ошибок

Возможности:
- Генерация промтов на основе X предложений текста через GPT
- Генерация изображений через Googler API
- Генерация видео из изображений через Googler API (async)
- Многопоточная обработка (50+ потоков для GPT)
"""

import os
import sys
import json
import json as _json
import base64
import argparse
import errno
import functools
import math
import re
import stat
import time
import random
import hashlib
import tempfile
import threading
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List, Tuple

import reference_contract as ref_contract

try:
    import fcntl as _fcntl
except ImportError:  # Windows: one desktop worker process, thread lock below is enough.
    _fcntl = None

try:
    import requests
except ImportError:
    print("❌ Модуль requests не установлен. Установите: pip install requests")
    sys.exit(1)

# Чем писать промпты: OpenAI API (дефолт) / подписка ChatGPT (Codex CLI) / подписка Claude.
# Выбор приходит из кабинета через env VS_PROMPT_PROVIDER; логика промптов от этого не зависит —
# меняется только транспорт и модель (см. prompt_llm.py). Модуля нет → работаем как раньше.
try:
    import prompt_llm
except Exception:
    prompt_llm = None

# 03.09.2026: журнал платных вызовов кабинета. Один кадр = один платный заказ у провайдера,
# и до сих пор от повторной оплаты защищала только проверка «файл уже на диске»: она не
# закрывает окно между «провайдер принял заказ» и «файл скачан». Модуля нет или переменных
# окружения нет → CallJournal отдаёт action="off"/may_call=True и движок работает как раньше
# (движки запускают руками из консоли, без кабинета).
try:
    from call_journal import CallJournal
except Exception:
    CallJournal = None

# Исправление кодировки для Windows
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        import io
        if hasattr(sys.stdout, 'buffer'):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'buffer'):
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Импорт локализации
try:
    from i18n import _
except ImportError:
    # Если модуль не найден, используем функцию-заглушку
    def _(key, *args, **kwargs):
        return key

# Импорт системы логирования
try:
    from logger_config import print_user, print_debug, print_progress, print_error_user_friendly
except ImportError:
    # Если модуль не найден, используем функции-заглушки
    def print_user(msg, msg_type="INFO"):
        emoji_map = {"INFO": "ℹ️", "SUCCESS": "✅", "WARNING": "⚠️", "ERROR": "❌", "PROCESS": "⏳"}
        emoji = emoji_map.get(msg_type.upper(), "ℹ️")
        print(f"{emoji} {msg}", flush=True)
    def print_debug(msg):
        pass
    def print_progress(current, total, item_name="элементов", show_bar=True):
        percentage = int((current / total) * 100) if total > 0 else 0
        if show_bar and total > 0:
            bar_length = 20
            filled = int(bar_length * current / total)
            bar = "█" * filled + "░" * (bar_length - filled)
            print_user(f"Обработка {item_name}: [{bar}] {current}/{total} ({percentage}%)", "PROCESS")
        else:
            print_user(f"Обработка {item_name}: {current} из {total} ({percentage}%)", "PROCESS")
    def print_error_user_friendly(error_msg, show_debug_hint=True):
        print_user(error_msg, "ERROR")


def print_eta(done: int, total: int, start_time: float, item_name: str = "элементов"):
    """Грубая оценка остатка времени батча (только при done>0 и есть незавершённые).
    Дополнительный вывод поверх print_progress — оператор видит сколько ещё ждать."""
    try:
        if done > 0 and total > done:
            elapsed = time.time() - start_time
            remaining = int(elapsed / done * (total - done))
            print_user(f"~{remaining}с осталось ({item_name} {done}/{total})", "PROCESS")
    except Exception:
        pass


# ============ API КЛЮЧИ ============
# Приоритет загрузки ключей:
# 1. Переменная окружения (устанавливается через APIKeysManager в app_main.py)
# 2. Файл конфигурации video_studio_api_keys.json
# 3. Значение по умолчанию (новый ключ)

# Сначала пытаемся загрузить из файла конфигурации
api_keys_config_path = os.path.join(os.path.expanduser("~"), "video_studio_api_keys.json")
GOOGLER_API_KEY = None
OPENAI_API_KEY = None
VEO_API_KEY = None  # VeoNonStop (veononstop.org) — отдельный провайдер видео
_SERVER_CREDENTIAL_ISOLATION = (
    os.environ.get("VS_SERVER_CREDENTIAL_ISOLATION", "").strip().lower()
    in ("1", "true", "yes", "on")
)

if not _SERVER_CREDENTIAL_ISOLATION and os.path.exists(api_keys_config_path):
    try:
        with open(api_keys_config_path, 'r', encoding='utf-8', errors='replace') as f:
            api_keys_config = json.load(f)
            if 'googler' in api_keys_config and api_keys_config['googler']:
                GOOGLER_API_KEY = api_keys_config['googler']
                print_debug(f"API ключ Googler загружен из конфигурационного файла")
            if 'openai' in api_keys_config and api_keys_config['openai']:
                OPENAI_API_KEY = api_keys_config['openai']
                print_debug(f"API ключ OpenAI загружен из конфигурационного файла")
            if 'veo' in api_keys_config and api_keys_config['veo']:
                VEO_API_KEY = api_keys_config['veo']
                print_debug(f"API ключ Veo (NonStop) загружен из конфигурационного файла")
    except Exception as e:
        print_debug(f"Не удалось загрузить API ключи из конфигурационного файла: {e}")

# Переменная окружения имеет приоритет над файлом конфигурации
env_googler_key = os.environ.get('GOOGLER_API_KEY')
if env_googler_key:
    GOOGLER_API_KEY = env_googler_key
    print_debug(f"API ключ Googler загружен из переменной окружения")

env_veo_key = os.environ.get('VEO_API_KEY')
if env_veo_key:
    VEO_API_KEY = env_veo_key
    print_debug(f"API ключ Veo загружен из переменной окружения")

env_openai_key = os.environ.get('OPENAI_API_KEY')
if env_openai_key:
    OPENAI_API_KEY = env_openai_key
    print_debug(f"API ключ OpenAI загружен из переменной окружения")

# Хардкод-ключи удалены 2026-06-05: openai-fallback был отозван (401), googler-fallback
# мёртв, и хранить секреты в исходнике небезопасно. Ключи берём ТОЛЬКО из
# ~/video_studio_api_keys.json + env. Если ключа нет — переменная остаётся None, и
# срабатывает loud-fail (_assert_openai_key / _assert_googler_key) ДО любого сетевого
# расхода, вместо тихой деградации «предложение=промпт» и жжёных recrafter-кредитов.


EX_TEMPFAIL = getattr(os, "EX_TEMPFAIL", 75)
# Контракт кодов выхода для кабинета. Смысл в том, что отказ плана бывает ДВУХ пород:
# «этот текст вообще не превращается в план» и «этот текст не превращается в план ИМЕННО
# с этой галочкой проекта». Вторая порода лечится сама: у плана того же текста без
# «разбора персонажей и мест» (analyze_story) или без «дробления длинных предложений»
# (split_long_sentences) таких проверок нет вовсе. Отдельный код выхода — единственный
# способ сказать об этом кабинету, не заставляя его разбирать текст ошибки.
#   11 — канон разбора персонажей и мест отклонил план (story_bible);
#   12 — смысловая нарезка длинных предложений не сошлась после всех повторов.
# Любой другой отказ плана остаётся кодом 1: кабинет ничего выключать не должен.
EX_PLAN_CANON_CONFLICT = 11
EX_PLAN_SEMANTIC_EXHAUSTED = 12


class ScenePlanError(RuntimeError):
    """Базовая ошибка построения плана сцен: план не публикуется."""


class ScenePlanTransientError(ScenePlanError):
    """Временный сбой транспорта/провайдера; внешний scheduler может повторить job."""


class ScenePlanTerminalError(ScenePlanError):
    """Ответ отклонён или нарушил схему; повтор без изменения входа не поможет."""


class ScenePlanSchemaError(ScenePlanTerminalError):
    """Модель вернула malformed JSON/scene schema; допустим точечный corrective retry."""


class ScenePlanCanonConflictError(ScenePlanTerminalError):
    """План отклонён каноном разбора персонажей и мест (story_bible).

    Одно имя у двух сущностей, каноническое место, не отмеченное в сцене, или entities
    сцены вне канона. Всё это порождено разбором персонажей: у плана того же текста без
    разбора таких проверок нет. Сами проверки остаются fail-closed — план с неоднозначным
    каноном публиковать нельзя. Отдельный класс нужен только затем, чтобы кабинет мог
    ЯВНО перезапустить промпты с выключенным разбором и записать это в проект, а не
    молча снять этап картинок.
    """


class ScenePlanCanonSchemaError(ScenePlanSchemaError, ScenePlanCanonConflictError):
    """Канонический отказ, который ещё можно исправить точечным повтором одной сцены.

    Наследует обе природы намеренно: повторы идут как у любой schema-ошибки, а если они
    не помогли — код выхода всё равно канонический, и кабинет узнаёт настоящую причину.
    """


class ScenePlanSemanticExhaustedError(ScenePlanSchemaError):
    """Смысловая нарезка (split_long, semantic v4) не сошлась после всех повторов блока.

    Кабинет может ЯВНО перезапустить промпты без дробления длинных предложений.
    """


class OpenAIKeyError(Exception):
    """OpenAI-ключ отсутствует/пуст ИЛИ невалиден (401/403/invalid_api_key).
    Фатально для prompt-генерации — НЕ деградируем, останавливаемся."""
    pass


class GooglerKeyError(Exception):
    """Googler/recrafter-ключ отсутствует. Фатально для генерации/трансформации картинок."""
    pass


_OPENAI_KEY_HINT = ("Впиши рабочий ключ в ~/video_studio_api_keys.json['openai'] "
                    "(или env OPENAI_API_KEY). Останавливаюсь, чтобы не генерить картинки "
                    "по деградированным промптам.")


def _prompt_provider() -> str:
    """Канал написания промптов: 'openai' (дефолт) / 'codex' (подписка ChatGPT) / 'claude' (подписка Claude)."""
    if prompt_llm is None:
        return "openai"
    try:
        return prompt_llm.provider()
    except Exception:
        return "openai"


def _has_openai_key() -> bool:
    return bool(OPENAI_API_KEY and str(OPENAI_API_KEY).strip())


def _chat_key() -> str:
    """Чем авторизоваться в чате: своим ключом, если адрес переставлен, иначе OpenAI.

    Отказ при переставленном адресе без своего ключа — намеренный и до запроса: молча
    подставить сюда OPENAI_API_KEY значит отдать ключ OpenAI постороннему хосту.
    """
    if GPT_CHAT_URL_OVERRIDDEN:
        if not GPT_CHAT_KEY:
            raise OpenAIKeyError(
                "адрес чата переставлен (VS_GPT_CHAT_URL), а ключ для него не задан "
                "(VS_GPT_CHAT_KEY). Останавливаюсь: ключ OpenAI чужому адресу не отдаю.")
        return GPT_CHAT_KEY
    return str(OPENAI_API_KEY or "")


def _openai_chat_headers() -> dict:
    return {"Authorization": f"Bearer {_chat_key()}", "Content-Type": "application/json"}


def _has_prompt_channel() -> bool:
    """Есть ли чем писать промпты: ключ OpenAI (на API-канале) или выбранная подписка."""
    if _prompt_provider() != "openai":
        return True
    return _has_openai_key()


def _assert_openai_key():
    """Loud-fail если канал промптов не готов (до любых запросов и расхода recrafter).
    На подписках OpenAI-ключ не нужен — проверяем авторизацию выбранной подписки."""
    if _prompt_provider() != "openai":
        ok, msg = prompt_llm.preflight()
        if ok:
            return
        # основной канал не готов — это не приговор, если есть чем его заменить (фолбэк)
        if prompt_llm.fallback_enabled() and _has_openai_key():
            print_user(f"⚠️ {msg} Промпты пойдут запасным каналом (OpenAI API).", "WARNING")
            return
        raise OpenAIKeyError(msg + " Останавливаюсь, чтобы не генерить картинки по деградированным промптам.")
    if not OPENAI_API_KEY or not str(OPENAI_API_KEY).strip():
        raise OpenAIKeyError("OpenAI-ключ отсутствует/пуст — генерация промптов недоступна. "
                             + _OPENAI_KEY_HINT)


def _raise_if_openai_auth_failed(response):
    """401/403/invalid_api_key = ФАТАЛЬНО (мёртвый/неверный ключ), а не транзиент-деградация.
    429 (rate limit) и 5xx (transient) сюда НЕ попадают — их обрабатывает обычная retry-логика."""
    try:
        body = response.text[:300]
    except Exception:
        body = ""
    bl = body.lower()
    if _prompt_provider() != "openai":
        # Подписка: 401 = нет/протух токен подписки. Тоже фатально (иначе 20 ретраев впустую
        # и деградация в мусорные промпты), но подсказка — про подписку, а не про ключ OpenAI.
        if response.status_code in (401, 403):
            raise OpenAIKeyError(f"Подписка не авторизована: {body[:220]} — генерация промптов недоступна.")
        return
    if response.status_code in (401, 403) or "invalid_api_key" in bl or "incorrect api key" in bl:
        raise OpenAIKeyError(f"OpenAI-ключ невалиден (HTTP {response.status_code}): {body[:160]} — "
                             "генерация промптов недоступна. " + _OPENAI_KEY_HINT)


# ── Исчерпан счёт/лимит канала промптов ≠ обычный троттл ─────────────────────
# Инцидент 04.08.2026: у серверного OpenAI-ключа кончились кредиты
# (429 `insufficient_quota` / `credit_balance_exhausted`), а ретрай-петля сцен считала это
# штатным rate limit и крутила SCENE_MAX_RETRIES=20 попыток на КАЖДУЮ сцену: 4 задачи по
# 329 частей за 2 часа дали 21 279 строк лога, 10 564 из них «Rate limit GPT», и НОЛЬ сцен —
# дальше был бы провал в аварийные промпты. Сервер об этом не узнавал: метку «канал мёртв»
# он ставит по строке из prompt_llm._mark_dead, а прямой openai-путь её не печатает.
#
# Признаки берём ТОЛЬКО однозначные и ТОЛЬКО вместе с HTTP 429 (та же дисциплина, что у
# _is_account_limit_response для картинок): родового «rate limit» тут быть не должно —
# секундный троттл не должен гасить прогон (грабли 30.07, вторая итерация).
_LLM_EXHAUSTED_PATTERNS = (
    "insufficient_quota",            # OpenAI: кончились кредиты/квота
    "credit_balance_exhausted",
    "no credits remaining",
    "exceeded your current quota",
    "quota exceeded",
    "billing_not_active",
    "billing hard limit",
    "usage limit",                   # подписки: «You've hit your usage limit…»
    "кончился лимит аккаунта",
)
# Подтверждение: первое срабатывание только предупреждаем (вдруг разовая формулировка),
# фатальным делаем ВТОРОЕ — метка на весь прогон, снимается любым успешным чат-ответом.
_LLM_EXHAUSTED_LOCK = threading.Lock()
_LLM_EXHAUSTED = {"hits": 0, "why": "", "channel": "", "announced": False}


def _llm_exhausted_reason():
    with _LLM_EXHAUSTED_LOCK:
        return _LLM_EXHAUSTED["why"] if _LLM_EXHAUSTED["hits"] >= 2 else ""


def _note_llm_chat_ok():
    """Успешный чат-ответ снимает заявку: транзиентная формулировка не копится за прогон."""
    with _LLM_EXHAUSTED_LOCK:
        if _LLM_EXHAUSTED["hits"] and not _LLM_EXHAUSTED["announced"]:
            _LLM_EXHAUSTED["hits"] = 0
            _LLM_EXHAUSTED["why"] = ""


def _raise_if_llm_credits_exhausted(response):
    """429 + однозначная подпись исчерпания счёта/лимита = ФАТАЛЬНО (после подтверждения).

    Обычный 429 (троттл) сюда не попадает и ретраится как раньше. При подтверждении печатаем
    строку, по которой СЕРВЕР помечает канал нерабочим (`_PROMPT_DEAD_RE` в server/main.py),
    иначе очередь продолжит слать задачи в канал, которому нечем платить.
    """
    if getattr(response, "status_code", 0) != 429:
        return
    try:
        body = (response.text or "")[:400]
    except Exception:
        body = ""
    bl = body.lower()
    if not any(p in bl for p in _LLM_EXHAUSTED_PATTERNS):
        return
    chan = _prompt_provider()
    with _LLM_EXHAUSTED_LOCK:
        _LLM_EXHAUSTED["hits"] += 1
        _LLM_EXHAUSTED["why"] = body.strip()
        _LLM_EXHAUSTED["channel"] = chan
        hits, announced = _LLM_EXHAUSTED["hits"], _LLM_EXHAUSTED["announced"]
        if hits >= 2:
            _LLM_EXHAUSTED["announced"] = True
    if hits < 2:
        print_user(f"⚠️ Похоже, у канала промптов «{chan}» исчерпан счёт/лимит: {body[:140]} — проверяю ещё раз",
                   "WARNING")
        return
    if not announced:
        # ровно эту фразу сервер ловит регуляркой и помечает канал нерабочим
        print_user(f"⚠️ Канал промптов «{chan}» недоступен (исчерпан счёт/лимит: {body[:120]})", "ERROR")
    raise OpenAIKeyError(
        f"У канала промптов «{chan}» исчерпан счёт/лимит (HTTP 429): {body[:200]} — "
        "останавливаюсь, чтобы не крутить ретраи впустую и не генерить по деградированным "
        "промптам. Пополни счёт или выбери другой канал (Ключи → Чем писать промпты)."
    )


def _assert_googler_key():
    """Loud-fail если Googler/recrafter-ключ отсутствует (вместо None→молчаливый 401)."""
    if not GOOGLER_API_KEY or not str(GOOGLER_API_KEY).strip():
        raise GooglerKeyError("Googler/recrafter-ключ отсутствует — генерация/трансформация "
                              "картинок недоступна. Впиши ключ в "
                              "~/video_studio_api_keys.json['googler'] (или env GOOGLER_API_KEY). "
                              "Останавливаюсь.")

# Проверяем что ключ не старый
if GOOGLER_API_KEY == os.environ.get("GOOGLER_EXPIRED_KEY_MARKER", ""):
    print_user("⚠️ ВНИМАНИЕ: Обнаружен старый истекший API ключ Googler!", "WARNING")
    print_user("   Установите новый ключ через настройки в приложении (Настройки → API ключи)", "WARNING")
    print_user("   Новый ключ будет сохранен в файл конфигурации", "WARNING")

# ============ НАСТРОЙКИ ============
# API URLs
# 2026-06-12: API переехал app.recrafter.fun → googler.fast-gen.ai (старый хост = 502 Bad Gateway).
# Эндпоинты, auth (X-API-Key), контракт (operation_id-поллинг) идентичны — сменился только хост.
# 04.09.2026: хост вынесен в окружение. Значение по умолчанию прежнее, поведение
# в бою не меняется. Нужно это для песочницы: без подменяемого адреса единственный
# способ проверить путь к провайдеру — заплатить за настоящий вызов.
GOOGLER_HOST = (os.environ.get("VS_GOOGLER_BASE")
                or "https://googler.fast-gen.ai").rstrip("/")
GOOGLER_BASE_URL_V4 = f"{GOOGLER_HOST}/api/v4"
GOOGLER_BASE_URL_V3 = f"{GOOGLER_HOST}/api/v3"
GOOGLER_BASE_URL_V2 = f"{GOOGLER_HOST}/api/v2"
# 2026-06-27: провайдер мигрировал v4→v6 (старые per-provider /api/v4/*/image/* = 404). Новый
# унифицированный эндпоинт POST /api/v6/generations (model+prompt+inputs+seed) → поллинг
# GET /api/v6/generations/{id} → results[].download_url. Маппинг старых имён моделей → v6 id:
GOOGLER_BASE_URL_V6 = f"{GOOGLER_HOST}/api/v6"
IMAGE_MODEL_V6 = {
    "OPENAI": "openai-image", "FLOWER": "flower-image", "NARWHAL": "nano-banana-2",
    "NARWHAL_LITE": "nano-banana-2-lite",  # Nano Banana 2 Lite (flow, HARBOR_SEAL) — добавлена 2026-07-04
    "GEM_PIX_2": "nano-banana-pro", "IMAGEN_3_5": "nano-banana-pro",  # Imagen убран из v6 → ближайший flow
    "GROK": "grok-image", "GROK_SPEED": "grok-image",
}  # VEO_BANANA / GPT_IMAGE2 — отдельные провайдеры (VeoNonStop / OpenAI напрямую), НЕ через v6
# v6-модели, КОТОРЫЕ ПРИНИМАЮТ option 'seed' (allowlist, fail-safe). grok-image (28.06) и
# openai-image (29.06) отвергают seed → validation.invalid_request → 100% провал прогона.
# Денилист рос бы бесконечно и каждый раз ломал прогон; allowlist = seed шлём ТОЛЬКО туда, где
# он точно работает (flow/nano-banana — в прогонах seed не отвергали), всем прочим (openai/grok/
# flower) НЕ шлём. Новая/неизвестная модель по умолчанию seed не получает → не ломается.
V6_SEED_MODELS = {"nano-banana-2", "nano-banana-pro", "nano-banana-2-lite"}
VIDEO_METHOD_V6 = {"flower": "flower-video", "flow": "flow-video-fast", "grok": "grok-video"}  # veo (VeoNonStop) — отдельно
GOOGLER_BASE_URL_V1 = GOOGLER_BASE_URL_V2  # Для обратной совместимости
VEO_BASE_URL = ((os.environ.get("VS_VEO_BASE") or "https://veononstop.org").rstrip("/")
                + "/api/v1")  # VeoNonStop — отдельный провайдер видео (X-API-Key: veo_...)
# Адрес чата вынесен в окружение по тому же принципу, что VS_GOOGLER_BASE (04.09) и
# VS_VEO_BASE: адрес провайдера меняется без правки кода.
#
# Повод 02.10.2026. Этот адрес — НЕ канал промптов. Канал («Ключи → Чем писать промпты»)
# с 02.09 переведён на Codex и openai в нём запрещён вовсе. А вот предварительные
# анализы — visual_context и semantic_beats — политику канала не спрашивали никогда и
# постили прямо сюда своим ключом OpenAI. Счёт OpenAI пуст, поэтому прогон падал на
# первом же анализе («You have no credits remaining»), а за ним обрывался этап картинок
# («конвейер оборван»). Из интерфейса это читалось как поломка у нас, хотя канал
# промптов был настроен верно.
#
# Умолчание оставлено прежним: вернуться на OpenAI — одна переменная, а не откат кода.
OPENAI_API_URL = (os.environ.get("VS_GPT_CHAT_URL")
                  or "https://api.openai.com/v1/chat/completions")

# Модели
# Имя модели ОБЯЗАНО соответствовать адресу выше: у FastGen, например, модели
# `gpt-4.1-nano` не существует вовсе — живой вызов отвечает 400 «No supported model
# found», и это выглядит как поломка прогона, а не как неверная настройка. Поэтому
# адрес и модель переставляются вместе.
GPT_MODEL = os.environ.get("VS_GPT_MODEL") or "gpt-4.1-nano"
# Ключ чата отдельный от OPENAI_API_KEY — и это не удобство, а защита. Если адрес
# увести на чужого провайдера, а ключ оставить прежним, мы отправим ключ OpenAI
# постороннему хосту. Поэтому правило: СВОЙ адрес — СВОЙ ключ, иначе отказ до запроса.
# Ровно так же устроен рерайт (REWRITE_GPT_CHAT_URL + REWRITE_GPT_CHAT_KEY).
GPT_CHAT_KEY = (os.environ.get("VS_GPT_CHAT_KEY") or "").strip()
GPT_CHAT_URL_OVERRIDDEN = bool((os.environ.get("VS_GPT_CHAT_URL") or "").strip())
# Сильнее nano для задач, где важна НАДЁЖНОСТЬ (извлечение персонажей + тег entities на сцену):
# nano мусорит (пере/недо-тегает), mini == 4.1 по качеству тега, но дешевле (~$0.17/ролик). Output мал → дёшево.
SCENE_GPT_MODEL = os.environ.get("VS_SCENE_GPT_MODEL") or "gpt-4.1-mini"
# 16.09.2026: живой GET https://api.openai.com/v1/models показал, что модели "gpt-image-2.5"
# не существует — есть "gpt-image-2.5-flare" и "gpt-image-2.5-sunburst"; владелец выбрал
# sunburst. Cover Studio с 17.09 шлёт ту же модель (cover/cover_core.py, GPT_IMAGE_MODEL). Вынесено в окружение по тому же принципу, что и адрес
# FastGen в VS_GOOGLER_BASE (04.09): имя модели у провайдера меняется без правки кода.
GPT_IMAGE_MODEL = os.environ.get("VS_GPT_IMAGE_MODEL") or "gpt-image-2.5-sunburst"

# --- Модели для генерации изображений ---
# Формат: "backend_model" → описание (API route)
GOOGLER_MODEL = "OPENAI"  # Дефолт (1 кредит). Может быть переопределена через --model
# Полный список моделей FastGen (источник: https://googler.fast-gen.ai/openapi.json, v1.0.0)
# ID сохраняем как есть (требуется API), labels — человекочитаемые для UI.
# Проверено live 2026-06-05 (см. research/IMAGE_GEN_TEST_2026-06-05.md):
#   GEMINI убран — провайдера НЕТ, /v4/gemini/image/generate = 404.
#   GEM_PIX_2/IMAGEN_3_5 ВОЗВРАЩЕНЫ — живы через /v4/flow/image/generate (generate-only, 4 кредита).
#   WHISK_IMAGEN4 не возвращён — whisk-провайдера нет.
GOOGLER_AVAILABLE_MODELS = [
    "OPENAI",         # ChatGPT image (v4/openai/image/generate) — 1 кредит, ДЕФОЛТ. gen + transform
    "FLOWER",         # Flower — Nano Banana 2 (v4/flower/image/generate) — 1 кредит. gen + transform
    "GROK",           # Grok quality (v4/grok/image/generate) — 3 кредита. generate-only
    "GROK_SPEED",     # Grok speed  (v4/grok/image/generate) — 1 кредит. generate-only
    "NARWHAL",        # Flow — Nano Banana 2 (v4/flow/image/generate model=NARWHAL) — 4 кр. generate-only
    "NARWHAL_LITE",   # Flow — Nano Banana 2 Lite (v6 nano-banana-2-lite, HARBOR_SEAL). generate-only
    "GEM_PIX_2",      # Flow — Nano Banana Pro (v4/flow/image/generate model=GEM_PIX_2) — 4 кр. generate-only
    "IMAGEN_3_5",     # Flow — Imagen 4 (v4/flow/image/generate model=IMAGEN_3_5) — 4 кр. generate-only
    "VEO_BANANA",     # VeoNonStop Nano Banana Pro (image/banana/generate, model_key=GEM_PIX_2 — дефолт API, ключ veo). generate + до 5 refs
    "VEO_BANANA_2",   # VeoNonStop Nano Banana 2 (тот же эндпоинт, model_key=NARWHAL). generate + refs
    "VEO_BANANA_LITE",# VeoNonStop Nano Banana 2 Lite (тот же эндпоинт, model_key=HARBOR_SEAL) — быстрее/дешевле. generate + refs
    "GPT_IMAGE2",     # OpenAI gpt-image-2.5-sunburst НАПРЯМУЮ (quality=low) — ПЛАТНО, цена не
                      # перепроверена под 2.5 (см. GPT_IMAGE_MODEL). generate-only
]
GOOGLER_MODEL_LABELS = {
    "OPENAI":         "ChatGPT Image",
    "FLOWER":         "Nano Banana 2 (Flower)",
    "GROK":           "Grok Quality",
    "GROK_SPEED":     "Grok Speed",
    "NARWHAL":        "Nano Banana 2 (Flow)",
    "NARWHAL_LITE":   "Nano Banana 2 Lite (Flow)",
    "GEM_PIX_2":      "Nano Banana Pro (4cr)",
    "IMAGEN_3_5":     "Imagen 4 (4cr)",
    "VEO_BANANA":     "Nano Banana Pro (Veo)",
    "VEO_BANANA_2":   "Nano Banana 2 (Veo)",
    "VEO_BANANA_LITE":"Nano Banana 2 Lite (Veo)",
    "GPT_IMAGE2":     "GPT Image 2.5 low (💲платно)",
}
# Кредиты FastGen/recrafter за ОДНУ генерацию — машиночитаемый дубль комментариев выше
# (GOOGLER_AVAILABLE_MODELS). Это ИСТОЧНИК ИСТИНЫ для взвешенного бюджета серверного ключа
# (server/main.py::_FASTGEN_IMAGE_WEIGHTS сверяется с ним тестом). Только recrafter-модели:
# VEO_BANANA* идут по ключу veo, GPT_IMAGE2 — напрямую в OpenAI за деньги.
# NARWHAL_LITE: провайдер цену lite-варианта не публикует → держим консервативные 4.
GOOGLER_MODEL_CREDITS = {
    "OPENAI":       1,
    "FLOWER":       1,
    "GROK":         3,
    "GROK_SPEED":   1,
    "NARWHAL":      4,
    "NARWHAL_LITE": 4,
    "GEM_PIX_2":    4,
    "IMAGEN_3_5":   4,
}
# VeoNonStop banana варианты: ОДИН эндпоинт image/banana/generate + разный model_key.
# HARBOR_SEAL=Banana 2 Lite (быстрее/дешевле), NARWHAL=Banana 2, GEM_PIX_2=Banana Pro (дефолт API).
VEO_BANANA_MODEL_KEYS = {
    "VEO_BANANA":      "GEM_PIX_2",    # Pro (= дефолт API, шлём явно)
    "VEO_BANANA_2":    "NARWHAL",      # Banana 2
    "VEO_BANANA_LITE": "HARBOR_SEAL",  # Banana 2 Lite
}
VEO_BANANA_MODELS = set(VEO_BANANA_MODEL_KEYS)


def _assert_image_generation_key(model: Optional[str] = None) -> None:
    """Loud preflight именно generation credential выбранной image-модели.

    GPT_IMAGE2 использует прямой OpenAI Images API, поэтому здесь намеренно
    нельзя вызывать `_assert_openai_key`: тот проверяет prompt-channel и может
    принять Claude/Codex-подписку без прямого OPENAI_API_KEY.
    """
    selected = str(model or GOOGLER_MODEL or "OPENAI").strip().upper()
    if selected in VEO_BANANA_MODELS:
        if not VEO_API_KEY or not str(VEO_API_KEY).strip():
            raise GooglerKeyError(
                f"{selected}: прямой Veo API key отсутствует — задай env "
                "VEO_API_KEY или server credential 'veo'. Останавливаюсь."
            )
        return
    if selected == "GPT_IMAGE2":
        if not OPENAI_API_KEY or not str(OPENAI_API_KEY).strip():
            raise OpenAIKeyError(
                "GPT_IMAGE2: прямой OpenAI API key отсутствует — Claude/Codex "
                "prompt-channel его не заменяет. Задай env OPENAI_API_KEY или "
                "server credential 'openai'. Останавливаюсь."
            )
        return
    _assert_googler_key()
# Модели для image2image (transform). Остальные — generate-only: при выборе их для
# transform воркер автоматически делает откат на OPENAI (см. transform_image).
# GROK/GROK_SPEED edit блокируется анти-ботом (см. research) → не transform-capable.
TRANSFORM_CAPABLE_MODELS = {"OPENAI", "FLOWER"}  # для отдельной команды transform (остальные → откат на OPENAI)
# Flow-модели роутятся на один и тот же /v4/flow/image/generate с параметром model=<ID>.
FLOW_GENERATE_MODELS = {"NARWHAL", "GEM_PIX_2", "IMAGEN_3_5"}
# Модели, реально принимающие reference_images В ОБЫЧНОЙ ГЕНЕРАЦИИ (generate_image per-model routing):
# OPENAI/FLOWER — reference_image(s); GROK — grok/image/edit (img2img); Flow (NARWHAL/GEM_PIX_2/IMAGEN_3_5) — reference_images;
# VEO_BANANA — reference_images (до 5). НЕ принимает только GPT_IMAGE2 (прямой OpenAI, без рефов).
REF_CAPABLE_MODELS = {
    model for model in GOOGLER_AVAILABLE_MODELS
    if ref_contract.reference_capacity(model) > 0
}

# Ракурсы камеры для разнообразия визуального ряда
CAMERA_ANGLES = [
    "Extreme Close-up on eyes/hands",
    "Wide Shot of landscape",
    "Over-the-shoulder view",
    "Low Angle looking up",
    "Top-down view",
    "Dutch Angle (tilted)",
    "Macro shot of an object",
    "Medium shot from waist up",
    "Bird's eye view",
    "Worm's eye view",
    "Side profile view",
    "Tracking shot following movement"
]

# Папки для сохранения
GOOGLER_IMAGES_DIR = "googler"
GOOGLER_VIDEO_DIR = "googler video"

# Параллельность
GPT_PARALLEL_THREADS = 100  # Увеличено для ускорения генерации промптов
GOOGLER_IMAGE_THREADS = 10  # Лимит API (25), используем 20 чтобы оставить запас для повторных попыток
GOOGLER_VIDEO_THREADS = 10   # Доступно 10 потоков на видео-job (полный лимит). Долбёж был не отсюда,
                             # а от max_parallel_video_projects=10 (10 проектов × 10 = 100) — теперь проекты=1.
GPT_SCENE_PARALLEL_THREADS = int(os.environ.get("VS_SCENE_THREADS") or 20)  # env-оверрайд: кабинет поднимает пул


def _scene_batch_size() -> int:
    """Сколько сцен просить за ОДИН запрос. Число потоков не меняется — меняется размер
    единицы работы, поэтому этап ускоряется во столько же раз, во сколько меньше запросов.

    Смысл только на ПОДПИСКАХ (codex/claude): там платится каждый вызов, и замер 06.08 на
    боевом проекте дал 49% стоимости и треть времени при пакете 6 (12 сегментов, claude).
    На обычном OpenAI-ключе оставляем поштучно — там префикс дешёвый и кэшируется провайдером,
    а одиночные вызовы дают более длинные, детальные промпты (370 симв. против 274).
    """
    raw = os.environ.get("VS_SCENE_BATCH")
    if raw:
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return 6 if _prompt_provider() in ("codex", "claude") else 1
# под подписки (там вызов = отдельный CLI-процесс, сеть не узкое место). Замер 28.07 на 6-ядерном VPS:
# 40 параллельных → codex 0.66 c/сцена (RSS 6.3 GB), claude 1.25 c/сцена (RSS 10 GB). Потолок — RAM, не API.
# Параллельная генерация сцен по частям (запросы независимы — на качество НЕ влияет;
                                  # одинаковый кэшируемый system-префикс снимает дублирование input-токенов).
                                  # Поставлено 20 по запросу (мягче по rate-limit нового ключа).

# Таймауты и ретраи
REQUEST_TIMEOUT = 200
# 03.09.2026: у провайдера СВОЯ очередь. Пока наш запрос стоит в ней (status=queued),
# таймер уже тикает — и кадры, которые просто ждали свободного слота, снимались по
# таймауту («Таймаут v6 операции», «⏱️ veo … таймаут ожидания»). Потолки подняты ×10
# по запросу владельца; env оставлен, чтобы крутить их без правки кода.
VIDEO_POLL_TIMEOUT = int(os.environ.get("VS_VIDEO_POLL_TIMEOUT", "3000") or 3000)  # было 300
IMAGE_POLL_TIMEOUT = int(os.environ.get("VS_IMAGE_POLL_TIMEOUT", "10000") or 10000)  # было 1000
VIDEO_POLL_INTERVAL = 5   # Интервал опроса статуса
# Сколько секунд подряд провайдер должен отвечать «такого заказа нет» (404), чтобы опрос
# перестал ждать. 04.10.2026: шесть заказов, потерянных провайдером, держали оба места
# картинок с 12:25 — опрос считал 404 «статус неизвестен» и ждал полный таймаут кадра
# (10 000 с), а каждый перезапуск студии начинал этот отсчёт заново. Пауза, а не первый
# же ответ: только что созданный заказ на мгновение может быть ещё не виден.
V6_LOST_GRACE_SEC = int(os.environ.get("VS_V6_LOST_GRACE_SEC", "120") or 120)
# VeoNonStop: провайдер предупредил «сильно медленнее» (рекапча стороннего вендора),
# их таск-таймаут 30 мин. Даём 20 мин на задачу, опрос каждые 10с (по их рекомендации).
VEO_POLL_TIMEOUT = int(os.environ.get("VS_VEO_POLL_TIMEOUT", "12000") or 12000)  # было 1200
VEO_POLL_INTERVAL = 10
# banana (image/banana/generate) — СИНХРОННЫЙ, без task_id-поллинга: POST держится всю генерацию.
# Латентность плавает (5–53с, иногда >240с). Таймаут = брошенная генерация = лишний кредит
# (на сервере могла уже завершиться) → ставим щедро. Тредами медленный кадр батч не блокирует.
VEO_BANANA_TIMEOUT = int(os.environ.get("VS_VEO_BANANA_TIMEOUT", "1800") or 1800)  # 30 мин
# (было 600; синхронный POST дольше держать бессмысленно — рвут прокси/CDN, а не наш таймер)
VEO_BANANA_RETRY_DELAY = 3  # пауза между ретраями картинки banana: плоско 3с (не 2/4/6/8). Капча на стороне VeoNonStop — наш backoff её не лечит, ждать смысла нет.
# Veo-задачи часто падают на reCAPTCHA (403 UNUSUAL_ACTIVITY) — провайдер предупредил.
# Больше попыток + ДЛИННЫЙ backoff (анти-бот должен «остыть» между попытками).
VEO_MAX_ATTEMPTS = 20     # ↑ с 5 (по запросу): рекапча флакает → больше попыток = выше шанс пробиться

# ── Фатальные ошибки генерации (контент-фильтры провайдера): детерминированы — тот же вход даст
# тот же отказ. Ретраить их 20 раз бессмысленно: жжёт время/кредиты и ДЕРЖИТ ОЧЕРЕДЬ (один
# клип-отказник = паузы до 63 мин суммарно → следующий проект ждёт). Поллеры записывают текст
# последней ошибки в thread-local, ретрай-циклы дают фатальным максимум 2 попытки (04.07).
_GEN_LAST_ERROR = threading.local()

def _set_last_gen_error(msg) -> None:
    _GEN_LAST_ERROR.msg = str(msg or "")

def _last_gen_error() -> str:
    return getattr(_GEN_LAST_ERROR, "msg", "")


# ── Настоящая причина отказа провайдера (HTTP-код и суть) ────────────────────
# Отдельно от _GEN_LAST_ERROR НАМЕРЕННО. _GEN_LAST_ERROR — вход ретрай-логики: по нему
# решают, фатален ли отказ, транзиентен ли, часовая ли это квота. Дописывать туда ещё и
# отказы транспорта значит менять решения о повторах. А здесь нужна только правда для
# человека и для журнала вызовов: что именно ответил провайдер и каким кодом.
# Зачем вообще: до этой правки отказ POST /api/v6/generations доезжал до кабинета
# обёрткой «<модель>: v6 генерация не удалась», и по ней причину отказа установить
# нельзя ВООБЩЕ — ни код, ни текст провайдера в неё не входят.
_GEN_PROVIDER_REFUSAL = threading.local()


# Отказы, которые провайдер объясняет машинно, а человеку — никак. Сырой JSON в карточке
# задачи не отвечает ни на один из трёх вопросов («что упало, потрачены ли деньги, какую
# кнопку жать»), поэтому знакомые причины переводим на человеческий вместе с лекарством.
#
# 21.09.2026, повод: владелец выбрал метод клипов «Flow» и получил
# «HTTP 403 {"error": "Video generation not allowed for this license", "code":
# "auth.forbidden"}». Это правда: у лицензии FastGen `video.generate = false`, а квоты
# video.* нулевые — проверяется бесплатно через /api/v6/usage. Но по такому сообщению
# нельзя догадаться, что рядом есть рабочий метод на ДРУГОМ провайдере и другом ключе.
_KNOWN_REFUSALS = (
    (("video generation not allowed", "auth.forbidden"),
     "лицензия FastGen не включает генерацию видео — этим методом клипы сделать нельзя. "
     "Выберите метод «Veo 3.1 — по умолчанию»: он идёт через VeoNonStop и отдельный ключ. "
     "Денег это не стоило, повтор не поможет."),
)


def _humanize_refusal(msg: str) -> str:
    """Знакомый отказ провайдера → что это значит и что делать. Незнакомый — как есть."""
    low = str(msg or "").lower()
    for needles, human in _KNOWN_REFUSALS:
        if all(n in low for n in needles):
            return human
    return str(msg or "")


def _set_provider_refusal(msg) -> None:
    _GEN_PROVIDER_REFUSAL.msg = " ".join(_humanize_refusal(msg).split())[:300]


def _provider_refusal() -> str:
    return getattr(_GEN_PROVIDER_REFUSAL, "msg", "")


def _real_gen_reason() -> str:
    """Причина отказа словами провайдера. "" — провайдер ничего внятного не сказал."""
    return _provider_refusal() or _last_gen_error()


# Наши обёртки: текст есть, причины в нём нет. Такой текст уступает место ответу
# провайдера везде, где выбирают, что показать человеку и что записать в журнал.
_WRAPPER_GEN_PATTERNS = ("генерация не удалась", "кадр не получен", "клип не получен",
                         "результат пуст", "нет результата",
                         # видео-ветки: та же обёртка, только другими словами
                         "video не удалось", "video не удалась")


def _is_wrapper_gen_error(msg) -> bool:
    s = " ".join(str(msg or "").split()).lower()
    return (not s) or any(p in s for p in _WRAPPER_GEN_PATTERNS)


def _gen_failure_error(label: str, fallback: str = "v6 генерация не удалась") -> str:
    """Текст неудачной генерации: правда провайдера, обёртка — только если правды нет.

    Ровно эта строка печатается в «Попытка N неудачна» и «Все N попыток исчерпаны» и
    уезжает в кабинет. Пока в ней стояла одна обёртка, причину отказа по логу задачи
    установить было нельзя: ни HTTP-кода, ни слов провайдера в ней нет.

    `fallback` — прежняя обёртка ЭТОЙ ветки (у картинок и у видео она разная). Менять
    её нельзя: когда провайдер промолчал, человек должен увидеть ровно тот же текст,
    что и до правки, иначе мы поменяли смысл строки, не добавив ни одного факта.
    """
    why = _real_gen_reason()
    return f"{label}: {why}" if why else f"{label}: {fallback}"


_FATAL_GEN_PATTERNS = ("danger_filter", "safety filter", "safety_filter", "inputisimproper",
                       "policy violation", "prominent_people", "prominent people", "filter_failed",
                       "child", "celebrity", "timed_out", "generation_timed_out")
# veo t2v: *_FILTER_FAILED (напр. PROMINENT_PEOPLE) детерминирован; VIDEO_GENERATION_TIMED_OUT — сцена
# слишком сложная/проблемная, таймаутит одинаково каждый раз → не крутить 20× по ~6 мин (одна дырит батч
# на ~2ч). Смягчатель их упрощает/обезличивает → часто проходит; не прошло за 3 раунда — пропуск.
# NB: INTERNAL/cookie slots/No accounts этих подстрок НЕ содержат → остаются транзиентными (крутятся).
# NB: транзиентные (INTERNAL/cookie slots/No accounts) НЕ содержат этих подстрок → крутятся как надо.

def _is_fatal_gen_error(msg) -> bool:
    s = str(msg or "").lower()
    return any(p in s for p in _FATAL_GEN_PATTERNS)


# Инфраструктурные сбои провайдера: их лечит ретрай, а не переписывание промпта. Нужны для
# «смягчения последним шансом» — чтобы не жечь LLM-вызов и лишнюю генерацию, когда провайдер
# просто болеет. ⚠️ «try again later» сюда НЕ входит намеренно: именно так выглядит
# обезличенный отказ фильтра («Generation failed, please try again later»), ради которого
# последний шанс и делается.
_TRANSIENT_GEN_PATTERNS = ("internal", "cookie slot", "no accounts", "rate limit",
                           "too many requests", "timeout", "timed out", "connection",
                           "gateway", "unavailable", "502", "503", "504")
_TRANSIENT_HTTP_STATUS_RE = re.compile(r"\bhttp\s+(?:429|5\d{2})\b", re.IGNORECASE)


def _is_transient_gen_error(msg) -> bool:
    s = str(msg or "").lower()
    return bool(
        any(p in s for p in _TRANSIENT_GEN_PATTERNS)
        or _TRANSIENT_HTTP_STATUS_RE.search(s)
    )


# FastGen v6 occasionally terminates ``openai-image`` with only ``status=failed`` /
# ``Generation failed``.  That is neither an HTTP transient (the paid generation already
# reached a terminal state) nor a typed safety refusal.  Preserve that distinction across
# ``googler_v6_generate``'s Optional[str] boundary so generate_image can bound paid retries.
_V6_GENERIC_TERMINAL_MARKER = "v6_generic_terminal_failed"
_V6_GENERIC_TERMINAL_PATTERNS = (
    "generation failed",
    "status=failed",
    '"status": "failed"',
    '"status":"failed"',
)
_V6_GENERIC_TERMINAL_FRAME_LOCK = threading.Lock()
_V6_GENERIC_TERMINAL_FRAMES = set()

# Live A/B on job 10955: one raw exact OPENAI request succeeds in isolation, while three
# simultaneous operations all terminate generic-failed. Exact proxies are rare recovery
# prompts, so serialize only this branch. The file lock also covers two server worker
# processes; ordinary OPENAI frames and every other model keep their configured parallelism.
_OPENAI_EXACT_RAW_THREAD_LOCK = threading.Lock()
_OPENAI_EXACT_RAW_LOCK_PATH = os.environ.get(
    "VS_OPENAI_EXACT_RAW_LOCK_PATH",
    os.path.join(tempfile.gettempdir(), "videostudio-openai-exact-raw-v1.lock"),
)


def _run_openai_exact_raw_serialized(call):
    """Run one rare exact-raw OPENAI generation without an overlapping exact recovery."""
    with _OPENAI_EXACT_RAW_THREAD_LOCK:
        lock_fd = None
        try:
            if _fcntl is not None:
                flags = os.O_CREAT | os.O_RDWR
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                lock_fd = os.open(_OPENAI_EXACT_RAW_LOCK_PATH, flags, 0o600)
                try:
                    os.fchmod(lock_fd, 0o600)
                except OSError:
                    pass
                _fcntl.flock(lock_fd, _fcntl.LOCK_EX)
            return call()
        finally:
            if lock_fd is not None:
                try:
                    _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)


def _typed_v6_terminal_error(msg) -> str:
    """Tag only unclassified terminal v6 failures; keep safety/transient reasons intact."""
    raw = str(msg or "v6 generation failed")
    if (
        _is_fatal_gen_error(raw)
        or _is_transient_gen_error(raw)
        or _is_hourly_limit_error(raw)
    ):
        return raw
    return f"{_V6_GENERIC_TERMINAL_MARKER}: {raw}"


def _is_generic_v6_terminal_error(msg) -> bool:
    s = str(msg or "").lower()
    if _V6_GENERIC_TERMINAL_MARKER in s:
        return True
    return (
        any(pattern in s for pattern in _V6_GENERIC_TERMINAL_PATTERNS)
        and not _is_fatal_gen_error(s)
        and not _is_transient_gen_error(s)
        and not _is_hourly_limit_error(s)
    )


def _v6_generic_terminal_frame_key(model: str, prompt: str,
                                   output_path: Optional[str], index: int) -> tuple:
    """Stable frame identity inside one worker/job, including its original provider prompt."""
    locator = os.path.abspath(output_path) if output_path else f"index:{int(index)}"
    digest = hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()
    return str(model or "").upper(), locator, digest


def _v6_generic_terminal_frame_blocked(frame_key: Optional[tuple]) -> bool:
    if frame_key is None:
        return False
    with _V6_GENERIC_TERMINAL_FRAME_LOCK:
        return frame_key in _V6_GENERIC_TERMINAL_FRAMES


def _block_v6_generic_terminal_frame(frame_key: Optional[tuple]) -> None:
    if frame_key is None:
        return
    with _V6_GENERIC_TERMINAL_FRAME_LOCK:
        _V6_GENERIC_TERMINAL_FRAMES.add(frame_key)


# ── ИСЧЕРПАН ЛИМИТ АККАУНТА ПРОВАЙДЕРА (VeoNonStop) ──────────────────────────
# Инцидент 30.07: партнёр запустил 8 задач картинок на VEO_BANANA, провайдер начал отдавать
#   HTTP 429 {"success":false,"error":"RATE_LIMIT: Rate limit - кончился лимит аккаунта.
#             Смени модель или подожди 20-30 минут"}
# Тот же ответ приходил и на ОДИНОЧНЫЙ запрос тем же ключом → дело не в нашей параллельности,
# а в исчерпанном аккаунте. Воркер же продолжал долбить: 264 запроса и 2616 строк «Попытка N
# неудачна» за 12 минут, ноль картинок. Ретраи такое не лечат (восстановление 20-30 мин), но и
# «фатально навсегда» это не является — поэтому отдельная семантика:
#   * пара попыток с длинной паузой у ПЕРВОГО кадра, наткнувшегося на лимит;
#   * дальше признак общий на весь прогон → остальные кадры отказывают мгновенно, БЕЗ запроса;
#   * задача падает понятной ошибкой с текстом провайдера (а не «ничего не происходит»).
# Обычный троттл (429 без этого текста) остаётся транзиентным и ретраится как раньше.
ACCOUNT_LIMIT_MARKER = "ЛИМИТ АККАУНТА ПРОВАЙДЕРА"
# Признаки именно ИСЧЕРПАННОГО аккаунта в теле ответа VeoNonStop (проверяются ТОЛЬКО с HTTP 429).
# ⚠️ Родового «rate limit» / «rate_limit» здесь БЫТЬ НЕ ДОЛЖНО (правка 30.07, вторая итерация):
# это типовой текст обычного троттла и лимита конкурентности (у VeoNonStop 4/12/24 одновременных).
# Пока он был в списке, ОДИН транзиентный 429 помечал провайдера мёртвым на весь прогон:
# на 8 кадрах с 429 только на первом запросе получалось 2 запроса и 1 картинка вместо 8.
# Ловим формулировку исчерпания («кончился лимит аккаунта», «смени модель», «подожди 20-30 минут»).
# ── Часовая квота ключа FastGen ───────────────────────────────────────────────
# Ключ считает УСПЕШНЫЕ генерации в час (у ключа владельца 6000). Исчерпание отдаёт
# HTTP 429 + code=rate_limit.hourly_exceeded. Это НИ то, ни другое из уже известного:
# не «аккаунт кончился» (там ждать 20-30 мин и метка на прогон) и не транзиентный
# троттл — окно сбрасывается РОВНО на границе часа. Раньше такой отказ уходил в общий
# backoff-ретрай, и за ночь это дало 6027 отказов и 5448 пауз впустую.
# Теперь: первый поймавший ставит метку до сброса окна, остальные потоки ждут её, не
# отправляя запросов; ожидание не съедает попытку кадра.
_HOURLY_LIMIT_PATTERNS = ("rate_limit.hourly_exce", "requests per hour")
_HOURLY_LIMIT_LOCK = threading.Lock()
_HOURLY_LIMIT_UNTIL = 0.0
HOURLY_LIMIT_MAX_WAIT = int(os.environ.get("VS_HOURLY_LIMIT_MAX_WAIT", "3600"))


def _is_hourly_limit_error(msg) -> bool:
    s = str(msg or "").lower()
    return "429" in s and any(p in s for p in _HOURLY_LIMIT_PATTERNS)


def _note_hourly_limit() -> bool:
    """Метка до сброса часового окна. True — поставили мы (логируем один раз на окно)."""
    global _HOURLY_LIMIT_UNTIL
    now = time.time()
    with _HOURLY_LIMIT_LOCK:
        if _HOURLY_LIMIT_UNTIL > now:
            return False
        _HOURLY_LIMIT_UNTIL = now - (now % 3600) + 3600 + 5     # начало следующего часа + запас
        return True


def _hourly_limit_left() -> int:
    with _HOURLY_LIMIT_LOCK:
        return max(0, int(_HOURLY_LIMIT_UNTIL - time.time()))


_ACCOUNT_LIMIT_PATTERNS = (
    "кончился лимит аккаунта",
    "лимит аккаунта",
    "смени модель",
    "смените модель",
    "подожди 20-30",
    "подождите 20-30",
    "account limit reached",
    "account limit exceeded",
    "account limit exhausted",
    "quota exhausted",
)
ACCOUNT_LIMIT_ATTEMPTS = max(1, int(os.environ.get("VS_ACCOUNT_LIMIT_ATTEMPTS", "2") or 2))
ACCOUNT_LIMIT_RETRY_DELAY = max(0, int(os.environ.get("VS_ACCOUNT_LIMIT_RETRY_DELAY", "60") or 0))
# Сколько метка живёт. Провайдер обещает восстановление за 20-30 мин, и 30.07 боевой ключ
# действительно ожил сам через ~25 мин. Без TTL метка вечная — а на сервере generate_image
# зовётся внутри долгоживущего uvicorn (регенерация картинки из кабинета), и один пойманный
# лимит убивал бы генерацию ВСЕМ юзерам до рестарта. Аналог: VS_CHANNEL_DEAD_TTL у каналов промптов.
ACCOUNT_LIMIT_TTL = max(60, int(os.environ.get("VS_ACCOUNT_LIMIT_TTL", "1200") or 1200))


def _is_account_limit_response(status_code, body) -> bool:
    """VeoNonStop: HTTP 429 + текст «кончился лимит аккаунта»/«смени модель» = аккаунт исчерпан.

    Гейт по коду 429 обязателен, а вызывается детектор только из veo-ветки — у
    recrafter/FastGen формат ответа другой, их 429 по-прежнему обрабатывает обычная
    retry-логика (см. googler_request_v6). Родовой троттл («rate limit exceeded», «too many
    requests») исчерпанием НЕ считается и ретраится как раньше.
    """
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return False
    if code != 429:
        return False
    s = str(body or "").lower()
    return any(p in s for p in _ACCOUNT_LIMIT_PATTERNS)


def _account_limit_error(provider_label: str, body) -> str:
    """Ошибка с маркером — отличима от обычного сбоя и в логе, и в ошибке задачи."""
    txt = " ".join(str(body or "").split())[:200]
    return (f"{ACCOUNT_LIMIT_MARKER} ({provider_label}): кончился лимит аккаунта — "
            f"смените модель или подождите 20-30 минут. Ответ провайдера: {txt}")


def _is_account_limit_error(msg) -> bool:
    return ACCOUNT_LIMIT_MARKER.lower() in str(msg or "").lower()


# Признак «аккаунт лёг» — общий на прогон, но НЕ вечный:
#   _ACCOUNT_LIMIT_STATE — ПОДТВЕРЖДЁННАЯ метка (живёт ACCOUNT_LIMIT_TTL, снимается успехом);
#   _ACCOUNT_LIMIT_PROBE — заявка кадра-первооткрывателя на подтверждающую вторую попытку.
# Метка ставится ТОЛЬКО после того, как вторая попытка первооткрывателя снова упёрлась в лимит.
# Мигнувший на секунду троттл до метки не доходит: вторая попытка проходит → заявка снимается.
_ACCOUNT_LIMIT_STATE = {}          # provider -> {"msg": str, "at": float}
_ACCOUNT_LIMIT_PROBE = {}          # provider -> float (время заявки)
_ACCOUNT_LIMIT_LOCK = threading.Lock()


def _account_limit_probe_ttl() -> float:
    """Заявка протухает, если первооткрыватель умер, не подтвердив (иначе никто не проверит)."""
    return max(30.0, ACCOUNT_LIMIT_RETRY_DELAY * 2 + 60.0)


def _account_limit_live(provider: str, now=None) -> bool:
    """Метка жива? (вызывать под _ACCOUNT_LIMIT_LOCK) Протухшую по TTL сразу убираем."""
    st = _ACCOUNT_LIMIT_STATE.get(provider)
    if not st:
        return False
    if (now or time.time()) - float(st.get("at") or 0) >= ACCOUNT_LIMIT_TTL:
        _ACCOUNT_LIMIT_STATE.pop(provider, None)
        return False
    return True


def _claim_account_limit_probe(provider: str) -> bool:
    """Занять роль первооткрывателя (право на подтверждающую вторую попытку).

    True — ровно одному кадру. Лимит уже подтверждён или заявка занята другим → False
    (такой кадр не тратит вторую попытку в возможно исчерпанный аккаунт).
    """
    provider = str(provider or "").strip().lower()
    now = time.time()
    with _ACCOUNT_LIMIT_LOCK:
        if _account_limit_live(provider, now):
            return False
        claimed = float(_ACCOUNT_LIMIT_PROBE.get(provider) or 0)
        if claimed and now - claimed < _account_limit_probe_ttl():
            return False
        _ACCOUNT_LIMIT_PROBE[provider] = now
        return True


def _mark_account_limit(provider: str, msg: str) -> bool:
    """Подтвердить исчерпание: метка на прогон (в пределах TTL). True = отметка первая."""
    provider = str(provider or "").strip().lower()
    with _ACCOUNT_LIMIT_LOCK:
        first = not _account_limit_live(provider)
        _ACCOUNT_LIMIT_STATE[provider] = {"msg": str(msg or "")[:400], "at": time.time()}
        _ACCOUNT_LIMIT_PROBE.pop(provider, None)
    return first


def _account_limit_hit(provider: str) -> bool:
    """Есть ли ЖИВАЯ подтверждённая метка (протухшая по TTL не считается)."""
    provider = str(provider or "").strip().lower()
    with _ACCOUNT_LIMIT_LOCK:
        return _account_limit_live(provider)


def _clear_account_limit(provider: str = "") -> None:
    """Успешная генерация тем же провайдером = аккаунт жив → метку и заявку снимаем."""
    provider = str(provider or "").strip().lower()
    with _ACCOUNT_LIMIT_LOCK:
        if provider:
            _ACCOUNT_LIMIT_STATE.pop(provider, None)
            _ACCOUNT_LIMIT_PROBE.pop(provider, None)
        else:
            _ACCOUNT_LIMIT_STATE.clear()
            _ACCOUNT_LIMIT_PROBE.clear()


def _account_limit_msg(provider: str = "") -> str:
    provider = str(provider or "").strip().lower()
    with _ACCOUNT_LIMIT_LOCK:
        if provider:
            return (_ACCOUNT_LIMIT_STATE.get(provider) or {}).get("msg", "") if _account_limit_live(provider) else ""
        for _p in list(_ACCOUNT_LIMIT_STATE):
            if _account_limit_live(_p):
                return (_ACCOUNT_LIMIT_STATE.get(_p) or {}).get("msg", "")
    return ""


def _reset_account_limit() -> None:
    """Полный сброс состояния (тесты + начало серверного вызова в долгоживущем процессе)."""
    _clear_account_limit()


def _model_provider_group(model: str) -> str:
    """Группа аккаунта: у всех VEO_BANANA* один ключ veo → лимит общий."""
    return "veo" if str(model or "") in VEO_BANANA_MODELS else str(model or "")


class ProviderAccountLimitError(Exception):
    """Лимит аккаунта провайдера исчерпан — задача останавливается с понятной причиной."""
    pass


def _account_limit_raise_if_hit(model: str, produced: int = 0, total: int = 0) -> None:
    """Лимит зафиксирован и результат неполный → падаем с текстом провайдера.

    Полностью успешный прогон (produced >= total > 0) не роняем: значит лимит мигнул,
    а картинки всё равно получены.
    """
    group = _model_provider_group(model)
    if not _account_limit_hit(group):
        return
    if total and produced >= total:
        return
    msg = _account_limit_msg(group) or f"{ACCOUNT_LIMIT_MARKER}: кончился лимит аккаунта"
    if total:
        msg = f"{msg} (сгенерировано {produced} из {total})"
    raise ProviderAccountLimitError(msg)


# Сколько реальных замен сделал последний локальный пасс В ЭТОМ потоке. Нужен ступенчатому
# smooth-механизму (soften_prompt): пасс ВСЕГДА дописывает хвост-суффикс, поэтому «текст
# изменился» не равно «что-то нашли», и без счётчика GPT-ступень не наступала бы никогда.
_LOCAL_PASS_HITS = {}

# A later safety round replaces the prior round's instruction; it must not append a
# contradictory second subject count.  Keep exact terminal phrases here so ordinary scene
# prose is never stripped.  Longest/specific forms are tried first by the cleanup loop.
_SAFETY_RETRY_SUFFIXES = tuple(sorted((
    "calm archival staging, historical adult subjects",
    "calm archival staging, anonymous fictional adults",
    "one adult subject, one quiet action, calm uncluttered background",
    "symbolic historical record, historical adult subject, restrained documentary detail",
    "symbolic historical record, anonymous fictional adult, restrained documentary detail",
    "one clear focal element, one quiet action, calm uncluttered background",
    "symbolic historical record, restrained documentary detail",
    "calm archival staging",
), key=len, reverse=True))


def _soften_prompt_local(prompt: str, round_no: int = 1,
                         preserve_named_identity: bool = False) -> str:
    """Детерминированный safety-pass без второго provider/credential.

    Generation jobs имеют lease только своего image/video-провайдера, поэтому
    незапланированный OpenAI/Claude/Codex-вызов здесь нарушал бы честность пула.
    """
    out = str(prompt or "")
    # Живой прогон 04.08 (смягчатель отказался, работал только этот пасс) показал дыры:
    # «mutilated / dismembered / entrails / torture / extremely graphic» проходили насквозь,
    # а повторные замены давали кашу «Extremely graphic non-graphic aftermath …».
    # Поэтому: сначала вычищаем усилители графичности, потом заменяем сами термины,
    # в конце схлопываем повторы подставленных формулировок.
    out = re.sub(r"\b(extremely|highly|very)?\s*graphic\b(\s+(gore|violence|detail\w*))?",
                 " ", out, flags=re.IGNORECASE)
    replacements = {
        # Replacement text must itself be neutral.  Live retries used to put
        # filter vocabulary back into the next round (``non-graphic`` became
        # the broken fragment ``non- aftermath`` on round two).
        r"\b(gore|blood(?:y)?|bleeding|entrails|viscera|guts)\b": "indistinct archival traces",
        r"\b(mutilat\w+|dismember\w+|sever(?:ed)?|decapitat\w+|behead\w+)\b": "intact",
        # Формы слова обязаны ловиться во множественном числе: «corpses», «weapons»,
        # «children» проскакивали мимо \bcorpse\b и уезжали к провайдеру как есть
        # (найдено тестом 06.08 — ровно на этом падали кадры с полями боя).
        r"\b(corpses?|dead bod(?:y|ies)|cadavers?)\b": "covered archival markers",
        r"\b(tortur\w+|executions?|executed|massacres?|atrocit\w+)\b": "restrained archival evidence",
        r"\b(kill(?:s|ed|ings?)?|murder(?:s|ed|ings?)?|deaths?)\b": "restrained archival evidence",
        r"\b(guns?|rifles?|pistols?|weapons?|knives|knife|swords?)\b": "inactive period prop",
        r"\b(wound(?:ed|s)?|injur(?:y|ies|ed))\b": "subtle signs of fatigue",
        r"\b(nude|nudity|naked)\b": "fully clothed",
        r"\b(child|children|minors?|boys?|girls?|youths?)\b": "adult",
    }
    _hits = 0
    for pattern, replacement in replacements.items():
        out, _c = re.subn(pattern, replacement, out, flags=re.IGNORECASE)
        _hits += _c
    _LOCAL_PASS_HITS[threading.get_ident()] = _hits
    # Схлопываем подряд идущие/повторяющиеся подстановки: после замены нескольких жёстких
    # слов в одной фразе получалось «non-graphic aftermath, non-graphic aftermath …».
    for _phrase in set(replacements.values()):
        out = re.sub(r"(?:\b" + re.escape(_phrase) + r"\b)(\s*[, ]\s*(?:and\s+)?\b"
                     + re.escape(_phrase) + r"\b)+", _phrase, out, flags=re.IGNORECASE)
    # Prominent-person фильтр: убираем самые опасные identity-конструкции без
    # попытки угадать имена/биографию.
    if not preserve_named_identity:
        out = re.sub(
            r"\b(?:portrait of|looks? like|depicting)\s+[A-ZА-ЯЁ][\w'’.-]+"
            r"(?:\s+[A-ZА-ЯЁ][\w'’.-]+){0,3}",
            "portrait of an anonymous fictional adult",
            out,
        )
    # ``scene_prose`` is carried from round to round.  Remove every terminal suffix from
    # previous rounds before selecting the current one, otherwise round 1 plural adults,
    # round 2 one adult and round 3 another adult all survive in the same provider prompt.
    while True:
        removed = False
        for previous_suffix in _SAFETY_RETRY_SUFFIXES:
            updated, count = re.subn(
                r"(?:,\s*)?" + re.escape(previous_suffix) + r"\s*$",
                "", out, flags=re.IGNORECASE,
            )
            if count:
                out = updated.rstrip(" .,:;")
                removed = True
                break
        if not removed:
            break
    # No negated policy words here: providers still tokenize ``no violence``
    # and ``no disturbing detail`` as the same sensitive concepts.
    environment_only = any(marker in out.casefold() for marker in (
        "composition stays focused on the departing convoy",
        "fara persoane vizibile",
        "fără persoane vizibile",
        "without visible people",
        "no visible people",
    ))
    if environment_only:
        suffixes = {
            1: "calm archival staging",
            2: "one clear focal element, one quiet action, calm uncluttered background",
            3: "symbolic historical record, restrained documentary detail",
        }
    else:
        subject_plural = (
            "historical adult subjects"
            if preserve_named_identity else "anonymous fictional adults"
        )
        subject_single = (
            "historical adult subject"
            if preserve_named_identity else "anonymous fictional adult"
        )
        suffixes = {
            1: f"calm archival staging, {subject_plural}",
            2: "one adult subject, one quiet action, calm uncluttered background",
            3: f"symbolic historical record, {subject_single}, restrained documentary detail",
        }
    suffix = suffixes.get(min(3, max(1, int(round_no))), suffixes[3])
    out = re.sub(r"\s+", " ", out).strip(" ,")
    # вырезанный усилитель («Extremely graphic gore:») оставлял висячую пунктуацию в начале
    out = re.sub(r"^[\s,:;.\-–—]+", "", out)
    # Regex-proxy intentionally does not consume terminal punctuation.  Strip it before
    # adding the suffix, otherwise retries accumulate fragments like ``scene., calm``.
    out = out.rstrip(" .,:;")
    # Произвольные имена этот широкий pass не угадывает. Доказанные исторические имена
    # обезличивает контекстный узкий pass перед входом сюда.
    if suffix.lower() not in out.lower():
        out = f"{out}, {suffix}" if out else suffix
    return out


# Модель может ОТКАЗАТЬСЯ переписывать промпт (жёсткий сюжет) и ответить прозой вместо
# промпта. Такой ответ нельзя отдавать генератору картинок: рисовать по тексту «I can't help
# rewrite this…» бессмысленно, а раунд смягчения сгорает. Признаки берём консервативно —
# ложное срабатывание всего лишь откатывает на детерминированный локальный пасс.
_REFUSAL_MARKERS = (
    "i can't help", "i cannot help", "i can't rewrite", "i cannot rewrite",
    "i can't assist", "i cannot assist", "i won't", "i will not",
    "i'm not able to", "i am not able to", "i'm unable", "i am unable",
    "i can't create", "i cannot create", "i can't produce", "i cannot produce",
    "as an ai", "не могу помочь", "я не могу", "не буду",
)


def _looks_like_refusal(out: str, original: str) -> bool:
    """Ответ смягчателя — это отказ/рассуждение, а не промпт?

    Два независимых признака: (1) явные маркеры отказа от первого лица в начале ответа;
    (2) ответ заметно ДЛИННЕЕ исходного промпта — переписанный промпт сопоставим по длине,
    а объяснение «почему не буду» разрастается.
    """
    s = str(out or "").strip().lower()
    if not s:
        return True
    head = s[:200]
    if any(m in head for m in _REFUSAL_MARKERS):
        return True
    src_len = len(str(original or ""))
    return src_len > 0 and len(s) > 2 * src_len + 200


def _soften_prompt_gpt(prompt: str, round_no: int = 1) -> Optional[str]:
    """После отказа контент-фильтра GPT переписывает промпт мягче/завуалированнее (по запросу
    юзера 04.07: до 3 раундов, каждый агрессивнее). Смысл/композиция/стиль сцены сохраняются,
    жёсткие слова (кровь/оружие/трупы и т.п.) уходят в implied/off-screen формулировки.
    Возвращает новый промпт или None (нет канала промптов/ошибка GPT)."""
    if (os.environ.get("VS_DISABLE_PROMPT_LLM", "").strip().lower()
            in ("1", "true", "yes", "on")):
        return _soften_prompt_local(prompt, round_no)
    if not _has_prompt_channel():
        return None
    strength = {1: "Lightly soften", 2: "Strongly soften"}.get(round_no, "Aggressively sanitize")
    sys_p = (
        f"{strength} this AI image/video generation prompt so it passes strict content-safety filters. "
        "Remove or euphemize: violence, gore, blood, weapons, corpses, death, injuries, cruelty, war brutality, "
        "nudity and shocking/disturbing wording. "
        "ALSO replace any named real, historical or famous person with a GENERIC anonymous description that keeps "
        "era/role/appearance but drops the identity (e.g. 'George Washington' -> 'an 18th-century colonial general "
        "in blue uniform', 'Hitler' -> 'a 1940s European dictator figure'); remove ALL proper names of real people "
        "and any 'looks like / portrait of <name>' phrasing — video models reject recognizable public figures "
        "(PROMINENT_PEOPLE filter). "
        "ALSO SIMPLIFY overly complex scenes: if there are many subjects, crowds, or several simultaneous actions, "
        "reduce to ONE clear subject and ONE simple action with a calm background — complex prompts make video "
        "models time out (GENERATION_TIMED_OUT). KEEP the same scene meaning, composition, camera and style words. "
        "Prefer implied/off-screen phrasing (aftermath, tension, symbolism) over explicit depiction. "
        "Return ONLY the rewritten prompt text, same language as the input, no comments."
    )
    payload = {
        "model": GPT_MODEL,
        "messages": [{"role": "system", "content": sys_p}, {"role": "user", "content": str(prompt)[:4000]}],
        "max_tokens": 500, "temperature": 0.4,
    }
    try:
        r = _llm_post(OPENAI_API_URL,
                              headers=_openai_chat_headers(),
                              json=payload, timeout=60)
        if r.status_code != 200:
            print_debug(f"смягчение промпта (раунд {round_no}): HTTP {r.status_code}")
            return None
        out = (r.json()["choices"][0]["message"]["content"] or "").strip()
        if not out:
            return None
        if _looks_like_refusal(out, prompt):
            # Модель отказалась переписывать (жёсткий сюжет) и ответила прозой «I can't help…».
            # Раньше этот ОТКАЗ подставлялся как новый промпт и уходил в генератор картинок:
            # раунд смягчения сгорал впустую, а провайдер рисовал по тексту отказа.
            print_user(f"🩹 Смягчатель отказался переписывать (раунд {round_no}) — "
                       "применяю детерминированный safety-пасс", "WARNING")
            return _soften_prompt_local(prompt, round_no)
        return out
    except Exception as e:
        print_debug(f"смягчение промпта (раунд {round_no}): {e}")
        return None


# ── Смягчение промпта: ступенчато, от дешёвого к дорогому ─────────────────────
# Ночь 05→06.08: 2434 вызова LLM на смягчение и 751 всё равно пропущенный кадр. Разбор
# показал, что резала не жестокость (её ловит _soften_prompt_local), а СИМВОЛИКА и ИМЕНА:
# «SS uniform», «skull insignia», «runic banners», «Himmler». Для них LLM не нужен —
# это словарная замена. Поэтому порядок такой: символика → жестокость → и только потом GPT.
_BLOCKED_ENTITY_MAP = (
    # Exact phrases recovered from fresh failures in jobs
    # 10727/10747/10751/10836/10845/10852/10855.  Common words such as
    # ``striking``, ``bodies`` and ``dead`` are deliberately not broad rules.
    (r"\bnational[-\s]+social(?:ism|ist\w*)\b", "period state ideology"),
    (r"\b(?:Societății\s+Thule|Thule\s+Society|Society\s+of\s+Thule)\b",
     "period esoteric study circle"),
    (r"\bAryan(?:ism|s)?\b", "period exclusionary origin doctrine"),
    (r"\brune\s+nordice\s+vechi\b", "motive geometrice de epocă"),
    (r"\b(?:old\s+)?(?:Norse|Nordic)\s+runes?\b", "period geometric motifs"),
    (r"\britual\s+runes?\b", "period ceremonial geometric motifs"),
    (r"\bsimboluri\s+ritualice\b", "motive ceremoniale de epocă"),
    (r"\bocultiști\s+germani\b", "cercetători ezoterici ai epocii"),
    (r"\bposturi\s+conspirative\b", "posturi discrete"),
    (r"\bpoison\s+capsules?\b", "sealed emergency capsule"),
    (r"\blashing\s+out\s+and\s+striking\b",
     "appearing only as a distant silhouette during a tense family exchange"),
    (r"\bsource\s+of\s+the\s+violence\b", "source of the tense family dynamic"),
    (r"\bpaternal\s+brutalit\w*\b", "strict family dynamic"),
    (r"\bbrutalit\w*\b", "stark archival evidence"),
    (r"\bviolent\s+withdrawal\s+of\s+the\s+blades\b", "tense withdrawal of the equipment"),
    (r"\bexterminatory\s+violence\b", "documented state program"),
    (r"\bbeing\s+violently\s+driven\s+forward\b", "moving forward under close supervision"),
    (r"\bbeing\s+driven\s+violently\s+toward\b", "moving under close supervision toward"),
    (r"\bsoldiers\s+violently\s+interrogate\b", "soldiers conduct a tense street questioning of"),
    (r"\bmilitary\s+violence\b", "military operations"),
    (r"\bviolent\b", "chaotic"),
    (r"\bviolence\b", "historical action"),
    (r"\bHolocaust\b", "documented 1940s state campaign"),
    (r"\bdeportation\s+machinery\b", "rail transfer system"),
    (r"\bregime\s+terror\b", "cold command presence"),
    (r"\bdeath['’]s[- ]head\b", "plain historical unit emblem"),
    (r"\bcoup\s+de\s+grace\b", "implied final moment"),
    (r"\bhead-targeting\b", "close framing"),
    (r"\baimed\s+directly\s+at\s+his\s+head\b", "held within the foreground"),
    (r"\baligned\s+at\s+his\s+skull\b", "held across the foreground"),
    (r"\bexecutioners?\b", "uniformed guards"),
    (r"\bfiring\s+squad\b", "guard formation"),
    (r"\bmass[-\s]?killing\s+installation\b", "sealed state compound"),
    (r"\bindustrial\s+murder\b", "centralized state process"),
    (r"\bmassacr\w*\b", "documented event"),
    (r"\bnearby\s+victims\s+at\s+the\s+base\s+of\s+a\s+wall\b",
     "covered archival markers at the base of a wall"),
    (r"\bscattered\s+victims\b", "scattered archival markers"),
    (r"\b(?:being\s+)?hanged\b", "represented only by an empty memorial marker"),
    (r"\bhanging\s+body\b", "covered memorial silhouette"),
    (r"\bremains\s+suspended\b", "is represented symbolically"),
    (r"\bsuspended\s+form\b", "memorial silhouette"),
    (r"\bhangs?\s+alone\b", "remains alone"),
    (r"\bbattered\s+body\b", "covered foreground figure"),
    (r"\bfallen\s+prisoners\b", "seated detainees"),
    (r"\bbodies\s+and\s+execution\s+traces\b", "covered archival markers"),
    (r"\brepression\s+apparatus\b", "state control apparatus"),
    (r"\bmachinery\s+of\s+repression\b", "state administration"),
    (r"\bshort,\s+savage\s+street\s+operation\b", "short, chaotic street operation"),

    # Job 10747 frame 9: use one visual proxy for the whole explicit action,
    # then cover the same provider vocabulary in other retry candidates.
    (r"\byanking\s+bloodied\s+bayonets\s+upward\s+from\s+bodies\s+on\s+the\s+ground\s+after\s+the\s+gunfire\b",
     "lifting period field equipment while covered markers and lingering smoke imply the preceding clash"),
    (r"\bdead\s+and\s+dying\s+captives\s+at\s+their\s+boots\b", "covered markers at their feet"),
    (r"\bbloodied\b", "dust-stained"),
    (r"\bbayonets?\b", "period field equipment"),
    (r"\bgunfire\b", "lingering smoke"),
    (r"\bdead\s+and\s+dying\s+(British\s+soldiers|French\s+colonial\s+troops)\b",
     r"motionless \1"),
    (r"\bamid\s+the\s+dead\b", "among covered markers"),

    # Live canary 10905, frame 001: keep the naval scale and rescue context,
    # but move people from explicit loss imagery to visible survival markers.
    (r"\bstruggling\s+and\s+disappearing\s+in\s+the\s+rough\s+water\b",
     "holding to life rafts in the rough water"),
    (r"\bshattered\s+transport\s+ships\b", "damaged transport ships"),
    (r"\bbodies\s+spread\s+across\s+a\s+long\s+band\s+of\s+ocean\b",
     "survivors and empty life jackets spread across a long band of ocean"),
    (r"\bmaking\s+clear\s+that\s+the\s+deaths\s+are\s+happening\b",
     "making clear that a large maritime rescue is underway"),
    (r"\bsingle\s+devastating\s+scene\b", "single wide rescue scene"),

    # Live kirill 10911, frames 007/019/031/158/208.  These are deliberately
    # exact phrases from the five repeatedly rejected scene prompts, not broad
    # language rules.  Frames 007/019 reduce overloaded two-subject prompts to
    # one quiet action; the other three preserve the historical beat while
    # moving explicit captivity/injury/mass-loss imagery to neutral staging.
    (r"\bJapanese\s+transports\s+and\s+escorting\s+destroyers\s+steam\s+out\s+into\s+the\s+black\s+tropical\s+night\s+under\s+blackout\s+conditions\b",
     "period transport ships move across the dark tropical water at night"),
    (r"\bloading\s+cranes\s+lean\s+ominously\s+in\s+the\s+background\b",
     "still loading cranes stand in silhouette in the background"),
    (r"\bIn\s+the\s+foreground,\s+a\s+dim\s+operations\s+table\s+with\s+a\s+listening\s+American\s+general\s+and\s+the\s+grief-worn\s+mechanic\s+Paul\s+Gunn\s+bent\s+over\s+charts\s+ties\s+the\s+departing\s+convoy\s+to\s+the\s+coming\s+fifteen\s+minutes\s+that\s+will\s+change\s+the\s+war\b",
     "The composition stays focused on the departing convoy and its pale wakes"),
    (r"\bPaul\s+Irvin\s+Gunn,\s+a\s+lean\s+man\s+in\s+stained\s+mechanic['’]s\s+coveralls\b",
     "an adult mechanic in plain period coveralls"),
    (r"\bfaces\s+an\s+aircraft\s+under\s+repair\s+at\s+Eagle\s+Farm\s+airfield\s+and\s+argues\s+with\s+the\s+war\s+through\s+sheer\s+stubborn\s+physical\s+work\b",
     "steadily inspects a parked aircraft under repair at Eagle Farm airfield"),
    (r"\bhis\s+gaunt\s+profile\s+is\s+tense\s+as\s+he\s+gestures\s+toward\s+exposed\s+machinery\s+and\s+improvised\s+modifications\s+on\s+the\s+bomber\b",
     "he calmly checks a single engine panel and one practical field modification"),
    (r"\bsurrounded\s+by\s+tools,\s+fuel\s+drums,\s+and\s+the\s+rough\s+corrugated\s+hangar\s+interior\s+of\s+wartime\s+Australia\b",
     "with a few tools visible inside a simple corrugated hangar in period Australia"),
    (r"\ban\s+American\s+civilian\s+family\s+confined\s+behind\s+a\s+barred\s+gate\s+or\s+fenced\s+enclosure\s+in\s+occupied\s+Manila\b",
     "American civilian adults gathered inside a supervised wartime courtyard in Manila"),
    (r"\btheir\s+anxious\s+faces\s+pressed\s+into\s+a\s+tight\s+group\b",
     "their somber expressions visible as they stand together"),
    (r"\bJapanese\s+soldiers\s+in\s+historically\s+accurate\s+early-1940s\s+uniforms\s+patrol\s+just\s+beyond\s+them\b",
     "period-uniformed officials stand at a respectful distance"),
    (r"\bthe\s+family['’]s\s+captivity\s+with\s+the\s+visible\s+presence\s+of\s+the\s+occupiers\b",
     "the adults' separation from home with the visible presence of the new authorities"),
    (r"\bManila['’]s\s+swift\s+conquest\b", "Manila's abrupt wartime transition"),
    (r"\bUn\s+soldat\s+japonez\s+imperial\s+rănit\b",
     "Un adult în uniformă de epocă, epuizat"),
    (r"\bcu\s+brațul\s+stâng\s+grav\s+ars\s+și\s+inert\s+și\s+corpul\s+răsucit\s+de\s+durere\b",
     "cu mâneca stângă udă și corpul sprijinit cu greu"),
    (r"\bpiciorul\s+stâng\s+atârnă\s+amorțit\s+sub\s+apă,\s+fără\s+reacție\b",
     "picioarele rămân parțial sub apă în timp ce așteaptă ajutor"),
    (r"\bcu\s+valuri\s+joase,\s+fum\s+de\s+luptă\s+și\s+pete\s+de\s+păcură\s+în\s+fundalul\s+apropiat\b",
     "cu valuri joase, ceață marină și urme de combustibil în fundalul apropiat"),
    (r"\bsute\s+de\s+soldati\s+japonezi\s+imprastiati\s+in\s+apa\s+agitata\s+printre\s+epave,\s+plute\s+rupte,\s+butoaie\s+si\s+resturi\s+de\s+transporturi\s+incendiate\b",
     "o intindere larga de apa cu plute goale, veste de salvare abandonate, butoaie si resturi langa transporturi avariate"),
    (r"\bDeasupra\s+lor,\s+foarte\s+jos,\s+trece\s+un\s+B-25\s+cu\s+botul\s+inarmat\b",
     "La orizont se vede silueta unui B-25 indepartandu-se"),
    (r"\brelatia\s+cauza-efect\s+dintre\s+refuzul\s+mecanicului\s+australian\s+de\s+a\s+dormi\s+si\s+moartea\s+lor\s+este\s+exprimata\s+prin\s+prezenta\s+decisiva\s+a\s+avionului\s+improvizat\s+care\s+a\s+facut\s+macelul\s+posibil\b",
     "legatura cu munca neobosita a mecanicului australian este sugerata doar de silueta indepartata a avionului modificat"),
    (r"\bImaginea\s+trebuie\s+sa\s+arate\s+clar\s+ca\s+aproape\s+trei\s+mii\s+nu\s+vor\s+mai\s+atinge\s+niciodata\s+uscatul\b",
     "Imaginea trebuie sa transmita amploarea pierderii prin spatiile goale si echipamentul de salvare abandonat, intr-un cadru sobru fara persoane vizibile"),

    # Live kirill 10913, frames 006/007/188.  The provider returned
    # the same opaque generic rejection through every bounded attempt.  Keep
    # these proxies tied to the full rejected clauses: one simple action or an
    # empty archival environment, without globally anonymizing Gunn/Kenney or
    # ordinary vocabulary that succeeds in neighbouring scenes.  Frames
    # 039/043/046 moved to whole-scene preflight keys after their clause-level
    # variants also exhausted cache retry 10943.
    (r"\bFrom\s+high\s+above\s+a\s+convoy\s+at\s+sea,\s+a\s+Japanese\s+observer\s+on\s+a\s+transport\s+deck\s+looks\s+upward\s+through\s+binoculars\s+at\s+a\s+low-flying\s+modified\s+bomber\s+approaching\s+in\s+a\s+profile\s+easy\s+to\s+mistake\s+for\s+another\s+aircraft\s+type,\s+while\s+below\s+him\s+ships\s+churn\s+through\s+dark\s+water\s+in\s+tight\s+formation\b",
     "From high above a convoy at sea, an adult observer on a transport deck studies the silhouette of one distant modified aircraft through binoculars while the ships move below in an orderly formation"),
    (r"\bThe\s+altered\s+attack\s+plane,\s+bristling\s+at\s+the\s+nose\s+and\s+skimming\s+toward\s+the\s+convoy,\s+visually\s+connects\s+the\s+moment\s+of\s+mistaken\s+identification\s+to\s+the\s+weapon\s+that\s+will\s+soon\s+send\s+ships\s+under\s+and\s+transform\s+Pacific\s+air-sea\s+warfare\b",
     "The composition centers on the observer and the unfamiliar aircraft silhouette during a mistaken identification before a major maritime turning point"),
    (r"\bAt\s+a\s+tense\s+Dutch\s+angle,\s+a\s+Japanese\s+convoy\s+steams\s+out\s+of\s+Rabaul\s+harbor\s+at\s+midnight\s+on\s+the\s+last\s+night\s+of\s+February\s+1943,\s+troop\s+transports\s+and\s+escorts\s+sliding\s+past\s+volcanic\s+shoreline,\s+harbor\s+lights,\s+and\s+black\s+tropical\s+water\s+under\s+a\s+clouded\s+sky\b",
     "At a calm Dutch angle, period transport ships depart Rabaul harbor at midnight beneath a clouded tropical sky, their pale wakes crossing dark water beside the volcanic shoreline"),
    (r"\bIn\s+the\s+near\s+foreground,\s+silhouetted\s+against\s+chart\s+light\s+at\s+the\s+edge\s+of\s+the\s+composition,\s+an\s+American\s+mechanic\s+and\s+a\s+listening\s+general\s+study\s+the\s+departure['’]s\s+implications,\s+tying\s+the\s+convoy['’]s\s+movement\s+to\s+the\s+grief,\s+command\s+decision,\s+and\s+fifteen-minute\s+catastrophe\s+that\s+will\s+change\s+everything\b",
     "The composition stays focused on the departing convoy and the quiet harbor"),
    (r"\bScattered\s+along\s+the\s+path\s+and\s+into\s+the\s+underbrush\s+lie\s+the\s+skeletal\s+remains\s+and\s+decayed\s+uniforms\s+of\s+many\s+Japanese\s+soldiers,\s+helmets,\s+packs,\s+and\s+rusting\s+rifles\s+half\s+buried\s+in\s+mud\s+and\s+roots,\s+showing\s+that\s+thousands\s+died\s+on\s+the\s+march\s+without\s+ever\s+reaching\s+battle\b",
     "Abandoned helmets, packs, and weathered field equipment rest along the path beneath vines and roots, suggesting the scale of a failed march through the jungle"),
    (r"\bThe\s+frame\s+emphasizes\s+the\s+trail\s+disappearing\s+under\s+the\s+jungle\s+canopy\s+and\s+the\s+bodies\s+strewn\s+across\s+it,\s+with\s+rain-dark\s+ground\s+and\s+oppressive\s+vegetation\s+closing\s+in\s+from\s+every\s+side\b",
     "The frame emphasizes the empty trail disappearing under the jungle canopy, with scattered field equipment, rain-dark ground, and dense vegetation closing in from every side, without visible people"),

    (r"\b(?:waffen[-\s]?)?ss\b(?=\s*(?:uniform|officer|men|troops|insignia|rune|cap|guard)|\b)",
     "wartime state security"),
    (r"\bschutzstaffel\b|\bgestapo\b|\bnsdap\b", "wartime state security"),
    (r"\bnazi\w*\b|\bthird reich\b|\bhitlerjugend\b", "1940s authoritarian state"),
    (r"\bswastika\w*\b|\bhakenkreuz\b|\bsig\s+runes?\b|\bss\s+runes?\b|\brunic insignia\b",
     "plain unadorned banner"),
    (r"\btotenkopf\b|\bskull\s+(?:insignia|badge|emblem|device)\b", "plain metal cap badge"),
    (r"\bconcentration camp\b|\bdeath camp\b|\bextermination camp\b", "wartime internment site"),
    (r"\bwehrmacht\b|\bluftwaffe\b|\bkriegsmarine\b", "period military service"),
)

_PROMINENT_SURNAME_RULE = (
    r"\bhitler\b|\bhimmler\b|\bgoebbels\b|\bg[oö]ring\b|\bheydrich\b|\beichmann\b|"
    r"\bmussolini\b|\bstalin\b|\bberia\b",
    "an unnamed official of the era",
)

# Full-name anchors make the replacement contextual.  A benign prompt that
# merely contains ``Klara``, ``Heinrich``, ``Davy`` or another common name is
# untouched; remnants are replaced only when the full historical identity is
# present in the same mutable scene prose.
_CONTEXTUAL_IDENTITY_RULES = (
    (r"\bAdolf\s+Hitler\b", (
        (r"\bAdult\s+Adolf\s+Hitler\b", "an unnamed adult national leader of the era"),
        (r"\byoung\s+Adolf(?:\s+Hitler)?\b", "a younger adult family member"),
        (r"\bAdolf\s+Hitler\b", "an unnamed adult national leader of the era"),
        (r"\b(?:Adult\s+)?Adolf\b", "the unnamed adult leader"),
    )),
    (r"\bKlara\s+Hitler\b", (
        (r"\bKlara\s+Hitler\b|\bKlara\b", "the leader's unnamed mother"),
    )),
    (r"\bAlois\s+Hitler\b", (
        (r"\bAlois\s+Hitler\b|\bAlois\b", "the leader's unnamed father"),
    )),
    (r"\bHeinrich\s+Himmler\b", (
        (r"\bHeinrich\s+Himmler\b|\bHimmler\b", "an unnamed senior state official"),
    )),
    (r"\bTheodor\s+Eicke\b", (
        (r"\bTheodor\s+Eicke\b|\bEicke\b", "an unnamed senior unit commander"),
    )),
    (r"\bFritz\s+(?:Kn[oö]chlein|Knoechlein)\b", (
        (r"\bFritz\s+(?:Kn[oö]chlein|Knoechlein)\b|\bKn[oö]chlein\b|\bKnoechlein\b",
         "an unnamed former unit officer"),
    )),
    (r"\bPaul\s+Davy\b", (
        (r"\bPaul\s+Davy\b|\bDavy\b", "an unnamed adult village resident"),
    )),
)


def _exact_safety_prompt_key(prompt):
    """Stable key for proven whole-prompt provider failures, never a substring rule."""
    return " ".join(str(prompt or "").split()).casefold()


# These prompts failed repeatedly with a provider terminal ``status=failed`` or explicit
# safety rejection while neighbouring frames succeeded through the same route.  Whole-prompt
# keys keep the recovery surgical: ordinary mentions of the same people, ships or places are
# untouched.  Protected named-identity scenes bypass this table in _sanitize_blocked_terms.
_EXACT_PREFLIGHT_SAFETY_PROXIES = {
    # Job 10913 + cache retry 10943, frames 039/043/046: each original prompt and
    # each former clause-level proxy exhausted three independent 16-call cycles.
    # Route these proven full prompts to one quiet subject before the first paid
    # provider call.  Protected identity/reference scenes require their own
    # non-anonymous variant so continuity bytes are never contradicted.
    _exact_safety_prompt_key(
        "A workshop wall inside Eagle Farm fills the frame from a low viewpoint, making "
        "the hanging tools feel heavy and imposing: spanners, hammers, wrenches, and metal "
        "implements suspended in tight rows on rough boards. The composition emphasizes "
        "that something is concealed behind the tools, with layered silhouettes, deep "
        "shadows between metal shapes, and the wall rising above a cluttered aircraft "
        "repair bench in a humid wartime hangar. Primary subject, large and centered in "
        "frame: ściana warsztatu zasłonięta narzędziami."
    ): (
        "An empty Eagle Farm aircraft workshop in period Australia shows one plain wooden "
        "wall covered by orderly rows of hand tools above a clear repair bench, without "
        "visible people."
    ),
    _exact_safety_prompt_key(
        "A wide wartime hangar shot shows the exhausted mechanic still working at his "
        "station long after the others are faltering, shoulders hunched over an aircraft "
        "engine assembly while his cramped hands cling to tools and his eyes are reddened "
        "from strain. Around him, several subordinate mechanics in period overalls and "
        "tropical work gear pause and plead with him to go sleep, their weary gestures and "
        "faces turned toward him amid benches, parts, ladders, and the cavernous Eagle Farm "
        "repair shop. Primary subject, large and centered in frame: wyczerpująca praca "
        "mechanika aż do skurczu dłoni."
    ): (
        "Inside a quiet Eagle Farm repair hangar, one unnamed adult mechanic calmly checks "
        "one aircraft engine at a clear workbench after a long repair shift."
    ),
    _exact_safety_prompt_key(
        "George Kenney strides through a sprawling Australian repair depot only days after "
        "arrival, inspecting rows of damaged aircraft, engine crates, and busy mechanics "
        "with unsentimental intensity. The low vantage makes him look forceful and "
        "results-driven as he moves past open hangars and improvised maintenance yards "
        "stretching into the distance, suggesting a rapid cross-country sweep of scattered "
        "depots. Greasy crews pause at their work, but Kenney keeps moving, surveying "
        "wrecked airframes and repair lines with the impatience of a commander who cares "
        "only whether the machines can be made useful again."
    ): (
        "At an Australian aircraft repair depot, one unnamed adult aviation commander "
        "calmly inspects one repairable aircraft beside an open hangar, with orderly engine "
        "crates in the background."
    ),
    # Job 10932 + retry 10945, frames 034/038/040/060/178/190: the first
    # exact proxies also exhausted a complete provider cycle while four sibling
    # exact proxies succeeded on the same model and key.  Route only these six
    # proven full prompts to smaller civilian/empty compositions before the
    # first paid request.  None currently has an identity/reference contract;
    # protected or immutable variants remain fail-closed in preflight.
    _exact_safety_prompt_key(
        "An overhead view inside Paul Gunn's workshop at Eagle Farm shows a plain wooden wall "
        "carefully lined with hanging tools, the dominant subject filling the composition with "
        "spanners, hammers, and metal implements arranged in practical order; below and around "
        "it, the workbench, oil-stained floor, and rough wartime repair environment establish "
        "that this is the wall he built his labor around."
    ): (
        "One plain wooden workshop wall fills the frame, with a small row of ordinary hand "
        "tools hanging above a clean empty bench in soft daylight, without visible people."
    ),
    _exact_safety_prompt_key(
        "Inside a humid Australian wartime hangar in August 1942, Pappy Gunn dominates the "
        "foreground from the waist up, lean and grim, face lined with exhaustion and grief, "
        "sleeves rolled, hands working a welding torch into the nose of a light bomber under "
        "repair; sparks burst across the stripped metal as improvised gun hardware takes shape. "
        "In the deeper background through the open hangar doors, General George Kenney, "
        "sharp-eyed and sinewy, has just arrived on the airfield with a small staff beside a "
        "transport aircraft, his presence introducing a new command force into the same world. "
        "The composition ties Gunn's obsessive labor to Kenney's arrival, with real depth from "
        "oily tools and fuselage ribs in front to sun-bleached tarmac and aircraft outside."
    ): (
        "Inside a quiet civilian workshop, one unnamed adult technician calmly checks a small "
        "metal panel at a clean bench in soft daylight. The uncluttered background contains no "
        "other people."
    ),
    _exact_safety_prompt_key(
        "From just behind George Kenney's shoulder, the viewer looks across a sprawling "
        "maintenance base as he strides past rows of battered aircraft, open engine cowlings, "
        "oil drums, and improvised repair stations under corrugated roofs. Mechanics in stained "
        "coveralls work in heat and dust while Kenney surveys everything with brisk, "
        "unsentimental scrutiny, suggesting that procedure matters less to him than usable "
        "machines and immediate results. The frame captures his rapid tour of scattered "
        "Australian support bases through a practical commander's viewpoint, with the hangar "
        "floor and repair lines stretching away into depth."
    ): (
        "At a quiet civilian workshop, one unnamed adult supervisor calmly inspects a single "
        "clean machine beside an open doorway, with orderly storage boxes in the background."
    ),
    _exact_safety_prompt_key(
        "General George Kenney stands in Brisbane as the commanding figure of the shot, viewed "
        "from a low angle on an airfield workshop floor while he watches mechanics in Australia "
        "fitting and preparing aircraft for attacks from wave-top height; around him are "
        "propeller bombers under repair, ladders, tools, ammunition fittings, and oil-stained "
        "ground crews, making clear that he immediately understands the value of planes able to "
        "silence anti-aircraft fire from extremely low altitude"
    ): (
        "Inside a quiet civilian workshop, one unnamed adult supervisor calmly inspects one "
        "stationary machine beside a ladder and a small set of ordinary hand tools."
    ),
    _exact_safety_prompt_key(
        "On the Eagle Farm flight line, Pappy Gunn stands beside the nose of a newly modified "
        "B-25 under repair, shown from about the waist up with the aircraft dominating the "
        "frame; he drags one palm across the bomber's metal nose in a brief pause, staring past "
        "it as if his thoughts are far to the north, then turns back toward the workshop where "
        "he grips a wrench and resumes work. The scene should keep the B-25 central and tangible, "
        "with open hangar doors, scattered tools, mechanics' equipment, and the hot airfield "
        "stretching behind him, making work itself the barrier against memory."
    ): (
        "At a quiet workshop entrance, one unnamed adult technician calmly checks a stationary "
        "metal machine beside a clean bench with one ordinary hand tool."
    ),
    _exact_safety_prompt_key(
        "A wide wartime airfield scene in the Southwest Pacific showing the concrete cause of "
        "death: a heavy American B-25 in a dangerously low, aggressive pass over the runway "
        "while other aircraft, mechanics, and distant bomber revetments fill the background. "
        "The composition links this single reckless low-altitude maneuver to a broader pattern, "
        "with multiple similar medium bombers parked or under repair across the humid base, "
        "suggesting the hundreds of American pilots lost to routine flying accidents across the "
        "Pacific rather than enemy action."
    ): (
        "A wide empty field shows one stationary unmarked aircraft parked on level ground in "
        "soft daylight, with simple storage buildings in the distance and no visible people."
    ),
    # Job 10939 + retry 10948, frame 019: the first whole-scene repair proxy
    # itself returned the same opaque terminal failure and refill immediately
    # restarted the original fixed input.  Keep only the workshop still life
    # and route it before the first paid request.  Current scene has no protected
    # identity/reference contract; protected or immutable variants bypass it.
    _exact_safety_prompt_key(
        "Seen from just behind a shoulder in the workshop, Paul Irvin Gunn, a lean man in "
        "stained mechanic's coveralls, faces an aircraft under repair at Eagle Farm airfield "
        "and argues with the war through sheer stubborn physical work; his gaunt profile is "
        "tense as he gestures toward exposed machinery and improvised modifications on the "
        "bomber, surrounded by tools, fuel drums, and the rough corrugated hangar interior of "
        "wartime Australia"
    ): (
        "A clean workbench in a quiet civilian workshop displays one small metal panel and a "
        "few ordinary hand tools in soft daylight, without visible people."
    ),
}


_EXACT_PREFLIGHT_PROTECTED_SAFETY_PROXIES = {
    # Current frame 046 has no reference contract.  If the same exact scene is
    # later bound to one, keep the mutable scene aligned with its protected
    # named subject rather than introducing an anonymous identity conflict.
    _exact_safety_prompt_key(
        "George Kenney strides through a sprawling Australian repair depot only days after "
        "arrival, inspecting rows of damaged aircraft, engine crates, and busy mechanics "
        "with unsentimental intensity. The low vantage makes him look forceful and "
        "results-driven as he moves past open hangars and improvised maintenance yards "
        "stretching into the distance, suggesting a rapid cross-country sweep of scattered "
        "depots. Greasy crews pause at their work, but Kenney keeps moving, surveying "
        "wrecked airframes and repair lines with the impatience of a commander who cares "
        "only whether the machines can be made useful again."
    ): (
        "At an Australian aircraft repair depot, the named senior aviation commander "
        "specified by the identity and reference contract calmly inspects one repairable "
        "aircraft beside an open hangar, with orderly engine crates in the background."
    ),
}


_EXACT_LIVE_SAFETY_PROXIES = {
    **_EXACT_PREFLIGHT_SAFETY_PROXIES,
    _exact_safety_prompt_key(
        "The interior-forward section of Kyokusei Maru is torn open after the bomb punches "
        "through the deck, showing the blast erupting inside the forward cargo hold packed for "
        "the transport of more than twelve hundred troops. Flames, shattered beams, twisted "
        "metal, and bodies thrown by the shockwave fill the confined shipboard space, with the "
        "broken deck above revealing the bomb's entry path and the catastrophic destruction "
        "within the hold."
    ): (
        "Close archival view inside the forward cargo hold of the transport Kyokusei Maru after "
        "a deck breach: bent beams, twisted metal, smoke, and scattered cargo show severe "
        "structural damage in the empty compartment, without visible people."
    ),
    _exact_safety_prompt_key(
        "Nízký dramatický pohled od hladiny ukazuje torpédoborec Shirayuki, jak už necelou "
        "půlminutu po Larnerově průletu smrtelně kolabuje, trup zlomený a zabořený do moře, kouř "
        "stoupá z rozervané paluby. Nad ním a za odletujícím Larnerovým B-25 se ve vrstvené "
        "hloubce objevují další bombardéry devadesáté perutě, letící nízko nad vodou v "
        "rozptýlené formaci a stáčející se k dalším japonským lodím jako k novým cílům. "
        "Kompozice spojuje umírající loď v popředí s pokračujícím útokem na obloze, takže zkáza "
        "jedné lodi a okamžité hledání dalších obětí probíhá v jediném záběru."
    ): (
        "Low archival view from the water shows the damaged destroyer Shirayuki listing with a "
        "broken deck and smoke above it. Several distant B-25 aircraft continue across the sky "
        "toward other ships; the scene focuses on the disabled vessel and ongoing air operation, "
        "without visible people."
    ),
    _exact_safety_prompt_key(
        "Over the shoulder of a Japanese staff officer studying a campaign map, the frame shows "
        "the new supply system for New Guinea as plotted routes of submarines and small boats "
        "replace the former large transport lanes; nearby markers or silhouettes indicate how "
        "few men and supplies can move compared with the mass just lost in fifteen minutes of "
        "gunfire. The map table, notes, and naval symbols should make the logistical reduction "
        "immediately visible, presenting the decision as grimly necessary rather than "
        "triumphant. Primary subject, large and centered in frame: nový zásobovací režim ponorek "
        "a malých člunů pro Novou Guineu."
    ): (
        "Over the shoulder of one unnamed adult staff officer, an archival campaign map shows "
        "thin submarine and small-boat supply routes to New Guinea replacing broad transport "
        "lanes. The map table makes the reduced logistics capacity clear in one calm planning "
        "scene."
    ),
    _exact_safety_prompt_key(
        "American soldiers push into the Santo Tomas internment camp at the university compound, "
        "seen over a soldier's shoulder through the opened gateway into a crowded courtyard where "
        "five civilians are found alive: Polly, Nathalie, Julia, young Paul, and Connie, all "
        "visibly emaciated, sick, and exhausted in worn civilian clothes, their faces hollow but "
        "unmistakably living as troops move toward them with urgency; prison buildings and barbed "
        "boundaries frame the discovery, with the rescued family becoming the emotional center of "
        "the shot amid the chaos of liberation"
    ): (
        "At the Santo Tomas university compound in February 1945, an opened gateway reveals one "
        "adult civilian family member in worn clothes standing in a calm courtyard. The arrival "
        "of American troops is suggested by the open gate and parked equipment outside, while "
        "the adult survivor represents the family found alive."
    ),
}

_SOFTEN_CACHE = {}
_SOFTEN_CACHE_LOCK = threading.Lock()
_SOFTEN_EXHAUSTED = [0]          # кадров, которые не спасло даже полное смягчение
SOFTEN_GIVE_UP_AFTER = int(os.environ.get("VS_SOFTEN_GIVE_UP_AFTER", "5"))


@dataclass(frozen=True)
class _ExactPreflightSafetyResult:
    scene_prose: str
    matched: bool = False
    style_override: Optional[str] = None
    raw_provider_prompt: bool = False
    variant: str = "none"
    source_key_sha256: str = ""

    def fingerprint_plan(self, *, model: str = "", visible_text=None) -> Optional[dict]:
        if not self.matched:
            return None
        return {
            "schema_version": 1,
            "contract": "exact-preflight-safety-v1",
            "variant": self.variant,
            "source_key_sha256": self.source_key_sha256,
            "proxy": self.scene_prose,
            "style_policy": (
                "inherit" if self.style_override is None else "omit"
            ),
            "prompt_policy": (
                (
                    "raw-scene-with-visible-copy-no-negative-empty-suffix"
                    if (
                        str(model).strip().upper() == "OPENAI"
                        and visible_text == []
                    )
                    else "raw-scene-with-visible-copy"
                )
                if self.raw_provider_prompt else "segmented-composer"
            ),
        }


def _resolve_preflight_exact_safety(scene_prose: str, *, identity_block: str = "",
                                    ref_hints=None,
                                    immutable_scene: bool = False
                                    ) -> _ExactPreflightSafetyResult:
    """Resolve one proven whole-scene recovery before prompt composition.

    Unprotected exact failures omit the inherited preset style: the incident
    proved that reattaching the WWII style made a neutral proxy unsafe again.
    Protected variants retain the preset so identity/reference behaviour stays
    byte-compatible. Immutable scenes remain ineligible.
    """
    original = str(scene_prose or "")
    protected_refs = any(str(value or "").strip() for value in (ref_hints or []))
    if immutable_scene:
        return _ExactPreflightSafetyResult(original)
    protected = bool(str(identity_block or "").strip() or protected_refs)
    source = (
        _EXACT_PREFLIGHT_PROTECTED_SAFETY_PROXIES
        if protected else _EXACT_PREFLIGHT_SAFETY_PROXIES
    )
    source_key = _exact_safety_prompt_key(original)
    proxy = source.get(source_key)
    if not proxy:
        return _ExactPreflightSafetyResult(original)
    return _ExactPreflightSafetyResult(
        scene_prose=proxy,
        matched=True,
        style_override=None if protected else "",
        raw_provider_prompt=not protected,
        variant="protected" if protected else "unprotected",
        source_key_sha256=hashlib.sha256(source_key.encode("utf-8")).hexdigest(),
    )


def _preflight_exact_safety_proxy(scene_prose: str, *, identity_block: str = "",
                                  ref_hints=None, immutable_scene: bool = False):
    """Backward-compatible tuple facade for focused safety tests/callers."""
    result = _resolve_preflight_exact_safety(
        scene_prose,
        identity_block=identity_block,
        ref_hints=ref_hints,
        immutable_scene=immutable_scene,
    )
    return result.scene_prose, result.matched


def _sanitize_blocked_terms(prompt: str, preserve_named_identity: bool = False):
    """Словарная замена символики и имён, на которых режет провайдер. → (промпт, замен)."""
    out, n = str(prompt or ""), 0
    if not preserve_named_identity:
        exact_proxy = _EXACT_LIVE_SAFETY_PROXIES.get(_exact_safety_prompt_key(out))
        if exact_proxy:
            out, n = exact_proxy, n + 1
        for anchor, replacements in _CONTEXTUAL_IDENTITY_RULES:
            if not re.search(anchor, out, flags=re.IGNORECASE):
                continue
            for pattern, repl in replacements:
                out, cnt = re.subn(pattern, repl, out, flags=re.IGNORECASE)
                n += cnt
    for pattern, repl in _BLOCKED_ENTITY_MAP:
        out, cnt = re.subn(pattern, repl, out, flags=re.IGNORECASE)
        n += cnt
    if not preserve_named_identity:
        out, cnt = re.subn(
            _PROMINENT_SURNAME_RULE[0], _PROMINENT_SURNAME_RULE[1], out,
            flags=re.IGNORECASE,
        )
        n += cnt
    if n:
        out = re.sub(r"\s{2,}", " ", out).strip(" ,")
    return out, n


def _note_soften_exhausted():
    """Кадр не спасло полное смягчение. Набралось многовато → LLM-ступень отключаем."""
    _SOFTEN_EXHAUSTED[0] += 1
    if _SOFTEN_EXHAUSTED[0] == SOFTEN_GIVE_UP_AFTER:
        print_user(f"⛔ Провайдер отказывает по сюжету устойчиво ({SOFTEN_GIVE_UP_AFTER} кадров "
                   f"подряд не спасло смягчение) — GPT-переписывание отключено до конца прогона, "
                   f"остаются быстрые словарные замены", "WARNING")


def soften_prompt(prompt: str, round_no: int = 1):
    """Единая точка смягчения. Ступени по возрастанию цены:

      1) символика и имена — словарь, мгновенно, без LLM и без расхода канала;
      2) жестокость/графика — детерминированный пасс;
      3) переписывание GPT — только если предыдущие ничего не изменили.

    Результат кэшируется: та же сцена при перезапуске задачи не оплачивается повторно
    (проект `3 scenariusz_20_wisla` за ночь прошёл этот круг четырежды).
    Возвращает новый промпт или None, если смягчить нечем — контракт как был.
    """
    base = str(prompt or "")
    if not base.strip():
        return None
    key = (hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest(), int(round_no))
    with _SOFTEN_CACHE_LOCK:
        if key in _SOFTEN_CACHE:
            return _SOFTEN_CACHE[key]

    def _done(value, how):
        if value and value.strip() and value.strip() != base.strip():
            print_debug(f"смягчение раунд {round_no}: {how}")
            with _SOFTEN_CACHE_LOCK:
                if len(_SOFTEN_CACHE) < 4000:
                    _SOFTEN_CACHE[key] = value
            return value
        return None

    terms, n = _sanitize_blocked_terms(base)
    if n:
        return _done(terms, f"словарь символики ({n} замен)")
    _LOCAL_PASS_HITS.pop(threading.get_ident(), None)
    local = _soften_prompt_local(base, round_no)
    # именно СРАБОТАВШИЕ замены, а не «текст стал другим»: пасс всегда дописывает суффикс
    if _LOCAL_PASS_HITS.get(threading.get_ident(), 0) > 0 and local:
        return _done(local, "детерминированный safety-пасс")
    if _SOFTEN_EXHAUSTED[0] >= SOFTEN_GIVE_UP_AFTER:
        return None                       # сюжет провайдер не пропускает — LLM не тратим
    return _done(_soften_prompt_gpt(base, round_no), "переписывание GPT")


VEO_RETRY_DELAY = 20      # 20с × попытка → 20/40/…/до 380с (на поздних попытках backoff большой)
MAX_RETRIES = 3           # HTTP/GPT-уровневые ретраи (rate-limit/сеть) — НЕ «попытки генерации»


def _gen_attempts(default):
    """«Попыток генерации» картинки/клипа (внешний цикл max_attempts, который печатает «Попытка N»).
    Env-оверрайд GOOGLER_MAX_RETRIES — кабинет ставит per-key под ключ юзера; не задан → штатный
    дефолт (десктоп env не ставит → поведение прежнее). Читается в момент вызова (после спавна проца)."""
    _ov = os.environ.get("GOOGLER_MAX_RETRIES")
    if _ov:
        try:
            return max(1, int(_ov))
        except (TypeError, ValueError):
            pass
    return default
HERO_MAX_RETRIES = max(MAX_RETRIES, int(os.environ.get("VS_HERO_MAX_RETRIES", "8") or 8))
                          # Подготовка референса героя ходит в тот же общий ключ OpenAI, что
                          # rewrite и cover. Трёх попыток (2+4+6 = 12 секунд терпения) на
                          # занятый ключ не хватало: это 52 из 115 падений промптов за волну.
SCENE_MAX_RETRIES = 3     # Локально пережидаем только транзиент (timeout/429/5xx), не более 3 раз.
                          # После этого весь план падает с EX_TEMPFAIL=75; внешний scheduler решает,
                          # когда повторить job.
SCENE_SCHEMA_MAX_RETRIES = 3  # После первого malformed/schema ответа исправляем ТОЛЬКО эту часть.
                              # Канал/модель не меняются; после трёх corrective retries — fail-closed.
SCENE_SCHEMA_RETRY_DELAY = 1
RETRY_DELAY = 2

# Соотношения сторон
ASPECT_RATIOS_IMAGE = {
    'landscape': 'IMAGE_ASPECT_RATIO_LANDSCAPE',
    'portrait': 'IMAGE_ASPECT_RATIO_PORTRAIT',
    'square': 'IMAGE_ASPECT_RATIO_SQUARE'
}

ASPECT_RATIOS_VIDEO = {
    'landscape': 'VIDEO_ASPECT_RATIO_LANDSCAPE',
    'portrait': 'VIDEO_ASPECT_RATIO_PORTRAIT'
}

# Соотношения сторон для Grok API v4 (from-text): 1:1, 2:3, 3:2, 9:16, 16:9
GROK_ASPECT_RATIOS = {
    'landscape': '16:9',
    'portrait': '9:16',
    'square': '1:1',
}


def log(message: str):
    """Логирование с flush для real-time вывода (legacy, используйте print_user/print_debug)"""
    # Определяем тип сообщения по префиксу
    if message.startswith("✅") or "успешно" in message.lower() or "готово" in message.lower():
        print_user(message.replace("✅", "").strip(), "SUCCESS")
    elif message.startswith("❌") or "ошибка" in message.lower() or "error" in message.lower():
        print_user(message.replace("❌", "").strip(), "ERROR")
    elif message.startswith("⚠️") or "предупреждение" in message.lower() or "warning" in message.lower():
        print_user(message.replace("⚠️", "").strip(), "WARNING")
    elif message.startswith("⏳") or "ожидание" in message.lower() or "waiting" in message.lower():
        print_user(message.replace("⏳", "").strip(), "PROCESS")
    else:
        # Технические сообщения идут в debug
        print_debug(message)


# ============ УТИЛИТЫ ============

def split_text_to_sentences(text: str) -> List[str]:
    """
    Разбивает текст на предложения с поддержкой множества языков.
    Поддерживает знаки препинания из:
    - Английский, русский, европейские языки: . ! ? 
    - Японский: 。！？
    - Китайский: 。！？
    - Корейский: . ! ? (обычно латинские)
    - Арабский: ؟ . (вопросительный справа налево)
    - Хинди: । (danda - точка)
    - Греческий: ; (может использоваться как точка)
    - Армянский: ՜ (восклицательный)
    - Тайский: ฯ (abbreviation mark, может быть концом предложения)
    """
    # Расширенный набор знаков препинания для разных языков
    # Включаем все основные знаки конца предложения
    sentence_endings = r'[.!?。！？؟।;՜]'

    # Проверяем, есть ли в тексте CJK (китайский/японский/корейский) символы
    # Unicode ranges: китайский (4e00-9fff), хирагана (3040-309f), катакана (30a0-30ff), корейский (ac00-d7af)
    has_cjk = bool(re.search(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]', text))
    
    # Паттерн для разбиения:
    # 1. После знака конца предложения может быть пробел или перенос строки
    # 2. В японском/китайском пробелы могут отсутствовать, поэтому учитываем и без пробелов
    # 3. Переносы строк также считаем разделителями
    if has_cjk:
        # Для CJK языков: знак препинания может быть сразу перед следующим символом (без пробела)
        pattern = rf'(?<={sentence_endings})(?:\s+|\n+|(?=\S))|(?<=\n)\s*'
    else:
        # Для языков с пробелами: обычно есть пробел после знака препинания
        pattern = rf'(?<={sentence_endings})\s+|(?<={sentence_endings})\n+|(?<=\n)\s*'
    
    # 2026-06-16: ИСХОДНИК часто — СУБТИТРЫ с \n после каждой фразы, и раньше КАЖДЫЙ \n
    # считался концом предложения → обрывки посреди фразы («Лесная» / «свастика.»).
    # Сначала склеиваем строки в целые предложения: одиночный \n внутри предложения = пробел,
    # граница — знак конца предложения или ПУСТАЯ строка (\n\n = абзац). CJK не трогаем.
    # Проверено на реальном проекте: 202 обрывка → 111 целых предложений.
    if not has_cjk:
        _paras = re.split(r'\n[ \t]*\n', text.strip())
        _out = []
        for _para in _paras:
            _lines = [_ln.strip() for _ln in _para.split('\n') if _ln.strip()]
            _joined, _buf = [], ""
            for _ln in _lines:
                _buf = (_buf + " " + _ln).strip() if _buf else _ln
                if re.search(sentence_endings + r'[)\]»”"’]*$', _buf):
                    _joined.append(_buf)
                    _buf = ""
            if _buf:
                _joined.append(_buf)
            _out.append(" ".join(_joined))
        text = "\n\n".join(_p for _p in _out if _p)

    sentences = re.split(pattern, text.strip())
    # Фильтруем пустые и очищаем от пробелов
    result = [s.strip() for s in sentences if s.strip()]
    
    # Если не нашлось предложений (например, весь текст без знаков препинания),
    # разбиваем по переносам строк или по длине
    if not result or (len(result) == 1 and len(text) > 500):
        # Пробуем разбить по переносам строк
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        if len(lines) > 1:
            return lines
        
        # Если все еще одно предложение и текст длинный, разбиваем по длине
        # Для японского/китайского без пробелов используем меньший размер чанка
        chunk_size = 150 if has_cjk else 200  # Меньший размер для CJK языков
        
        if len(text) > chunk_size:
            # Тот же word-safe splitter, что использует long-scene contract:
            # обычную латиницу/кириллицу режем только по whitespace, а жёсткий
            # codepoint-cut остаётся последним fallback для единственного токена.
            chunks = _split_long_sentence(text, max_chars=chunk_size)
            return chunks if chunks else [text]
    
    return result


# ── Лимит одиночной сцены ДО Whisper. Точных секунд на этом этапе ещё нет, поэтому
# используем консервативную оценку по production: 12.5 символа/с. На 522 готовых VGC-аудио
# медиана = 13.56; у медленных массовых языков 10-й перцентиль ≈11.8–12.4. Дефолт
# 6 с × 12.5 = 75 символов даёт примерно 6 секунд и не выдаёт оценку за точный таймкод.
DEFAULT_MAX_SOLO_SCENE_SECONDS = 6.0
SPEECH_CHARS_PER_SEC = 12.5
MAX_SOLO_SCENE_CHARS = round(DEFAULT_MAX_SOLO_SCENE_SECONDS * SPEECH_CHARS_PER_SEC)  # 75
LEGACY_SPLIT_LONG_ALGORITHM_VERSION = 2
SPLIT_LONG_ALGORITHM_VERSION = 4  # v4: hard cadence units; semantic cuts only inside solo sentences

# Совместимость с внешними импортами старых имён; реальный алгоритм использует новые константы.
MAX_SCENE_SECONDS = DEFAULT_MAX_SOLO_SCENE_SECONDS
MAX_SCENE_CHARS = MAX_SOLO_SCENE_CHARS


def _validate_solo_scene_seconds(value) -> float:
    """CLI/config validation: разумный положительный лимит, без тихого clamp/default."""
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("лимит длинной сцены должен быть числом секунд") from exc
    if not math.isfinite(seconds) or not 1.0 <= seconds <= 60.0:
        raise ValueError("лимит длинной сцены должен быть от 1 до 60 секунд")
    return seconds


def _solo_scene_seconds_arg(value) -> float:
    try:
        return _validate_solo_scene_seconds(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _solo_scene_chars(seconds=DEFAULT_MAX_SOLO_SCENE_SECONDS) -> int:
    return max(1, round(_validate_solo_scene_seconds(seconds) * SPEECH_CHARS_PER_SEC))


def _split_long_sentence(sentence: str, max_chars: int = MAX_SOLO_SCENE_CHARS) -> List[str]:
    """Балансированно режет текст на непустые куски ``<= max_chars``.

    Число кусков минимально возможное по длине. Для каждого разреза сначала ищется
    смысловая пауза (запятая/точка с запятой/двоеточие/тире) рядом с равновесной
    границей, затем ближайшая граница слов. Если одной границы слов физически нет
    (URL/артикул/испорченный run-on), остаётся безопасный посимвольный разрез.
    """
    try:
        cap = int(max_chars)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_chars должен быть положительным целым") from exc
    if cap < 1:
        raise ValueError("max_chars должен быть положительным целым")
    s = re.sub(r'\s+', ' ', sentence or '').strip()
    if not s:
        return []
    if len(s) <= cap:
        return [s]

    # Пробел между сценами не принадлежит ни одному text_segment, поэтому иногда два
    # куска по cap вмещают строку длиной 2*cap+1. Начинаем с этого нижнего предела;
    # если подходящих границ нет, увеличиваем число частей до безопасного. Выбор
    # границ делаем динамически: локально удачная запятая не должна оставить хвост
    # cap+1 (такое реально встречается в production-текстах).
    min_parts = max(2, math.ceil((len(s) + 1) / (cap + 1)))
    max_parts = max(min_parts, math.ceil(len(s) / cap))
    for total_parts in range(min_parts, max_parts + 1):
        # start → ((число посимвольных разрезов, штраф качества, макс. дисбаланс), куски).
        # Посимвольный разрез — строго последний fallback. Внутри путей без него
        # смысловая пунктуация получает преимущество примерно в пределах 35% от
        # равновесной цели, после чего более ровная граница слова честно выигрывает.
        layer = {0: ((0, 0.0, 0.0), tuple())}
        for part_index in range(total_parts):
            parts_after = total_parts - part_index - 1
            next_layer = {}
            for start, (score, pieces) in layer.items():
                target = (len(s) - start) / (parts_after + 1)
                if parts_after == 0:
                    piece = s[start:]
                    if 0 < len(piece) <= cap:
                        deviation = abs(len(piece) - target)
                        candidate = ((score[0], score[1] + deviation ** 2,
                                      max(score[2], deviation)), pieces + (piece,))
                        current = next_layer.get(len(s))
                        if current is None or candidate[0] < current[0]:
                            next_layer[len(s)] = candidate
                    continue

                # end — exclusive. Если сразу после него пробел, он является
                # межсценовой границей и не попадает ни в один text_segment.
                max_end = min(len(s) - 1, start + cap)
                for end in range(start + 1, max_end + 1):
                    at_word_boundary = s[end].isspace()
                    next_start = end + 1 if at_word_boundary else end
                    if next_start >= len(s):
                        continue
                    raw_left = len(s) - next_start
                    # Не строим заведомо невозможные ветки. Будущие части вмещают
                    # по cap символов и максимум по одному удаляемому пробелу между ними.
                    if raw_left < parts_after or raw_left > parts_after * cap + parts_after - 1:
                        continue
                    piece = s[start:end]
                    if not piece or len(piece) > cap:
                        continue

                    is_clause = piece[-1] in ',;:—–，；：、،'
                    is_char_cut = not is_clause and not at_word_boundary
                    deviation = abs(len(piece) - target)
                    quality = deviation ** 2
                    if not is_clause and at_word_boundary:
                        quality += (target * 0.35) ** 2
                    candidate_score = (
                        score[0] + int(is_char_cut),
                        score[1] + quality,
                        max(score[2], deviation),
                    )
                    candidate = (candidate_score, pieces + (piece,))
                    current = next_layer.get(next_start)
                    if current is None or candidate_score < current[0]:
                        next_layer[next_start] = candidate
            layer = next_layer
            if not layer:
                break

        result = layer.get(len(s))
        if result and len(result[1]) == total_parts:
            return list(result[1])

    raise RuntimeError("внутренняя ошибка дробления: не удалось соблюсти лимит")


def _group_split_solo(sentences: List[str], head_step: int, boundary: int,
                      tail_step: int, max_chars: int = MAX_SOLO_SCENE_CHARS) -> List[str]:
    """Группирует предложения в сцены, сохраняя ритм ``1x40,3``.

    Только в позициях ``step == 1`` действует абсолютный детерминированный лимит;
    длинная фраза становится несколькими сценами. Группы ``step > 1`` намеренно не
    трогаются: галка отвечает именно за режим «одно предложение = один кадр».
    """
    n = len(sentences)
    if n == 0:
        return []
    cap = int(max_chars)
    if cap < 1:
        raise ValueError("max_chars должен быть положительным целым")
    out, i, cut, added = [], 0, 0, 0
    while i < n:
        in_head = i < boundary
        step = max(1, head_step if in_head else tail_step)
        stop = min(n, i + step)
        if in_head:
            # Boundary в ``2x5,3`` означает ровно первые пять предложений:
            # последняя неполная head-группа не имеет права украсть шестое.
            stop = min(stop, boundary)
        if step == 1:
            s = sentences[i]
            if len(re.sub(r'\s+', ' ', s or '').strip()) > cap:
                pieces = _split_long_sentence(s, cap)
                out.extend(pieces)
                cut += 1
                added += max(0, len(pieces) - 1)
            else:
                out.append(s)
            i = stop
        else:
            out.append(' '.join(sentences[i:stop]))
            i = stop
    if cut:
        print_debug(f"По одному кадру: разрезано {cut} длинных предложений, добавлено "
                    f"{added} сцен (абсолютный лимит {cap} симв ≈ "
                    f"{cap / SPEECH_CHARS_PER_SEC:g} с до Whisper)")
    return out


def group_sentences(sentences: List[str], group_size: int) -> List[str]:
    """Группирует по X штук; при X==1 применяет абсолютный лимит одиночной сцены."""
    return _group_split_solo(sentences, group_size, 0, group_size)


def generate_scenes_with_full_context(text: str, sentences_per_prompt: int = 3) -> List[dict]:
    """
    Генерирует сцены из текста, разбивая на группы по X предложений.
    Для каждой группы передает полный контекст текста в GPT для понимания контекста.
    Возвращает список объектов сцен с полями: text_segment, visual_description, camera_angle, subject_focus
    """
    # Разбиваем текст на предложения
    sentences = split_text_to_sentences(text)
    if not sentences:
        print_user("⚠️ Не найдено предложений в тексте", "WARNING")
        return []
    
    # Группируем предложения
    groups = []
    for i in range(0, len(sentences), sentences_per_prompt):
        group = sentences[i:i + sentences_per_prompt]
        groups.append(' '.join(group))
    
    print_user(f"📋 Разбито на {len(groups)} групп по {sentences_per_prompt} предложений", "INFO")
    print_debug(f"Полный текст: {len(text)} символов, {len(sentences)} предложений")
    
    # Генерируем сцены для каждой группы с полным контекстом
    scenes = []
    
    system_prompt = """You are a visionary Film Director. For the given text segment, create a detailed visual description for image generation.
You have access to the FULL SCRIPT CONTEXT to understand the story, characters, and atmosphere.

OUTPUT FORMAT (JSON):
{
  "text_segment": "The exact text segment you are analyzing...",
  "visual_description": "Detailed visual description of the action/environment. Focus on lighting, atmosphere, colors, and visual details. Use the full script context to understand character appearance, setting, and mood.",
  "camera_angle": "Select ONE: Wide Shot / Extreme Close-up / Low Angle / Overhead / Dutch Angle / Over-the-shoulder / Medium Shot / Bird's eye view",
  "subject_focus": "Main Hero / Environment / Object / Crowd"
}

IMPORTANT:
- Use the FULL SCRIPT CONTEXT to understand character descriptions, settings, and story flow
- Each scene must be visually distinct and cinematic
- Camera angles should vary for visual interest
- Focus on what can be SEEN, not thoughts or feelings
- CRITICAL: NEVER mention text, words, letters, numbers, statistics, charts, graphs, signs, labels, captions, subtitles, or any written content in visual_description. Images must be completely text-free.
- CRITICAL: Character appearances MUST match their nationality/ethnicity mentioned in the script. If Japanese, describe Japanese features; if Russian, describe Russian features, etc.
- CRITICAL: NEVER mention names of famous people, celebrities, historical figures, or real persons. Describe generic characters only, matching the nationality/ethnicity from the script.
- Return ONLY valid JSON object, no markdown, no code blocks"""
    
    headers = _openai_chat_headers()
    
    # Генерируем сцены параллельно
    with ThreadPoolExecutor(max_workers=min(GPT_PARALLEL_THREADS, len(groups))) as executor:
        futures = {}
        for i, group_text in enumerate(groups):
            # Создаем промпт с полным контекстом
            user_prompt = f"""FULL SCRIPT CONTEXT (for understanding the story, characters, and atmosphere):
{text}

---
CURRENT TEXT SEGMENT (for which you need to generate visual description):
{group_text}

Generate a visual scene description for the CURRENT TEXT SEGMENT, using the FULL SCRIPT CONTEXT to understand the story, characters, and visual style."""

            payload = {
                "model": GPT_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "max_tokens": 500,
                "temperature": 0.7
            }

            future = executor.submit(_generate_single_scene, headers, payload, group_text, i)
            futures[future] = i
        
        # Собираем результаты
        for future in as_completed(futures):
            try:
                scene = future.result()
                if scene:
                    scenes.append(scene)
            except Exception as e:
                print_debug(f"Ошибка генерации сцены: {e}")
                import traceback
                print_debug(traceback.format_exc())
                # Fallback для этой группы
                index = futures[future]
                scenes.append({
                    "text_segment": groups[index],
                    "visual_description": groups[index],
                    "camera_angle": "Wide Shot",
                    "subject_focus": "Environment"
                })
    
    # Сортируем сцены по порядку групп (сохраняем порядок генерации)
    # Создаем словарь для быстрого поиска индекса
    group_to_index = {group: i for i, group in enumerate(groups)}
    scenes.sort(key=lambda x: group_to_index.get(x.get('text_segment', ''), 999))
    
    if scenes:
        print_user(f"✅ Сгенерировано {len(scenes)} сцен с полным контекстом", "SUCCESS")
    else:
        print_user("⚠️ Не удалось сгенерировать сцены, используем fallback", "WARNING")
        # Fallback: создаем простые сцены из групп
        scenes = []
        for group_text in groups:
            scenes.append({
                "text_segment": group_text,
                "visual_description": group_text,
                "camera_angle": "Wide Shot",
                "subject_focus": "Environment"
            })
    
    return scenes


def _generate_single_scene(headers: dict, payload: dict, group_text: str, index: int) -> Optional[dict]:
    """Вспомогательная функция для генерации одной сцены"""
    for attempt in range(MAX_RETRIES):
        try:
            response = _llm_post(
                OPENAI_API_URL,
                headers=headers,
                json=payload,
                timeout=GPT_CHAT_TIMEOUT
            )

            _raise_if_openai_auth_failed(response)  # 401/403/invalid_api_key → фатально (до деградации)

            if response.status_code == 429:
                wait_time = RETRY_DELAY * (attempt + 1)
                print_debug(f"[{index}] Rate limit, ждем {wait_time}с...")
                time.sleep(wait_time)
                continue

            response.raise_for_status()

            try:
                result = response.json()
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                print_debug(f"[{index}] Ошибка декодирования JSON: {e}")
                text_response = response.content.decode('utf-8', errors='replace')
                result = json.loads(text_response)

            content = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
            
            # Очищаем от markdown code blocks если есть
            if content.startswith('```'):
                lines = content.split('\n')
                content = '\n'.join(lines[1:-1]) if len(lines) > 2 else content
            
            # Парсим JSON
            try:
                scene = json.loads(content)
                if isinstance(scene, dict) and all(key in scene for key in ['text_segment', 'visual_description', 'camera_angle', 'subject_focus']):
                    # Убеждаемся что text_segment правильный
                    scene['text_segment'] = group_text
                    return scene
                else:
                    raise ValueError("Invalid scene structure")
            except (json.JSONDecodeError, ValueError) as e:
                print_debug(f"[{index}] Ошибка парсинга JSON: {e}")
                print_debug(f"[{index}] Ответ GPT: {content[:200]}")
                if attempt < MAX_RETRIES - 1:
                    continue
                return None
                
        except requests.exceptions.RequestException as e:
            print_debug(f"[{index}] Попытка {attempt + 1}/{MAX_RETRIES} - Ошибка: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    
    return None


def _internal_generate_smart_scene_plan(
    text: str,
    style: str = "cinematic, photorealistic",
    canon: Optional[dict] = None,
) -> List[dict]:
    """Smart Cinematic Mode без fallback/degradation.

    Модель выбирает семантические границы, но каждая сцена обязана содержать
    дословный последовательный фрагмент исходника. Полноту и offsets считает код,
    а не модель; любое пропущенное содержание блокирует публикацию всего плана.
    """
    print_user("🎬 Smart Mode: Семантический анализ текста...", "INFO")

    system_prompt = """You are an expert Film Director and Story Analyst. Split the script into visual scenes based on NARRATIVE SHIFTS, not arbitrary sentence counts.

CRITICAL RULES:
1. text_segment MUST be copied VERBATIM from the script. Never paraphrase it.
2. In original order, text_segment values MUST cover the ENTIRE script once: no omitted words, overlaps, reordering, or invented text. Whitespace between adjacent segments may be left outside; the application assigns it deterministically.
3. Every scene is a complete visual moment caused by a location/time/character/action/mood shift.
4. subject is the one concrete dominant subject of the frame, as a specific English noun phrase.
5. visual_prompt describes action, subject, composition, environment, and (when needed) where a sign/title/document appears. It MUST NOT contain or quote the literal wording; every allowed exact string belongs only in visible_text.
6. visible_text is an array of exact final strings to render, or [] when the image needs no writing. For viewer-facing/editorial labels, dates, headings, or diagram annotations, use the language and script of THIS narration segment. For diegetic writing physically belonging to the depicted world (street/store signs, newspapers, letters, historical documents), use the authentic local language and script of that place and era, even when narration uses another language. Preserve proper names naturally. Never translate or transliterate authentic diegetic copy merely to match narration. Keep it short and never invent decorative copy.
7. camera_angle is exactly one allowed enum value; subject_type is character, environment, or object.
8. includes_main_character is true only when the protagonist is visibly present.
9. Keep named real/historical figures by exact name with era-accurate appearance. Do not genericize them.
10. Return only the requested structured JSON."""

    canon_names = _canon_names_from_bible(canon)
    if canon_names:
        known_entities = []
        for entity in (
            list((canon or {}).get("characters") or [])
            + list((canon or {}).get("locations") or [])
        ):
            if not isinstance(entity, dict) or not entity.get("names"):
                continue
            primary = str(entity["names"][0])
            aliases = [
                str(name) for name in entity.get("names", [])[1:]
                if str(name).strip()
            ]
            description = str(entity.get("appearance") or "").strip()
            line = primary
            if aliases:
                line += f" (aliases: {', '.join(aliases)})"
            if description:
                line += f" — {description}"
            known_entities.append(line)
        system_prompt += (
            "\n11. entities is mandatory. For every known character or location "
            "visible in a scene, return its exact canonical name from KNOWN ENTITIES; "
            "resolve aliases and pronouns yourself. Return [] only when none appears.\n"
            "KNOWN ENTITIES:\n- " + "\n- ".join(known_entities)
        )

    user_prompt = f"""FULL SCRIPT CONTEXT:
{text}

VISUAL STYLE: "{style}"

Split the complete script into semantic visual scenes. Copy every text_segment verbatim and cover the complete script exactly once. The style is applied separately; do not repeat it in visual_prompt."""
    if canon_names:
        user_prompt += (
            "\nFor every scene, fill entities with exact canonical names from "
            "KNOWN ENTITIES for all known characters/locations present."
        )

    payload = {
        "model": SCENE_GPT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 8000,
        "temperature": 0.4,
        "response_format": _smart_scene_response_format(canon_names),
    }

    result = _scene_llm_request(payload, GPT_CHAT_TIMEOUT, "Smart Mode")
    data = _scene_content_json(result, "Smart Mode")
    scenes = data.get("scenes") if isinstance(data, dict) else None
    checked = _validate_smart_scene_plan(
        scenes, text, require_visible_text=True, canon_names=canon_names
    )
    print_user(f"✅ Smart Mode: Создан полный план из {len(checked)} семантических сцен", "SUCCESS")
    return checked


# ============ ДИСК-КЭШ ОТВЕТОВ ПЛАНА СЦЕН (content-addressed) ============
# Один и тот же чанк+стиль → одна и та же сцена. При re-run проекта (или одинаковом
# тексте в разных проектах) GPT-вызов пропускается, сцена берётся с диска мгновенно.
# Папка глобальная (tempdir), НЕ привязана к project_dir.

_PROMPT_CACHE_VERSION = "v13"  # v13: contextual exact visible_text contract.
_PLANNING_ARTIFACT_CACHE_VERSION = "v2"  # v2: complete story analysis, no truncation.

# ── Общий HTTP-пул для GPT-вызовов ────────────────────────────────────────────
# Без пула urllib3 держит pool_maxsize=10 → 200 потоков душатся до ~10 реальных
# соединений + TLS-хендшейк на каждый вызов (кэш-промахи, медленно). Сессия с
# pool_maxsize по числу потоков = keep-alive + реальная параллельность.
GPT_CHAT_TIMEOUT = (5, 60)  # (connect, read) сек для ЧАТ-вызовов GPT — НЕ 200с image-пути
# 21.09.2026. Альтернативный OpenAI-совместимый канал ходит не в сам OpenAI, а в провайдера
# поверх сохранённых веб-аккаунтов: даже однословный ответ там идёт ~10-12 с, а план сцен в
# наши 60 с на чтение не укладывается вовсе — проверено живым вызовом (ReadTimeout).
# Это ровно та болезнь, из-за которой 03.09 подняли таймауты картинок: наш собственный
# таймер снимает вызов, который провайдер ЕЩЁ ВЫПОЛНЯЕТ, и оплаченный ответ выбрасывается.
# Поднимаем ТОЛЬКО для этого канала и только вверх: у openai/codex/claude свои сроки, и
# заданный вызывающим больший таймаут не урезается.
ALT_CHAT_READ_TIMEOUT = int(os.environ.get("VS_ALT_CHAT_READ_TIMEOUT") or 240)
try:
    from requests.adapters import HTTPAdapter as _HTTPAdapter
    _GPT_SESSION = requests.Session()
    _gpt_pool = max(16, GPT_SCENE_PARALLEL_THREADS)
    _gpt_adapter = _HTTPAdapter(pool_connections=_gpt_pool, pool_maxsize=_gpt_pool, max_retries=0)
    _GPT_SESSION.mount("https://", _gpt_adapter)
    _GPT_SESSION.mount("http://", _gpt_adapter)
except Exception as _e:
    _GPT_SESSION = requests  # фолбэк на голый модуль


# Учёт расхода LLM за прогон. Вопрос «куда уходит подписка» до сих пор решался догадками:
# CLI отдаёт usage в ResultMessage, но он никуда не доезжал. Теперь суммируем в одной точке —
# через _llm_post идут ВСЕ чат-вызовы (сцены, субъект, bible, смягчение, главы).
_LLM_USAGE = {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cost": 0.0}
_LLM_USAGE_LOCK = threading.Lock()
# Story analysis, semantic partitioning and Visual Bible may run concurrently.
# One process-wide gate keeps their aggregate live LLM calls inside the same
# server-configured cap used by phase 2, including nested per-chunk executors.
_LLM_CONCURRENCY_GATE = threading.BoundedSemaphore(
    max(1, GPT_SCENE_PARALLEL_THREADS)
)


def _note_llm_usage(response):
    try:
        if getattr(response, "status_code", 0) != 200:
            return
        u = (response.json() or {}).get("usage") or {}
        if not isinstance(u, dict):
            return
        with _LLM_USAGE_LOCK:
            _LLM_USAGE["calls"] += 1
            _LLM_USAGE["input"] += int(u.get("input_tokens") or u.get("prompt_tokens") or 0)
            _LLM_USAGE["output"] += int(u.get("output_tokens") or u.get("completion_tokens") or 0)
            _LLM_USAGE["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
            _LLM_USAGE["cost"] += float(u.get("cost_usd") or 0.0)
    except Exception:
        pass          # учёт не имеет права ломать генерацию


def print_llm_usage_summary(where: str = ""):
    """Итог расхода за прогон — чтобы решения про пакетирование/сессии опирались на цифры."""
    with _LLM_USAGE_LOCK:
        d = dict(_LLM_USAGE)
    if not d["calls"]:
        return
    per = d["input"] / max(1, d["calls"])
    msg = (f"📊 LLM за прогон{(' (' + where + ')') if where else ''}: вызовов {d['calls']}, "
           f"вход {d['input']:,} токенов (в среднем {per:,.0f} на вызов), выход {d['output']:,}")
    if d["cache_read"]:
        msg += f", из кэша префикса {d['cache_read']:,}"
    if d["cost"]:
        msg += f", стоимость ${d['cost']:.2f}"
    print_user(msg.replace(",", " "), "INFO")


def _alt_shape_json(resp):
    """Ответ альт-канала → строгий JSON в content, если модель прислала текст вокруг.

    Возвращает объект с тем же интерфейсом, что requests.Response (.status_code/.json()),
    поэтому все 10 мест разбора ответов остаются нетронутыми.
    """
    if getattr(resp, "status_code", 0) != 200:
        return resp
    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception:                                          # noqa: BLE001 — не наш формат, отдаём как есть
        return resp
    if not isinstance(content, str) or not content.strip():
        return resp
    try:
        _json.loads(content)
        return resp                                            # уже валидный JSON — не трогаем
    except Exception:                                          # noqa: BLE001
        pass
    try:
        extracted = prompt_llm._extract_json(content)
    except Exception:                                          # noqa: BLE001
        return resp
    if not extracted or extracted == content:
        return resp
    data["choices"][0]["message"]["content"] = extracted
    return _AltResponse(resp.status_code, data)


class _AltResponse:
    """requests.Response-совместимая обёртка с исправленным content."""

    def __init__(self, status_code, data):
        self.status_code = int(status_code)
        self._data = data
        self.text = _json.dumps(data, ensure_ascii=False)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _alt_timeout(timeout):
    """Срок чтения для альт-канала: не короче ALT_CHAT_READ_TIMEOUT, но и не короче заданного."""
    t = timeout or GPT_CHAT_TIMEOUT
    conn, read = t if isinstance(t, tuple) else (5, t)
    return (conn, max(read, ALT_CHAT_READ_TIMEOUT))


def _llm_post_ungated(url, headers=None, json=None, timeout=None, **kwargs):
    """Транспорт ЧАТ-вызовов (написание промптов). Сигнатура = requests.post, чтобы места вызова
    не переписывались: меняется только канал доставки, payload и разбор ответа те же.

      openai (дефолт) → тот же _GPT_SESSION.post на api.openai.com, что и раньше;
      codex / claude  → подписка (prompt_llm), ответ приходит в OpenAI-совместимой форме.
    """
    if (os.environ.get("VS_DISABLE_PROMPT_LLM", "").strip().lower()
            in ("1", "true", "yes", "on")):
        raise OpenAIKeyError(
            "LLM-вызов запрещён для этой server-owned задачи: используйте готовый план"
        )
    payload = json or {}
    # Счёт канала уже признан исчерпанным в этом прогоне — не ходим впустую (иначе каждая
    # из сотен сцен снова упиралась бы в свои 20 ретраев).
    _why = _llm_exhausted_reason()
    if _why:
        raise OpenAIKeyError(
            f"У канала промптов «{_prompt_provider()}» исчерпан счёт/лимит: {_why[:200]} — "
            "останавливаюсь. Пополни счёт или выбери другой канал (Ключи → Чем писать промпты)."
        )

    def _openai_call():
        return _GPT_SESSION.post(url, headers=(headers or _openai_chat_headers()), json=payload,
                                 timeout=(timeout or GPT_CHAT_TIMEOUT), **kwargs)

    def _alt_llm_call():
        """Альтернативный Claude API (сторонний OpenAI-совместимый прокси).

        Своя база и ключ; модель подменяем — воркер шлёт gpt-4.1-*, которых там нет.

        Прокси уважает response_format НЕ на всех моделях (проверено: sonnet-4-6 да,
        sonnet-5 нет — возвращает свободный текст). Поэтому схему дополнительно
        передаём текстовой директивой, а из ответа достаём первый JSON-объект — тот же
        приём, что для подписок. Если прокси схему уважает, извлечение ничего не меняет.
        """
        base = prompt_llm.alt_base()
        body = dict(payload)
        body["model"] = prompt_llm.alt_model()
        rf = body.get("response_format")
        if rf:
            try:
                body["messages"] = prompt_llm._with_directive(
                    list(body.get("messages") or []), prompt_llm._json_directive(rf))
            except Exception:                                  # noqa: BLE001 — директива не критична
                pass
        resp = _GPT_SESSION.post(
            base + "/chat/completions",
            headers={"Authorization": "Bearer " + prompt_llm.alt_key(),
                     "Content-Type": "application/json"},
            json=body,
            timeout=_alt_timeout(timeout),
        )
        return _alt_shape_json(resp) if rf else resp

    def _gated(resp):
        """Один шлюз на ВСЕ чат-вызовы: исчерпание счёта → фатально, успех → снимает заявку."""
        _raise_if_llm_credits_exhausted(resp)
        if getattr(resp, "status_code", 0) == 200:
            _note_llm_chat_ok()
            _note_llm_usage(resp)          # копим расход прогона в одной точке
        return resp

    if prompt_llm is None or not payload.get("messages"):
        return _gated(_openai_call())
    if _prompt_provider() == "openai" and not prompt_llm.fallback_enabled():
        return _gated(_openai_call())
    if (_prompt_provider() == "altclaude" and prompt_llm.alt_configured()
            and not prompt_llm.fallback_enabled()):
        return _gated(_alt_llm_call())
    # Цепочка каналов (выбранный → codex → claude → openai): если один не работает (лимит/нет
    # авторизации), промпты дописывает следующий, а не деградируют в мусор. OpenAI-звено — этот же
    # запрос тем же ключом. Подписке нужен свой (больший) таймаут — берётся из VS_SUB_TIMEOUT.
    return _gated(prompt_llm.post(
        payload,
        openai_call=(_openai_call if _has_openai_key() else None),
        alt_call=(_alt_llm_call if prompt_llm.alt_configured() else None),
    ))


def _llm_post(url, headers=None, json=None, timeout=None, **kwargs):
    """Aggregate prompt-call concurrency gate shared by every planning phase."""
    with _LLM_CONCURRENCY_GATE:
        return _llm_post_ungated(
            url, headers=headers, json=json, timeout=timeout, **kwargs
        )

# ── Structured Outputs: схема сцены ────────────────────────────────────────────
# API ГАРАНТИРУЕТ валидный JSON по этой схеме (strict) → исчезают парс-ошибки,
# ретраи на «Expecting ',' delimiter», деградация в сырой текст и разбухание счёта
# сцен. enum на camera_angle/subject_type не даёт модели вернуть мусорное значение.
_SCENE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "scene",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "subject", "visual_prompt", "camera_angle", "subject_type", "visible_text",
            ],
            "properties": {
                "subject": {"type": "string"},
                "visual_prompt": {"type": "string"},
                "camera_angle": {"type": "string", "enum": [
                    "Wide Shot", "Medium Shot", "Close-up", "Low Angle",
                    "Over-the-shoulder", "Overhead", "Dutch Angle"]},
                "subject_type": {"type": "string", "enum": ["character", "environment", "object"]},
                "visible_text": {
                    "type": "array", "maxItems": 3,
                    "items": {"type": "string", "minLength": 1, "maxLength": 120},
                },
            },
        },
    },
}


def _scene_array_response_format(canon_names, n: int):
    """Та же схема сцены, но ответ — массив ровно из n сцен (пакетный запрос).

    Замер 06.08 на боевом проекте (12 сегментов, канал claude): пакет по 6 стоит 49% от
    поштучного и идёт втрое быстрее — префикс читается один раз, а не на каждый вызов.
    strict-режим требует объект в корне, поэтому массив кладём в поле scenes.
    """
    item = _scene_response_format(canon_names)["json_schema"]["schema"]
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "scene_batch",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["scenes"],
                "properties": {
                    "scenes": {"type": "array", "minItems": n, "maxItems": n, "items": item},
                },
            },
        },
    }


def _scene_response_format(canon_names):
    """strict-схема сцены. Если есть канон — добавляет поле entities с ENUM из канонических имён:
    GPT физически может вернуть ТОЛЬКО точные несклонённые имена из списка (решает склонение/кореференс
    на своей стороне, а матчинг у нас — точное сравнение). Пусто → обычная схема без entities."""
    if not canon_names:
        return _SCENE_RESPONSE_FORMAT
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "scene",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "subject", "visual_prompt", "camera_angle", "subject_type",
                    "visible_text", "entities",
                ],
                "properties": {
                    "subject": {"type": "string"},
                    "visual_prompt": {"type": "string"},
                    "camera_angle": {"type": "string", "enum": [
                        "Wide Shot", "Medium Shot", "Close-up", "Low Angle",
                        "Over-the-shoulder", "Overhead", "Dutch Angle"]},
                    "subject_type": {"type": "string", "enum": ["character", "environment", "object"]},
                    "visible_text": {
                        "type": "array", "maxItems": 3,
                        "items": {"type": "string", "minLength": 1, "maxLength": 120},
                    },
                    "entities": {"type": "array", "items": {"type": "string", "enum": list(canon_names)}},
                },
            },
        },
    }


def _smart_scene_response_format(canon_names: Optional[List[str]] = None):
    """Strict schema Smart Mode; offsets вычисляет приложение после coverage-check."""
    required = [
        "subject", "text_segment", "visual_prompt",
        "camera_angle", "subject_type", "visible_text",
        "includes_main_character",
    ]
    properties = {
        "subject": {"type": "string"},
        "text_segment": {"type": "string"},
        "visual_prompt": {"type": "string"},
        "camera_angle": {"type": "string", "enum": [
            "Wide Shot", "Medium Shot", "Close-up", "Low Angle",
            "Over-the-shoulder", "Overhead", "Dutch Angle",
        ]},
        "subject_type": {"type": "string", "enum": [
            "character", "environment", "object",
        ]},
        "visible_text": {
            "type": "array", "maxItems": 3,
            "items": {"type": "string", "minLength": 1, "maxLength": 120},
        },
        "includes_main_character": {"type": "boolean"},
    }
    if canon_names:
        required.append("entities")
        properties["entities"] = {
            "type": "array",
            "items": {"type": "string", "enum": list(canon_names)},
        }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "smart_scene_plan",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["scenes"],
                "properties": {
                    "scenes": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": required,
                            "properties": properties,
                        },
                    },
                },
            },
        },
    }


_SEMANTIC_VISUAL_MODES = (
    "literal_action",
    "object_in_context",
    "process",
    "cause_effect",
    "comparison",
    "historical_artifact",
    "environment",
    "metaphor",
)
_SEMANTIC_BLOCK_TARGET_CHARS = 2200
_SEMANTIC_BLOCK_MAX_CHARS = 3200
_PRECOMPUTE_MISSING = object()
_SEMANTIC_CONTRACT_KEYS = (
    "sentence_index", "sentence_end_index", "sentence_count",
    "sentence_start", "sentence_end", "cadence_unit_index",
    "beat_index", "beat_count",
)
_SEMANTIC_SCENE_KEYS = (
    "semantic_claim", "visual_mode", "required_elements",
    "continuity_group", "continuity_relation",
    "character_start", "character_end",
) + _SEMANTIC_CONTRACT_KEYS


def _normalise_source_with_raw_map(source: str) -> Tuple[str, List[int]]:
    """Whitespace-normalised text plus a character -> raw-source index map.

    ``split_text_to_sentences`` intentionally normalises subtitle newlines.  Semantic
    v4 still needs byte-for-byte source offsets, so sentence discovery happens in the
    normalised view and every boundary is projected back onto the untouched source.
    """
    raw = str(source or "")
    chars: List[str] = []
    raw_indices: List[int] = []
    index = 0
    while index < len(raw):
        if raw[index].isspace():
            run_start = index
            while index < len(raw) and raw[index].isspace():
                index += 1
            if chars:
                chars.append(" ")
                raw_indices.append(run_start)
            continue
        chars.append(raw[index])
        raw_indices.append(index)
        index += 1
    if chars and chars[-1] == " ":
        chars.pop()
        raw_indices.pop()
    return "".join(chars), raw_indices


def _sentence_source_units(source: str) -> List[dict]:
    """Return a contiguous raw-source partition owned by detected sentences.

    Separator whitespace is attached to the following sentence (and trailing
    whitespace to the final one).  This mirrors the existing semantic coverage
    contract while making sentence ownership explicit and deterministic.
    """
    raw = str(source or "")
    if not raw or not raw.strip():
        return []
    normalised, raw_map = _normalise_source_with_raw_map(raw)
    sentence_texts = [
        re.sub(r"\s+", " ", item or "").strip()
        for item in split_text_to_sentences(raw)
        if str(item or "").strip()
    ]
    if not normalised or not sentence_texts:
        return []

    norm_cursor = 0
    raw_cursor = 0
    units = []
    for sentence_index, sentence in enumerate(sentence_texts):
        start_norm = normalised.find(sentence, norm_cursor)
        if start_norm < 0 or normalised[norm_cursor:start_norm].strip():
            raise ScenePlanTerminalError(
                f"semantic v4: предложение {sentence_index + 1} не найдено в исходнике"
            )
        end_norm = start_norm + len(sentence)
        if end_norm <= start_norm or end_norm > len(raw_map):
            raise ScenePlanTerminalError(
                f"semantic v4: некорректная граница предложения {sentence_index + 1}"
            )
        raw_end = raw_map[end_norm - 1] + 1
        if raw_end <= raw_cursor:
            raise ScenePlanTerminalError(
                f"semantic v4: предложения {sentence_index + 1} перекрываются"
            )
        units.append({
            "sentence_index": sentence_index,
            "start": raw_cursor,
            "end": raw_end,
            "text": raw[raw_cursor:raw_end],
        })
        norm_cursor = end_norm
        raw_cursor = raw_end

    if normalised[norm_cursor:].strip():
        raise ScenePlanTerminalError("semantic v4: предложения не покрывают хвост исходника")
    if raw_cursor < len(raw):
        units[-1]["end"] = len(raw)
        units[-1]["text"] = raw[units[-1]["start"]:]
    if "".join(unit["text"] for unit in units) != raw:
        raise ScenePlanTerminalError("semantic v4: sentence units не покрывают исходник")
    return units


def _is_cjk_char(char: str) -> bool:
    return bool(char and re.match(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", char
    ))


def _semantic_boundary_inside_word(source: str, position: int) -> bool:
    if position <= 0 or position >= len(source):
        return False
    left, right = source[position - 1], source[position]
    return bool(
        left.isalnum() and right.isalnum()
        and not (_is_cjk_char(left) or _is_cjk_char(right))
    )


def _semantic_atoms(source: str, start: int, end: int) -> List[Tuple[int, int]]:
    """Small deterministic ranges whose indexes are safe for an LLM to return."""
    segment = source[start:end]
    nonspace = list(re.finditer(r"\S+", segment))
    if not nonspace:
        return []
    # Space-delimited languages use word atoms.  CJK narration commonly has no
    # spaces, so codepoint atoms retain usable semantic cut positions there.
    if len(nonspace) == 1 and any(_is_cjk_char(ch) for ch in segment):
        boundaries = [i + 1 for i, ch in enumerate(segment) if not ch.isspace()]
    else:
        boundaries = [match.end() for match in nonspace]
    atoms = []
    cursor = 0
    for atom_end in boundaries:
        if atom_end > cursor:
            atoms.append((start + cursor, start + atom_end))
            cursor = atom_end
        while cursor < len(segment) and segment[cursor].isspace():
            cursor += 1
        if atoms and cursor > atoms[-1][1] - start:
            atoms[-1] = (atoms[-1][0], start + cursor)
    if cursor < len(segment):
        atoms.append((start + cursor, end))
    elif atoms and atoms[-1][1] < end:
        atoms[-1] = (atoms[-1][0], end)
    if not atoms or atoms[0][0] != start or atoms[-1][1] != end:
        raise ScenePlanTerminalError("semantic v4: atoms не покрывают cadence unit")
    for left, right in zip(atoms, atoms[1:]):
        if left[1] != right[0]:
            raise ScenePlanTerminalError("semantic v4: разрыв между atoms")
    return atoms


def _semantic_cadence_units(source: str, sentences_per_prompt="1",
                            split_long: bool = True,
                            preferred_max_seconds: float = DEFAULT_MAX_SOLO_SCENE_SECONDS
                            ) -> List[dict]:
    """Hard outer contract: cadence groups are immutable LLM ownership units."""
    sentences = _sentence_source_units(source)
    if not sentences:
        return []
    head_step, boundary, tail_step = _parse_spp(sentences_per_prompt)
    cap = _solo_scene_chars(preferred_max_seconds)
    units = []
    index = 0
    while index < len(sentences):
        in_head = index < boundary
        step = max(1, head_step if in_head else tail_step)
        stop = min(len(sentences), index + step)
        if in_head:
            # The cadence boundary is absolute.  A partial final head group must
            # stop there; otherwise e.g. 3x4,1 incorrectly steals sentence 5
            # from the solo tail and changes both image count and ownership.
            stop = min(stop, boundary)
        owned = sentences[index:stop]
        start, end = owned[0]["start"], owned[-1]["end"]
        content_len = len(re.sub(r"\s+", " ", source[start:end]).strip())
        atoms = _semantic_atoms(source, start, end)
        requested_beats = max(1, math.ceil(content_len / cap))
        minimum_beats = min(len(atoms), requested_beats)
        allow_split = bool(
            split_long and step == 1 and content_len > cap and minimum_beats > 1
        )
        units.append({
            "unit_index": len(units),
            "start": start,
            "end": end,
            "text": source[start:end],
            "sentence_index": owned[0]["sentence_index"],
            "sentence_end_index": owned[-1]["sentence_index"],
            "sentence_count": len(owned),
            "allow_split": allow_split,
            "minimum_beat_count": minimum_beats if allow_split else 1,
            "atoms": atoms,
        })
        index = stop
    if "".join(unit["text"] for unit in units) != str(source or ""):
        raise ScenePlanTerminalError("semantic v4: cadence units не покрывают исходник")
    return units


def _semantic_unit_blocks(units: List[dict], target_chars: int = _SEMANTIC_BLOCK_TARGET_CHARS,
                          max_chars: int = _SEMANTIC_BLOCK_MAX_CHARS) -> List[List[dict]]:
    """Pack whole ownership units for speed; a unit is never split across calls."""
    target, cap = max(400, int(target_chars)), max(400, int(max_chars))
    blocks: List[List[dict]] = []
    current: List[dict] = []
    current_chars = 0
    for unit in units:
        unit_chars = len(unit["text"])
        if current and (current_chars >= target or current_chars + unit_chars > cap):
            blocks.append(current)
            current, current_chars = [], 0
        current.append(unit)
        current_chars += unit_chars
    if current:
        blocks.append(current)
    return blocks


def _semantic_beat_response_format():
    """Compact phase-1 schema: boundaries and visual intent, not image prose.

    The application attaches exact character offsets after a verbatim coverage check.
    Keeping this pass compact lets one request plan a whole narrative block without
    paying for the detailed image prompt twice.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "semantic_beat_plan",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["units"],
                "properties": {
                    "units": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["unit_index", "beats"],
                            "properties": {
                                "unit_index": {"type": "integer", "minimum": 0},
                                "beats": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": [
                                            "atom_start", "atom_end", "semantic_claim",
                                            "visual_mode", "required_elements",
                                            "continuity_group",
                                        ],
                                        "properties": {
                                            "atom_start": {"type": "integer", "minimum": 0},
                                            "atom_end": {"type": "integer", "minimum": 1},
                                            "semantic_claim": {"type": "string"},
                                            "visual_mode": {
                                                "type": "string",
                                                "enum": list(_SEMANTIC_VISUAL_MODES),
                                            },
                                            "required_elements": {
                                                "type": "array",
                                                "minItems": 1,
                                                "maxItems": 5,
                                                "items": {"type": "string"},
                                            },
                                            "continuity_group": {
                                                "type": "string",
                                                "enum": ["new", "continue_previous"],
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }


_SCENE_TRANSIENT_HTTP = {408, 409, 425, 429}
_SCENE_CAMERA_ANGLES = {
    "Wide Shot", "Medium Shot", "Close-up", "Low Angle",
    "Over-the-shoulder", "Overhead", "Dutch Angle",
}
_SCENE_SUBJECT_TYPES = {"character", "environment", "object"}


def _scene_http_is_transient(status_code: int) -> bool:
    return status_code in _SCENE_TRANSIENT_HTTP or 500 <= status_code <= 599


def _scene_response_body(response, limit: int = 240) -> str:
    try:
        return str(response.text or "")[:limit]
    except Exception:
        return ""


def _scene_llm_request(payload: dict, timeout, label: str,
                       abort_event: Optional[threading.Event] = None) -> dict:
    """Один scene-plan вызов с максимум тремя локальными попытками.

    Только transport/429/5xx считаются transient. Любой иной HTTP-отказ и
    повреждённая внешняя JSON-обёртка терминальны: повтор тем же запросом не
    должен маскировать нарушение контракта провайдера.
    """
    last_error = "временный сбой провайдера"
    for attempt in range(1, SCENE_MAX_RETRIES + 1):
        if abort_event is not None and abort_event.is_set():
            raise ScenePlanTransientError("план сцен остановлен после ошибки другого чанка")
        try:
            response = _llm_post(
                OPENAI_API_URL,
                headers=_openai_chat_headers(),
                json=payload,
                timeout=timeout,
            )
            _raise_if_openai_auth_failed(response)
        except OpenAIKeyError:
            raise
        except requests.exceptions.RequestException as exc:
            status = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
            if status and not _scene_http_is_transient(status):
                raise ScenePlanTerminalError(
                    f"{label}: провайдер отклонил запрос (HTTP {status})"
                ) from exc
            last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            if attempt == SCENE_MAX_RETRIES:
                raise ScenePlanTransientError(
                    f"{label}: {last_error} после {SCENE_MAX_RETRIES} попыток"
                ) from exc
            delay = RETRY_DELAY * attempt
            print_user(
                f"⚠️ {label}: временная ошибка ({attempt}/{SCENE_MAX_RETRIES}): "
                f"{last_error}; повтор через {delay}с",
                "WARNING",
            )
            time.sleep(delay)
            continue
        except TimeoutError as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            if attempt == SCENE_MAX_RETRIES:
                raise ScenePlanTransientError(
                    f"{label}: {last_error} после {SCENE_MAX_RETRIES} попыток"
                ) from exc
            delay = RETRY_DELAY * attempt
            print_user(
                f"⚠️ {label}: временная ошибка ({attempt}/{SCENE_MAX_RETRIES}): "
                f"{last_error}; повтор через {delay}с",
                "WARNING",
            )
            time.sleep(delay)
            continue
        except ScenePlanError:
            raise
        except Exception as exc:
            raise ScenePlanTerminalError(
                f"{label}: локальная ошибка канала промптов: {type(exc).__name__}: {str(exc)[:180]}"
            ) from exc

        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            body = _scene_response_body(response)
            if not _scene_http_is_transient(status):
                raise ScenePlanTerminalError(
                    f"{label}: провайдер отклонил запрос (HTTP {status}): {body}"
                )
            last_error = f"HTTP {status}: {body}".rstrip(": ")
            if attempt == SCENE_MAX_RETRIES:
                raise ScenePlanTransientError(
                    f"{label}: {last_error} после {SCENE_MAX_RETRIES} попыток"
                )
            delay = RETRY_DELAY * attempt
            print_user(
                f"⚠️ {label}: временная ошибка ({attempt}/{SCENE_MAX_RETRIES}): "
                f"{last_error}; повтор через {delay}с",
                "WARNING",
            )
            time.sleep(delay)
            continue

        try:
            result = response.json()
        except Exception as exc:
            raise ScenePlanSchemaError(
                f"{label}: провайдер вернул невалидный JSON-конверт"
            ) from exc
        if not isinstance(result, dict):
            raise ScenePlanSchemaError(
                f"{label}: JSON-конверт провайдера должен быть объектом"
            )
        return result

    raise ScenePlanTransientError(
        f"{label}: {last_error} после {SCENE_MAX_RETRIES} попыток"
    )


def _scene_content_json(result: dict, label: str):
    """Достаёт content; refusal terminal, malformed JSON пригоден для corrective retry."""
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ScenePlanSchemaError(f"{label}: в ответе нет choices[0]")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ScenePlanSchemaError(f"{label}: в ответе нет message")
    refusal = message.get("refusal")
    finish = str(choice.get("finish_reason") or "")
    if refusal or finish in {"content_filter", "safety", "refusal"}:
        raise ScenePlanTerminalError(
            f"{label}: провайдер отклонил сцену (finish_reason={finish or 'refusal'})"
        )
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ScenePlanSchemaError(f"{label}: пустой content (finish_reason={finish or '?'})")
    content = content.strip()
    try:
        return json.loads(content)
    except (TypeError, ValueError) as exc:
        raise ScenePlanSchemaError(
            f"{label}: невалидный JSON сцены (finish_reason={finish or '?'})"
        ) from exc


def _validated_visible_text(scene: dict, label: str,
                            require_visible_text: bool = False) -> Optional[List[str]]:
    """Validates exact image copy without guessing or silently repairing it.

    Missing is accepted only for persisted/custom legacy scenes. New director
    responses must explicitly choose between a non-empty list and ``[]`` so the
    image stage can distinguish a deliberate no-text decision from legacy data.
    """
    if "visible_text" not in scene:
        if require_visible_text:
            raise ScenePlanSchemaError(f"{label}: отсутствует обязательный visible_text")
        return None
    value = scene.get("visible_text")
    if not isinstance(value, list) or len(value) > 3:
        raise ScenePlanSchemaError(f"{label}: visible_text должен быть массивом до 3 строк")
    checked = []
    for item in value:
        if (not isinstance(item, str) or not item or item != item.strip()
                or len(item) > 120 or any(ord(ch) < 32 for ch in item)):
            raise ScenePlanSchemaError(
                f"{label}: каждая строка visible_text должна быть точной непустой строкой до 120 символов"
            )
        if item in checked:
            raise ScenePlanSchemaError(f"{label}: visible_text содержит дубликат")
        checked.append(item)
    return checked


_SCENE_QUOTED_RE = re.compile(r'"[^"\r\n]{1,120}"|“[^”\r\n]{1,120}”|«[^»\r\n]{1,120}»')


def _validate_scene_object(scene, chunk_idx: int, chunk_text: str,
                           canon_names: Optional[List[str]] = None,
                           require_visible_text: bool = False) -> dict:
    """Проверяет один scene-object до кэша/сборки полного плана."""
    label = f"часть {chunk_idx + 1}"
    if not isinstance(scene, dict):
        raise ScenePlanSchemaError(f"{label}: сцена должна быть JSON-объектом")
    required = ("subject", "visual_prompt", "camera_angle", "subject_type")
    missing = [key for key in required if not isinstance(scene.get(key), str) or not scene[key].strip()]
    if missing:
        raise ScenePlanSchemaError(f"{label}: нарушена схема сцены, поля: {', '.join(missing)}")
    if scene["camera_angle"] not in _SCENE_CAMERA_ANGLES:
        raise ScenePlanSchemaError(f"{label}: недопустимый camera_angle")
    if scene["subject_type"] not in _SCENE_SUBJECT_TYPES:
        raise ScenePlanSchemaError(f"{label}: недопустимый subject_type")
    visible_text = _validated_visible_text(scene, label, require_visible_text)
    quoted = (_SCENE_QUOTED_RE.findall(scene["visual_prompt"])
              if require_visible_text else [])
    if quoted:
        # Какие именно кавычки — в тексте отказа: из него строится точечный повтор, и
        # без этой подсказки модель трижды не понимала, что убрать (задача 2095, 11.10).
        shown = ", ".join(q[:60] for q in quoted[:3])
        raise ScenePlanSchemaError(
            f"{label}: literal wording запрещён в visual_prompt; используйте visible_text "
            f"(в кавычках: {shown})"
        )
    if canon_names:
        entities = scene.get("entities")
        if (not isinstance(entities, list)
                or any(not isinstance(name, str) or name not in canon_names for name in entities)):
            raise ScenePlanCanonSchemaError(f"{label}: нарушена схема entities")
    out = dict(scene)
    if visible_text is not None:
        out["visible_text"] = visible_text
    out["text_segment"] = chunk_text
    return out


def _validate_complete_scene_plan(scenes, text_chunks: List[str],
                                  canon_names: Optional[List[str]] = None,
                                  require_visible_text: bool = False) -> List[dict]:
    """Fail-closed инвариант: ровно одна валидная сцена на каждый исходный чанк."""
    if not isinstance(scenes, list) or len(scenes) != len(text_chunks):
        got = len(scenes) if isinstance(scenes, list) else "не список"
        raise ScenePlanTerminalError(
            f"неполный план сцен: {got} сцен для {len(text_chunks)} частей"
        )
    checked = []
    for idx, (scene, chunk_text) in enumerate(zip(scenes, text_chunks)):
        if not isinstance(scene, dict) or scene.get("text_segment") != chunk_text:
            raise ScenePlanTerminalError(f"часть {idx + 1}: text_segment не совпадает с чанком")
        checked.append(_validate_scene_object(
            scene, idx, chunk_text, canon_names, require_visible_text
        ))
    return checked


def _validate_smart_scene_plan(
    scenes,
    full_text: str,
    require_visible_text: bool = False,
    canon_names: Optional[List[str]] = None,
) -> List[dict]:
    """Проверяет Smart-план и строит точные непрерывные offsets из исходника.

    Допустимы только whitespace gaps между дословными model segments; они
    присоединяются к следующей сцене. Любой пропущенный смысловой символ делает
    весь план terminal — Standard Mode не подставляется молча.
    """
    if not isinstance(scenes, list) or not scenes:
        raise ScenePlanTerminalError("Smart Mode: план сцен пуст или не является списком")
    source = str(full_text or "")
    cursor = 0
    checked = []
    for index, raw_scene in enumerate(scenes):
        if not isinstance(raw_scene, dict):
            raise ScenePlanTerminalError(f"Smart Mode, часть {index + 1}: сцена не объект")
        raw_segment = raw_scene.get("text_segment")
        if not isinstance(raw_segment, str) or not raw_segment.strip():
            raise ScenePlanTerminalError(
                f"Smart Mode, часть {index + 1}: отсутствует дословный text_segment"
            )
        segment = raw_segment.strip()
        start = source.find(segment, cursor)
        if start < 0:
            raise ScenePlanTerminalError(
                f"Smart Mode, часть {index + 1}: text_segment не найден в исходнике по порядку"
            )
        if source[cursor:start].strip():
            raise ScenePlanTerminalError(
                f"Smart Mode, часть {index + 1}: перед сценой пропущен фрагмент исходника"
            )
        end = start + len(segment)
        exact_chunk = source[cursor:end]
        scene = _validate_scene_object(
            raw_scene, index, exact_chunk,
            canon_names=canon_names,
            require_visible_text=require_visible_text,
        )
        if not isinstance(raw_scene.get("includes_main_character"), bool):
            raise ScenePlanTerminalError(
                f"Smart Mode, часть {index + 1}: includes_main_character должен быть boolean"
            )
        scene["includes_main_character"] = raw_scene["includes_main_character"]
        scene["character_start"] = cursor
        scene["character_end"] = end
        checked.append(scene)
        cursor = end

    if source[cursor:].strip():
        raise ScenePlanTerminalError("Smart Mode: конец исходного текста не покрыт сценами")
    if cursor < len(source):
        checked[-1]["text_segment"] += source[cursor:]
        checked[-1]["character_end"] = len(source)
    if "".join(scene["text_segment"] for scene in checked) != source:
        raise ScenePlanTerminalError("Smart Mode: внутренний coverage-check плана не сошёлся")
    return checked


def _semantic_source_blocks(source: str,
                            target_chars: int = _SEMANTIC_BLOCK_TARGET_CHARS,
                            max_chars: int = _SEMANTIC_BLOCK_MAX_CHARS):
    """Returns contiguous raw-source blocks cut only at safe textual boundaries.

    Blocks are transport units for the LLM, never visual scenes. A block may contain
    many sentences; the model is therefore free to merge rhetorical micro-sentences
    and split compound sentences by complete visual propositions.
    """
    text = str(source or "")
    if not text:
        return []
    target = max(400, int(target_chars))
    cap = max(target, int(max_chars))
    if len(text) <= cap:
        return [(0, len(text), text)]

    boundary_re = re.compile(r'[.!?。！？؟।;՜][)\]»”"’]*(?=\s|$)|\n[ \t]*\n')
    boundaries = sorted({m.end() for m in boundary_re.finditer(text)} | {len(text)})
    blocks = []
    start = 0
    min_size = max(300, target // 2)
    while start < len(text):
        # Остаток уже помещается целиком: забираем его одним transport-блоком.
        # Без этого min_size отбрасывал законный короткий хвост, fallback резал по
        # последнему пробелу и создавал отдельную сцену вроде «встреч.».
        if len(text) - start <= cap:
            blocks.append((start, len(text), text[start:]))
            break
        hard_end = min(len(text), start + cap)
        candidates = [b for b in boundaries if start + min_size <= b <= hard_end]
        after_target = [b for b in candidates if b >= min(len(text), start + target)]
        if after_target:
            end = after_target[0]
        elif candidates:
            end = candidates[-1]
        else:
            # A pathological run-on block: split the transport block at whitespace,
            # never the future visual scene and never inside a word. If the only
            # token is longer than the transport cap, keep that block oversized;
            # a hard character cut here would make exact semantic coverage invalid.
            end = text.rfind(" ", start + 1, hard_end + 1)
            if end <= start:
                next_space = text.find(" ", hard_end)
                end = len(text) if next_space < 0 else next_space
        if end <= start:
            raise ScenePlanTerminalError("semantic beats: не удалось построить блок исходника")
        blocks.append((start, end, text[start:end]))
        start = end
    if "".join(block[2] for block in blocks) != text:
        raise ScenePlanTerminalError("semantic beats: блоки не покрывают исходник")
    return blocks


def _validate_semantic_fields(raw: dict, label: str, planning: bool = False) -> dict:
    if not isinstance(raw, dict):
        raise ScenePlanSchemaError(f"{label}: semantic beat должен быть объектом")
    claim = raw.get("semantic_claim")
    mode = raw.get("visual_mode")
    elements = raw.get("required_elements")
    continuity = raw.get("continuity_group")
    if not isinstance(claim, str) or not claim.strip():
        raise ScenePlanSchemaError(f"{label}: пустой semantic_claim")
    if mode not in _SEMANTIC_VISUAL_MODES:
        raise ScenePlanSchemaError(f"{label}: недопустимый visual_mode")
    if (not isinstance(elements, list) or not 1 <= len(elements) <= 5
            or any(not isinstance(item, str) or not item.strip() for item in elements)):
        raise ScenePlanSchemaError(f"{label}: required_elements должен содержать 1–5 элементов")
    if not isinstance(continuity, str) or not continuity.strip():
        raise ScenePlanSchemaError(f"{label}: пустой continuity_group")
    if planning and continuity not in ("new", "continue_previous"):
        raise ScenePlanSchemaError(
            f"{label}: continuity_group должен быть new или continue_previous"
        )
    out = dict(raw)
    out["semantic_claim"] = claim.strip()
    out["required_elements"] = [item.strip() for item in elements]
    out["continuity_group"] = continuity.strip()
    return out


def _validate_semantic_beat_block(raw_beats, block_text: str,
                                  source_offset: int = 0) -> List[dict]:
    """Fail-closed verbatim partition for one planning block."""
    if not isinstance(raw_beats, list) or not raw_beats:
        raise ScenePlanSchemaError("semantic block: beats пуст или не является списком")
    source = str(block_text or "")
    cursor = 0
    checked = []
    for index, raw in enumerate(raw_beats):
        label = f"semantic block, beat {index + 1}"
        beat = _validate_semantic_fields(raw, label, planning=True)
        raw_segment = raw.get("text_segment")
        if not isinstance(raw_segment, str) or not raw_segment.strip():
            raise ScenePlanSchemaError(f"{label}: отсутствует дословный text_segment")
        segment = raw_segment.strip()
        start = source.find(segment, cursor)
        if start < 0:
            raise ScenePlanSchemaError(f"{label}: text_segment не найден по порядку")
        if source[cursor:start].strip():
            raise ScenePlanSchemaError(f"{label}: перед beat пропущен текст")
        end = start + len(segment)
        # A semantic beat is never allowed to start/end inside an alphanumeric token.
        if start > 0 and source[start - 1].isalnum() and source[start].isalnum():
            raise ScenePlanSchemaError(f"{label}: граница проходит внутри слова")
        if end < len(source) and source[end - 1].isalnum() and source[end].isalnum():
            raise ScenePlanSchemaError(f"{label}: граница проходит внутри слова")
        exact = source[cursor:end]
        beat["text_segment"] = exact
        beat["character_start"] = source_offset + cursor
        beat["character_end"] = source_offset + end
        checked.append(beat)
        cursor = end

    if source[cursor:].strip():
        raise ScenePlanSchemaError("semantic block: потерян хвост исходника")
    if cursor < len(source):
        checked[-1]["text_segment"] += source[cursor:]
        checked[-1]["character_end"] = source_offset + len(source)
    if "".join(beat["text_segment"] for beat in checked) != source:
        raise ScenePlanSchemaError("semantic block: внутренний coverage-check не сошёлся")
    return checked


def _validate_semantic_unit_block(raw_units, expected_units: List[dict],
                                  source: str) -> List[dict]:
    """Validate compact atom ranges and materialise exact raw-source beats."""
    if not isinstance(raw_units, list) or len(raw_units) != len(expected_units):
        got = len(raw_units) if isinstance(raw_units, list) else "не массив"
        raise ScenePlanSchemaError(
            f"semantic v4 block: {got} units вместо {len(expected_units)}"
        )
    checked = []
    for response_unit, expected in zip(raw_units, expected_units):
        unit_no = expected["unit_index"]
        label = f"semantic v4 unit {unit_no + 1}"
        if not isinstance(response_unit, dict) or response_unit.get("unit_index") != unit_no:
            raise ScenePlanSchemaError(f"{label}: unit_index изменён или нарушен порядок")
        raw_beats = response_unit.get("beats")
        if not isinstance(raw_beats, list) or not raw_beats:
            raise ScenePlanSchemaError(f"{label}: beats пуст или не является списком")
        if not expected["allow_split"] and len(raw_beats) != 1:
            raise ScenePlanSchemaError(f"{label}: cadence unit должен дать ровно один beat")
        if (expected["allow_split"]
                and len(raw_beats) < expected["minimum_beat_count"]):
            raise ScenePlanSchemaError(
                f"{label}: длинное solo-предложение требует минимум "
                f"{expected['minimum_beat_count']} beats"
            )

        atoms = expected["atoms"]
        atom_cursor = 0
        unit_beats = []
        for beat_index, raw in enumerate(raw_beats):
            beat_label = f"{label}, beat {beat_index + 1}"
            beat = _validate_semantic_fields(raw, beat_label, planning=True)
            atom_start, atom_end = raw.get("atom_start"), raw.get("atom_end")
            if (not isinstance(atom_start, int) or isinstance(atom_start, bool)
                    or not isinstance(atom_end, int) or isinstance(atom_end, bool)
                    or atom_start != atom_cursor or atom_end <= atom_start
                    or atom_end > len(atoms)):
                raise ScenePlanSchemaError(f"{beat_label}: некорректный atom range")
            start = atoms[atom_start][0]
            end = atoms[atom_end - 1][1]
            if _semantic_boundary_inside_word(source, start):
                raise ScenePlanSchemaError(f"{beat_label}: начало внутри слова")
            if _semantic_boundary_inside_word(source, end):
                raise ScenePlanSchemaError(f"{beat_label}: конец внутри слова")
            beat.pop("atom_start", None)
            beat.pop("atom_end", None)
            beat["text_segment"] = source[start:end]
            beat["character_start"] = start
            beat["character_end"] = end
            beat["sentence_index"] = expected["sentence_index"]
            beat["sentence_end_index"] = expected["sentence_end_index"]
            beat["sentence_count"] = expected["sentence_count"]
            beat["sentence_start"] = expected["start"]
            beat["sentence_end"] = expected["end"]
            beat["cadence_unit_index"] = unit_no
            unit_beats.append(beat)
            atom_cursor = atom_end
        if atom_cursor != len(atoms):
            raise ScenePlanSchemaError(f"{label}: beats не покрывают все atoms")
        for beat_index, beat in enumerate(unit_beats):
            beat["beat_index"] = beat_index
            beat["beat_count"] = len(unit_beats)
        if "".join(beat["text_segment"] for beat in unit_beats) != expected["text"]:
            raise ScenePlanSchemaError(f"{label}: beats не покрывают cadence unit")
        checked.extend(unit_beats)
    return checked


def _validate_semantic_cadence_contract(items: List[dict], source: str,
                                        sentences_per_prompt="1",
                                        split_long: bool = True,
                                        preferred_max_seconds: float = DEFAULT_MAX_SOLO_SCENE_SECONDS
                                        ) -> List[dict]:
    """Prove that no persisted beat can cross or merge an ownership unit."""
    units = _semantic_cadence_units(
        source, sentences_per_prompt, split_long, preferred_max_seconds
    )
    if not units:
        if items:
            raise ScenePlanTerminalError("semantic v4: сцены есть для пустого cadence plan")
        return items
    cursor = 0
    for unit in units:
        owned = []
        while cursor < len(items) and items[cursor].get("character_start") < unit["end"]:
            owned.append(items[cursor])
            cursor += 1
        if not owned:
            raise ScenePlanTerminalError(
                f"semantic v4: cadence unit {unit['unit_index'] + 1} не имеет сцены"
            )
        if owned[0].get("character_start") != unit["start"]:
            raise ScenePlanTerminalError("semantic v4: сцена начинается вне cadence unit")
        if owned[-1].get("character_end") != unit["end"]:
            raise ScenePlanTerminalError("semantic v4: сцена пересекла границу cadence unit")
        if not unit["allow_split"] and len(owned) != 1:
            raise ScenePlanTerminalError("semantic v4: неизменяемый cadence unit раздроблен")
        if (unit["allow_split"]
                and len(owned) < unit["minimum_beat_count"]):
            raise ScenePlanTerminalError(
                "semantic v4: длинное solo-предложение раздроблено недостаточно"
            )
        for beat_index, item in enumerate(owned):
            expected = {
                "sentence_index": unit["sentence_index"],
                "sentence_end_index": unit["sentence_end_index"],
                "sentence_count": unit["sentence_count"],
                "sentence_start": unit["start"],
                "sentence_end": unit["end"],
                "cadence_unit_index": unit["unit_index"],
                "beat_index": beat_index,
                "beat_count": len(owned),
            }
            for key, value in expected.items():
                if item.get(key) != value:
                    raise ScenePlanTerminalError(
                        f"semantic v4: {key} не соответствует cadence unit"
                    )
    if cursor != len(items):
        raise ScenePlanTerminalError("semantic v4: сцены выходят за cadence plan")
    return items


def _validate_semantic_scene_plan(scenes, full_text: str,
                                  canon_names: Optional[List[str]] = None,
                                  sentences_per_prompt="1",
                                  split_long: bool = True,
                                  preferred_max_seconds: float = DEFAULT_MAX_SOLO_SCENE_SECONDS,
                                  enforce_cadence: bool = True,
                                  require_visible_text: bool = False,
                                  ) -> List[dict]:
    """Validates persisted phase-2 scenes without making another LLM request."""
    if not isinstance(scenes, list) or not scenes:
        raise ScenePlanTerminalError("semantic plan пуст или не является списком")
    source = str(full_text or "")
    cursor = 0
    checked = []
    for index, raw in enumerate(scenes):
        label = f"semantic scene {index + 1}"
        scene = _validate_semantic_fields(raw, label)
        start, end = raw.get("character_start"), raw.get("character_end")
        if (not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool)
                or start != cursor or end <= start or end > len(source)):
            raise ScenePlanTerminalError(f"{label}: некорректные character offsets")
        segment = raw.get("text_segment")
        if not isinstance(segment, str) or segment != source[start:end]:
            raise ScenePlanTerminalError(f"{label}: text_segment не совпадает с исходником")
        content_start = start + len(segment) - len(segment.lstrip())
        content_end = end - (len(segment) - len(segment.rstrip()))
        if _semantic_boundary_inside_word(source, content_start):
            raise ScenePlanTerminalError(f"{label}: начало внутри слова")
        if _semantic_boundary_inside_word(source, content_end):
            raise ScenePlanTerminalError(f"{label}: конец внутри слова")
        scene = _validate_scene_object(
            scene, index, segment, canon_names, require_visible_text
        )
        scene["semantic_claim"] = raw["semantic_claim"]
        scene["visual_mode"] = raw["visual_mode"]
        scene["required_elements"] = list(raw["required_elements"])
        scene["continuity_group"] = raw["continuity_group"]
        scene["continuity_relation"] = raw.get("continuity_relation")
        scene["character_start"] = start
        scene["character_end"] = end
        for key in _SEMANTIC_CONTRACT_KEYS:
            scene[key] = raw.get(key)
        checked.append(scene)
        cursor = end
    if cursor != len(source) or "".join(scene["text_segment"] for scene in checked) != source:
        raise ScenePlanTerminalError("semantic plan не покрывает исходник полностью")
    if not enforce_cadence:
        return checked
    return _validate_semantic_cadence_contract(
        checked, source, sentences_per_prompt, split_long, preferred_max_seconds
    )


def _validate_semantic_beat_plan(beats, full_text: str,
                                 sentences_per_prompt="1",
                                 split_long: bool = True,
                                 preferred_max_seconds: float = DEFAULT_MAX_SOLO_SCENE_SECONDS
                                 ) -> List[dict]:
    """Fail-closed validation for a complete persisted phase-1 plan.

    Per-block validation already proves verbatim coverage before publication.  A
    cache load must independently re-prove the same whole-source invariants, plus
    the deterministic continuity ids that are assigned after blocks are joined.
    """
    if not isinstance(beats, list) or not beats:
        raise ScenePlanTerminalError("semantic beats cache пуст или не является списком")
    source = str(full_text or "")
    cursor = 0
    expected_group = 0
    checked = []
    for index, raw in enumerate(beats):
        label = f"semantic beat {index + 1}"
        beat = _validate_semantic_fields(raw, label)
        start, end = raw.get("character_start"), raw.get("character_end")
        if (not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool)
                or start != cursor or end <= start or end > len(source)):
            raise ScenePlanTerminalError(f"{label}: некорректные character offsets")
        segment = raw.get("text_segment")
        if not isinstance(segment, str) or segment != source[start:end]:
            raise ScenePlanTerminalError(f"{label}: text_segment не совпадает с исходником")
        content_start = start + len(segment) - len(segment.lstrip())
        content_end = end - (len(segment) - len(segment.rstrip()))
        if _semantic_boundary_inside_word(source, content_start):
            raise ScenePlanTerminalError(f"{label}: начало внутри слова")
        if _semantic_boundary_inside_word(source, content_end):
            raise ScenePlanTerminalError(f"{label}: конец внутри слова")
        relation = raw.get("continuity_relation")
        if relation not in ("new", "continue_previous"):
            raise ScenePlanTerminalError(f"{label}: некорректная continuity_relation")
        if index == 0 or relation == "new":
            expected_group += 1
        expected_id = f"sequence_{expected_group:03d}"
        if raw.get("continuity_group") != expected_id:
            raise ScenePlanTerminalError(f"{label}: continuity_group не соответствует порядку")
        beat["text_segment"] = segment
        beat["character_start"] = start
        beat["character_end"] = end
        beat["continuity_relation"] = relation
        for key in _SEMANTIC_CONTRACT_KEYS:
            beat[key] = raw.get(key)
        checked.append(beat)
        cursor = end
    if cursor != len(source) or "".join(item["text_segment"] for item in checked) != source:
        raise ScenePlanTerminalError("semantic beats cache не покрывает исходник полностью")
    return _validate_semantic_cadence_contract(
        checked, source, sentences_per_prompt, split_long, preferred_max_seconds
    )


def _semantic_cache_cadence(preferred_max_seconds: float,
                            sentences_per_prompt="1") -> dict:
    return {
        "sentences_per_prompt": str(sentences_per_prompt),
        "split_long": True,
        "preferred_max_seconds": float(preferred_max_seconds),
    }


def _plan_semantic_beats(text: str, preferred_max_seconds: float,
                         sentences_per_prompt="1",
                         use_cache: bool = False) -> List[dict]:
    """Phase 1: semantic intent inside immutable sentence/cadence units."""
    cache_identity = _planning_artifact_identity(
        "semantic_beats",
        text,
        algorithm_version=SPLIT_LONG_ALGORITHM_VERSION,
        prompt_version="semantic-beats-v4-contextual-visible-text",
        cadence=_semantic_cache_cadence(
            preferred_max_seconds, sentences_per_prompt
        ),
    )
    if use_cache:
        cached = _planning_artifact_cache_get(cache_identity)
        if cached is not None:
            try:
                checked = _validate_semantic_beat_plan(
                    cached, text, sentences_per_prompt, True,
                    preferred_max_seconds,
                )
            except ScenePlanError as exc:
                print_user(f"⚠️ Semantic beats: битый hash-cache проигнорирован ({exc})", "WARNING")
            else:
                print_user(
                    f"⚡ Semantic beats: {len(checked)} сцен из source-hash cache (без LLM)",
                    "SUCCESS",
                )
                return checked

    cadence_units = _semantic_cadence_units(
        text, sentences_per_prompt, True, preferred_max_seconds
    )
    blocks = _semantic_unit_blocks(cadence_units)
    if not cadence_units:
        return []
    provider = _prompt_provider()
    response_format = _semantic_beat_response_format()
    system_prompt = """You are a narrative editor planning image changes for spoken narration.
Plan semantic visual beats inside the supplied IMMUTABLE CADENCE UNITS.

NON-NEGOTIABLE RULES:
1. Return every unit_index exactly once and in the supplied order. Units are hard ownership boundaries: NEVER merge neighbouring units and NEVER move atoms between them.
2. A unit marked EXACTLY_ONE must return exactly one beat spanning all its atoms. This includes every short sentence and every configured multi-sentence group.
3. A unit marked SPLIT_REQUIRED_MIN_N is one long solo sentence. Return at least N contiguous beats (normally exactly N; add one only for a real extra proposition), but split only where every side is independently understandable and drawable. Never leave setup fragments or subordinate clauses alone.
4. Return only atom_start/atom_end ranges (end exclusive). Within each unit ranges start at atom 0, are contiguous, non-empty, ordered, and end at the supplied atom_count. Never copy or rewrite narration text.
5. semantic_claim states the exact visible meaning, not merely the topic.
6. Choose visual_mode by this priority: literal_action; object_in_context; process; cause_effect; comparison; historical_artifact; environment. Use metaphor only when no literal/contextual visual can express the completed claim.
7. required_elements contains 1-5 concrete visible facts that the later image prompt must show. Do not include style, camera, generic atmosphere, or exact quoted copy. When writing is essential, describe its ROLE conceptually (for example, "viewer-facing date label" or "authentic German station sign"); phase 2 alone chooses the exact final spelling in visible_text. Viewer-facing/editorial writing follows this unit's narration language and script. Diegetic writing physically belonging to a place/era uses that world's authentic local language and script, even when narration differs. Never invent decorative or paragraph-length text.
8. continuity_group is exactly "continue_previous" when this beat stays in the same people/place/process sequence as the immediately previous narration (including previous units and PREVIOUS CONTEXT); otherwise it is "new". The application assigns stable group IDs.
Return only the requested JSON."""

    topic = re.sub(r"\s+", " ", str(text or "")).strip()[:500]

    def make_payload(block_index: int):
        block_units = blocks[block_index]
        start, end = block_units[0]["start"], block_units[-1]["end"]
        before = re.sub(r"\s+", " ", text[max(0, start - 500):start]).strip()
        after = re.sub(r"\s+", " ", text[end:min(len(text), end + 500)]).strip()
        unit_sections = []
        for unit in block_units:
            atoms = [text[left:right] for left, right in unit["atoms"]]
            mode = (
                f"SPLIT_REQUIRED_MIN_{unit['minimum_beat_count']}"
                if unit["allow_split"] else "EXACTLY_ONE"
            )
            unit_sections.append(
                f"UNIT {unit['unit_index']} | {mode} | "
                f"sentences {unit['sentence_index'] + 1}-"
                f"{unit['sentence_end_index'] + 1} | atom_count={len(atoms)}\n"
                + "ATOMS (index:text):\n"
                + "\n".join(
                    f"{atom_index}: {json.dumps(atom, ensure_ascii=False)}"
                    for atom_index, atom in enumerate(atoms)
                )
            )
        prompt = f"""TOPIC/GLOBAL OPENING CONTEXT:
{topic}

PREVIOUS CONTEXT (read-only; do not copy into beats):
{before or '(none)'}

=== CURRENT BLOCK {block_index + 1}/{len(blocks)} — plan these units only ===
{chr(10).join(unit_sections)}
=== END CURRENT BLOCK ===

NEXT CONTEXT (read-only; do not copy into beats):
{after or '(none)'}

Preferred maximum cadence inside SPLIT_REQUIRED units is roughly {float(preferred_max_seconds):g} seconds. Unit boundaries and EXACTLY_ONE are absolute."""
        return {
            "model": SCENE_GPT_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 6000,
            "temperature": 0.25,
            "response_format": response_format,
        }

    def plan_one(block_index: int):
        label = f"semantic block {block_index + 1}/{len(blocks)}"
        base = make_payload(block_index)
        last_error = None
        for attempt in range(SCENE_SCHEMA_MAX_RETRIES + 1):
            attempt_started = time.monotonic()
            if _prompt_provider() != provider:
                raise ScenePlanTerminalError(
                    f"{label}: канал промптов изменился внутри задачи"
                )
            payload = (base if attempt == 0 else
                       _scene_corrective_payload(base, label, str(last_error)))
            if attempt:
                time.sleep(SCENE_SCHEMA_RETRY_DELAY * attempt)
            try:
                result = _scene_llm_request(
                    payload, (10, 180), label
                )
            except Exception:
                print_user(
                    f"⏱ {label}, попытка {attempt + 1}: "
                    f"{time.monotonic() - attempt_started:.1f}с (ошибка)",
                    "INFO",
                )
                raise
            try:
                data = _scene_content_json(result, label)
                raw_units = data.get("units") if isinstance(data, dict) else None
                checked = _validate_semantic_unit_block(
                    raw_units, blocks[block_index], text
                )
                print_user(
                    f"⏱ {label}, попытка {attempt + 1}: "
                    f"{time.monotonic() - attempt_started:.1f}с (готово)",
                    "INFO",
                )
                return checked
            except ScenePlanSchemaError as exc:
                print_user(
                    f"⏱ {label}, попытка {attempt + 1}: "
                    f"{time.monotonic() - attempt_started:.1f}с (schema retry)",
                    "INFO",
                )
                last_error = exc
        raise ScenePlanSemanticExhaustedError(
            f"{label}: схема не исправлена после {SCENE_SCHEMA_MAX_RETRIES} повторов: {last_error}"
        )

    print_user(
        f"🧠 Semantic beats v{SPLIT_LONG_ALGORITHM_VERSION}: "
        f"{len(cadence_units)} cadence units в {len(blocks)} блоках; "
        "границы предложений защищены",
        "INFO",
    )
    planned = {}
    # OpenAI benefits from prefix-cache warmup. Codex/Claude CLI calls do not share
    # that cache, so serialising their first block only adds wall-clock latency.
    warm_openai = provider == "openai"
    first_parallel = 0
    if warm_openai:
        planned[0] = plan_one(0)
        first_parallel = 1
    remaining_blocks = list(range(first_parallel, len(blocks)))
    if remaining_blocks:
        # Уважает тот же server-provided cap, что и phase 2. Иначе semantic pass
        # мог открыть 8 Codex/OpenAI вызовов даже при VS_SCENE_THREADS=2.
        with ThreadPoolExecutor(
            max_workers=min(max(1, GPT_SCENE_PARALLEL_THREADS), len(remaining_blocks))
        ) as executor:
            futures = {executor.submit(plan_one, i): i for i in remaining_blocks}
            for future in as_completed(futures):
                planned[futures[future]] = future.result()
    beats = [beat for i in range(len(blocks)) for beat in planned[i]]
    group_no = 0
    for index, beat in enumerate(beats):
        relation = beat["continuity_group"]
        if index == 0 or relation == "new":
            group_no += 1
        beat["continuity_relation"] = relation
        beat["continuity_group"] = f"sequence_{group_no:03d}"
    # A lightweight final exact-coverage assertion; detailed field validation already ran per block.
    beats = _validate_semantic_beat_plan(
        beats, text, sentences_per_prompt, True, preferred_max_seconds
    )
    if use_cache:
        _planning_artifact_cache_set(cache_identity, beats)
    print_user(f"✅ Semantic beats: {len(beats)} законченных сцен", "SUCCESS")
    return beats


def _prompt_cache_dir() -> str:
    """Глобальная content-addressed папка кэша сцен в tempdir. makedirs(exist_ok)."""
    d = os.path.join(tempfile.gettempdir(), "videostudio_prompt_cache")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        print_debug(f"Не удалось создать папку кэша сцен {d}: {e}")
    return d


def _scene_cache_context_digest(source_text: str, system_prompt: str,
                                response_format: dict, provider: str) -> str:
    """Digest всех общих входов, способных изменить смысл сцены.

    Кэш отдельной части нельзя переносить в другой ролик только потому, что в нём
    встретился тот же текст: на результат влияют полный source, Visual Bible, canon,
    subject anchor, схема, модель и зафиксированный provider.
    """
    context = {
        "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "system_prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "response_format": response_format,
        "provider": provider,
        "model": SCENE_GPT_MODEL,
        "cache_version": _PROMPT_CACHE_VERSION,
    }
    raw = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prompt_cache_key(chunk_text: str, style: str, context_digest: str) -> str:
    """Content-addressed key, обязательно связанный с source/context digest."""
    if not isinstance(context_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", context_digest):
        raise ValueError("scene cache context digest is required")
    raw = json.dumps(
        {
            "chunk": chunk_text,
            "style": style,
            "context_digest": context_digest,
            "model": SCENE_GPT_MODEL,
            "cache_version": _PROMPT_CACHE_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _scene_corrective_payload(payload: dict, label: str, reason: str) -> dict:
    """Новая immutable payload для schema retry; provider/model/response_format не меняются."""
    corrected = dict(payload)
    corrected["messages"] = [dict(message) for message in (payload.get("messages") or [])]
    safe_reason = re.sub(r"\s+", " ", str(reason or "schema mismatch")).strip()[:240]
    canonical_entity_hint = ""
    reason_folded = safe_reason.casefold()
    if "canonical entity coverage" in reason_folded and "scene.entities" in reason_folded:
        alias_match = re.search(
            r"\balias\s+((?:'[^'\r\n]+'|\"[^\"\r\n]+\"))",
            safe_reason,
            flags=re.IGNORECASE,
        )
        if alias_match:
            quoted_alias = alias_match.group(1)
            canonical_entity_hint = (
                f"The exact missing canonical alias is {quoted_alias}. Copy that exact quoted alias "
                "verbatim into the entities array. Do not substitute another grammatical case, "
                "spelling, transliteration, or a semantically related alias. "
            )
    literal_hint = ""
    if "literal wording" in reason_folded:
        literal_hint = (
            "visual_prompt must contain NO quotation marks of any kind (\"…\", “…”, «…»), not "
            "even around names, nicknames, titles or slogans. If that wording must be readable "
            "in the image, put it verbatim into visible_text and in visual_prompt only say where "
            "the writing is (for example, 'a painted name on the nose of the bomber'). If it need "
            "not be readable, describe it in your own words without quotes. "
        )
    corrected["messages"].append({
        "role": "user",
        "content": (
            f"CORRECTIVE RETRY for {label}. The previous answer was rejected: {safe_reason}. "
            f"{canonical_entity_hint}{literal_hint}"
            "Regenerate only the requested scene data. Obey the supplied JSON schema exactly: "
            "include every required field, keep required string fields non-empty, use allowed enum "
            "values, and use an empty array only where the contract explicitly allows it (such as "
            "visible_text when no writing is needed). "
            "return JSON only with no markdown, commentary, fallback, or placeholder scene."
        ),
    })
    return corrected


def _prompt_cache_get(key: str) -> Optional[List[dict]]:
    """Читает сцены из {hash}.json. None если нет/битый."""
    path = os.path.join(_prompt_cache_dir(), f"{key}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors='replace') as f:
            scenes = json.load(f)
        if isinstance(scenes, list) and scenes:
            return scenes
    except Exception as e:
        print_debug(f"Битый кэш сцены {path}: {e}")
    return None


def _prompt_cache_set(key: str, scenes: List[dict]) -> None:
    """Атомарно сохраняет сцены в {hash}.json."""
    if not scenes:
        return
    path = os.path.join(_prompt_cache_dir(), f"{key}.json")
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", errors='replace') as f:
            json.dump(scenes, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        print_debug(f"Не удалось записать кэш сцены {path}: {e}")


def _planning_prompt_model_identity(provider: str, payload_model: str) -> str:
    if prompt_llm is not None:
        try:
            if provider == "codex":
                return str(prompt_llm.codex_model())
            if provider == "claude":
                return str(prompt_llm.claude_model())
            if provider == "altclaude":
                return str(prompt_llm.alt_model())
        except Exception:
            pass
    return str(payload_model)


def _planning_artifact_identity(kind: str, source_text: str,
                                algorithm_version, prompt_version: str,
                                cadence, payload_model: str = SCENE_GPT_MODEL) -> dict:
    """Immutable identity for reusable planning artifacts.

    Source content alone is insufficient: a provider/model, prompt contract or
    cadence change can legitimately alter semantic boundaries and the story map.
    """
    provider = _prompt_provider()
    return {
        "kind": str(kind),
        "source_sha256": hashlib.sha256(
            str(source_text or "").encode("utf-8")
        ).hexdigest(),
        "algorithm_version": algorithm_version,
        "prompt_version": str(prompt_version),
        "cache_version": _PLANNING_ARTIFACT_CACHE_VERSION,
        "scene_cache_version": _PROMPT_CACHE_VERSION,
        "provider": provider,
        "model": _planning_prompt_model_identity(provider, payload_model),
        "cadence": cadence,
    }


def _planning_artifact_cache_path(identity: dict) -> str:
    raw = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    safe_kind = re.sub(r"[^a-z0-9_-]+", "_", str(identity.get("kind") or "artifact").lower())
    return os.path.join(_prompt_cache_dir(), f"artifact_{safe_kind}_{digest}.json")


def _planning_artifact_cache_get(identity: dict):
    path = _planning_artifact_cache_path(identity)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            envelope = json.load(stream)
        if (not isinstance(envelope, dict)
                or envelope.get("identity") != identity
                or "value" not in envelope):
            raise ValueError("cache identity mismatch")
        return envelope["value"]
    except Exception as exc:
        print_debug(f"Битый planning artifact cache {path}: {exc}")
        return None


def _planning_artifact_cache_set(identity: dict, value) -> None:
    path = _planning_artifact_cache_path(identity)
    try:
        _atomic_write_json(path, {"identity": identity, "value": value})
    except Exception as exc:
        # Cache is an optimization only; its write must not invalidate an already
        # fully validated in-memory result or publish a partial plan bundle.
        print_debug(f"Не удалось записать planning artifact cache {path}: {exc}")


# Артефакты «текста в кадре»: image-модель рендерит их кракозяброй, поэтому из visual_prompt
# их надо вычищать ДЕТЕРМИНИРОВАННО (не уговорами). Слабые модели (nano) ловят запрет хуже —
# скраб делает результат модель-независимым. Цельные слова, высокая уверенность (без bare 'sign',
# чтобы не съесть "signs of healing"; без 'table', чтобы не съесть мебель).
_ARTIFACT_PATTERNS = [
    r'graphs?', r'charts?', r'diagrams?', r'infographics?', r'statistics?',
    r'signboards?', r'signposts?', r'billboards?', r'(?:neon|street|road)\s+signs?',
    r'labell?ed', r'labels?', r'captions?', r'subtitles?', r'handwriting',
    r'handwritten', r'newspapers?', r'scientific\s+data',
    r'written\s+(?:words?|text|notes?)', r'data\s+readouts?',
]

def _scrub_visual_artifacts(prompt: str) -> str:
    """Убирает из visual_prompt упоминания текста/графиков/табличек/вывесок.
    Сначала пытается выкинуть только проблемную запятую-клаузу; если так теряется
    >50% длины — мягко заменяет токены нейтральным словом, сохраняя предложение."""
    import re
    if not prompt:
        return prompt
    art_re = re.compile(r'\b(?:' + '|'.join(_ARTIFACT_PATTERNS) + r')\b', re.IGNORECASE)
    if not art_re.search(prompt):
        return prompt
    # 1) дропаем целые клаузы, содержащие артефакт
    parts = prompt.split(',')
    kept = [p for p in parts if not art_re.search(p)]
    result = ','.join(kept)
    result = re.sub(r'\s+', ' ', result).strip().strip(',').strip()
    # 2) защита: если выкинули слишком много — замена токенов вместо дропа клаузы
    if len(result) < int(len(prompt) * 0.5):
        result = art_re.sub('natural detail', prompt)
        result = re.sub(r'\s+', ' ', result).strip()
    # 3) чистим осиротевшую пунктуацию
    result = re.sub(r'\s+,', ',', result)
    result = re.sub(r',\s*,+', ',', result)
    result = result.strip().strip(',').strip()
    return result or prompt


def _anchor_subject(visual_prompt: str, subject: str) -> str:
    """Универсальный (для ЛЮБОЙ темы) якорь субъекта. Если в visual_prompt нет ни одного
    значимого слова из subject — модель ушла в обобщёнку или показала не тот объект, —
    дописываем явный центр кадра. Не трогаем, если субъект уже присутствует (чтобы не
    плодить конфликт). subject модель извлекает из текста сегмента сама → не хардкод-список."""
    import re
    if not visual_prompt or not subject:
        return visual_prompt
    # слова-обобщения исключаем, чтобы 'green plant' не «совпало» с 'nettle plant'
    STOP = {'a','an','the','of','and','with','in','on','to','at','its','their',
            'plant','plants','leaf','leaves','herb','herbs','flower','flowers',
            'object','thing','scene','view','close','shot'}
    # \w+ с UNICODE → работают и не-латинские субъекты; порог >2 для латиницы,
    # но короткие/нелатинские значимые слова тоже берём (не отбрасываем кириллицу).
    subj_words = [w for w in re.findall(r'\w+', subject.lower(), re.UNICODE)
                  if w not in STOP and (len(w) > 2 or not w.isascii())]
    if not subj_words:
        return visual_prompt
    vp_low = visual_prompt.lower()
    # ВАЖНО: матч по ГРАНИЦЕ слова, иначе 'oil' ложно совпадёт в 'soil' и гард молча пропустит.
    if any(re.search(r'\b' + re.escape(w) + r'\b', vp_low) for w in subj_words):
        return visual_prompt  # субъект уже в кадре — ок
    s = visual_prompt.rstrip()
    sep = '' if s.endswith(('.', '!', '?')) else '.'
    return f"{s}{sep} Primary subject, large and centered in frame: {subject.strip()}."


# ── ГЛОБАЛЬНЫЙ ЯКОРЬ СУБЪЕКТА (фикс «теряется контекст → не та птица», 04.07) ──────────
# Аудит 9 языковых bird-роликов: subject решался ПО-ЧАНКОВО без глобального якоря вида →
# 479 родовых «a small bird» + 13 иноязычных имён (Rotschnabel-Madenhacker) → ~37% картинок
# рисовали НЕ волоклюя. Лечим: ОДИН раз на проект добываем канонический английский субъект +
# списки родовых алиасов и родных имён; детерминированный пост-пасс заменяет их на канон ПЕРЕД
# _anchor_subject. Стоимость: 1 дешёвый nano-ТЕКСТ-вызов на ПРОЕКТ (0 кредитов картинок).
# Только на дефолт-пути (canon=None); при мульти-протагонисте (single_subject=false) НЕ активен.
def _norm_subject_token(s: str) -> str:
    s = str(s or "").strip().lower()
    s = re.sub(r'^(a|an|the|one|some)\s+', '', s)
    s = re.sub(r'\s+', ' ', s).strip(' .,:;')
    return s


def extract_main_subject(text: str, title: str = "") -> dict:
    """ОДИН раз на проект: канонический англ. субъект + родовые алиасы + родные имена героя.
    Возвращает {single_subject:bool, main_subject, generic_aliases:[...], native_names:[...]}
    или {} / {single_subject:False} при мульти-субъекте/отсутствии ключа/ошибке (якорь не активен).
    title = имя проекта: в нём часто есть НАЗВАНИЕ ВИДА на языке ролика (Rotschnabel-Madenhacker,
    pique-bœuf, волоклюй) → точная видо-идентификация вместо угадывания по повадкам."""
    if not _has_prompt_channel() or not text.strip():
        return {}
    t = text.strip()
    # широкая выборка (не только начало): начало+середина+конец — герой ловится и на длинных скриптах
    if len(t) > 9000:
        mid = len(t) // 2
        sample = t[:4000] + "\n...\n" + t[mid - 1000:mid + 1000] + "\n...\n" + t[-2000:]
    else:
        sample = t
    # чистим имя проекта до читаемого заголовка (снять vN_/языковой префикс и хэш в скобках)
    _title = re.sub(r'\s*\[[0-9a-fA-F]+\]\s*$', '', str(title or '')).strip()
    _title = re.sub(r'^v\d+_', '', _title)
    _title = re.sub(r'^[a-z]{2}_', '', _title).strip()
    user_msg = (f"VIDEO TITLE: {_title}\n\nSCRIPT:\n{sample}" if _title else sample)
    sys_p = (
        "You analyze a full video script and identify its ONE recurring main visual subject (protagonist), "
        "if it truly has a single one. The VIDEO TITLE often contains the subject's name in the source "
        "language — use it as the strongest clue. Output STRICT JSON only. Fields:\n"
        "- single_subject: true ONLY if the ONE clear recurring HERO the whole video follows is a NON-HUMAN "
        "subject — a specific animal, creature, plant, object, place or phenomenon. IMPORTANT: supporting "
        "animals it interacts with (prey, predators, hosts such as buffalo or giraffe), its OWN babies/chicks/"
        "eggs, and the environment do NOT make it multi-subject — a wildlife story that follows ONE species is "
        "single_subject=true even if other animals appear. Set FALSE for: ANY human/person protagonist (people, "
        "named or not, are handled separately — never anchor them), multiple co-leads/ensembles, abstract/essay "
        "scripts, and topic-shifting compilations.\n"
        "- main_subject: the EXACT species/subject as a SHORT ENGLISH noun phrase WITH 1-2 key visual traits "
        "(e.g. 'red-billed oxpecker, small brown bird with a bright red beak'). TRANSLATE any non-English "
        "species name from the title/script to its correct English common name, and cross-check it against the "
        "behaviour in the script (habitat, diet, what it rides on, beak colour, nesting). Infer the PRECISE real "
        "species — do NOT guess a superficially-similar one. Empty string if single_subject is false.\n"
        "- generic_aliases: lowercase English generic words the script uses for that SAME subject that would "
        "draw a WRONG/random thing if taken literally (e.g. ['bird','small bird','little bird','songbird','chick']). "
        "No articles. [] if none.\n"
        "- native_names: the subject's names AS THEY APPEAR in the title/script in any non-English language "
        "(e.g. ['Rotschnabel-Madenhacker','Madenhacker','pique-bœuf','волоклюй']). [] if none."
    )
    payload = {
        "model": SCENE_GPT_MODEL,  # mini: видо-идентификация по повадкам нано не по зубам (угадывал дятла/птицу-носорога)
        "messages": [{"role": "system", "content": sys_p}, {"role": "user", "content": user_msg}],
        "max_tokens": 300, "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    try:
        r = _llm_post(OPENAI_API_URL,
                              headers=_openai_chat_headers(),
                              json=payload, timeout=60)
        if r.status_code != 200:
            print_debug(f"extract_main_subject: HTTP {r.status_code}")
            return {}
        d = json.loads(r.json()["choices"][0]["message"]["content"])
        if not d.get("single_subject") or not (d.get("main_subject") or "").strip():
            return {"single_subject": False}
        return {
            "single_subject": True,
            "main_subject": d["main_subject"].strip(),
            "generic_aliases": [_norm_subject_token(x) for x in (d.get("generic_aliases") or []) if x],
            "native_names": [str(x).strip() for x in (d.get("native_names") or []) if x],
        }
    except Exception as e:
        print_debug(f"extract_main_subject: {e}")
        return {}


def _normalize_scene_subject(subject: str, anchor: dict) -> str:
    """Детерминированно возвращает канонический субъект вместо родового/иноязычного.
    Не трогает субъект, если он уже про вид героя ИЛИ про ДРУГУЮ сущность."""
    if not (anchor and anchor.get("single_subject") and anchor.get("main_subject")):
        return subject
    ms = anchor["main_subject"]
    subj = str(subject or "").strip()
    if not subj:
        return ms
    low = subj.lower()
    # уже корректно (в субъекте есть ОТЛИЧИТЕЛЬНОЕ слово вида, напр. 'oxpecker') → не трогаем.
    # Берём токены только из ГОЛОВЫ до запятой ('red-billed oxpecker'), а не из трейт-хвоста
    # ('small brown bird') — иначе родовое 'small'/'brown' ложно засчитается как «вид уже назван».
    _COMMON_DESC = {"small", "large", "big", "little", "tiny", "young", "baby", "brown", "grey",
                    "gray", "black", "white", "pale", "dark", "light", "bright", "with", "and", "its"}
    _head = ms.split(",")[0]
    ms_tokens = [w for w in re.findall(r'[a-z]+', _head.lower()) if len(w) > 4 and w not in _COMMON_DESC]
    if ms_tokens and any(re.search(r'\b' + re.escape(tok) + r'\b', low) for tok in ms_tokens):
        return subj
    # (1) родное (иноязычное) имя героя внутри субъекта → канон (закрывает foreign_name детерминированно)
    for nn in anchor.get("native_names", []):
        if nn and nn.lower() in low:
            return ms
    # (2) родовой алиас как отдельное слово (a small bird / the bird / chick) → канон
    for al in anchor.get("generic_aliases", []):
        if al and re.search(r'\b' + re.escape(al) + r'\b', low):
            return ms
    return subj  # другая сущность / абстракция — оставляем как есть


def _parse_spp(spec):
    """Парсит «предложений на промт». Разделитель ЛЮБОЙ (x, х, ×, *, -, пробел и т.п.):
    "3"      → (3, 0, 3)  — равномерно по 3 (как раньше).
    "1x40,3" → (1, 40, 3) — первые 40 предложений по 1 на картинку, дальше по 3.
    Логика: вытаскиваем числа из строки. 3 числа → переменный шаг, 1 → равномерно.
    Шаг всегда ≥1 (защита от бесконечного цикла)."""
    nums = re.findall(r'\d+', str(spec))
    if len(nums) >= 3:
        return max(1, int(nums[0])), max(0, int(nums[1])), max(1, int(nums[2]))
    if nums:
        n = max(1, int(nums[0]))
        return n, 0, n
    return 3, 0, 3


def _semantic_split_enabled(sentences_per_prompt, split_long: bool) -> bool:
    """Semantic v4 is needed wherever the configured cadence has solo positions.

    Multi-sentence positions remain immutable one-scene units; only a solo sentence
    may receive internal semantic cuts.  Uniform N>1 therefore keeps the cheaper
    legacy path because there is nothing to repartition.
    """
    if not split_long:
        return False
    head, boundary, tail = _parse_spp(sentences_per_prompt)
    return tail == 1 or (boundary > 0 and head == 1)


def _build_scene_text_chunks(text: str, sentences_per_prompt=3,
                             split_long: bool = False,
                             max_solo_scene_seconds: float = DEFAULT_MAX_SOLO_SCENE_SECONDS
                             ) -> List[str]:
    """Единственный детерминированный splitter для generation и ``--use_plan``.

    Если эти два пути считают chunks раздельно, старый/неполный scenes.json может
    пройти image-job лишь потому, что проверка использовала другую логику.
    """
    sentences = split_text_to_sentences(text)
    head_step, boundary, tail_step = _parse_spp(sentences_per_prompt)
    if split_long:
        return _group_split_solo(
            sentences, head_step, boundary, tail_step,
            max_chars=_solo_scene_chars(max_solo_scene_seconds),
        )
    chunks = []
    index = 0
    while index < len(sentences):
        in_head = index < boundary
        step = max(1, head_step if in_head else tail_step)
        stop = min(len(sentences), index + step)
        if in_head:
            stop = min(stop, boundary)
        chunks.append(' '.join(sentences[index:stop]))
        index = stop
    return chunks


# Пост-пасс против засилья крупных планов: bias в системном промпте не гарантирует квоту
# (чанки генерятся независимо), поэтому режем детерминированно парсером — правим ТОЛЬКО ракурс
# (framing), не трогая сюжет/субъект/окружение сцены.
_CLOSEUP_MID = re.compile(r'\b(?:extreme\s+|tight\s+)?(?:macro|close[-\s]?ups?|closeups?)\b', re.I)
_CLOSEUP_LEAD = re.compile(
    r'^\s*(?:a |an |the )?(?:extreme |tight )?(?:macro|close[-\s]?up|closeup)'
    r'(?:\s+(?:view|shot|image|photo|photograph|angle|perspective|look|framing))?\s*', re.I)


def _reduce_closeup_bias(scenes, cap_frac=0.05):
    """Крупные/макро планы держим МЕНЬШИНСТВОМ (кап ~5% сцен, спред по ролику); остальные
    близкие планы переписываем в общий/средний. Меняем только framing-слова в начале visual_prompt
    и camera_angle, содержание сцены не трогаем."""
    if not scenes:
        return scenes
    idxs = [i for i, s in enumerate(scenes)
            if isinstance(s, dict) and _CLOSEUP_MID.search(s.get('visual_prompt') or '')]
    n = len(scenes)
    cap = max(1, round(n * cap_frac))
    if len(idxs) <= cap:
        return scenes
    step = max(1, len(idxs) // cap)
    keep = set(idxs[::step][:cap])          # оставить cap крупных планов, равномерно по ролику
    alt = ["A wide establishing shot ", "A medium shot ", "A wide shot "]
    rewritten = 0
    for i in idxs:
        if i in keep:
            continue
        s = scenes[i]
        p = s.get('visual_prompt') or ''
        m = _CLOSEUP_LEAD.match(p)
        if m:
            p = alt[rewritten % len(alt)] + p[m.end():]
        p = _CLOSEUP_MID.sub('wide shot', p)
        s['visual_prompt'] = p
        _ca = (s.get('camera_angle') or '').lower().replace(' ', '').replace('-', '')
        if _ca in ('closeup', 'extremecloseup', ''):
            s['camera_angle'] = 'Wide Shot' if rewritten % 2 == 0 else 'Medium Shot'
        rewritten += 1
    try:
        print_debug(f"anti-closeup: крупных планов {len(idxs)}/{n} → оставлено {cap}, переписано {rewritten}")
    except Exception:
        pass
    return scenes


def generate_scene_plan(text: str, style: str = "cinematic, photorealistic",
                        sentences_per_prompt=3, canon: Optional[dict] = None,
                        split_long: bool = False,
                        max_solo_scene_seconds: float = DEFAULT_MAX_SOLO_SCENE_SECONDS,
                        project_title: str = "",
                        precomputed_subject: Optional[dict] = None,
                        precomputed_semantic_beats=_PRECOMPUTE_MISSING,
                        precomputed_visual_context=_PRECOMPUTE_MISSING,
                        use_cache: bool = False) -> List[dict]:
    """
    Генерирует план сцен из текста через GPT с полным контекстом.
    Разбивает текст на части и генерирует сцены для каждой части, чтобы обойти лимиты токенов.
    GPT учитывает заданный стиль при создании визуальных описаний.
    Возвращает список объектов сцен с полями: text_segment, visual_prompt, camera_angle,
    subject_type, visible_text

    Args:
        text: Полный текст сценария
        style: Стиль изображений (например, "1940 bad quality photo", "cinematic, photorealistic")
        sentences_per_prompt: Примерное количество предложений на чанк (используется для расчета размера чанка)
    """
    text_length = len(text)
    if _prompt_provider() != "openai":
        # Видно в логе кабинета, чем писались промпты этого прогона (для сравнения качества).
        print_user(f"🧠 Промпты пишет: {prompt_llm.describe()}", "INFO")
    print_debug(f"Длина текста: {text_length} символов")
    print_debug(f"Стиль изображений: {style}")
    print_debug(f"Предложений на промт: {sentences_per_prompt}")

    # Разбиваем текст на предложения
    sentences = split_text_to_sentences(text)
    print_debug(f"Всего предложений в тексте: {len(sentences)}")
    if len(sentences) == 1 and len(text) > 1000:
        print_user(f"⚠️ ВНИМАНИЕ: Текст разбит только на 1 предложение ({len(text)} символов). Возможно, функция разбиения не распознала знаки препинания в тексте.", "WARNING")
        print_debug(f"Первые 200 символов текста: {text[:200]}")

    # Semantic v4 owns only cadence positions whose step is one. Multi-sentence units
    # in a mixed rhythm remain immutable and still produce exactly one image.
    head_step, boundary, tail_step = _parse_spp(sentences_per_prompt)
    semantic_mode = _semantic_split_enabled(sentences_per_prompt, split_long)
    if semantic_mode:
        if precomputed_semantic_beats is _PRECOMPUTE_MISSING:
            semantic_beats = _plan_semantic_beats(
                text, max_solo_scene_seconds,
                sentences_per_prompt=sentences_per_prompt,
                use_cache=use_cache,
            )
        else:
            semantic_beats = _validate_semantic_beat_plan(
                precomputed_semantic_beats, text, sentences_per_prompt, True,
                max_solo_scene_seconds,
            )
    else:
        semantic_beats = []
    if semantic_mode:
        text_chunks = [beat["text_segment"] for beat in semantic_beats]
        print_debug(
            f"Текст разбит на {len(text_chunks)} semantic beats "
            f"(полные визуальные мысли; лимит {float(max_solo_scene_seconds):g}с мягкий)"
        )
    else:
        _solo_cap = _solo_scene_chars(max_solo_scene_seconds) if split_long else None
        text_chunks = _build_scene_text_chunks(
            text,
            sentences_per_prompt=sentences_per_prompt,
            split_long=split_long,
            max_solo_scene_seconds=max_solo_scene_seconds,
        )
        _cap_note = (f"; legacy одиночная сцена ≤{_solo_cap} симв "
                     f"(≈{float(max_solo_scene_seconds):g}с)") if split_long else ""
        if boundary:
            print_debug(f"Текст разбит на {len(text_chunks)} частей (переменный шаг '{sentences_per_prompt}': по {head_step} предл. до {boundary}-го, дальше по {tail_step}{_cap_note})")
        else:
            print_debug(f"Текст разбит на {len(text_chunks)} частей по {head_step} предложений{_cap_note}")

    print_debug(f"Текст разбит на {len(text_chunks)} частей")
    if not text_chunks:
        raise ScenePlanTerminalError("исходный текст не образовал ни одного чанка сцены")

    system_prompt = f"""You are an expert Movie Director. Break the given script segment into ONE VISUAL SHOT (Scene).

CRITICAL: Generate EXACTLY ONE scene that covers ALL sentences in this segment.
DO NOT create multiple scenes - create only ONE scene for this entire segment.

VISUAL STYLE: All images must be generated in this style: "{style}"
You MUST incorporate this style into every visual description. The style affects:
- Color palette and mood
- Image quality and texture
- Lighting and atmosphere
- Overall aesthetic (e.g., "1940 bad quality photo" means grainy, black-and-white, vintage look)

Rules:
1. Follow the supplied strict JSON schema exactly.
2. Core fields (output every field required by the supplied schema; do NOT output "text_segment"; the app attaches the exact source text itself, so echoing it just wastes tokens):
   - "subject": (DECIDE THIS FIRST. Name the concrete visual focus or visible relationship THIS completed semantic beat is actually about, as a short English noun phrase — the specific person, action, process, object, place, structure, comparison or historical artifact named or clearly implied. For comparison/cause-effect, name the relationship or process and keep both required counterparts; do not collapse it into one unrelated hero object. Be specific. NEVER substitute a different entity or a generic stand-in. For an abstract claim, first show its concrete consequence, causal process, human action, comparison, or contextual object. Use a metaphor ONLY as the last resort when the completed claim truly has no literal/contextual visual; never default to jars, cracked paths, stones, empty landscapes, hands, or solitary figures merely to fill a frame.)
   - "visual_prompt": (Detailed visual description for Image Gen. The dominant subject filling the frame MUST be exactly the "subject" above — never swap in a different or more generic plant/object/animal than the one named. DO NOT repeat the style "{style}" in visual_prompt - it will be added automatically. Focus ONLY on: action, subject, composition, environment. If writing is genuinely needed, describe its physical placement (for example, one period street sign in the background) but NEVER put, quote, translate, transliterate, or invent its literal wording here. Every allowed exact string belongs ONLY in visible_text. Real, historical or well-known public figures NAMED in the text SHOULD be depicted BY NAME so the image shows the actual person — see Rule 11.)
   - "camera_angle": (Choose the angle that best serves THIS shot's content: a genuine tiny detail or a dramatic reveal -> Close-up (use SPARINGLY — keep Close-ups a MINORITY across the video, never the default for every object); an object shown in its setting -> Medium Shot or Wide Shot; a place or landscape -> Wide Shot; the on-screen narrator/host -> Medium Shot or Over-the-shoulder; tension or drama -> Low Angle or Dutch Angle; scale or layout seen from above -> Overhead. Use the SUGGESTED CAMERA ANGLE from the user message only as a tie-breaker for variety when several angles fit equally. One of: Wide Shot, Medium Shot, Close-up, Low Angle, Over-the-shoulder, Overhead, Dutch Angle. Avoid a needless repeat of the previous angle, but repeat it when that best preserves the action, continuity, or semantic claim. Do NOT default everything to Wide Shot, and do NOT default objects to Close-up.)
   - "subject_type": ("character", "environment", "object")
   - "visible_text": (An array containing the EXACT FINAL strings that must be legible in the generated image, or [] when the image needs no writing. Keep it short: at most 3 strings. VIEWER-FACING / EDITORIAL copy made for this video's audience — title cards, dates, headings, explanatory labels, diagram annotations — MUST use the language and script of THIS CURRENT NARRATION SEGMENT. DIEGETIC copy physically belonging to the depicted world — street or storefront signs, newspapers, letters, maps, monuments, historical documents — MUST use the authentic local language and script of that place and era, even when narration is in another language. Preserve proper names naturally. Never translate or transliterate authentic diegetic copy just to match narration. Do not invent decorative copy.)
3. Visual consistency means the same STYLE and recurring CHARACTERS across scenes. Preserve the same setting and concrete process while adjacent beats share a continuity_group; change it only for a real location/time/topic shift. Neighbour context resolves continuity but must never replace the current beat's meaning.
4. The scene should cover ALL sentences in the segment. Generate EXACTLY ONE scene for this segment.
5. Focus on what can be SEEN, not thoughts or feelings.
5a. HERO OF THE FRAME: make the single most concrete thing THIS segment names (the actual plant, food, organ, animal, place, event or process) the dominant subject of the shot — not a person talking about it. If the segment names a specific object, show that object large and central. Do NOT default to a generic "person gesturing in a kitchen/market/garden" unless the segment is specifically about the on-screen narrator/host.
5b. MAKE IT STRIKING and SPECIFIC: use one immediately readable composition with real depth (foreground vs background), a decisive moment of action, and a shot scale that varies from a plain wide establishing shot whenever the content allows. A comparison, causal chain, or process may require several co-dominant elements; include all of them rather than forcing an unrelated single hero. Derive the location and props strictly from THIS segment; do not reuse the previous scene's setting unless the text stays in the same place.
5c. COMPOSITION VARIETY — break the "hands holding an object" habit: across scenes, tight CLOSE-UPS and shots of disembodied HANDS gripping / holding / cupping / clutching / grasping an object must stay the rare EXCEPTION, not the default. When THIS segment is about a small hold-able object, show it IN CONTEXT — resting on a surface, sitting in its real environment, or being USED by a person shown at least from the waist up in a medium or wide shot — instead of filling the frame with a pair of hands. Use a hands-on-object close-up ONLY when the physical act of holding is literally the point of the sentence. Keep literal macro CLOSE-UPS a clear MINORITY too — this includes visual_prompt prose that begins with "Close-up of…"/"Extreme close-up of…": most shots (objects included) should place the subject inside a WIDER scene with real foreground/background depth, not fill the frame. Reserve a macro close-up (of hands OR of an object) only when a tiny physical detail is genuinely the whole point of the sentence; otherwise pull the camera back to a medium or wide establishing shot.
6. Create ONE comprehensive scene that covers all action and content in the segment. Do NOT break it into multiple scenes.
7. Return ONLY the JSON value required by the supplied schema, no markdown or code blocks.
8. IMPORTANT: Generate EXACTLY ONE scene that covers ALL sentences in this segment.
9. CRITICAL: Every visual_prompt must reflect the style "{style}" - adapt descriptions to match this aesthetic.
10. CRITICAL: Character appearances MUST match their nationality/ethnicity mentioned in the script. If Japanese, describe Japanese features; if Russian, describe Russian features, etc.
11. NAMED REAL PEOPLE — KEEP THE NAME: If THIS segment names a specific real, historical or well-known public figure (ruler, emperor, general, scientist, artist, leader, philosopher, explorer, etc.), you MUST keep that exact person BY NAME in visual_prompt and depict the ACTUAL individual, with era-accurate clothing, age and setting — e.g. 'Napoleon Bonaparte in his gray overcoat and bicorne hat surveying a battlefield', 'Albert Einstein with wild grey hair writing equations on a chalkboard', 'Cleopatra in Ptolemaic Egyptian royal regalia'. This is REQUIRED so the image shows the REAL person and not a generic stand-in. For a named god or deity, render it as a classical statue, fresco or painting of that specific deity. City and place names are always allowed. ONLY when a character is generic or fictional (not a real, named person) describe them generically, matching the nationality/ethnicity from the script.
12. HISTORICAL & CULTURAL ACCURACY: When the script is set in a specific historical era, region or culture — stated or clearly implied (e.g. India in the 1800s, ancient Rome, medieval Japan, 1920s New York) — EVERY scene must stay inside that world. Generic people's ethnicity and facial features, clothing, hairstyles, architecture, interiors, tools, vehicles, weapons and light sources must all match that era and region, for background crowds and props as much as for the main subject (e.g. no electric lighting before ~1880, no cars before ~1900, no modern clothing, no skyscrapers; crowds look like the local population of that time and place unless the text says otherwise). NEVER introduce modern or out-of-culture objects into a historical setting. If the script is contemporary or has no specific era/region, this rule is inactive — do not force a historical look."""

    # КАНОН (story_bible): даём GPT канонические имена и заставляем тегать поле entities точными
    # несклонёнными именами из списка (enum в strict-схеме) → детерминированная привязка референсов.
    canon_names = []
    if canon:
        # Every bible character is eligible for an exact manual reference,
        # including ``minor``. The strict enum prevents invented tags.
        _maj = [c for c in canon.get('characters', []) if c.get('names')]
        _loc = [l for l in canon.get('locations', []) if l.get('names')]
        canon_names = [c['names'][0] for c in _maj] + [l['names'][0] for l in _loc]
        if canon_names:
            def _ent_line(e):
                primary = e['names'][0]
                aliases = [n for n in e['names'][1:] if n and n.lower() != primary.lower()]
                head = f"{primary}" + (f" (also called: {', '.join(aliases)})" if aliases else "")
                app = (e.get('appearance') or '').strip().rstrip('.')
                return head + (f" — {app}" if app else "")
            system_prompt += (
                "\n\nKNOWN ENTITIES (recurring characters/locations), each with its FIXED look after the dash.\n"
                "(1) TAGGING — in the field \"entities\" output the EXACT canonical name (the name BEFORE any parentheses/dash) "
                "for EVERY known entity that appears in THIS segment — resolve any declension, nickname, alias, role-word, or "
                "pronoun (incl. the 'also called' forms) to that canonical name. E.g. if 'stary pszczelarz' is an alias of "
                "'Stanisław Nowak', tag «starego pszczelarza» as Stanisław Nowak. Use ONLY the canonical names; [] if none appear.\n"
                "(2) WRITING — when a KNOWN character appears, refer to them with a SHORT 2-4 word referent matching their known "
                "age/role ONLY (e.g. 'the elderly beekeeper', 'the old man', 'the young soldier') and add NOTHING else about their "
                "body, face, skin, hair or specific clothing — their FIXED look (after the dash) is inserted automatically. Spend "
                "visual_prompt on their ACTION, pose, expression and the SETTING, not on describing them. NEVER contradict their "
                "known age/role (do not call a known elderly person a 'young man'). For people NOT in this list, describe normally.\n"
                + (("Characters:\n  " + "\n  ".join(_ent_line(c) for c in _maj) + "\n") if _maj else "")
                + (("Locations:\n  " + "\n  ".join(_ent_line(l) for l in _loc)) if _loc else "")
            )
    scene_rf = _scene_response_format(canon_names)
    # сильная инструкция по entities прямо в user-сообщение чанка (иначе nano следует списку из 4 полей и шлёт [])
    entities_hint = ""
    if canon_names:
        entities_hint = (
            "\n\nENTITIES (REQUIRED, do NOT skip): fill the \"entities\" array with the EXACT canonical name(s) from the "
            "KNOWN ENTITIES list for EVERY known character/location that appears in THIS segment — including when it appears "
            "as a DECLINED form, a nickname, or a pronoun referring to it. Example: segment «Игоря завели» or «он осмотрелся» "
            "with Игорь known → \"entities\":[\"Игорь\"]. Use [] ONLY if truly none of the known entities are present."
        )

    headers = _openai_chat_headers()

    all_scenes = []

    # Ротация ракурсов по индексу сцены — лечит монотонность (GPT иначе ~90% ставит Wide Shot).
    SCENE_CAMERA_ROTATION = ["Wide Shot", "Medium Shot", "Close-up", "Low Angle",
                             "Over-the-shoulder", "Overhead", "Dutch Angle"]

    # Visual Bible (стиль/сеттинг/персонажи, ~60 слов) генерится ОДИН раз и идёт в каждый чанк
    # ВМЕСТО полного скрипта. Раньше весь текст слался в КАЖДЫЙ чанк (×N) → сотни тысяч лишних
    # input-токенов. Теперь — короткий контекст. Экономия ~90% токенов плана сцен.
    visual_context = ""
    if precomputed_visual_context is _PRECOMPUTE_MISSING:
        try:
            _vc = _get_visual_context(text, use_cache=use_cache)
            if _vc:
                visual_context = _vc
                print_debug(f"Visual Bible ({len(visual_context)} симв.) — идёт в чанки вместо полного скрипта")
        except OpenAIKeyError:
            raise  # мёртвый ключ — фатально, не деградируем
        except Exception as e:
            print_debug(f"Visual Bible не сгенерён, чанки без story-контекста: {e}")
    elif precomputed_visual_context:
        visual_context = str(precomputed_visual_context)
        print_debug(
            f"Visual Bible ({len(visual_context)} симв.) — готов параллельно до phase 2"
        )

    # ── ГЛОБАЛЬНЫЙ ЯКОРЬ СУБЪЕКТА (фикс «не та птица») ────────────────────────────
    # ОДИН раз на проект добываем канонический вид героя и вшиваем в system-префикс каждого
    # чанка + детерминированный пост-пасс ниже. Только на дефолт-пути (canon=None; при каноне
    # мульти-entity блок строже). При мульти-субъекте/без ключа — не активируется (статус-кво).
    subj_anchor_data: dict = {}
    main_subject = ""
    subject_anchor = ""
    if canon is None:
        if precomputed_subject is not None:
            # Уже посчитан в пайплайне (для авто-героя) — не зовём GPT второй раз
            subj_anchor_data = precomputed_subject or {}
        else:
            try:
                subj_anchor_data = extract_main_subject(text, title=project_title)
            except Exception as e:
                print_debug(f"extract_main_subject провалился: {e}")
                subj_anchor_data = {}
        if subj_anchor_data.get("single_subject") and subj_anchor_data.get("main_subject"):
            main_subject = subj_anchor_data["main_subject"]
            subject_anchor = (
                f"\n\nMAIN SUBJECT OF THE WHOLE VIDEO: {main_subject}.\n"
                "Whenever THIS segment is about that ONE recurring protagonist — even if the source text "
                "names it in another language, or refers to it only generically ('the bird', 'a small bird', "
                f"'it', 'the creature') — you MUST set \"subject\" and describe it in visual_prompt as "
                f"'{main_subject}' with its key visual traits. NEVER output a non-English species name; "
                "NEVER draw a generic or different animal for the protagonist.\n"
                "EXCEPTION: if the segment names a specific real, historical or well-known public figure, "
                "Rule 11 wins — keep that exact person's name (this anchor NEVER overrides a named real person). "
                "When a segment is genuinely about a DIFFERENT concrete entity, keep that other subject. "
                "For an abstract statement show its concrete consequence or causal process; use a metaphor "
                "only when the source itself makes that metaphor the claim."
            )
            system_prompt += subject_anchor
            print_user(f"🎯 Якорь субъекта активен на весь ролик: {main_subject}", "INFO")
        else:
            print_debug("Якорь субъекта не активирован (мульти-субъект / нет ключа / реальная личность)")

    # ── ПОЛНЫЙ КОНТЕКСТ КАК КЭШИРУЕМЫЙ SYSTEM-ПРЕФИКС ──────────────────────────
    # Раньше полный скрипт убрали ради экономии токенов и слали в чанк только Visual Bible.
    # Теперь возвращаем ПОЛНЫЙ скрипт + TOPIC обратно — но в SYSTEM-часть, ОДИНАКОВУЮ для
    # ВСЕХ запросов. Идентичный system-префикс ≥1024 токенов авто-кэшируется OpenAI
    # (prompt caching: cached input ≈ 25% цены обычного на gpt-4.1-nano + быстрее) → дубль 1 раз.
    # У GPT снова есть весь контекст видео, но без N-кратной переплаты.
    topic = text.strip()[:200].replace("\n", " ").strip()
    if not topic:
        topic = "(topic unavailable)"

    # ── АНТИ-КВАДРАТ: полный скрипт в префиксе — только для КОРОТКИХ роликов ──────────
    # Полный скрипт копируется в префикс КАЖДОГО из N вызовов (даже под кэшем — это 25%×N).
    # Скрипт ∝ N, вызовов N → стоимость ~N² (на 1000 сцен это десятки млн токенов).
    # Поэтому для длинных скриптов полный текст из префикса убираем и оставляем ТОЛЬКО
    # компактный Visual Bible (постоянный размер) → стоимость линейна. Bible несёт всё,
    # что нужно для консистентности (стиль/мир/повторяющийся герой); локальный контент —
    # в сегменте user-сообщения. Порог можно тюнить через SCRIPT_IN_PREFIX_MAX_CHARS.
    SCRIPT_IN_PREFIX_MAX_CHARS = 8000  # короткие ролики держат полный скрипт; выше — Bible-only
                                       # (полный скрипт × N вызовов = квадратично; Bible несёт ту же
                                       #  кросс-сегментную консистентность за константу)
    if len(text) <= SCRIPT_IN_PREFIX_MAX_CHARS and not semantic_mode:
        script_block = f"""
=== FULL SCRIPT (for context only — understand what the WHOLE video is about; illustrate ONLY the current segment given in the user message, never the whole script) ===
{text}
"""
    else:
        script_block = ""
        if semantic_mode:
            print_user("📉 Semantic beats: полный сценарий не дублируется в scene-вызовах; контекст = claim + соседи + Visual Bible", "INFO")
        else:
            print_user(f"📉 Длинный скрипт ({len(text)} симв > {SCRIPT_IN_PREFIX_MAX_CHARS}): полный текст убран из префикса, контекст = Visual Bible (линейная стоимость на масштабе)", "INFO")

    semantic_plan_digest = ""
    if semantic_mode:
        semantic_plan_digest = hashlib.sha256(json.dumps(
            [{key: beat.get(key) for key in (
                "text_segment",
            ) + _SEMANTIC_SCENE_KEYS} for beat in semantic_beats],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    system_prompt_full = system_prompt + f"""
{script_block}
TOPIC (one-line gist of the video): {topic}

STYLE & CHARACTER BIBLE (use ONLY for consistent visual STYLE, the ERA/CULTURAL SETTING of the world and recurring CHARACTERS — this is NOT a fixed location; each scene's location comes from its own segment):
{visual_context}
SEMANTIC PLAN DIGEST (cache identity only): {semantic_plan_digest or '(standard mode)'}"""

    abort_event = threading.Event()
    locked_prompt_provider = _prompt_provider()
    cache_context_digest = _scene_cache_context_digest(
        text, system_prompt_full, scene_rf, locked_prompt_provider
    )

    def assert_provider_lock(label):
        current = _prompt_provider()
        if current != locked_prompt_provider:
            raise ScenePlanTerminalError(
                f"{label}: канал промптов изменился внутри задачи "
                f"({locked_prompt_provider} → {current}); retry остановлен"
            )

    def cache_key_of(chunk_text):
        """Тот же ключ, что считает одиночный путь — иначе пакет писал бы мимо кэша."""
        return _prompt_cache_key(chunk_text, style, cache_context_digest)

    def semantic_directive(chunk_idx):
        if not semantic_mode:
            return ""
        beat = semantic_beats[chunk_idx]
        prev_text = (text_chunks[chunk_idx - 1].strip()[-500:]
                     if chunk_idx > 0 else "(none)")
        next_text = (text_chunks[chunk_idx + 1].strip()[:500]
                     if chunk_idx + 1 < len(text_chunks) else "(none)")
        elements = "; ".join(beat["required_elements"])
        relation = beat.get("continuity_relation") or "new"
        previous_group = (semantic_beats[chunk_idx - 1]["continuity_group"]
                          if chunk_idx > 0 else "(none)")
        return f"""

=== AUTHORITATIVE SEMANTIC DIRECTION ===
COMPLETE CLAIM TO SHOW: {beat['semantic_claim']}
VISUAL MODE: {beat['visual_mode']}
REQUIRED VISIBLE ELEMENTS: {elements}
CONTINUITY GROUP: {beat['continuity_group']}
CONTINUITY RELATION TO PREVIOUS BEAT: {relation}
PREVIOUS BEAT GROUP: {previous_group}
PREVIOUS BEAT (context only, do not illustrate instead): {prev_text}
NEXT BEAT (context only, do not illustrate instead): {next_text}

The image must communicate COMPLETE CLAIM TO SHOW at first glance and visibly include every REQUIRED VISIBLE ELEMENT. These requirements override generic one-subject composition advice: comparisons, cause-effect and processes may need several co-dominant elements in one readable frame. Preserve the same people/place/process when CONTINUITY RELATION is continue_previous. Do not invent a generic metaphor, jar, path, stone, empty landscape or decorative prop in place of the claim. The neighbouring beats are only for pronouns and continuity; the current claim always wins.
"""

    def single_payload_of(chunk_idx, chunk_text):
        suggested_angle = SCENE_CAMERA_ROTATION[chunk_idx % len(SCENE_CAMERA_ROTATION)]
        semantic_note = semantic_directive(chunk_idx)
        user_prompt = f"""=== CURRENT SEGMENT to illustrate (generate the visual scene for THIS segment ONLY) ===
{chunk_text}
{semantic_note}

The location/environment for THIS scene must come from the CURRENT SEGMENT and its semantic direction, not from the bible. Keep the location when its continuity group continues; change it only for a real narrative shift.

SUGGESTED CAMERA ANGLE for this scene (tie-breaker for variety only): {suggested_angle}. Prefer the angle that best fits the content per the camera_angle rule; use this suggestion only when several angles fit equally well. Avoid needless repetition, but repeat an angle when continuity or the semantic claim benefits. Do not default to Wide Shot.

NAMED REAL PEOPLE (mandatory): if THIS segment names a real, historical or well-known public figure, KEEP the exact name in visual_prompt and depict the REAL individual with era-accurate clothing, age and setting (per Rule 11) — do NOT genericize them into "the commander"/"the king". A named god or deity → render as a classical statue, fresco or painting of that specific deity. Describe generically ONLY characters who are generic or fictional (not real, named people). (City/place names are always fine.)

In visual_prompt, describe ONLY the visual content (action, subject, composition, environment) WITHOUT repeating the style "{style}" (it is applied automatically). Focus on WHAT is happening, not HOW it looks.

This segment contains the sentence(s) shown above. Generate EXACTLY ONE scene that covers ALL of them. Return ONE scene as a single JSON object with fields subject, visual_prompt, camera_angle, subject_type, visible_text.{entities_hint}"""
        return {
            "model": SCENE_GPT_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt_full},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 700,
            "temperature": 0.7,
            "response_format": scene_rf,
        }

    def note_prompt_cache_usage(result, label):
        try:
            usage = result.get("usage", {}) or {}
            prompt_tokens = usage.get("prompt_tokens", 0)
            cached_tokens = (usage.get("prompt_tokens_details", {}) or {}).get("cached_tokens", 0)
            print_debug(
                f"[cache] {label}: prompt_tokens={prompt_tokens}, cached_tokens={cached_tokens}"
            )
        except Exception:
            pass

    def request_single_scene(chunk_idx, chunk_text, initial_schema_error=None):
        """Запрашивает/чинит только один segment; schema retry не меняет provider/model."""
        label = f"часть {chunk_idx + 1}"
        base_payload = single_payload_of(chunk_idx, chunk_text)
        last_schema_error = initial_schema_error

        if last_schema_error is None:
            assert_provider_lock(label)
            attempt_started = time.monotonic()
            result = _scene_llm_request(
                base_payload, GPT_CHAT_TIMEOUT, label, abort_event=abort_event
            )
            note_prompt_cache_usage(result, label)
            try:
                scene = _scene_content_json(result, label)
                finalized = finalize_scene(
                    chunk_idx, chunk_text, scene, cache_key_of(chunk_text)
                )
                if semantic_mode:
                    print_user(
                        f"⏱ Semantic director {label}, попытка 1: "
                        f"{time.monotonic() - attempt_started:.1f}с (готово)",
                        "INFO",
                    )
                return finalized
            except ScenePlanSchemaError as exc:
                if semantic_mode:
                    print_user(
                        f"⏱ Semantic director {label}, попытка 1: "
                        f"{time.monotonic() - attempt_started:.1f}с (schema retry)",
                        "INFO",
                    )
                last_schema_error = exc

        for retry_no in range(1, SCENE_SCHEMA_MAX_RETRIES + 1):
            if abort_event.is_set():
                raise ScenePlanTransientError("план сцен остановлен после ошибки другого чанка")
            delay = SCENE_SCHEMA_RETRY_DELAY * retry_no
            print_user(
                f"⚠️ {label}: ответ нарушил JSON-схему; точечный повтор "
                f"{retry_no}/{SCENE_SCHEMA_MAX_RETRIES} через {delay}с",
                "WARNING",
            )
            time.sleep(delay)
            assert_provider_lock(label)
            corrective_payload = _scene_corrective_payload(
                base_payload, label, str(last_schema_error)
            )
            attempt_started = time.monotonic()
            result = _scene_llm_request(
                corrective_payload, GPT_CHAT_TIMEOUT, label, abort_event=abort_event
            )
            note_prompt_cache_usage(result, label)
            try:
                scene = _scene_content_json(result, label)
                finalized = finalize_scene(
                    chunk_idx, chunk_text, scene, cache_key_of(chunk_text)
                )
                if semantic_mode:
                    print_user(
                        f"⏱ Semantic director {label}, попытка {retry_no + 1}: "
                        f"{time.monotonic() - attempt_started:.1f}с (готово)",
                        "INFO",
                    )
                return finalized
            except ScenePlanSchemaError as exc:
                if semantic_mode:
                    print_user(
                        f"⏱ Semantic director {label}, попытка {retry_no + 1}: "
                        f"{time.monotonic() - attempt_started:.1f}с (schema retry)",
                        "INFO",
                    )
                last_schema_error = exc

        # Исчерпание сохраняет природу последнего отказа: если сцену не удалось
        # согласовать с каноном, кабинет должен узнать об этом по коду выхода.
        exhausted_class = (
            ScenePlanCanonSchemaError
            if isinstance(last_schema_error, ScenePlanCanonConflictError)
            else ScenePlanSchemaError
        )
        raise exhausted_class(
            f"{label}: схема не исправлена после {SCENE_SCHEMA_MAX_RETRIES} "
            f"точечных повторов ({last_schema_error})"
        )

    def request_batch_scenes(payload, pending, label):
        """Malformed whole batch is retried as one indivisible response, bounded."""
        last_schema_error = None
        for attempt in range(0, SCENE_SCHEMA_MAX_RETRIES + 1):
            if attempt:
                delay = SCENE_SCHEMA_RETRY_DELAY * attempt
                print_user(
                    f"⚠️ {label}: пакет нарушил JSON-схему; повтор "
                    f"{attempt}/{SCENE_SCHEMA_MAX_RETRIES} через {delay}с",
                    "WARNING",
                )
                time.sleep(delay)
                current_payload = _scene_corrective_payload(
                    payload, label, str(last_schema_error)
                )
            else:
                current_payload = payload
            assert_provider_lock(label)
            attempt_started = time.monotonic()
            result = _scene_llm_request(
                current_payload, (10, 120 + 20 * len(pending)), label,
                abort_event=abort_event,
            )
            try:
                data = _scene_content_json(result, label)
                scenes_got = data.get("scenes") if isinstance(data, dict) else None
                if not isinstance(scenes_got, list) or len(scenes_got) != len(pending):
                    got = len(scenes_got) if isinstance(scenes_got, list) else "не массив"
                    raise ScenePlanSchemaError(
                        f"{label}: нарушена схема пакета ({got} сцен вместо {len(pending)})"
                    )
                if semantic_mode:
                    print_user(
                        f"⏱ Semantic director {label}, попытка {attempt + 1}: "
                        f"{time.monotonic() - attempt_started:.1f}с (готово)",
                        "INFO",
                    )
                return scenes_got
            except ScenePlanSchemaError as exc:
                if semantic_mode:
                    print_user(
                        f"⏱ Semantic director {label}, попытка {attempt + 1}: "
                        f"{time.monotonic() - attempt_started:.1f}с (schema retry)",
                        "INFO",
                    )
                last_schema_error = exc
                if attempt == SCENE_SCHEMA_MAX_RETRIES:
                    break
        raise ScenePlanSchemaError(
            f"{label}: схема пакета не исправлена после {SCENE_SCHEMA_MAX_RETRIES} "
            f"повторов ({last_schema_error})"
        )

    def process_batch(indices):
        """N сегментов одним запросом; валидные siblings кэшируются до ремонта битых."""
        if abort_event.is_set():
            raise ScenePlanTransientError("план сцен остановлен после ошибки другого чанка")
        out = {}
        pending = []
        for i in indices:
            cached = _prompt_cache_get(cache_key_of(text_chunks[i]))
            if cached is not None:
                try:
                    _validate_complete_scene_plan(
                        cached, [text_chunks[i]], canon_names,
                        require_visible_text=True,
                    )
                    # A schema-valid cached scene may still omit the canonical
                    # entity tag for an alias that appears in its generated
                    # visual prose. Reject only that cache entry; never infer or
                    # auto-add scene.entities.
                    _validate_explicit_alias_entity_coverage(cached, canon)
                    if semantic_mode:
                        expected = semantic_beats[i]
                        got = cached[0]
                        for key in _SEMANTIC_SCENE_KEYS:
                            if got.get(key) != expected.get(key):
                                raise ScenePlanTerminalError(
                                    f"semantic cache mismatch: {key}"
                                )
                except ScenePlanTerminalError as exc:
                    print_user(f"⚠️ Часть {i + 1}: битый кэш проигнорирован ({exc})", "WARNING")
                    pending.append(i)
                else:
                    print_user(f"⚡ Часть {i + 1}/{len(text_chunks)}: сцена из кэша (без GPT-запроса)", "SUCCESS")
                    out[i] = cached
            else:
                pending.append(i)
        if not pending:
            return out
        if len(pending) == 1:
            out[pending[0]] = process_chunk((pending[0], text_chunks[pending[0]]))
            return out

        blocks = []
        for i in pending:
            if semantic_mode:
                blocks.append(
                    f"=== SOURCE SEGMENT {i + 1} ===\n{text_chunks[i]}\n"
                    f"{semantic_directive(i)}\n"
                    f"(camera tie-breaker only: "
                    f"{SCENE_CAMERA_ROTATION[i % len(SCENE_CAMERA_ROTATION)]})")
            else:
                blocks.append(
                    f"=== SOURCE SEGMENT {i + 1} ===\n{text_chunks[i]}\n"
                    f"(camera tie-breaker for this one: "
                    f"{SCENE_CAMERA_ROTATION[i % len(SCENE_CAMERA_ROTATION)]})")
        user_prompt = (
            f"Below are {len(pending)} ordered segments of the same script.\n"
            f"Return the SAME rules applied to EACH of them: a JSON object with key \"scenes\" — "
            f"an array of exactly {len(pending)} scene objects, in the same order.\n"
            f"Each scene must be as detailed as if you were writing it alone: do NOT shorten "
            f"visual_prompt because there are several segments.\n"
            f"Each scene's meaning comes from ITS OWN segment and authoritative semantic direction. "
            f"Neighbouring beats are context only, but preserve the same people/place/process while "
            f"the continuity_group stays the same.\n\n" + "\n\n".join(blocks))
        payload = {
            "model": SCENE_GPT_MODEL,
            "messages": [{"role": "system", "content": system_prompt_full},
                         {"role": "user", "content": user_prompt}],
            "max_tokens": 700 * len(pending),
            "temperature": 0.7,
            "response_format": _scene_array_response_format(canon_names, len(pending)),
        }
        label = f"пакет частей {pending[0] + 1}–{pending[-1] + 1}"
        scenes_got = request_batch_scenes(payload, pending, label)
        invalid = []
        for i, scene in zip(pending, scenes_got):
            try:
                out[i] = finalize_scene(
                    i, text_chunks[i], scene, cache_key_of(text_chunks[i])
                )
            except ScenePlanSchemaError as exc:
                invalid.append((i, exc))
        if invalid:
            # Each repair is independent. A nested bounded pool removes the old
            # N×retry latency; the process-wide LLM gate still caps aggregate
            # calls across this and every outer batch at VS_SCENE_THREADS.
            repair_workers = min(
                len(invalid), max(1, GPT_SCENE_PARALLEL_THREADS)
            )
            with ThreadPoolExecutor(max_workers=repair_workers) as repair_executor:
                repair_futures = {
                    repair_executor.submit(
                        request_single_scene, i, text_chunks[i], exc
                    ): i
                    for i, exc in invalid
                }
                for repair_future in as_completed(repair_futures):
                    i = repair_futures[repair_future]
                    out[i] = repair_future.result()
        return out

    def finalize_scene(chunk_idx, chunk_text, scene, cache_key):
        """Сцена от модели → готовая сцена проекта. Общая для одиночного и пакетного пути.

        Невалидная схема не маскируется fallback-кадром и останавливает весь план.
        """
        scene = _validate_scene_object(
            scene, chunk_idx, chunk_text, canon_names,
            require_visible_text=True,
        )
        # Legacy-якорь полезен для роликов про одного героя, но semantic contract уже
        # точно задаёт claim/elements. Пост-пасс не имеет права заменить одну сторону
        # comparison/cause-effect глобальным героем или дописать "large and centered".
        if not semantic_mode:
            scene['subject'] = _normalize_scene_subject(
                scene.get('subject', ''), subj_anchor_data
            )
        _vp = scene.get('visual_prompt', '')
        scene['visual_prompt'] = (
            _vp if semantic_mode else _anchor_subject(_vp, scene.get('subject', ''))
        )
        if semantic_mode:
            spec = semantic_beats[chunk_idx]
            for key in _SEMANTIC_SCENE_KEYS:
                value = spec[key]
                scene[key] = list(value) if isinstance(value, list) else value
            _validate_semantic_fields(scene, f"semantic scene {chunk_idx + 1}")
        _validate_scene_object(
            scene, chunk_idx, chunk_text, canon_names,
            require_visible_text=True,
        )
        try:
            # Run the deterministic omission detector before cache publication.
            # Convert this one-scene contract violation to the existing schema
            # repair class so a batch keeps valid siblings and regenerates only
            # the missing-tag scene. scene.entities remains model authority.
            _validate_explicit_alias_entity_coverage([scene], canon)
        except ScenePlanTerminalError as exc:
            raise ScenePlanCanonSchemaError(
                f"часть {chunk_idx + 1}: canonical entity coverage: {exc}"
            ) from exc
        out = [scene]                       # РОВНО одна сцена на чанк → счёт не плывёт
        if cache_key:
            _prompt_cache_set(cache_key, out)
        print_user(f"✅ Часть {chunk_idx + 1}: сцена готова", "SUCCESS")
        return out

    # Функция для обработки одной части текста (для параллелизации)
    def process_chunk(chunk_data):
        chunk_idx, chunk_text = chunk_data
        chunk_length = len(chunk_text)

        # Диск-кэш принимается только для этого source/context digest.
        cache_key = cache_key_of(chunk_text)
        cached = _prompt_cache_get(cache_key)
        if cached is not None:
            try:
                _validate_complete_scene_plan(
                    cached, [chunk_text], canon_names,
                    require_visible_text=True,
                )
                _validate_explicit_alias_entity_coverage(cached, canon)
                if semantic_mode:
                    expected = semantic_beats[chunk_idx]
                    got = cached[0]
                    for key in _SEMANTIC_SCENE_KEYS:
                        if got.get(key) != expected.get(key):
                            raise ScenePlanTerminalError(
                                f"semantic cache mismatch: {key}"
                            )
            except ScenePlanTerminalError as exc:
                print_user(f"⚠️ Часть {chunk_idx + 1}: битый кэш проигнорирован ({exc})", "WARNING")
            else:
                print_user(f"⚡ Часть {chunk_idx + 1}/{len(text_chunks)}: сцена из кэша (без GPT-запроса)", "SUCCESS")
                return cached

        print_user(f"📝 Обработка части {chunk_idx + 1}/{len(text_chunks)} ({chunk_length} символов)...", "INFO")

        if abort_event.is_set():
            raise ScenePlanTransientError("план сцен остановлен после ошибки другого чанка")
        return request_single_scene(chunk_idx, chunk_text)

    # Параллельная обработка частей текста
    # ВАЖНО: общий контекст (роль, правила, полный скрипт, TOPIC, Bible) лежит в ОДИНАКОВОМ
    # system-префиксе всех запросов → OpenAI кэширует его (prompt caching). Параллелизм НЕ
    # влияет на качество: GPT видит всю историю в каждом запросе.
    if len(text_chunks) > 1:
        results_by_chunk = {}

        # OpenAI gets real prefix caching, so retain its one-request warmup. Codex
        # and Claude launch independent CLI processes and gain nothing from it.
        warm_openai = locked_prompt_provider == "openai"
        if warm_openai:
            print_user(
                f"🔥 Прогрев кэша промпт-контекста: первая часть "
                f"1/{len(text_chunks)} (последовательно)...",
                "INFO",
            )
            results_by_chunk[0] = process_chunk((0, text_chunks[0]))
        remaining = list(range(1 if warm_openai else 0, len(text_chunks)))
        if remaining:
            cache_note = ", кэш-префикс прогрет" if warm_openai else ""
            print_user(
                f"🚀 Параллельная генерация {len(remaining)} частей "
                f"(до {GPT_SCENE_PARALLEL_THREADS} потоков{cache_note})...",
                "INFO",
            )
            # Используем submit вместо map для лучшего контроля и обработки ошибок
            with ThreadPoolExecutor(max_workers=min(GPT_SCENE_PARALLEL_THREADS, len(remaining))) as executor:
                futures = {}
                # Сабмитим все задачи сразу — пул сам держит max_workers одновременно.
                # (Троттл n%5→sleep раньше кормил пул ~10/сек → в полёте было ~40, а не {GPT_SCENE_PARALLEL_THREADS}.)
                _bs = _scene_batch_size()
                if _bs > 1 and not warm_openai:
                    # Preserve the exact pre-optimization request shapes: scene 1
                    # remains an individual director call and scenes 2+ keep their
                    # old batch boundaries. Only the scheduling changes — unit 1 is
                    # submitted alongside the batches instead of awaited first.
                    tail = remaining[1:]
                    _units = [[remaining[0]]] + [
                        tail[k:k + _bs] for k in range(0, len(tail), _bs)
                    ]
                else:
                    _units = (
                        [remaining[k:k + _bs] for k in range(0, len(remaining), _bs)]
                        if _bs > 1 else [[i] for i in remaining]
                    )
                if _bs > 1:
                    print_user(f"📦 Пакетами по {_bs}: {len(_units)} запросов вместо "
                               f"{len(remaining)} (префикс читается один раз на пакет)", "INFO")
                for unit in _units:
                    future = executor.submit(process_batch, unit)
                    futures[future] = unit

                for future in as_completed(futures):
                    unit = futures[future]
                    try:
                        got = future.result()
                        if not isinstance(got, dict) or set(got) != set(unit):
                            raise ScenePlanTerminalError(
                                f"пакет частей {[i + 1 for i in unit]} вернул неполный результат"
                            )
                        results_by_chunk.update(got)
                    except Exception:
                        abort_event.set()
                        for pending_future in futures:
                            if pending_future is not future:
                                pending_future.cancel()
                        raise

        if set(results_by_chunk) != set(range(len(text_chunks))):
            missing = sorted(set(range(len(text_chunks))) - set(results_by_chunk))
            raise ScenePlanTerminalError(
                f"неполный план: отсутствуют части {[idx + 1 for idx in missing]}"
            )
        for idx in range(len(text_chunks)):
            chunk_result = results_by_chunk[idx]
            if not isinstance(chunk_result, list) or len(chunk_result) != 1:
                raise ScenePlanTerminalError(
                    f"часть {idx + 1}: ожидалась ровно одна сцена"
                )
            all_scenes.append(chunk_result[0])
    else:
        # Если только одна часть, обрабатываем последовательно
        chunk_scenes = process_chunk((0, text_chunks[0]))
        if not isinstance(chunk_scenes, list) or len(chunk_scenes) != 1:
            raise ScenePlanTerminalError("часть 1: ожидалась ровно одна сцена")
        all_scenes.extend(chunk_scenes)

    # ── Анти-повтор камеры: модель иногда ставит подряд один угол. Подсказка в промпте
    # это не гарантирует (вызовы независимы), поэтому детерминированно разводим дубли здесь.
    if not semantic_mode:
        _prev_cam = None
        for i, _sc in enumerate(all_scenes):
            _ca = _sc.get('camera_angle')
            if _ca and _ca == _prev_cam:
                for _off in range(1, len(SCENE_CAMERA_ROTATION) + 1):
                    _cand = SCENE_CAMERA_ROTATION[(i + _off) % len(SCENE_CAMERA_ROTATION)]
                    if _cand != _prev_cam:
                        _sc['camera_angle'] = _cand
                        _ca = _cand
                        break
            _prev_cam = _ca

        # Legacy anti-closeup post-pass; semantic director owns composition in v4.
        all_scenes = _reduce_closeup_bias(all_scenes)

    if semantic_mode:
        all_scenes = _validate_semantic_scene_plan(
            all_scenes, text, canon_names, sentences_per_prompt, True,
            max_solo_scene_seconds, require_visible_text=True,
        )
    else:
        all_scenes = _validate_complete_scene_plan(
            all_scenes, text_chunks, canon_names, require_visible_text=True
        )
    print_user(f"✅ Сгенерирован полный план из {len(all_scenes)} сцен "
               f"(по одной на каждую из {len(text_chunks)} частей)", "SUCCESS")
    return all_scenes


def extract_prompt_from_brackets(text: str) -> Optional[str]:
    """Извлекает промт из [квадратных скобок] и чистит от мусора"""
    match = re.search(r'\[([^\]]+)\]', text)
    if match:
        raw_prompt = match.group(1).strip()

        # Удаляем технические префиксы, которые иногда сует GPT
        prefixes_to_remove = ["PROMPT:", "Prompt:", "SCENE:", "Scene:", "Visual:", "Description:", "IMAGE:", "Image:"]
        for prefix in prefixes_to_remove:
            if raw_prompt.upper().startswith(prefix.upper()):
                raw_prompt = raw_prompt[len(prefix):].strip()

        # Удаляем двоеточие в начале, если осталось
        if raw_prompt.startswith(":"):
            raw_prompt = raw_prompt[1:].strip()

        # Если промпт стал пустым или слишком коротким - это ошибка
        if len(raw_prompt) < 5:
            return None

        # Imagen 4 держит длинные промпты, увеличим лимит до 800
        return raw_prompt[:800]
    return None


def save_base64_to_file(data_uri: str, output_path: str,
                        validate_image: Optional[bool] = None) -> bool:
    """Атомарно сохраняет provider payload; изображения декодирует до replace."""
    temp_path = None
    try:
        if ',' in data_uri:
            _header, encoded = data_uri.split(',', 1)
        else:
            encoded = data_uri

        decoded = base64.b64decode(encoded, validate=True)
        if not decoded:
            raise ValueError("пустой результат изображения")
        image_suffix = os.path.splitext(str(output_path))[1].casefold()
        should_validate_image = (
            validate_image if validate_image is not None
            else image_suffix in {".jpg", ".jpeg", ".png", ".webp"}
            or str(data_uri).casefold().startswith("data:image/")
        )
        if should_validate_image:
            from io import BytesIO
            from PIL import Image
            with Image.open(BytesIO(decoded)) as probe:
                image_format = str(probe.format or "").upper()
                if image_format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError(f"неподдерживаемый формат изображения: {image_format}")
                if probe.width <= 0 or probe.height <= 0:
                    raise ValueError("некорректные размеры изображения")
                probe.verify()
            with Image.open(BytesIO(decoded)) as decoded_image:
                decoded_image.load()
        output_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(output_path)}.", suffix=".tmp",
            dir=os.path.dirname(output_path),
        )
        with os.fdopen(fd, 'wb') as f:
            f.write(decoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, output_path)
        temp_path = None
        return True
    except Exception as e:
        print_debug(f"Ошибка сохранения файла: {e}")
        import traceback
        print_debug(traceback.format_exc())
        return False
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def image_to_base64(image_path: str, max_size_mb: float = 10.0) -> Optional[str]:
    """
    Конвертирует изображение в base64 data URI
    Валидирует размер файла и при необходимости сжимает изображение
    """
    try:
        # Проверяем размер файла
        file_size = os.path.getsize(image_path) / (1024 * 1024)  # MB
        if file_size > max_size_mb:
            print_debug(f"Изображение слишком большое ({file_size:.2f}MB > {max_size_mb}MB): {os.path.basename(image_path)}")
            # Пробуем сжать через PIL
            try:
                from PIL import Image
                img = Image.open(image_path)
                # Сохраняем во временный файл с качеством 85%
                import tempfile
                temp_file = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
                temp_path = temp_file.name
                temp_file.close()

                # Конвертируем в RGB если нужно
                if img.mode in ('RGBA', 'LA', 'P'):
                    rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    rgb_img.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                    img = rgb_img

                # Сохраняем с качеством 85%
                img.save(temp_path, 'JPEG', quality=85, optimize=True)
                image_path = temp_path
                new_size = os.path.getsize(image_path) / (1024 * 1024)
                print_debug(f"Изображение сжато до {new_size:.2f}MB")
            except Exception as e:
                print_debug(f"Не удалось сжать изображение: {e}")
                # Продолжаем с оригиналом

        with open(image_path, 'rb') as f:
            data = f.read()

        # Проверяем финальный размер base64 (примерно на 33% больше оригинала)
        base64_size_mb = (len(data) * 4 / 3) / (1024 * 1024)
        if base64_size_mb > max_size_mb * 1.5:
            print_debug(f"Предупреждение: base64 размер будет {base64_size_mb:.2f}MB")

        ext = os.path.splitext(image_path)[1].lower()
        mime_types = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.webp': 'image/webp',
            '.gif': 'image/gif'
        }
        mime = mime_types.get(ext, 'image/jpeg')

        encoded = base64.b64encode(data).decode('utf-8', errors='replace')
        return f"data:{mime};base64,{encoded}"
    except Exception as e:
        print_debug(f"Ошибка чтения изображения {image_path}: {e}")
        import traceback
        print_debug(traceback.format_exc())
        return None


# ============ GPT API ============

def generate_visual_context(text: str) -> Optional[str]:
    """
    Генерирует визуальный контекст (Visual Context) из текста для единого стиля во всем видео.
    Всегда вызывается автоматически для обеспечения консистентности.
    """
    # Берем первые 4000 символов для понимания сеттинга
    sample_text = text[:4000].strip()
    if len(text) > 4000:
        sample_text += "..."

    system_prompt = """Analyze the script and write a concise VISUAL BIBLE (max 100 words) in English. Include: 1. Visual Style (e.g. 1940s photo, cinematic). 2. World/era and atmosphere: when the script has a specific setting, name the EXACT time period and region/culture (e.g. "British colonial India, 1850s"), what typical people of that world look like (ethnicity, period clothing), and the architecture and technology level of that era; then add one short line "NEVER show: ..." listing the most likely anachronisms (e.g. modern clothing, cars, electric lights, skyscrapers). This is the broad world and mood — NOT a single fixed location; individual scenes will change location to match their own content. If the script is contemporary or timeless, keep this part brief and generic and skip the NEVER line. 3. Recurring character ONLY if the script has a persistent on-screen narrator/host who is literally shown on camera: describe them in one short clause (appearance matching their nationality/ethnicity from the script). If there is NO such persistent on-screen person, write exactly "NO fixed character — each scene's hero is the specific subject, object, place or process that the segment names" and do NOT invent a presenter. Never inject a person into segments that describe plants, food, objects, places, or processes. CRITICAL: Do NOT lock the whole video into one room or one location — describe the world/era, not a single recurring set. CRITICAL: Never mention text, words, letters, numbers, or any written content. CRITICAL: Never mention names of famous people, celebrities, historical figures, or real persons. Describe generic characters only. Output as a single paragraph."""

    headers = _openai_chat_headers()

    payload = {
        "model": GPT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Script excerpt:\n{sample_text}"}
        ],
        "max_tokens": 250,
        "temperature": 0.7
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = _llm_post(
                OPENAI_API_URL,
                headers=headers,
                json=payload,
                timeout=GPT_CHAT_TIMEOUT
            )

            _raise_if_openai_auth_failed(response)  # 401/403/invalid_api_key → фатально (до деградации)

            if response.status_code == 429:
                wait_time = RETRY_DELAY * (attempt + 1)
                print_user(f"⏳ Rate limit GPT (Visual Context), ждем {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                time.sleep(wait_time)
                continue

            response.raise_for_status()
            try:
                result = response.json()
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                print_debug(f"Ошибка декодирования JSON ответа GPT: {e}")
                text = response.content.decode('utf-8', errors='replace')
                result = json.loads(text)

            content = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()

            # Очищаем от возможных кавычек и скобок
            content = content.strip('"\'[]')

            if content and len(content) > 20:
                print_debug(f"Visual Context сгенерирован: {content[:100]}")
                return content
            else:
                print_debug(f"Visual Context слишком короткий или пустой: {content[:100]}")
                return None

        except requests.exceptions.RequestException as e:
            error_type = type(e).__name__
            print_user(f"⚠️ Ошибка GPT (Visual Context, попытка {attempt + 1}/{MAX_RETRIES}): {error_type}: {str(e)[:150]}", "WARNING")
            if attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                print_user(f"🔄 Повтор через {wait_time}с...", "WARNING")
                time.sleep(wait_time)

    print_error_user_friendly("Не удалось сгенерировать визуальный контекст")
    return None


def _validate_visual_context_artifact(value) -> str:
    """Strict gate for a reusable Visual Bible string."""
    if not isinstance(value, str) or len(value.strip()) <= 20:
        raise ScenePlanTerminalError(
            "visual_context cache пуст или короче допустимого минимума"
        )
    return value.strip()


def _get_visual_context(text: str, use_cache: bool = False) -> Optional[str]:
    """Versioned source-hash cache around the unchanged Visual Bible call."""
    identity = _planning_artifact_identity(
        "visual_context",
        text,
        algorithm_version=1,
        prompt_version="visual-context-v1",
        cadence={"sample_chars": 4000},
        payload_model=GPT_MODEL,
    )
    if use_cache:
        cached = _planning_artifact_cache_get(identity)
        if cached is not None:
            try:
                checked = _validate_visual_context_artifact(cached)
            except ScenePlanError as exc:
                print_user(
                    f"⚠️ Visual Bible: битый hash-cache проигнорирован ({exc})",
                    "WARNING",
                )
            else:
                print_user("⚡ Visual Bible из source-hash cache (без LLM)", "SUCCESS")
                return checked

    result = generate_visual_context(text)
    if result is None:
        return None
    try:
        checked = _validate_visual_context_artifact(result)
    except ScenePlanError:
        # Preserve the existing generate_visual_context contract: a short/empty
        # answer is simply unavailable and is never made reusable.
        return None
    if use_cache:
        _planning_artifact_cache_set(identity, checked)
    return checked


# ============================================================================
# АНАЛИЗ ИСТОРИИ: персонажи + локации (story_bible) для ref-aware генерации
# ============================================================================

STORY_BIBLE_FILE = "story_bible.json"  # живёт в {project_dir}/_ASSETS/


def _story_bible_path(project_dir: str) -> str:
    return os.path.join(project_dir, "_ASSETS", STORY_BIBLE_FILE)


def _json_bytes(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def _atomic_write_bytes(path: str, payload: bytes) -> None:
    """Durable same-filesystem replace с уникальным temp-файлом."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _atomic_write_json(path: str, data) -> None:
    _atomic_write_bytes(path, _json_bytes(data))


def load_story_bible(project_dir: str) -> Optional[dict]:
    """Читает _ASSETS/story_bible.json (None если нет/битый)."""
    p = _story_bible_path(project_dir)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            d = json.load(f)
        if isinstance(d, dict) and ("characters" in d or "locations" in d):
            d.setdefault("characters", [])
            d.setdefault("locations", [])
            return d
    except Exception as e:
        print_debug(f"story_bible повреждён: {e}")
    return None


def _extract_json_object(content: str) -> Optional[dict]:
    """Достаёт JSON-объект из ответа GPT (срезает ```json-заборы и текст вокруг)."""
    if not content:
        return None
    s = content.strip()
    # убрать markdown-заборы
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    # взять от первой { до последней }
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        s = s[i:j + 1]
    try:
        return json.loads(s)
    except Exception as e:
        print_debug(f"analyze_story: JSON не распарсился: {e} | {s[:200]}")
        return None


STORY_ANALYSIS_CHUNK_CHARS = 3500   # размер фрагмента для анализа (nano точнее на коротком)
STORY_ANALYSIS_MAX_WORKERS = 20     # bounded concurrency; every source chunk is processed.
_ROLE_RANK = {"protagonist": 3, "major": 2, "minor": 1}


def _stronger_role(a: str, b: str) -> str:
    return a if _ROLE_RANK.get(a, 1) >= _ROLE_RANK.get(b, 1) else b


_STORY_ANALYSIS_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "story_entities",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["characters", "locations"],
            "properties": {
                "characters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "id", "identity_names", "generic_mentions",
                            "role", "appearance",
                        ],
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "identity_names": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "generic_mentions": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "role": {"type": "string", "enum": ["protagonist", "major", "minor"]},
                            "appearance": {"type": "string", "minLength": 1},
                        },
                    },
                },
                "locations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "id", "identity_names", "generic_mentions", "appearance",
                        ],
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "identity_names": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "generic_mentions": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "appearance": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
        },
    },
}


def _analyze_text_chunk(chunk_text: str) -> Optional[dict]:
    """ОДИН GPT-вызов: персонажи/локации из ОДНОГО фрагмента. Возвращает {characters,locations} (норм.) или None."""
    system_prompt = (
        "You are a story analyst for a faceless video generator. From THIS passage extract the CHARACTERS and LOCATIONS "
        "that appear in it. Output STRICT JSON ONLY — no prose, no markdown fences.\n"
        "Schema:\n"
        '{"characters":[{"id":"<short_latin_ascii_slug>","identity_names":["<proper name or uniquely identifying title IN THE ORIGINAL LANGUAGE>"],'
        '"generic_mentions":["<non-unique role/species/group words IN THE ORIGINAL LANGUAGE>"],'
        '"role":"protagonist|major|minor","appearance":"<English PERMANENT identity only>"}],'
        '"locations":[{"id":"<short_latin_ascii_slug>","identity_names":["<proper place name IN THE ORIGINAL LANGUAGE>"],'
        '"generic_mentions":["<non-unique place-type words IN THE ORIGINAL LANGUAGE>"],'
        '"appearance":"<English: stable place type, key structures, era/atmosphere>"}]}\n'
        "IDENTITY RULE (critical): identity_names are ONLY proper names, unique nicknames, or titles that uniquely identify "
        "one person/place in this passage (e.g. 'Xenophon', 'Athens', 'John the Baptist'). Put non-unique roles, ages, "
        "species, peoples, groups, and place types ONLY in generic_mentions in whatever language/script the passage uses "
        "(e.g. 'boy', 'men', 'Greeks', 'мальчик', 'Junge', 'παιδί', 'city'). A generic word NEVER becomes an identity alias, "
        "even when it recurs. If an entity has no unique name, return identity_names:[]; keep its generic words in "
        "generic_mentions. NEVER put a descriptive clause or one-off paraphrase in either list. Historical names from the "
        "source are valid identity_names. "
        "APPEARANCE RULE (critical): describe ONLY the character's PERMANENT identity — approx age, ethnicity/nationality, "
        "face shape, hair color/length/type, eye color, facial hair, body build, signature STABLE clothing, permanent marks. "
        "15-20 fixed concrete attributes, no synonyms. This text is injected into EVERY image of the character, so it must "
        "be pose-neutral and scene-neutral. NEVER include transient state: a current pose/posture, an action in progress, an "
        "emotion, what is happening in one scene, dirt or temporary condition (e.g. 'sitting on floor', 'clothes not changed "
        "for days', 'leaning against wall', 'crying'), or any location.\n"
        "LOCATION APPEARANCE RULE: describe stable geography, architecture, materials and era only; omit current lighting, "
        "weather, time of day, crowds, actions and other scene-specific conditions.\n"
        "Rules: id MUST be latin ascii (transliterate). identity_names and generic_mentions MUST preserve the source "
        "language/script. appearance MUST be English and use a historically plausible generic depiction rather than "
        "claiming an exact recognizable likeness for a real person; never mention text/letters/numbers. If this passage "
        "has no person, return characters:[]. Keep the SAME id and identity_names for the same named entity across passages."
    )
    headers = _openai_chat_headers()
    payload = {
        "model": SCENE_GPT_MODEL,  # надёжное извлечение персонажей (nano дублировал/путал роли)
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Passage:\n{chunk_text}"}],
        "max_tokens": 1600,  # запас под несколько персонажей с полной appearance (иначе обрезка→битый JSON)
        "temperature": 0.2,
        "response_format": _STORY_ANALYSIS_RESPONSE_FORMAT,  # strict → ВСЕГДА валидный JSON (без парс-ошибок)
    }
    for attempt in range(MAX_RETRIES):
        try:
            response = _llm_post(OPENAI_API_URL, headers=headers, json=payload, timeout=GPT_CHAT_TIMEOUT)
            _raise_if_openai_auth_failed(response)
            if response.status_code == 429:
                time.sleep(RETRY_DELAY * (attempt + 1)); continue
            response.raise_for_status()
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            data = _extract_json_object(content)
            if not isinstance(data, dict):
                return None

            def _norm_list(items, with_role):
                if not isinstance(items, list):
                    raise ScenePlanSchemaError("story entity collection must be an array")
                out = []
                for e in items:
                    if not isinstance(e, dict):
                        raise ScenePlanSchemaError("story entity must be an object")

                    def _aliases(field):
                        values = e.get(field)
                        if not isinstance(values, list):
                            raise ScenePlanSchemaError(
                                f"story entity {field} must be an array"
                            )
                        normalized = {}
                        for value in values:
                            if not isinstance(value, str) or not value.strip():
                                raise ScenePlanSchemaError(
                                    f"story entity {field} contains an invalid alias"
                                )
                            try:
                                key = ref_contract.normalize_alias(value)
                            except ref_contract.ReferenceContractError as exc:
                                raise ScenePlanSchemaError(
                                    f"story entity {field} contains an invalid alias"
                                ) from exc
                            normalized.setdefault(key, value.strip())
                        return normalized

                    identity_by_alias = _aliases("identity_names")
                    generic_by_alias = _aliases("generic_mentions")
                    # Semantic classification is conservative: if the model
                    # emits an alias in both lists, non-unique/generic wins and
                    # that token can never become reference authority.
                    for alias in generic_by_alias:
                        identity_by_alias.pop(alias, None)
                    identity_names = list(identity_by_alias.values())
                    generic_mentions = list(generic_by_alias.values())
                    appearance = str(e.get("appearance", "")).strip()
                    usable_aliases = sorted(
                        set(identity_by_alias) | set(generic_by_alias)
                    )
                    # A row without any matchable alias or without a permanent
                    # appearance cannot participate in story/reference binding.
                    # Dropping only that unusable extraction is safer than
                    # rejecting every otherwise valid entity from the chunk.
                    if not appearance or not usable_aliases:
                        print_debug(
                            "analyze_story: dropped unusable entity row "
                            f"(kind={'character' if with_role else 'location'}, "
                            f"aliases={len(usable_aliases)}, "
                            f"appearance={bool(appearance)})"
                        )
                        continue
                    gid = re.sub(r"[^a-z0-9_]+", "", str(e.get("id", "")).lower().replace(" ", "_"))
                    if not gid:
                        # The model schema guarantees a non-empty id but cannot
                        # force Latin ASCII.  Entity ids are not identity
                        # authority; aliases are.  Derive a stable safe id from
                        # the already-normalized usable aliases instead of
                        # failing a multilingual chunk.
                        kind = "character" if with_role else "location"
                        digest = hashlib.sha256(
                            (kind + "\0" + "\0".join(usable_aliases)).encode("utf-8")
                        ).hexdigest()[:12]
                        gid = f"{kind}_{digest}"
                    row = {
                        "id": gid,
                        "identity_names": identity_names,
                        "generic_mentions": generic_mentions,
                        "appearance": appearance,
                    }
                    if with_role:
                        row["role"] = str(e.get("role", "minor")).lower()
                    out.append(row)
                return out

            return {"characters": _norm_list(data.get("characters"), True),
                    "locations": _norm_list(data.get("locations"), False)}
        except OpenAIKeyError:
            raise
        except ScenePlanError:
            raise
        except requests.exceptions.RequestException:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
        except Exception as e:
            print_debug(f"_analyze_text_chunk ошибка: {e}")
            return None
    return None


_IDENTITY_LINK_RF = {
    "type": "json_schema",
    "json_schema": {
        "name": "identity_link_decision", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "decision": {"type": "string", "enum": ["same", "distinct", "ambiguous"]},
                "evidence": {"type": "string", "minLength": 1},
                "canonical_appearance": {"type": "string"},
            },
            "required": ["decision", "evidence", "canonical_appearance"],
        },
    },
}


def _explicit_story_identity_link(candidates: List[dict],
                                  is_char: bool = True) -> dict:
    """Request one explicit, auditable link and canonical appearance decision."""
    if len(candidates) < 2 or not _has_prompt_channel():
        return {
            "decision": "ambiguous",
            "evidence": "no explicit linker available",
            "canonical_appearance": "",
        }
    kind = "person" if is_char else "physical location"
    rows = [{
        "candidate_id": row.get("id"),
        "names": row.get("names") or [],
        "appearance": row.get("appearance") or "",
        "source_excerpt": row.get("_identity_link_context") or "",
    } for row in candidates]
    system = (
        f"You are resolving candidates for one {kind} registry across story chunks. "
        "Identity authority is ONLY names plus source_excerpt narrative context. The appearance "
        "fields are independently generated visual proposals, NOT identity evidence. Never decide "
        "DISTINCT or AMBIGUOUS merely because generated appearance proposals disagree. An appearance "
        "fact may affect identity only when that exact permanent fact is explicitly stated in the "
        "corresponding source_excerpt. In particular, ignore unsupported differences in estimated "
        "age, hair, beard, face, body, clothing, lighting, weather, pose, emotion or verbosity. "
        "Return SAME when the excerpts establish one recurring entity; a repeated exact proper name "
        "with compatible narrative continuity, including contiguous mentions of the same titled "
        "person/place, is positive evidence. Return DISTINCT when source excerpts establish separate "
        "entities despite a shared name. Return AMBIGUOUS only when source-context identity evidence "
        "is insufficient or the source excerpts themselves state conflicting permanent identity facts "
        "(such as two different people, explicit incompatible biography, or incompatible geography). "
        "For SAME, canonical_appearance MUST be one English, pose-neutral, scene-neutral description. "
        "Retain compatible high-level role, culture, era, place type and stable source-grounded traits; "
        "drop conflicting unsupported biometric or scene details from the generated proposals. If the "
        "source has no physical details, produce a neutral historically plausible role/era depiction "
        "without asserting unique biometric facts. For DISTINCT or AMBIGUOUS return "
        "canonical_appearance as an empty string. Ground identity evidence in source excerpts, never "
        "in generated appearance wording."
    )
    payload = {
        "model": SCENE_GPT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(rows, ensure_ascii=False)},
        ],
        "max_tokens": 500,
        "temperature": 0.0,
        "response_format": _IDENTITY_LINK_RF,
    }
    try:
        response = _llm_post(
            OPENAI_API_URL,
            headers=_openai_chat_headers(),
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        data = json.loads(response.json()["choices"][0]["message"]["content"])
    except OpenAIKeyError:
        raise
    except Exception as exc:
        raise ScenePlanTransientError(
            "explicit story identity linker failed"
        ) from exc
    if (
        not isinstance(data, dict)
        or set(data) != {"decision", "evidence", "canonical_appearance"}
        or data.get("decision") not in {"same", "distinct", "ambiguous"}
        or not isinstance(data.get("evidence"), str) or not data["evidence"].strip()
        or not isinstance(data.get("canonical_appearance"), str)
        or (
            data.get("decision") == "same"
            and not data["canonical_appearance"].strip()
        )
        or (
            data.get("decision") != "same"
            and bool(data["canonical_appearance"].strip())
        )
    ):
        raise ScenePlanTerminalError("explicit story identity linker returned invalid decision")
    return {
        "decision": data["decision"],
        "evidence": data["evidence"].strip(),
        "canonical_appearance": data["canonical_appearance"].strip(),
    }


def _consolidate_characters(chars: List[dict], is_char: bool = True) -> List[dict]:
    """Merge only identities corroborated by deterministic candidate evidence and context.

    Chunk-local ids and an LLM's semantic grouping are not identity authority.
    Exact alias overlap or a repeated analyser-declared stable id may only place
    rows before the contextual linker.  The linker must still confirm SAME and
    returns the canonical permanent appearance, so grammatical case drift such
    as Weles/Welesa cannot create two reference identities.  A stable-id-only
    collision that the linker does not confirm remains as separate rows; an
    unresolved exact alias collision stays fail-closed.
    """
    if not chars or len(chars) < 2:
        return chars

    aliases = []
    for index, row in enumerate(chars):
        try:
            row_aliases = {
                _story_identity_component_alias(name, is_char=is_char)
                for name in (row.get("names") or [])
            }
        except ref_contract.ReferenceContractError as exc:
            raise ScenePlanTerminalError(
                f"story identity candidate {index + 1} has invalid alias"
            ) from exc
        if not row_aliases:
            raise ScenePlanTerminalError(
                f"story identity candidate {index + 1} has no canonical alias"
            )
        aliases.append(row_aliases)

    ignored_candidate_keys = {
        "", "entity", "character", "person", "location", "place", "unknown",
    }
    candidate_keys = []
    for row in chars:
        key = re.sub(
            r"[^a-z0-9_]+", "",
            str(row.get("_identity_candidate_key") or "").casefold(),
        )
        candidate_keys.append("" if key in ignored_candidate_keys else key)

    # Exact-alias overlap defines candidates that could be the same identity.
    # Components make overlap order-independent. A chain is only a candidate
    # set: the contextual linker must still confirm the whole component, so a
    # bridge alias never becomes unconditional or transitive identity authority.
    remaining = set(range(len(chars)))
    components = []
    while remaining:
        component = {remaining.pop()}
        component_aliases = set().union(*(aliases[i] for i in component))
        changed = True
        while changed:
            changed = False
            for other in tuple(remaining):
                same_candidate_key = bool(
                    candidate_keys[other]
                    and any(
                        candidate_keys[index] == candidate_keys[other]
                        for index in component
                    )
                )
                if component_aliases & aliases[other] or same_candidate_key:
                    remaining.remove(other)
                    component.add(other)
                    component_aliases.update(aliases[other])
                    changed = True
        components.append(sorted(component))

    out = []
    for component in sorted(components, key=lambda values: values[0]):
        if len(component) == 1:
            row = dict(chars[component[0]])
            row.pop("_identity_link_context", None)
            row.pop("_identity_candidate_key", None)
            row.setdefault("identity_provenance", [str(row.get("id") or "")])
            row.setdefault("merge_policy", "single-candidate")
            row.setdefault("manual_binding_safe", True)
            out.append(row)
            continue
        shared = sorted(set.intersection(*(aliases[index] for index in component)))
        component_aliases = sorted(
            set().union(*(aliases[index] for index in component))
        )
        repeated_candidate_keys = sorted({
            candidate_keys[index]
            for index in component
            if candidate_keys[index]
            and sum(
                candidate_keys[other] == candidate_keys[index]
                for other in component
            ) > 1
        })
        label = (
            shared[0] if shared
            else (
                f"analyser id {repeated_candidate_keys[0]}"
                if repeated_candidate_keys else
                "transitive chain [" + " ↔ ".join(component_aliases) + "]"
            )
        )
        candidates = [chars[index] for index in component]
        link = _explicit_story_identity_link(candidates, is_char=is_char)
        if link.get("decision") != "same":
            has_exact_alias_overlap = any(
                bool(aliases[left] & aliases[right])
                for position, left in enumerate(component)
                for right in component[position + 1:]
            )
            if has_exact_alias_overlap:
                raise ScenePlanCanonConflictError(
                    "story identity alias неоднозначен: "
                    f"{label!r}; explicit linker decided {link.get('decision')}"
                )
            # A model-declared id is only a candidate key. If source context
            # does not confirm identity, preserve the independent entities.
            for index in component:
                row = dict(chars[index])
                row.pop("_identity_link_context", None)
                row.pop("_identity_candidate_key", None)
                row.setdefault(
                    "identity_provenance", [str(row.get("id") or "")]
                )
                row.setdefault("merge_policy", "single-candidate")
                row.setdefault("manual_binding_safe", True)
                out.append(row)
            continue
        canonical_appearance = str(link.get("canonical_appearance") or "").strip()
        if not canonical_appearance:
            raise ScenePlanTerminalError(
                "explicit story identity linker omitted canonical permanent appearance"
            )
        base = dict(chars[component[0]])
        base.pop("_identity_link_context", None)
        base.pop("_identity_candidate_key", None)
        base["appearance"] = canonical_appearance
        base["names"] = sorted({
            str(name).strip()
            for index in component
            for name in (chars[index].get("names") or [])
            if str(name).strip()
        })
        if is_char:
            role = str(base.get("role") or "minor")
            for index in component[1:]:
                role = _stronger_role(role, chars[index].get("role", "minor"))
            base["role"] = role
        base["identity_provenance"] = sorted({
            str(candidate_id)
            for index in component
            for candidate_id in (
                chars[index].get("identity_provenance") or [chars[index].get("id")]
            )
            if str(candidate_id or "")
        })
        base["merge_policy"] = "explicit-link-canonical-appearance-v2"
        base["identity_link_evidence"] = str(link.get("evidence") or "")
        # A generated story identity may safely reuse this deterministic merge,
        # but a user photo is stronger identity authority and may not be bound
        # across multiple raw candidates without an explicit disambiguation UI.
        base["manual_binding_safe"] = False
        out.append(base)

    if is_char and out:
        protagonists = [row for row in out if row.get("role") == "protagonist"]
        if len(protagonists) > 1:
            for row in protagonists[1:]:
                row["role"] = "major"
        elif not protagonists:
            out[0]["role"] = "protagonist"
    if len(out) != len(chars):
        print_user(
            f"🔗 Проверяемая консолидация ({'персонажи' if is_char else 'локации'}): "
            f"{len(chars)} → {len(out)}",
            "INFO",
        )
    return out


def _story_identity_component_alias(name: str, is_char: bool = True) -> str:
    """Candidate-only alias used to decide which rows need contextual linking.

    English location mentions commonly alternate between ``Mediterranean`` and
    ``the Mediterranean`` across chunks. Ignoring the leading definite article
    only places those location rows before the existing fail-closed linker; it
    never changes persisted aliases, scene matching, reference normalization or
    character behavior. Names such as ``The Hague`` therefore still require an
    explicit SAME decision and are never merged automatically.
    """
    alias = ref_contract.normalize_alias(name)
    if not is_char and alias.startswith("the ") and alias[4:].strip():
        return alias[4:].strip()
    return alias


def _validate_story_bible(bible) -> dict:
    """Strict reusable-cache gate for the existing story-bible contract."""
    if not isinstance(bible, dict):
        raise ScenePlanTerminalError("story_bible cache должен быть объектом")
    checked = {"characters": [], "locations": []}
    seen_ids = set()
    alias_owners = {}
    for kind in ("characters", "locations"):
        rows = bible.get(kind)
        if not isinstance(rows, list):
            raise ScenePlanTerminalError(f"story_bible cache: {kind} должен быть массивом")
        for index, raw in enumerate(rows):
            if not isinstance(raw, dict):
                raise ScenePlanTerminalError(
                    f"story_bible cache: {kind}[{index}] должен быть объектом"
                )
            entity_id = raw.get("id")
            names = raw.get("names")
            appearance = raw.get("appearance")
            if (not isinstance(entity_id, str) or not entity_id.strip()
                    or entity_id in seen_ids):
                raise ScenePlanTerminalError(
                    f"story_bible cache: {kind}[{index}] имеет "
                    "некорректный или глобально дублирующийся id"
                )
            if (not isinstance(names, list) or not names
                    or any(not isinstance(name, str) or not name.strip() for name in names)):
                raise ScenePlanTerminalError(
                    f"story_bible cache: {kind}[{index}] имеет некорректные names"
                )
            if not isinstance(appearance, str) or not appearance.strip():
                raise ScenePlanTerminalError(
                    f"story_bible cache: {kind}[{index}] имеет некорректный appearance"
                )
            row = dict(raw)
            provenance = row.get("identity_provenance")
            merge_policy = row.get("merge_policy")
            manual_binding_safe = row.get("manual_binding_safe")
            link_evidence = row.get("identity_link_evidence")
            provenance_fields = (
                provenance is not None
                or merge_policy is not None
                or manual_binding_safe is not None
                or link_evidence is not None
            )
            if provenance_fields:
                if (
                    not isinstance(provenance, list) or not provenance
                    or any(not isinstance(value, str) or not value for value in provenance)
                    or len(set(provenance)) != len(provenance)
                    or merge_policy not in {
                        "single-candidate",
                        "explicit-link-exact-alias-appearance-v1",
                        "explicit-link-canonical-appearance-v2",
                    }
                    or not isinstance(manual_binding_safe, bool)
                    or (
                        merge_policy == "single-candidate"
                        and (len(provenance) != 1 or manual_binding_safe is not True)
                    )
                    or (
                        merge_policy in {
                            "explicit-link-exact-alias-appearance-v1",
                            "explicit-link-canonical-appearance-v2",
                        }
                        and (
                            len(provenance) < 2 or manual_binding_safe is not False
                            or not isinstance(link_evidence, str)
                            or not link_evidence.strip()
                        )
                    )
                    or merge_policy == "single-candidate" and link_evidence is not None
                ):
                    raise ScenePlanTerminalError(
                        f"story_bible cache: {kind}[{index}] имеет "
                        "некорректный identity provenance"
                    )
            if kind == "characters" and row.get("role") not in _ROLE_RANK:
                raise ScenePlanTerminalError(
                    f"story_bible cache: characters[{index}] имеет некорректную role"
                )
            seen_ids.add(entity_id)
            owner = f"{kind}[{index}]/{entity_id}"
            for name in names:
                try:
                    alias = ref_contract.normalize_alias(name)
                except ref_contract.ReferenceContractError as exc:
                    raise ScenePlanTerminalError(
                        f"story_bible cache: {owner} имеет некорректное имя"
                    ) from exc
                previous_owner = alias_owners.get(alias)
                if previous_owner is not None and previous_owner != owner:
                    raise ScenePlanCanonConflictError(
                        "story_bible cache: канонический alias "
                        f"{name!r} неоднозначен между {previous_owner} и {owner}"
                    )
                alias_owners[alias] = owner
            checked[kind].append(row)
    return checked


def _story_cache_cadence(cadence) -> dict:
    return dict(cadence) if isinstance(cadence, dict) else {"mode": "unspecified"}


def analyze_story(text: str, project_dir: str, use_cache: bool = False,
                  cadence: Optional[dict] = None) -> Optional[dict]:
    """Извлекает повторяющихся персонажей и ключевые локации из текста → story_bible dict.

    Схема: {"characters":[{"id","names":[оригинальный язык],"role","appearance":<англ>}],
            "locations":[{"id","names":[оригинальный язык],"appearance":<англ>}]}
    names — для детерминированного матчинга по упоминанию; appearance — англ. карта внешности.
    Сохраняет в _ASSETS/story_bible.json (атомарно). use_cache=True → читает только
    versioned source-hash cache, чтобы старый project-local файл другого сценария
    не мог тихо подменить персонажей.
    Возвращает None при неудаче (пайплайн идёт по-старому).
    """
    cache_identity = _planning_artifact_identity(
        "story_bible",
        text,
        algorithm_version=6,
        prompt_version="story-bible-v3-identity-names",
        cadence=_story_cache_cadence(cadence),
    )
    if use_cache:
        cached = _planning_artifact_cache_get(cache_identity)
        if cached is not None:
            try:
                checked = _validate_story_bible(cached)
            except ScenePlanError as exc:
                print_user(f"⚠️ story_bible: битый hash-cache проигнорирован ({exc})", "WARNING")
            else:
                _atomic_write_json(_story_bible_path(project_dir), checked)
                print_user(
                    f"📖 story_bible из source-hash cache "
                    f"({len(checked['characters'])} персонажей, "
                    f"{len(checked['locations'])} локаций)",
                    "INFO",
                )
                return checked

    # 1. режем на чанки по границам предложений → покрываем ВЕСЬ текст, а не только начало
    sentences = split_text_to_sentences(text)
    chunks, cur, cur_len = [], [], 0
    for s in sentences:
        cur.append(s); cur_len += len(s)
        if cur_len >= STORY_ANALYSIS_CHUNK_CHARS:
            chunks.append(" ".join(cur)); cur, cur_len = [], 0
    if cur:
        chunks.append(" ".join(cur))
    if not chunks:
        chunks = [text[:STORY_ANALYSIS_CHUNK_CHARS]]
    print_user(f"🔍 Анализ истории по {len(chunks)} чанкам...", "INFO")

    # 2. параллельный GPT по каждому чанку (порядок сохраняем по индексу для эвристики «ранний = протагонист»)
    results: List[Optional[dict]] = [None] * len(chunks)
    with ThreadPoolExecutor(
        max_workers=min(STORY_ANALYSIS_MAX_WORKERS, len(chunks))
    ) as ex:
        futs = {ex.submit(_analyze_text_chunk, ch): i for i, ch in enumerate(chunks)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except OpenAIKeyError:
                raise
            except ScenePlanError:
                raise
            except Exception as e:
                print_debug(f"чанк {i} анализ упал: {e}")
                results[i] = None

    if any(result is None for result in results):
        failed = [str(index + 1) for index, result in enumerate(results) if result is None]
        raise ScenePlanTransientError(
            "анализ story_bible неполон: не готовы чанки " + ", ".join(failed)
        )

    # 3. Preserve every chunk-local candidate. Raw ids are generated
    # independently by parallel LLM calls and therefore are not global identity
    # authority. Conservative consolidation runs later; unresolved alias
    # ambiguity is rejected by the final global validator.
    def _norm(n: str) -> str:
        return re.sub(r"\s+", " ", n.lower().strip())

    def _identity_context_excerpt(chunk: str, names, limit: int = 1200) -> str:
        """Bounded context centred on the first proper alias, not chunk start."""
        source = str(chunk or "")
        folded = unicodedata.normalize("NFKC", source).casefold()
        positions = []
        for name in names:
            needle = unicodedata.normalize("NFKC", str(name or "")).casefold().strip()
            if needle:
                position = folded.find(needle)
                if position >= 0:
                    positions.append((position, len(needle)))
        if not positions:
            return source[:limit]
        position, needle_len = min(positions)
        before = max(0, (limit - needle_len) // 2)
        start = max(0, position - before)
        end = min(len(source), start + limit)
        start = max(0, end - limit)
        return source[start:end]

    dropped_generic_mentions = []

    def _merge(kind: str, with_role: bool):
        acc = []
        for i, r in enumerate(results):
            if not r:
                continue
            for row_index, e in enumerate(r.get(kind, [])):
                # ``names`` is accepted only for local legacy test doubles and
                # pre-v3 in-memory callers. Production strict extraction always
                # returns the separated v3 fields.
                identity_names = e.get("identity_names")
                if identity_names is None:
                    identity_names = e.get("names") or []
                generic_mentions = e.get("generic_mentions") or []
                enames = {_norm(n) for n in identity_names if _norm(n)}
                if not enames:
                    dropped_generic_mentions.extend(
                        str(value).strip() for value in generic_mentions
                        if str(value).strip()
                    )
                    continue
                stable_id = re.sub(
                    r"[^a-z0-9_]+", "_", str(e.get("id") or "").casefold()
                ).strip("_") or "entity"
                candidate_id = f"c{i + 1:04d}_r{row_index + 1:03d}_{stable_id[:20]}"
                acc.append({
                    "keys": set(enames), "names": set(identity_names),
                    "id": candidate_id,
                    # The chunk analyser is explicitly instructed to keep one
                    # stable latin id for the same named entity across chunks.
                    # This is candidate evidence only: consolidation still
                    # requires the explicit contextual linker below.
                    "identity_candidate_key": stable_id,
                    "appearance": e.get("appearance", ""),
                    "role": e.get("role", "minor"),
                    "count": 1, "order": i,
                    "source_excerpt": _identity_context_excerpt(
                        chunks[i], identity_names
                    ),
                })
        acc.sort(key=lambda a: (-a["count"], a["order"]))  # чаще встречается / раньше = важнее
        return acc

    chars_acc = _merge("characters", True)
    locs_acc = _merge("locations", False)
    if dropped_generic_mentions:
        examples = sorted(set(dropped_generic_mentions), key=str.casefold)[:6]
        print_user(
            "ℹ️ story_bible: отброшены generic-only сущности "
            f"({len(dropped_generic_mentions)} упоминаний: "
            f"{', '.join(examples)})",
            "INFO",
        )
    # ровно ОДИН протагонист = самый частый/ранний; остальные protagonist → major (раньше было 4 «протагониста»)
    for _k, _a in enumerate(chars_acc):
        if _k == 0:
            _a["role"] = "protagonist"
        elif _a["role"] == "protagonist":
            _a["role"] = "major"

    def _finalize(acc, with_role, fallback_prefix):
        out, used = [], set()
        for k, a in enumerate(acc):
            base = a["id"] or re.sub(r"[^a-z0-9_]+", "", (sorted(a["names"], key=len)[0] if a["names"] else "").lower().replace(" ", "_"))
            base = base[:20] or f"{fallback_prefix}{k+1}"
            base = f"{'char' if with_role else 'loc'}:{base}"
            cid, j = base, 2
            while cid in used:
                cid = f"{base}{j}"; j += 1
            used.add(cid)
            row = {
                "id": cid,
                "names": sorted(a["names"]),
                "appearance": a["appearance"],
                "identity_provenance": [str(a["id"])],
                "merge_policy": "single-candidate",
                "manual_binding_safe": True,
                "_identity_link_context": str(a.get("source_excerpt") or ""),
                "_identity_candidate_key": str(
                    a.get("identity_candidate_key") or ""
                ),
            }
            if with_role:
                row["role"] = a["role"]
            out.append(row)
        return out

    chars = _finalize(chars_acc, True, "char")
    locs = _finalize(locs_acc, False, "loc")
    # Character and location consolidation are independent. Each helper already
    # falls back to its input on failure; joining them in parallel preserves that
    # behavior while removing one serial LLM round-trip.
    with ThreadPoolExecutor(
        max_workers=min(2, max(1, GPT_SCENE_PARALLEL_THREADS))
    ) as consolidate_executor:
        chars_future = consolidate_executor.submit(
            _consolidate_characters, chars
        )
        locs_future = consolidate_executor.submit(
            _consolidate_characters, locs, False
        )
        chars = chars_future.result()  # dziadek ≡ Stanisław
        locs = locs_future.result()    # dom ≡ dom Stanisława
    for row in chars + locs:
        if isinstance(row, dict):
            row.pop("_identity_link_context", None)
            row.pop("_identity_candidate_key", None)
    if not chars and not locs:
        print_user(
            "ℹ️ Анализ полон: повторяющихся персонажей/локаций нет",
            "INFO",
        )
    bible = _validate_story_bible({"characters": chars, "locations": locs})
    _atomic_write_json(_story_bible_path(project_dir), bible)
    if use_cache:
        _planning_artifact_cache_set(cache_identity, bible)
    print_user(f"📖 Анализ ({len(chunks)} чанков, дедуп): {len(chars)} персонажей, {len(locs)} локаций → story_bible.json", "SUCCESS")
    return bible


def match_refs_for_scene(scene: dict, segment_text: str, bible: dict) -> List[str]:
    """Bind visual refs only from validated exact ``scene.entities`` values.

    Prose, name prefixes, age words and subject heuristics are deliberately not
    reference authority: a false positive puts the wrong person's face into a
    paid generation. ``segment_text`` remains only for call-site compatibility.
    """
    if not bible:
        return []
    bible = _validate_story_bible(bible)
    _ = segment_text
    ents = (bible.get("characters", []) or []) + (bible.get("locations", []) or [])
    if not ents:
        return []
    valid_ids = {e["id"] for e in ents}
    # Exact NFC/casefold alias registry.  One alias may never silently pick the
    # first entity: an ambiguous scene entity is a blocking reference error.
    alias_owners = {}
    for e in ents:
        for nm in e.get("names", []):
            key = ref_contract.normalize_alias(nm)
            alias_owners.setdefault(key, set()).add(e["id"])
    scene_entities = scene.get("entities")
    if not isinstance(scene_entities, list):
        raise ref_contract.ReferenceContractError(
            "scene.entities is required for authoritative reference binding; replan the scene"
        )
    matched = set()
    for raw_entity in scene_entities:
        if not isinstance(raw_entity, str) or not raw_entity.strip():
            raise ref_contract.ReferenceContractError(
                "scene.entities must contain only non-empty entity ids or aliases"
            )
        token = raw_entity.strip()
        owners = set(alias_owners.get(ref_contract.normalize_alias(token), set()))
        if token in valid_ids:
            owners.add(token)
        if len(owners) > 1:
            raise ref_contract.ReferenceContractError(
                f"scene entity {raw_entity!r} is ambiguous between "
                + ", ".join(sorted(owners))
            )
        if not owners:
            raise ref_contract.ReferenceContractError(
                f"scene entity {raw_entity!r} is not present in the story bible"
            )
        matched.add(next(iter(owners)))
    # A set is useful for membership while matching, but its iteration order is
    # process-dependent. Persist refs in canonical bible order so identical
    # prompt/cache reruns produce byte-identical scenes.json. Duplicate ids (for
    # example the same entity accidentally repeated across lists) are emitted once.
    ordered = []
    seen = set()
    for entity in ents:
        entity_id = entity["id"]
        if entity_id in matched and entity_id in valid_ids and entity_id not in seen:
            ordered.append(entity_id)
            seen.add(entity_id)
    return ordered


def _validate_explicit_alias_entity_coverage(
    scenes: List[dict], bible: Optional[dict]
) -> None:
    """Reject explicit untagged canon mentions without ever adding a binding.

    ``scene.entities`` remains the sole identity authority. Prose is used only
    as a deterministic omission detector: a globally unique exact alias that is
    visibly present must already resolve to the same entity through
    ``entities`` (or through an already-derived ``matched_refs`` id). Duplicate
    aliases and overlapping/crossing phrases are intentionally non-authoritative.
    """
    if not isinstance(bible, dict):
        return
    rows = [
        row
        for kind in ("characters", "locations")
        for row in (bible.get(kind) or [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    ]
    if not rows:
        return

    alias_owners = {}
    display_by_id = {}
    valid_ids = set()
    for row in rows:
        entity_id = row["id"]
        valid_ids.add(entity_id)
        names = row.get("names") or []
        if names:
            display_by_id[entity_id] = str(names[0])
        for name in names:
            try:
                alias = ref_contract.normalize_alias(name)
            except ref_contract.ReferenceContractError:
                continue
            alias_owners.setdefault(alias, set()).add(entity_id)
    unique_aliases = {
        alias: next(iter(owners))
        for alias, owners in alias_owners.items()
        if len(owners) == 1
    }
    if not unique_aliases:
        return

    def _explicit_ids(text: str) -> set:
        if not isinstance(text, str) or not text.strip():
            return set()
        try:
            normalized = ref_contract.normalize_alias(text)
        except ref_contract.ReferenceContractError:
            return set()
        occurrences = []
        for alias, entity_id in unique_aliases.items():
            pattern = rf"(?<!\w){re.escape(alias)}(?!\w)"
            for match in re.finditer(pattern, normalized, flags=re.UNICODE):
                occurrences.append({
                    "start": match.start(), "end": match.end(),
                    "alias": alias, "entity_id": entity_id,
                })

        # A shorter alias inside a longer exact canonical phrase is not a
        # separate mention ("Ann" inside "Ann Marie"). Crossing aliases for
        # different entities are ambiguous context and therefore infer nothing.
        keep = [True] * len(occurrences)
        for index, current in enumerate(occurrences):
            for other_index, other in enumerate(occurrences):
                if index == other_index:
                    continue
                current_span = (current["start"], current["end"])
                other_span = (other["start"], other["end"])
                if (
                    other_span[0] <= current_span[0]
                    and other_span[1] >= current_span[1]
                    and other_span != current_span
                ):
                    keep[index] = False
                    break
                overlaps = (
                    current_span[0] < other_span[1]
                    and other_span[0] < current_span[1]
                )
                contains = (
                    current_span[0] <= other_span[0]
                    and current_span[1] >= other_span[1]
                ) or (
                    other_span[0] <= current_span[0]
                    and other_span[1] >= current_span[1]
                )
                if (
                    overlaps and not contains
                    and current["entity_id"] != other["entity_id"]
                ):
                    keep[index] = False
                    keep[other_index] = False
        return {
            occurrence["entity_id"]
            for occurrence, selected in zip(occurrences, keep)
            if selected
        }

    for index, scene in enumerate(scenes or []):
        if not isinstance(scene, dict):
            continue
        provided_ids = {
            value for value in (scene.get("matched_refs") or [])
            if isinstance(value, str) and value in valid_ids
        }
        scene_entities = scene.get("entities")
        if isinstance(scene_entities, list):
            for raw in scene_entities:
                if not isinstance(raw, str):
                    continue
                if raw in valid_ids:
                    provided_ids.add(raw)
                    continue
                try:
                    owners = alias_owners.get(
                        ref_contract.normalize_alias(raw), set()
                    )
                except ref_contract.ReferenceContractError:
                    owners = set()
                if len(owners) == 1:
                    provided_ids.add(next(iter(owners)))

        explicit_ids = set()
        for field in ("text_segment", "visual_prompt", "visual_description"):
            explicit_ids.update(_explicit_ids(scene.get(field)))
        missing = sorted(explicit_ids - provided_ids)
        if missing:
            labels = [display_by_id.get(entity_id, entity_id) for entity_id in missing]
            raise ScenePlanCanonConflictError(
                f"сцена {index + 1}: явный канонический alias "
                f"{', '.join(repr(label) for label in labels)} отсутствует в "
                "scene.entities; требуется replan"
            )


def generate_prompt_via_gpt(text: str, style: str, index: int = 0, visual_context: Optional[str] = None) -> Tuple[int, Optional[str]]:
    """Генерирует визуальный промт для изображения через GPT"""

    # Выбираем случайный ракурс камеры для разнообразия
    angle = random.choice(CAMERA_ANGLES)

    system_prompt = """You are a professional art director and prompt engineer. Your task is to transform text into a VISUAL scene description in English.

RULES:
- Ignore abstractions (thoughts, feelings). Describe ONLY what is visible to the eye.
- Structure: [Object and Action], [Environment], [Lighting and Colors], [Camera Style].
- If the text mentions a character, invent visual details (clothing, face) if not specified.
- Style: Cinematic, photorealistic, 8k.
- DIVERSITY RULE: Do not start every prompt with 'A group of...'. Use the mandatory camera angle to describe the scene creatively.
- CRITICAL: NEVER mention text, words, letters, numbers, statistics, charts, graphs, signs, labels, captions, subtitles, or any written content in the visual description. Images must be completely text-free.
- NAMED REAL PEOPLE: if the text names a specific real, historical or well-known public figure, KEEP the exact name and depict the ACTUAL person (era-accurate clothing/age/setting) — do NOT genericize; a named deity → a classical statue/painting of that deity. Only generic/fictional characters are described generically.
- Answer ONLY inside square brackets [PROMPT]."""

    # Формируем пользовательское сообщение
    user_content = f"Text scene: {text}.\nStyle: {style}."
    if visual_context:
        user_content += f"\nGLOBAL VISUAL CONTEXT (Style, Setting, Characters): {visual_context}. Ensure every generated prompt adheres to this context."

    # Обязательно добавляем ракурс камеры
    user_content += f"\nMANDATORY CAMERA ANGLE: {angle}. You MUST write the prompt description matching this specific angle. Do not ignore this."

    headers = _openai_chat_headers()

    payload = {
        "model": GPT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "max_tokens": 500,
        "temperature": 0.7
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = _llm_post(
                OPENAI_API_URL,
                headers=headers,
                json=payload,
                timeout=GPT_CHAT_TIMEOUT
            )

            _raise_if_openai_auth_failed(response)  # 401/403/invalid_api_key → фатально (до деградации)

            if response.status_code == 429:
                wait_time = RETRY_DELAY * (attempt + 1)
                print_user(f"⏳ Rate limit GPT (промпт {index+1}), ждем {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                time.sleep(wait_time)
                continue

            response.raise_for_status()
            try:
                result = response.json()
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                print_debug(f"Ошибка декодирования JSON ответа GPT: {e}")
                text = response.content.decode('utf-8', errors='replace')
                result = json.loads(text)

            content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
            print_debug(f"[{index}] Ответ GPT: {content[:200]}")
            prompt = extract_prompt_from_brackets(content)

            if prompt:
                print_debug(f"[{index}] Промт извлечен ({len(prompt)} символов): {prompt[:50]}...")
                print_user(f"Промпт создан для кадра {index}", "SUCCESS")
                return (index, prompt)
            else:
                print_debug(f"[{index}] Не удалось извлечь промт из ответа: {content[:200]}")
                return (index, None)

        except requests.exceptions.RequestException as e:
            error_type = type(e).__name__
            print_user(f"⚠️ Ошибка GPT (промпт {index+1}, попытка {attempt + 1}/{MAX_RETRIES}): {error_type}: {str(e)[:150]}", "WARNING")
            if attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                print_user(f"🔄 Повтор через {wait_time}с...", "WARNING")
                time.sleep(wait_time)

    print_error_user_friendly(f"Не удалось сгенерировать промт для кадра {index}")
    return (index, None)


def generate_prompts_batch(texts: List[str], style: str, visual_context: Optional[str] = None) -> List[Optional[str]]:
    """Генерирует промты для списка текстов параллельно"""
    total = len(texts)
    print_user(f"🤖 Начинаю генерацию промптов: {total} кадров", "PROCESS")
    if visual_context:
        print_debug(f"Visual Context применен: {visual_context[:80]}...")
    print_progress(0, total, "промптов", show_bar=True)

    results: List[Optional[str]] = [None] * total
    completed = 0
    last_progress_time = time.time()

    with ThreadPoolExecutor(max_workers=GPT_PARALLEL_THREADS) as executor:
        futures = {
            executor.submit(generate_prompt_via_gpt, text, style, i, visual_context): i
            for i, text in enumerate(texts)
        }

        for future in as_completed(futures):
            completed += 1
            try:
                index, prompt = future.result()
                results[index] = prompt

                # Обновляем прогресс чаще для больших батчей
                update_interval = 1 if total < 50 else (5 if total < 200 else 10)
                current_time = time.time()
                time_since_last = current_time - last_progress_time

                # Обновляем прогресс каждые N элементов или каждые 3 секунды
                if completed % update_interval == 0 or time_since_last >= 3:
                    print_progress(completed, total, "промптов", show_bar=True)
                    last_progress_time = current_time

            except OpenAIKeyError:
                raise  # мёртвый/неверный OpenAI-ключ — фатально, НЕ глотаем
            except Exception as e:
                print_debug(f"Ошибка в потоке: {e}")
                import traceback
                print_debug(traceback.format_exc())
            if completed % 10 == 0:
                    print_progress(completed, total, "промптов", show_bar=True)

    # Финальный прогресс-бар (100%)
    print_progress(total, total, "промптов", show_bar=True)

    successful = sum(1 for p in results if p is not None)
    print_user(f"Сгенерировано промптов: {successful} из {total}", "SUCCESS")

    return results


# ============ GOOGLER API v1 (изображения - синхронный) ============

def googler_request_v1(endpoint: str, method: str = "POST", data: Optional[dict] = None) -> dict:
    """Базовый запрос к Googler API v1 (синхронный)"""
    url = f"{GOOGLER_BASE_URL_V1}/{endpoint}"
    headers = {
        "X-API-Key": GOOGLER_API_KEY,
        "Content-Type": "application/json"
    }

    for attempt in range(MAX_RETRIES):
        try:
            if method == "GET":
                response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            else:
                response = requests.post(url, headers=headers, json=data, timeout=REQUEST_TIMEOUT)

            if response.status_code == 429:
                wait_time = RETRY_DELAY * (attempt + 1) * 2
                print_user(f"⏳ Rate limit Googler v1, ждем {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                time.sleep(wait_time)
                continue

            response.raise_for_status()
            try:
                return response.json()
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                print_debug(f"Ошибка декодирования JSON ответа Googler: {e}")
                text = response.content.decode('utf-8', errors='replace')
                return json.loads(text)

        except requests.exceptions.RequestException as e:
            error_type = type(e).__name__
            print_user(f"⚠️ Ошибка Googler v1 ({endpoint}, попытка {attempt + 1}/{MAX_RETRIES}): {error_type}: {str(e)[:150]}", "WARNING")
            if attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                print_user(f"🔄 Повтор через {wait_time}с...", "WARNING")
                time.sleep(wait_time)

    return {"success": False, "error": f"Не удалось выполнить запрос после {MAX_RETRIES} попыток"}


# ============ GOOGLER API v2 (видео - асинхронный) ============

def googler_request_v3(endpoint: str, method: str = "POST", data: Optional[dict] = None) -> dict:
    """Базовый запрос к Googler API v3"""
    url = f"{GOOGLER_BASE_URL_V3}/{endpoint}"
    headers = {
        "X-API-Key": GOOGLER_API_KEY,
        "Content-Type": "application/json; charset=utf-8"
    }

    for attempt in range(MAX_RETRIES):
        if attempt == 0:
            print_debug(f"Запрос v3: {method} {url}")
            if data:
                operation = data.get("provider", "unknown")
                print_debug(f"   Провайдер: {operation}, эндпоинт: {endpoint}")
        try:
            if method == "GET":
                response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            else:
                response = requests.post(url, headers=headers, json=data, timeout=REQUEST_TIMEOUT)

            # Логируем статус ответа
            if response.status_code != 200:
                print_debug(f"HTTP {response.status_code} от API v3 ({endpoint})")

            # Специальная обработка для HTTP 502 (Bad Gateway) - ДО raise_for_status()
            if response.status_code == 502:
                if attempt < MAX_RETRIES - 1:
                    # Для 502 используем более длинные задержки (10, 20, 30 секунд)
                    wait_time = 10 * (attempt + 1)
                    print_user(f"❌ HTTP 502 от API v3 ({endpoint}): Bad Gateway", "ERROR")
                    print_user(f"🔄 Повтор через {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                    time.sleep(wait_time)
                    continue
                else:
                    print_user(f"❌ HTTP 502 от API v3 ({endpoint}): Bad Gateway - все попытки исчерпаны", "ERROR")
                    return {"success": False, "error": "HTTP 502: Bad Gateway"}

            if response.status_code == 429:
                wait_time = RETRY_DELAY * (attempt + 1) * 2
                print_user(f"⏳ Rate limit API v3, ждем {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                time.sleep(wait_time)
                continue

            if response.status_code == 404:
                try:
                    error_detail = response.text[:300] if hasattr(response, 'text') else str(response.status_code)
                except (UnicodeDecodeError, AttributeError):
                    error_detail = response.content.decode('utf-8', errors='replace')[:300] if hasattr(response, 'content') else str(response.status_code)
                print_user(f"❌ 404 Not Found: эндпоинт '{endpoint}' не существует. Ответ: {error_detail}", "ERROR")
                return {"success": False, "error": f"404 Not Found: {endpoint}"}

            if response.status_code == 500:
                try:
                    error_json = response.json()
                    error_msg = error_json.get("error", "") or error_json.get("detail", "")
                    if "InputIsImproper" in str(error_msg):
                        print_user(f"❌ API отклонил запрос: InputIsImproper (политика контента)", "ERROR")
                        return {"success": False, "error": "InputIsImproper", "skip_retry": True}
                    else:
                        print_user(f"❌ HTTP 500 от API v3: {error_msg[:200]}", "ERROR")
                except:
                    try:
                        error_text = response.text[:300] if hasattr(response, 'text') else "Unknown error"
                    except (UnicodeDecodeError, AttributeError):
                        error_text = response.content.decode('utf-8', errors='replace')[:300] if hasattr(response, 'content') else "Unknown error"
                    print_user(f"❌ HTTP 500 от API v3: {error_text}", "ERROR")

            response.raise_for_status()
            try:
                result = response.json()
                print_debug(f"✅ Успешный ответ от API v3 ({endpoint})")
                return result
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                print_user(f"⚠️ Ошибка декодирования JSON ответа: {e}", "WARNING")
                text = response.content.decode('utf-8', errors='replace')
                return json.loads(text)

        except requests.exceptions.HTTPError as e:
            status_code = response.status_code if hasattr(response, 'status_code') else "unknown"
            error_detail = ""
            try:
                if hasattr(response, 'text'):
                    try:
                        error_detail = response.text[:300]
                    except (UnicodeDecodeError, AttributeError):
                        error_detail = response.content.decode('utf-8', errors='replace')[:300] if hasattr(response, 'content') else str(e)
                elif hasattr(response, 'content'):
                    error_detail = response.content.decode('utf-8', errors='replace')[:300]
                else:
                    error_detail = str(e)
            except:
                error_detail = str(e)

            # Специальная обработка для HTTP 502 (Bad Gateway) - нужна более длинная задержка
            if status_code == 502:
                print_user(f"❌ HTTP 502 от API v3 ({endpoint}): Bad Gateway", "ERROR")
                if attempt < MAX_RETRIES - 1:
                    # Для 502 используем более длинные задержки (10, 20, 30 секунд)
                    wait_time = 10 * (attempt + 1)
                    print_user(f"🔄 Повтор через {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                    time.sleep(wait_time)
            else:
                print_user(f"❌ HTTP {status_code} от API v3 ({endpoint}): {error_detail}", "ERROR")
                if attempt < MAX_RETRIES - 1:
                    wait_time = RETRY_DELAY * (attempt + 1)
                    print_user(f"🔄 Повтор через {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                    time.sleep(wait_time)
        except requests.exceptions.Timeout as e:
            print_user(f"⏱️ Таймаут запроса к API v3 ({endpoint}): {REQUEST_TIMEOUT}с", "WARNING")
            if attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                print_user(f"🔄 Повтор через {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                time.sleep(wait_time)
        except requests.exceptions.ConnectionError as e:
            error_str = str(e)
            # Проверяем на ошибки разрыва соединения (ConnectionAbortedError, ConnectionResetError)
            is_connection_aborted = (
                "Connection aborted" in error_str or
                "ConnectionAbortedError" in error_str or
                "ConnectionResetError" in error_str or
                "разорвала" in error_str or
                "разорвал" in error_str or
                "10053" in error_str or  # Windows error code
                "10054" in error_str     # Windows error code
            )

            if is_connection_aborted:
                # При ошибках разрыва соединения возвращаем специальный флаг для немедленного fallback
                print_user(f"🔌 Ошибка подключения к API v3 ({endpoint}): {error_str[:200]}", "ERROR")
                return {"success": False, "error": f"Connection aborted: {error_str[:200]}", "connection_error": True}
            else:
                print_user(f"🔌 Ошибка подключения к API v3 ({endpoint}): {error_str[:200]}", "WARNING")
                if attempt < MAX_RETRIES - 1:
                    wait_time = RETRY_DELAY * (attempt + 1)
                    print_user(f"🔄 Повтор через {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                    time.sleep(wait_time)
        except requests.exceptions.RequestException as e:
            error_type = type(e).__name__
            error_str = str(e)
            # Проверяем на ошибки разрыва соединения
            is_connection_aborted = (
                "Connection aborted" in error_str or
                "ConnectionAbortedError" in error_str or
                "ConnectionResetError" in error_str or
                "разорвала" in error_str or
                "разорвал" in error_str or
                "10053" in error_str or
                "10054" in error_str
            )

            if is_connection_aborted:
                print_user(f"❌ Ошибка подключения к API v3 ({endpoint}): {error_type}: {error_str[:200]}", "ERROR")
                return {"success": False, "error": f"Connection aborted: {error_str[:200]}", "connection_error": True}
            else:
                print_user(f"❌ Ошибка запроса к API v3 ({endpoint}): {error_type}: {error_str[:200]}", "WARNING")
                if attempt < MAX_RETRIES - 1:
                    wait_time = RETRY_DELAY * (attempt + 1)
                    print_user(f"🔄 Повтор через {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                    time.sleep(wait_time)

    return {"success": False, "error": f"Не удалось выполнить запрос после {MAX_RETRIES} попыток"}


def googler_request_v2(endpoint: str, method: str = "POST", data: Optional[dict] = None) -> dict:
    """Базовый запрос к Googler API v2"""
    url = f"{GOOGLER_BASE_URL_V2}/{endpoint}"
    headers = {
        "X-API-Key": GOOGLER_API_KEY,
        "Content-Type": "application/json; charset=utf-8"
    }

    for attempt in range(MAX_RETRIES):
        # Логирование для отладки (только первый раз)
        if attempt == 0:
            print_debug(f"Запрос: {method} {url}")
            if data and "operation" in data:
                print_debug(f"   Операция: {data.get('operation')}")
        try:
            if method == "GET":
                response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            else:
                response = requests.post(url, headers=headers, json=data, timeout=REQUEST_TIMEOUT)

            # Специальная обработка для HTTP 502 (Bad Gateway) - ДО raise_for_status()
            if response.status_code == 502:
                if attempt < MAX_RETRIES - 1:
                    # Для 502 используем более длинные задержки (10, 20, 30 секунд)
                    wait_time = 10 * (attempt + 1)
                    print_user(f"❌ HTTP 502 от API v2 ({endpoint}): Bad Gateway", "ERROR")
                    print_user(f"🔄 Повтор через {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                    time.sleep(wait_time)
                    continue
                else:
                    print_user(f"❌ HTTP 502 от API v2 ({endpoint}): Bad Gateway - все попытки исчерпаны", "ERROR")
                    return {"success": False, "error": "HTTP 502: Bad Gateway"}

            if response.status_code == 429:
                wait_time = RETRY_DELAY * (attempt + 1) * 2
                print_user(f"⏳ Rate limit API v2, ждем {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                time.sleep(wait_time)
                continue

            if response.status_code == 503:
                try:
                    err = response.json().get("error", "service unavailable")
                except Exception:
                    err = "service unavailable"
                print_user(f"❌ HTTP 503 от API v2 ({endpoint}): {err}", "ERROR")
                return {"success": False, "error": f"503: {err}", "skip_retry": True}

            # Логируем статус ответа
            if response.status_code != 200:
                print_debug(f"HTTP {response.status_code} от API v2 ({endpoint})")

            # Детальная обработка ошибок
            if response.status_code == 404:
                try:
                    error_detail = response.text[:300]
                except (UnicodeDecodeError, AttributeError):
                    try:
                        error_detail = response.content.decode('utf-8', errors='replace')[:300]
                    except:
                        error_detail = str(response.status_code)
                print_user(f"❌ 404 Not Found: эндпоинт '{endpoint}' не существует. Ответ: {error_detail}", "ERROR")
                return {"success": False, "error": f"404 Not Found: {endpoint}. Проверьте правильность эндпоинта."}

            # Обработка ошибки 500 с InputIsImproper - не повторяем запрос
            if response.status_code == 500:
                try:
                    error_json = response.json()
                    error_msg = error_json.get("error", "") or error_json.get("detail", "")
                    if "InputIsImproper" in str(error_msg):
                        print_user(f"❌ API отклонил запрос: InputIsImproper (политика контента)", "ERROR")
                        return {"success": False, "error": "InputIsImproper", "skip_retry": True}
                    else:
                        print_user(f"❌ HTTP 500 от API v2: {error_msg[:200]}", "ERROR")
                except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                    try:
                        error_text = response.text[:300] if hasattr(response, 'text') else "Unknown error"
                    except (UnicodeDecodeError, AttributeError):
                        error_text = response.content.decode('utf-8', errors='replace')[:300] if hasattr(response, 'content') else "Unknown error"
                    print_user(f"❌ HTTP 500 от API v2: {error_text}", "ERROR")

            response.raise_for_status()
            try:
                result = response.json()
                print_debug(f"✅ Успешный ответ от API v2 ({endpoint})")
                return result
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                print_user(f"⚠️ Ошибка декодирования JSON ответа: {e}", "WARNING")
                # Пробуем декодировать вручную
                try:
                    text = response.content.decode('utf-8', errors='replace')
                    return json.loads(text)
                except:
                    raise Exception(f"Не удалось декодировать ответ сервера: {e}")

        except requests.exceptions.HTTPError as e:
            status_code = response.status_code if hasattr(response, 'status_code') else "unknown"
            error_detail = ""
            try:
                if hasattr(response, 'text'):
                    try:
                        error_detail = response.text[:300]
                    except (UnicodeDecodeError, AttributeError):
                        error_detail = response.content.decode('utf-8', errors='replace')[:300] if hasattr(response, 'content') else str(e)
                elif hasattr(response, 'content'):
                    error_detail = response.content.decode('utf-8', errors='replace')[:300]
                else:
                    error_detail = str(e)
            except (UnicodeDecodeError, AttributeError):
                error_detail = str(e)

            # Специальная обработка для HTTP 502 (Bad Gateway) - нужна более длинная задержка
            if status_code == 502:
                print_user(f"❌ HTTP 502 от API v2 ({endpoint}): Bad Gateway", "ERROR")
                if attempt < MAX_RETRIES - 1:
                    wait_time = 10 * (attempt + 1)
                    print_user(f"🔄 Повтор через {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                    time.sleep(wait_time)
            else:
                print_user(f"❌ HTTP {status_code} от API v2 ({endpoint}): {error_detail}", "ERROR")
                if attempt < MAX_RETRIES - 1:
                    wait_time = RETRY_DELAY * (attempt + 1)
                    print_user(f"🔄 Повтор через {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                    time.sleep(wait_time)
        except requests.exceptions.Timeout as e:
            print_user(f"⏱️ Таймаут запроса к API v2 ({endpoint}): {REQUEST_TIMEOUT}с", "WARNING")
            if attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                print_user(f"🔄 Повтор через {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                time.sleep(wait_time)
        except requests.exceptions.ConnectionError as e:
            print_user(f"🔌 Ошибка подключения к API v2 ({endpoint}): {str(e)[:200]}", "WARNING")
            if attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                print_user(f"🔄 Повтор через {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                time.sleep(wait_time)
        except requests.exceptions.RequestException as e:
            error_type = type(e).__name__
            print_user(f"❌ Ошибка запроса к API v2 ({endpoint}): {error_type}: {str(e)[:200]}", "WARNING")
            if attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                print_user(f"🔄 Повтор через {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                time.sleep(wait_time)

    return {"success": False, "error": f"Не удалось выполнить запрос после {MAX_RETRIES} попыток"}


def googler_request_v4(endpoint: str, method: str = "POST", data: Optional[dict] = None) -> dict:
    """Запрос к API v4 (возвращает operation_id для последующего polling)"""
    url = f"{GOOGLER_BASE_URL_V4}/{endpoint}"
    headers = {
        "X-API-Key": GOOGLER_API_KEY,
        "Content-Type": "application/json; charset=utf-8"
    }

    for attempt in range(MAX_RETRIES):
        # Подробный лог только на первой попытке (в debug-режиме)
        if attempt == 0:
            try:
                body_preview = ""
                if data is not None:
                    body_preview = json.dumps(data, ensure_ascii=False)[:300]
                print_debug(f"v4 {method} {url}")
                if body_preview:
                    print_debug(f"   body: {body_preview}")
            except Exception:
                pass
        try:
            if method == "GET":
                response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            else:
                response = requests.post(url, headers=headers, json=data, timeout=REQUEST_TIMEOUT)

            if response.status_code == 429:
                wait_time = min(60, RETRY_DELAY * (2 ** attempt))  # exp backoff 5/10/20/40/60s
                print_user(f"⏳ Rate limit API v4, ждем {wait_time}с... (exp backoff)", "WARNING")
                time.sleep(wait_time)
                continue

            if response.status_code == 500:
                try:
                    error_json = response.json()
                    error_msg = error_json.get("error", "") or error_json.get("detail", "")
                    if "InputIsImproper" in str(error_msg):
                        return {"success": False, "error": "InputIsImproper", "skip_retry": True}
                except Exception:
                    # Если не удалось распарсить JSON, логируем это ниже
                    pass

            response.raise_for_status()
            try:
                result = response.json()
            except (ValueError, json.JSONDecodeError):
                # Если API вернул не-JSON, пытаемся вывести текст в отладку
                text_preview = ""
                try:
                    text_preview = response.text[:500]
                except Exception:
                    pass
                print_debug(f"v4 ({endpoint}) не-JSON ответ: {text_preview}")
                raise

            print_debug(f"✅ Успешный ответ от API v4 ({endpoint})")
            return result

        except requests.exceptions.HTTPError:
            status_code = getattr(response, 'status_code', 'unknown')
            resp_preview = ""
            try:
                resp_preview = response.text[:500]
            except Exception:
                pass
            print_debug(f"HTTP {status_code} от v4 ({endpoint}) — {resp_preview}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
        except requests.exceptions.Timeout:
            print_user(f"⏱️ Таймаут v4 ({endpoint})", "WARNING")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
        except requests.exceptions.RequestException as e:
            print_user(f"❌ Ошибка v4 ({endpoint}): {str(e)[:200]}", "WARNING")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))

    return {"success": False, "error": f"Не удалось выполнить v4 запрос после {MAX_RETRIES} попыток"}


def poll_v4_operation(operation_id: str, index: int = 0, timeout: int = None) -> Optional[str]:
    """
    Опрашивает статус v4 операции до завершения.
    Используется для async-эндпоинтов v4 (gemini, grok и т.д.)

    Returns:
        base64 data URI результата или None при ошибке
    """
    if timeout is None:
        timeout = VIDEO_POLL_TIMEOUT
    start_time = time.time()
    last_heartbeat = 0.0  # гарантируем print_user-хартбит не реже раза в 15с

    while time.time() - start_time < timeout:
        result = googler_request_v4(f"operations/{operation_id}", method="GET")

        status = result.get("status", "unknown")

        if status == "success":
            op_result = result.get("result")
            elapsed = int(time.time() - start_time)
            if op_result:
                # Unwrap list results (Flower API returns ["base64_data"] instead of "base64_data")
                if isinstance(op_result, list) and len(op_result) > 0:
                    print_debug(f"[{index}] v4 result was list[{len(op_result)}], unwrapping first element")
                    op_result = op_result[0]
                print_user(f"✅ [{index}] v4 операция завершена! (время: {elapsed}с)", "SUCCESS")
                # Debug: log result type and prefix for video troubleshooting
                if isinstance(op_result, str):
                    print_debug(f"[{index}] v4 result type=str len={len(op_result)} prefix={op_result[:60]}")
                elif isinstance(op_result, dict):
                    print_debug(f"[{index}] v4 result type=dict keys={list(op_result.keys())}")
                else:
                    print_debug(f"[{index}] v4 result type={type(op_result).__name__}")
            else:
                print_user(f"⚠️ [{index}] v4 статус success, но результат пуст (время: {elapsed}с)", "WARNING")
            return op_result
        elif status == "error":
            error_msg = (result.get("error") or result.get("message") or
                         result.get("error_message") or result.get("details") or
                         json.dumps(result, ensure_ascii=False)[-300:])
            print_user(f"❌ v4 operation {operation_id} status=error: {error_msg}", "ERROR")
            return None
        elif status in ("pending", "processing"):
            elapsed = time.time() - start_time
            if elapsed - last_heartbeat >= 15:
                print_user(f"v4 операция [{index}]: {int(elapsed)}с...", "PROCESS")
                last_heartbeat = elapsed
            time.sleep(VIDEO_POLL_INTERVAL)
        else:
            elapsed = time.time() - start_time
            print_debug(f"[{index}] v4 неизвестный статус: {status}")
            if elapsed - last_heartbeat >= 15:
                print_user(f"v4 операция [{index}]: {int(elapsed)}с ({status})...", "PROCESS")
                last_heartbeat = elapsed
            time.sleep(VIDEO_POLL_INTERVAL)

    print_error_user_friendly(f"Таймаут v4 операции [{index}] ({timeout}с)")
    return None


# ============================================================================
# API v6 (унифицированный /api/v6/generations) — миграция 2026-06-27
# ============================================================================

def googler_request_v6(endpoint: str, method: str = "POST", data: Optional[dict] = None) -> dict:
    """Запрос к API v6. Возвращает dict ответа; при 4xx-ошибке кладёт текст в '_error' (без слепого ретрая)."""
    url = f"{GOOGLER_BASE_URL_V6}/{endpoint}"
    headers = {"X-API-Key": GOOGLER_API_KEY, "Content-Type": "application/json; charset=utf-8"}
    if method == "POST" and data is not None:
        try:
            print_debug(f"v6 POST {url} body={json.dumps({k:(v if k!='inputs' else f'[{len(v)} ref]') for k,v in data.items()}, ensure_ascii=False)[:300]}")
        except Exception:
            pass
    is_post = (method == "POST")
    _tries = 6 if is_post else 3          # v6 троттлит под нагрузкой → POST даём больше попыток
    _cap = 30 if is_post else 8
    _last = ""

    def _wait(a):
        # экспон. бэкофф + ДЖИТТЕР: разводит одновременные потоки, чтобы не долбить троттл в лоб
        time.sleep(min(_cap, RETRY_DELAY * (2 ** a)) + random.uniform(0, 2.0))

    for attempt in range(_tries):
        try:
            if method == "GET":
                response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            else:
                response = requests.post(url, headers=headers, json=data, timeout=REQUEST_TIMEOUT)
            sc = response.status_code
            if sc == 429 or sc in (500, 502, 503, 504):   # троттл/транзиентный серверный → ждём и повторяем
                _last = f"HTTP {sc}: {response.text[:120]}"
                print_debug(f"v6 ({endpoint}) попытка {attempt+1}: {_last}")
                if attempt < _tries - 1:
                    _wait(attempt)
                continue
            # 401 добавлен к списку «без ретрая»: мёртвый или отозванный ключ шесть раз
            # подряд отвечает тем же самым, а до правки он уходил в raise_for_status и
            # приезжал наверх как «HTTPError: 401 Client Error» БЕЗ тела ответа — то есть
            # без единственного места, где провайдер называет причину.
            # 404 добавлен 03.10.2026. Для GET generations/{id} это ОПРЕДЕЛЁННЫЙ ответ:
            # «такого заказа у меня нет». Провайдер чистит старые операции, и у заказов
            # 14–17-часовой давности это штатный исход, а не сбой. Раньше 404 уходил в
            # raise_for_status и крутился тремя попытками — три одинаковых ответа и
            # потерянное время вместо ясного вывода.
            if sc == 404:
                return {"_error": f"HTTP 404: заказ или адрес не найден: "
                                  f"{str(response.text or '').strip()[:200]}"}
            if sc in (400, 401, 402, 403, 422):  # bad request / мёртвый ключ / нет кредитов / auth / валидация
                try:
                    detail = json.dumps(response.json(), ensure_ascii=False)
                except Exception:
                    detail = response.text
                # Код ОБЯЗАН быть в тексте: по нему человек понимает, менять ключ (401/403),
                # пополнять счёт (402) или чинить запрос (400/422). Без кода все эти отказы
                # выглядят одинаково.
                return {"_error": f"HTTP {sc}: {str(detail or '').strip()[:300]}"}
            response.raise_for_status()
            j = response.json()
            # 200, НО пустой ответ / без operation id (мягкий троттл провайдера отдаёт {} вместо 429):
            # раньше это возвращалось СРАЗУ → «нет id» и провал. Теперь повторяем со стаггером.
            if is_post and isinstance(j, dict) and not j.get("id"):
                _last = f"пустой ответ 200 ({str(j)[:100]})"
                print_debug(f"v6 ({endpoint}) попытка {attempt+1}: {_last}")
                if attempt < _tries - 1:
                    _wait(attempt)
                    continue
                return {"_error": _last}   # исчерпали — отдаём ВНЯТНУЮ причину, не голый {}
            return j
        except (requests.exceptions.HTTPError, requests.exceptions.Timeout, requests.exceptions.RequestException) as e:
            _last = f"{type(e).__name__}: {str(e)[:140]}"
            print_debug(f"v6 ({endpoint}) попытка {attempt+1}: {_last}")
            if attempt < _tries - 1:
                _wait(attempt)
    return {"_error": _last or "нет ответа после ретраев"}


# ── Кадры, которые не дались (владелец 05.10.2026) ──────────────────────────────────
#
# «Что минимально повлияет на контент»: «попробуйте позже» — повторить позже; отказ
# фильтра после смягчений и остаток — соседний кадр (рендер сам отдаёт время сцены без
# картинки предыдущей: render_worker._collapse_missing_images); больше 2% ролика — это уже
# не случайный кадр, а беда всей темы, и задача падает, как раньше.
#
# Подмена честная: файл сцены не подделывается и квитанция ему не выписывается. Сцена
# записывается в googler/image_gaps.json с причиной и исключается из проверки квитанций.
IMAGE_GAPS_FILE = "image_gaps.json"
IMAGE_GAP_MAX_SHARE = float(os.environ.get("VS_IMAGE_GAP_MAX_SHARE", "0.02") or 0.02)
# 11.10.2026, владелец: «если не проходит — заменяем, иначе падает ошибка и требует
# оператора». Кадр, который фильтр отверг после смягчений и на всех моделях замены,
# повтор не возьмёт — его отдаём соседнему кадру сверх 2%, но всего пропусков не больше
# 10% ролика: дальше это уже беда всей темы, и задача падает, как раньше. Прочие сбои
# по-прежнему укладываются в 2%.
IMAGE_FILTER_GAP_MAX_SHARE = float(os.environ.get("VS_IMAGE_FILTER_GAP_MAX_SHARE", "0.10") or 0.10)
IMAGE_LATE_RETRY_SEC = int(os.environ.get("VS_IMAGE_LATE_RETRY_SEC", "900") or 900)
IMAGE_LATE_RETRY_ROUNDS = int(os.environ.get("VS_IMAGE_LATE_RETRY_ROUNDS", "3") or 3)
# Во время ожидания воркер раз в 5 минут пишет строку — это его пульс для сервера.
IMAGE_LATE_WAIT_PULSE_SEC = 300
GAP_FILTER = "фильтр"       # провайдер отказал фильтром безопасности и после смягчений
GAP_LATER = "позже"         # «Generation failed, please try again later»
GAP_OTHER = "сбой"          # всё прочее: попытки исчерпаны, заказ не дал результата
_FRAME_GIVEUP: dict = {}          # номер кадра → (вид, причина)
_FRAME_GIVEUP_LOCK = threading.Lock()


def _note_frame_giveup(index: int, kind: str, why: str) -> None:
    """Запомнить, почему кадр сдался: от этого зависит, повторять ли его позже."""
    with _FRAME_GIVEUP_LOCK:
        _FRAME_GIVEUP[int(index)] = (kind, " ".join(str(why or "").split())[:160])


def _giveup_kind(error: str) -> str:
    return GAP_LATER if "try again later" in str(error or "").lower() else GAP_OTHER


def image_gaps_allowed(total: int) -> int:
    """Сколько сцен ролика можно отдать соседнему кадру: 2%, но не меньше одной."""
    return max(1, int(int(total) * IMAGE_GAP_MAX_SHARE))


def image_filter_gaps_allowed(total: int) -> int:
    """Сколько сцен всего можно отдать соседнему кадру, если сверх 2% — только отказы фильтра."""
    return max(image_gaps_allowed(total), int(int(total) * IMAGE_FILTER_GAP_MAX_SHARE))


def image_gaps_acceptable(gaps, total: int, giveup=None) -> bool:
    """Можно ли отдать эти сцены соседним кадрам: прочих сбоев не больше 2%, а вместе с
    отказами фильтра — не больше 10% ролика."""
    gaps = list(gaps)
    other = late_retry_indices(gaps, giveup)
    return (len(other) <= image_gaps_allowed(total)
            and len(gaps) <= image_filter_gaps_allowed(total))


# 14.10.2026, владелец: «ребята сказали просто в таком случае кадры растягивать, которые
# уже готовы, вместо тех, что не отрисовались». За неделю 29 задач картинок упали на
# пределах 2% / 10%, хотя рендер и так отдаёт время сцены без картинки предыдущему
# готовому кадру (render_worker._collapse_missing_images). Теперь итог решает не доля, а
# самая длинная серия пропусков подряд: разрозненные пропуски растягиваются всегда, а
# серия длиннее IMAGE_GAP_MAX_RUN — минуты одной картинки — по-прежнему ошибка. Граница
# та же, что у гейта рендера (main.IMG_RENDER_MAX_GAP = 5). Доли выше остаются для
# «повтора позже»: ждать и платить повторно имеет смысл, только когда недостаёт немного.
IMAGE_GAP_MAX_RUN = int(os.environ.get("VS_IMAGE_GAP_MAX_RUN", "5") or 5)


def longest_gap_run(gaps) -> int:
    """Самая длинная серия пропущенных сцен подряд."""
    best = run = 0
    prev = None
    for i in sorted(set(int(g) for g in gaps)):
        run = run + 1 if prev is not None and i == prev + 1 else 1
        best = max(best, run)
        prev = i
    return best


def image_gaps_stretchable(gaps, total: int) -> bool:
    """Можно ли отдать пропуски готовым кадрам: хоть один кадр есть, и подряд пропущено
    не больше IMAGE_GAP_MAX_RUN сцен."""
    gaps = list(gaps)
    return (bool(gaps) and len(set(gaps)) < int(total)
            and longest_gap_run(gaps) <= IMAGE_GAP_MAX_RUN)


def late_retry_indices(gaps, giveup=None) -> list:
    """Кадры, которые имеет смысл повторить позже: всё, кроме отказа фильтра."""
    giveup = _FRAME_GIVEUP if giveup is None else giveup
    return [i for i in gaps if (giveup.get(i) or (GAP_OTHER, ""))[0] != GAP_FILTER]


def write_image_gaps(output_dir: str, gaps, giveup=None) -> list:
    """Записать (или убрать) список сцен, отданных соседнему кадру. Возвращает строки."""
    giveup = _FRAME_GIVEUP if giveup is None else giveup
    path = os.path.join(output_dir, IMAGE_GAPS_FILE)
    rows = []
    for i in sorted(gaps):
        kind, why = giveup.get(i) or (GAP_OTHER, "")
        rows.append({"scene": i + 1, "kind": kind,
                     "reason": why or "кадр не получен",
                     "fallback": "кадр предыдущей сцены"})
    if rows:
        _atomic_write_json(path, {"schema_version": 1, "at": int(time.time()), "frames": rows})
    elif os.path.exists(path):
        os.remove(path)
    return rows


# ── Откат на другую модель после отказа фильтра (05.10.2026) ─────────────────────────────
# Кадр, который провайдер режет фильтром безопасности и после трёх смягчений, раньше
# сразу уходил в «пропуск» (подмена предыдущим кадром до 2% ролика, сверх — разбор).
# Выпуск №101: 6 кадров из 175 про реального человека и марку — больше 2%, и ни повтор,
# ни «вернуть в работу» его не лечили: фильтр отвечает одинаково. У моделей одного ключа
# FastGen фильтры разные — тот же приём уже спасает обложки
# (cover_core.COVER_SAFETY_FALLBACK_PROVIDER = flower).
#
# Правила:
#   * ровно ОДИН дополнительный заказ на кадр за прогон и только на отказе фильтра после
#     исчерпанных смягчений (404, потерянный заказ, «try again later» идут своими путями);
#   * откатная модель любой неудачей заканчивает кадр — повторов на ней нет, иначе каждый
#     повтор был бы ещё одной оплатой;
#   * журнал платных вызовов: строка основной модели закрывается отказом, откат идёт
#     своей строкой со своей моделью и провайдером — видно, кто взял кредиты;
#   * квитанция кадра (image_receipts.v1.json) остаётся квитанцией КОНТРАКТА кадра
#     (сцена, стиль, референсы): набор её полей сервер сверяет строго, а отпечаток с
#     другой моделью сделал бы кадр «устаревшим», и следующий прогон перезаказывал бы его
#     по кругу (основная модель → фильтр → откат). Фактическая модель кадра пишется
#     рядом, в image_model_fallbacks.json; кадр, позже перерисованный основной моделью,
#     оттуда убирается.
#
# Порядок моделей отката (11.10.2026, владелец: «Flower по качеству сильно хуже, я бы
# в последнюю очередь его ставил»): Nano Banana 2 (Flow) → Nano Banana Pro → Flower.
# Все три — тот же /api/v6/generations и тот же ключ, кадр не выпадает из ролика по
# стилю. Берётся первая модель списка, отличная от основной; модель, ответившая «занято»
# (No accounts available), до конца прогона пропускается — следующий откат идёт
# следующей по списку. Заказ отката на кадр по-прежнему один. Flow-модели — 4 кредита,
# Flower — 1. Через запятую в VS_IMAGE_SAFETY_FALLBACK_MODEL; пусто / «off» — отката нет.
IMAGE_SAFETY_FALLBACK_MODEL = (os.environ.get("VS_IMAGE_SAFETY_FALLBACK_MODEL",
                                              "NARWHAL,GEM_PIX_2,FLOWER")
                               or "").strip().upper()
# flower принимает ровно одну картинку на вход (так же решено у обложек) — откату на неё
# отдаём только первый референс кадра.
IMAGE_FALLBACK_MAX_INPUTS = {"FLOWER": 1}
IMAGE_MODEL_FALLBACKS_FILE = "image_model_fallbacks.json"
_SAFETY_FALLBACK_USED: set = set()      # кадры, уже получившие откат в этом прогоне
_SAFETY_FALLBACK_BUSY: set = set()      # модели отката, ответившие «занято» в этом прогоне
_SAFETY_FALLBACK_LOCK = threading.Lock()
_FRAME_FALLBACK = threading.local()     # откат текущего кадра (поток = кадр)


def safety_fallback_model(model: str) -> str:
    """Какой моделью заказать кадр после отказа фильтра — или "" (отката нет)."""
    chain = [m.strip() for m in str(IMAGE_SAFETY_FALLBACK_MODEL or "").split(",") if m.strip()]
    if not chain or chain[0] in ("OFF", "NONE", "0", "НЕТ"):
        return ""
    current = str(model or "").strip().upper()
    usable = [m for m in chain if m != current and m in GOOGLER_AVAILABLE_MODELS]
    with _SAFETY_FALLBACK_LOCK:
        free = [m for m in usable if m not in _SAFETY_FALLBACK_BUSY]
    # Заняты все — пробуем первую по списку: «занято» временное.
    return (free or usable or [""])[0]


def release_safety_fallback(frame_key: str) -> None:
    """Вернуть кадру право на откат: откатная модель была временно недоступна.

    09.10.2026, задача 993: откат на Flower трижды получил «No accounts available, please
    try again later» — провайдер занят, кадр он не рассматривал. Право на откат при этом
    сгорало, добивки снова шли в основную модель, её снова резал фильтр, и сцены уходили
    соседнему кадру. Временный отказ — не ответ модели отката, его повторяют позже.
    """
    with _SAFETY_FALLBACK_LOCK:
        _SAFETY_FALLBACK_USED.discard(str(frame_key))


def _fallback_gave_up(index: int, model: str, reason: str, last: str,
                      frame_key: str) -> None:
    """Откат не дал кадр. Временный сбой провайдера — «повторить позже» и право на откат
    сохраняется; ответ самой модели (фильтр, отказ) — окончательно, как раньше."""
    label = GOOGLER_MODEL_LABELS.get(model, model)
    if _is_transient_gen_error(last):
        release_safety_fallback(frame_key)
        with _SAFETY_FALLBACK_LOCK:
            _SAFETY_FALLBACK_BUSY.add(str(model or "").strip().upper())
        _note_frame_giveup(index, GAP_LATER,
                           f"{reason}; откат на модель {model} временно недоступен: {last}")
        print_user(f"⏳ [{index+1}] Откат на {label} временно недоступен ({last}) — "
                   f"кадр повторится откатом в добивке", "WARNING")
        return
    _note_frame_giveup(index, GAP_FILTER,
                       f"{reason}; откат на модель {model} не дал кадр: {last}")
    print_user(f"⛔ [{index+1}] Откат на {label} не дал кадр ({last}) — пропускаю картинку",
               "ERROR")


def claim_safety_fallback(frame_key: str) -> bool:
    """Откат для кадра — один раз за прогон: добивки пропущенных и повтор позже снова
    зовут генерацию, и без этой отметки каждый их круг оплачивал бы ещё один откат."""
    key = str(frame_key)
    with _SAFETY_FALLBACK_LOCK:
        if key in _SAFETY_FALLBACK_USED:
            return False
        _SAFETY_FALLBACK_USED.add(key)
        return True


def record_model_fallback(output_path: Optional[str], scene: int,
                          entry: Optional[dict]) -> None:
    """Записать (entry) или убрать (None) фактическую модель кадра в image_model_fallbacks.json."""
    if not output_path:
        return
    path = os.path.join(os.path.dirname(os.path.abspath(output_path)), IMAGE_MODEL_FALLBACKS_FILE)
    if not entry and not os.path.exists(path):
        return                           # убирать нечего — обычный кадр, обычный прогон
    with _SAFETY_FALLBACK_LOCK:
        data = _load_json_object(path) if os.path.exists(path) else None
        rows = [row for row in ((data or {}).get("frames") or [])
                if isinstance(row, dict) and row.get("scene") != int(scene)]
        if entry:
            rows.append(dict(entry, scene=int(scene)))
        rows.sort(key=lambda row: int(row.get("scene") or 0))
        if rows:
            _atomic_write_json(path, {"schema_version": 1, "at": int(time.time()), "frames": rows})
        elif os.path.exists(path):
            os.remove(path)


def order_never_delivers(probe) -> str:
    """Почему по уже созданному заказу результата не будет НИКОГДА — или "".

    Два определённых ответа провайдера на вопрос о заказе:
      * 404 — заказа у него нет (потерял или вычистил старые операции, 03.10);
      * status=failed — он сам отклонил заказ, чаще всего фильтром безопасности
        («Request blocked by provider safety filters»). 05.10.2026 из-за этого пять
        выпусков встали в разбор: в каждом не хватало 5 кадров из 1184, журнал считал
        такой заказ «оплаченным без результата» и кадр не перезаказывал никогда, хотя
        обычный путь генерации умеет смягчать промпт именно на этот отказ.
    Всё прочее (сеть, 5xx, «ещё в работе») — не вывод: ждать или спросить позже.
    """
    if not isinstance(probe, dict):
        return ""
    err = str(probe.get("_error") or "")
    if "404" in err:
        return "провайдер потерял заказ (404)"
    if str(probe.get("status") or "").lower() == "failed":
        why = probe.get("error") or probe.get("message") or ""
        if isinstance(why, dict):
            why = why.get("message") or json.dumps(why, ensure_ascii=False)
        why = " ".join(str(why or "").split())[:160]
        return "провайдер отклонил заказ" + (f": {why}" if why else "")
    return ""


def poll_v6_generation(gen_id: str, index: int = 0, timeout: int = None) -> Optional[str]:
    """Поллит GET /api/v6/generations/{id} до succeeded/failed. Возвращает base64 (картинка/видео) или None.
    Контракт возврата как у poll_v4_operation (base64-строка → save_base64_to_file)."""
    if timeout is None:
        timeout = VIDEO_POLL_TIMEOUT
    start = time.time(); hb = 0.0
    lost_since = None          # с какого момента провайдер подряд отвечает «заказа нет»
    while time.time() - start < timeout:
        r = googler_request_v6(f"generations/{gen_id}", method="GET")
        st = r.get("status", "unknown")
        if st == "succeeded":
            elapsed = int(time.time() - start)
            results = r.get("results") or []
            if results:
                item = results[0]
                data = item.get("data")
                if data:  # inline data-uri
                    print_user(f"✅ [{index + 1}] v6 готово ({elapsed}с, inline)", "SUCCESS")
                    return data.split(",", 1)[-1] if (isinstance(data, str) and data.startswith("data:")) else data
                url = item.get("download_url")
                if url:
                    # Ретраим СКАЧИВАНИЕ отдельно: генерация уже готова и ОПЛАЧЕНА — обрыв CDN
                    # (IncompleteRead и т.п.) не повод перегенерировать за новые кредиты (04.07).
                    _dl_err = None
                    for _dl_try in range(3):
                        try:
                            b = requests.get(url, timeout=REQUEST_TIMEOUT).content
                            print_user(f"✅ [{index + 1}] v6 готово ({elapsed}с, {len(b)//1024}КБ)", "SUCCESS")
                            return base64.b64encode(b).decode("ascii")
                        except Exception as e:
                            _dl_err = e
                            print_debug(f"[{index + 1}] v6 download попытка {_dl_try+1}/3: {str(e)[:120]}")
                            time.sleep(2 * (_dl_try + 1))
                    print_user(f"❌ [{index + 1}] v6: не скачался download_url (3 попытки): {str(_dl_err)[:120]}", "ERROR")
                    return None
            print_user(f"⚠️ [{index + 1}] v6 succeeded, но results пусты", "WARNING")
            return None
        elif st == "failed":
            err = r.get("error") or r.get("_error") or json.dumps(r, ensure_ascii=False)[-200:]
            # Optional[str] ниже не должен стирать тип terminal failure.  Typed safety and
            # transport reasons stay byte-for-byte unchanged; only an unclassified terminal
            # failure gets a private marker used by the OPENAI image retry bound.
            _set_last_gen_error(_typed_v6_terminal_error(err))
            _set_provider_refusal(str(err))     # правда провайдера — в лог и в журнал
            print_user(f"❌ [{index + 1}] v6 generation failed: {str(err)[:200]}", "ERROR")
            return None
        else:  # queued / running / unknown
            if r.get("_error"):
                # Заказ уже создан и оплачен, поэтому опрос НЕ прерываем. Но причину
                # запоминаем: мёртвый ключ отвечает отказом на КАЖДЫЙ опрос, и без этого
                # наверх уезжал голый «таймаут операции» без единого слова почему.
                _set_provider_refusal(str(r["_error"]))
            # Исключение одно: провайдер сам говорит, что заказа у него нет («not found
            # or expired»). Результата по нему не будет никогда, и ждать полный таймаут
            # кадра — значит держать место картинок часами. Решает вызывающий: ветка
            # журнала помечает заказ потерянным, и кадр заказывается заново.
            if str(r.get("_error") or "").startswith("HTTP 404"):
                now = time.time()
                if lost_since is None:
                    lost_since = now
                elif now - lost_since >= V6_LOST_GRACE_SEC:
                    print_user(f"❌ [{index + 1}] v6: провайдер не знает заказ {gen_id} — "
                               f"{int(now - lost_since)}с подряд отвечает 404 "
                               f"(«не найден или истёк»), ждать нечего", "ERROR")
                    return None
            else:
                lost_since = None
            el = time.time() - start
            if el - hb >= 15:
                print_user(f"v6 операция [{index + 1}]: {int(el)}с ({st})...", "PROCESS")
                hb = el
            time.sleep(VIDEO_POLL_INTERVAL)
    print_error_user_friendly(f"Таймаут v6 операции [{index + 1}] ({timeout}с)")
    return None


# ── Стиль-серия → стиль КАДРА ────────────────────────────────────────────────
# Стиль подставляется в КАЖДЫЙ промпт первым и доминирующим (см. generate_image).
# Поэтому стиль, описывающий СЕРИЮ («alternating three registers: A; B; C»,
# «deliberately heterogeneous, like a folder of documents»), модель честно исполняет
# внутри ОДНОГО кадра: получается страница-коллаж из трёх полос на бумаге вместо сцены
# (боевой случай 05.08.2026). Чередование — свойство ПОСЛЕДОВАТЕЛЬНОСТИ, поэтому здесь
# оно и реализуется: на каждый кадр берётся ровно один регистр, по кругу.
_REGISTERS_MARK = re.compile(
    r"(?i)\b(?:alternating\s+)?(?:two|three|four|five|\d+)?\s*registers?\s*:",
)
_SERIES_PHRASES = re.compile(
    r"(?i)\s*(?:[^.;]*\b(?:deliberately\s+heterogeneous|like\s+a\s+folder\s+of\s+documents|"
    r"folder\s+of\s+documents|alternating|across\s+scenes|each\s+scene\s+differs|"
    r"varied\s+registers|mix\s+of\s+registers)\b[^.;]*)(?:\.|;|$)"
)


def split_style_registers(style: str):
    """«…alternating three registers: A; B; C. Общий хвост.» → (голова, [A,B,C], хвост).

    None, если стиль не описывает регистры (обычный стиль не трогаем вовсе).
    """
    if not style or not str(style).strip():
        return None
    m = _REGISTERS_MARK.search(style)
    if not m:
        return None
    head, rest = style[:m.start()].strip(" ,;"), style[m.end():].strip()
    parts = [p.strip(" ,;") for p in rest.split(";")]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None
    # У последнего регистра к хвосту приклеены общие фразы («Faded ochre… 16:9»):
    # регистр — первое предложение, остальное общее для всех кадров.
    last, _sep, tail = parts[-1].partition(".")
    regs = parts[:-1] + [last.strip(" ,;")]
    return (head, [r for r in regs if r], tail.strip())


def style_for_index(style: str, index: int = 0):
    """Стиль ДЛЯ ОДНОГО кадра: один регистр вместо перечня + без фраз про серию."""
    parsed = split_style_registers(style)
    if not parsed:
        return style, ""
    head, regs, tail = parsed
    chosen = regs[index % len(regs)]
    out = ", ".join(p for p in (head, chosen) if p)
    if tail:
        out = out.rstrip(" .,;") + ". " + tail
    # «как папка документов» осмысленно только для серии — в кадре это и есть коллаж
    out = _SERIES_PHRASES.sub(" ", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,;")
    return out, f"регистр {index % len(regs) + 1}/{len(regs)}"


def fit_prompt_budget(prompt: str, style: str = "", max_bytes: int = 8000):
    """Уложить промпт в байтовый бюджет API. При переполнении режем СТИЛЬ (он общий
    для всех картинок проекта), а НЕ хвост, где уникальная сцена: обрезка хвоста
    делала все промпты проекта одинаковыми => 100+ почти одинаковых картинок
    (баг 2026-07-07, мега-стиль ~4.9КБ в пресете). Лимит поднят 4000→8000: v6 API
    без maxLength, живой запрос 5313Б принят (openapi + live-проверка 07.07).
    Возвращает (prompt, флаг_был_ли_рез)."""
    pb = prompt.encode('utf-8')
    if len(pb) <= max_bytes:
        return prompt, False
    # errors='ignore' на срезе по байтам сам отбрасывает неполный utf-8 символ на границе
    # (ручной цикл по continuation-байтам оставлял висячий lead-байт => '�' раздувал бюджет)
    if style and prompt.startswith(style):
        tail = prompt[len(style):]
        budget = max_bytes - len(tail.encode('utf-8'))
        if budget > 0:
            return style.encode('utf-8')[:budget].decode('utf-8', errors='ignore') + tail, True
    # fallback: стиль не префикс или хвост сам больше бюджета — режем с конца (как раньше)
    return pb[:max_bytes].decode('utf-8', errors='ignore'), True


def _fit_reference_identity_prompt(style: str, identity_body: str,
                                   max_bytes: int = 4000) -> Tuple[str, bool]:
    """Preserve the complete identity body; only optional style may be trimmed."""
    body = str(identity_body or "").strip()
    body_bytes = len(body.encode("utf-8"))
    if body_bytes > max_bytes:
        raise ref_contract.ReferenceContractError(
            f"character identity alone exceeds provider prompt budget ({body_bytes}/{max_bytes} bytes)"
        )
    frame_style = str(style_for_index(style)[0] or "").strip(" ,")
    if not frame_style:
        return body, False
    separator = ", "
    available = max_bytes - body_bytes - len(separator.encode("utf-8"))
    if available <= 0:
        return body, True
    style_bytes = frame_style.encode("utf-8")
    trimmed_style = style_bytes[:available].decode("utf-8", errors="ignore").strip(" ,")
    if not trimmed_style:
        return body, True
    return f"{trimmed_style}{separator}{body}", len(style_bytes) > available


_VISIBLE_TEXT_CONTRACT_MARKER = "\n\nVISIBLE TEXT CONTRACT:\n"


def _visible_text_contract_suffix(scene_data: dict) -> str:
    """Builds the protected final image-copy suffix; missing means legacy/custom.

    ``visible_text=[]`` is an explicit no-writing decision. A missing key is kept
    as legacy behavior because old/custom visual prompts may already contain their
    own copy and must not be silently contradicted.
    """
    if "visible_text" not in scene_data:
        return ""
    visible_text = _validated_visible_text(scene_data, "image scene", True) or []
    if not visible_text:
        body = (
            "Render NO visible text: no letters, words, numbers, captions, labels, "
            "signage, logos, or watermarks anywhere in the image."
        )
    else:
        exact_json = json.dumps(visible_text, ensure_ascii=False)
        body = (
            "Render every decoded string value in the following JSON array exactly once "
            "and verbatim, using the placement described in the scene. JSON brackets, "
            "commas, escapes, and string-delimiter quotes are instruction syntax, not "
            f"image content:\n{exact_json}\n"
            "Preserve every character, spelling, language, script, capitalization, "
            "punctuation, and numeral exactly. Do NOT translate or transliterate. "
            "Add no other visible letters, words, numbers, captions, labels, logos, "
            "or watermarks."
        )
    return _VISIBLE_TEXT_CONTRACT_MARKER + body


def _strip_visible_text_contract(prompt: str) -> str:
    return str(prompt or "").split(_VISIBLE_TEXT_CONTRACT_MARKER, 1)[0].rstrip()


def _attach_visible_text_contract(prompt: str, suffix: str) -> str:
    base = _strip_visible_text_contract(prompt)
    return base + suffix if suffix else base


def _fit_prompt_with_visible_text_contract(prompt: str, style: str, suffix: str,
                                           max_bytes: int = 8000):
    """Fits the mutable style/scene while never byte-truncating exact image copy."""
    base = _strip_visible_text_contract(prompt)
    if not suffix:
        return fit_prompt_budget(base, style, max_bytes=max_bytes)
    suffix_bytes = len(suffix.encode("utf-8"))
    if suffix_bytes >= max_bytes:
        raise ScenePlanTerminalError("visible_text contract exceeds image prompt budget")
    fitted_base, trimmed = fit_prompt_budget(
        base, style, max_bytes=max_bytes - suffix_bytes
    )
    combined = _attach_visible_text_contract(fitted_base, suffix)
    return combined, trimmed or combined != _attach_visible_text_contract(prompt, suffix)


def _soften_with_visible_text_contract(prompt: str, round_no: int, style: str,
                                       suffix: str) -> Optional[str]:
    """Lets the safety rewriter change only scene prose, never exact image copy."""
    softened = soften_prompt(_strip_visible_text_contract(prompt), round_no)
    if not softened or not softened.strip():
        return None
    fitted, _trimmed = _fit_prompt_with_visible_text_contract(
        softened.strip(), style, suffix
    )
    return fitted


IMAGE_PROMPT_CONTRACT_VERSION = "image-segments-v1-scene-only-deterministic-softening"


def _compose_image_prompt_contract(*, style: str, camera: str,
                                   scene_prose: str, identity_block: str,
                                   ref_hints: List[str], visible_suffix: str,
                                   monochrome: bool,
                                   immutable_scene: bool = False,
                                   max_bytes: int = 8000) -> Tuple[str, bool, dict]:
    """Assemble an image prompt without ever truncating required semantics."""
    scene = str(scene_prose or "").strip()
    if not scene:
        raise ref_contract.ReferenceContractError("image scene prompt is empty")
    identity = str(identity_block or "").strip().rstrip(".")
    hints = [str(value).strip() for value in ref_hints if str(value).strip()]
    protected_parts = []
    if identity:
        protected_parts.append(f"NAMED IDENTITY CONTRACT: {identity}")
    protected_parts.append(f"SCENE CONTRACT: {scene}")
    if hints:
        protected_parts.append(
            "REFERENCE INPUT CONTRACT: " + " | ".join(hints)
        )
    protected_base = ". ".join(protected_parts)
    protected = protected_base + str(visible_suffix or "")
    protected_bytes = len(protected.encode("utf-8"))
    if protected_bytes > max_bytes:
        raise ref_contract.ReferenceContractError(
            "protected scene/identity/reference prompt exceeds provider budget "
            f"({protected_bytes}/{max_bytes} bytes)"
        )

    optional_parts = [str(style or "").strip(" ,")]
    if monochrome:
        optional_parts.extend([
            "black and white, monochrome, grayscale",
            "no color, no colors, monochrome only",
        ])
    optional_parts.append(str(camera or "").strip(" ,"))
    optional = ", ".join(value for value in optional_parts if value)
    if not optional:
        prompt = protected
        trimmed = False
    else:
        separator = ", "
        available = max_bytes - protected_bytes - len(separator.encode("utf-8"))
        optional_bytes = optional.encode("utf-8")
        if available <= 0:
            prompt = protected
            trimmed = True
        else:
            fitted_optional = optional_bytes[:available].decode(
                "utf-8", errors="ignore"
            ).strip(" ,")
            prompt = (
                f"{fitted_optional}{separator}{protected}"
                if fitted_optional else protected
            )
            trimmed = len(optional_bytes) > available
    state = {
        "style": str(style or ""),
        "camera": str(camera or ""),
        "scene_prose": scene,
        "identity_block": identity,
        "ref_hints": hints,
        "visible_suffix": visible_suffix,
        "monochrome": bool(monochrome),
        "immutable_scene": bool(immutable_scene),
        "max_bytes": int(max_bytes),
    }
    return prompt, trimmed, state


def _soften_image_prompt_contract(state: dict, round_no: int) -> Optional[Tuple[str, dict]]:
    """Deterministically soften scene prose; identity/ref/copy stay byte-exact."""
    if state.get("immutable_scene"):
        return None
    identity_block = str(state.get("identity_block") or "").strip()
    ref_hints = [
        str(value).strip()
        for value in (state.get("ref_hints") or [])
        if str(value).strip()
    ]
    # Any immutable identity/reference context means the mutable scene may name
    # that same subject.  Preserve those names and use a neutral suffix; the
    # exact identity/reference/copy bytes are still composed from ``state``.
    preserve_named_identity = bool(identity_block or ref_hints)
    scene = str(state.get("scene_prose") or "")
    sanitized, _count = _sanitize_blocked_terms(
        scene,
        preserve_named_identity=preserve_named_identity,
    )
    softened = _soften_prompt_local(
        sanitized, round_no,
        preserve_named_identity=preserve_named_identity,
    )
    if not softened or softened.strip() == scene.strip():
        return None
    updated = dict(state)
    updated["scene_prose"] = softened.strip()
    if updated.get("raw_provider_prompt"):
        prompt, _trimmed = _fit_prompt_with_visible_text_contract(
            updated["scene_prose"], "", updated.get("visible_suffix") or "",
            max_bytes=int(updated.get("max_bytes") or 8000),
        )
        return prompt, updated
    prompt, _trimmed, updated = _compose_image_prompt_contract(**updated)
    return prompt, updated


def _immutable_identity_provider_prompt(scene_prompt: str, camera: str) -> str:
    """Return the exact prompt emitted by generate_image for an identity asset."""
    prompt, _trimmed, _state = _compose_image_prompt_contract(
        style="",
        camera=camera,
        scene_prose=scene_prompt,
        identity_block="",
        ref_hints=[],
        visible_suffix="",
        monochrome=False,
        immutable_scene=True,
    )
    return prompt


def googler_v6_generate(v6_model: str, prompt: str, aspect_ratio: Optional[str] = "16:9",
                        inputs: Optional[list] = None, seed: Optional[int] = None,
                        quality: Optional[str] = None, count: Optional[int] = None,
                        duration_seconds: Optional[int] = None, index: int = 0,
                        timeout: int = None) -> Optional[str]:
    """Единый v6-вызов: POST /api/v6/generations → поллинг → base64. inputs=реф-картинки (data-uri/base64/dict)."""
    body = {"prompt": prompt, "model": v6_model}
    if aspect_ratio:
        body["aspect_ratio"] = aspect_ratio
    if inputs:
        _norm = []
        for i in inputs:
            if isinstance(i, dict):
                value = i.get("input") or i.get("image")
                if value:
                    item = dict(i)
                    item.pop("image", None)
                    item["input"] = value
                    _norm.append(item)
            else:
                if i:
                    _norm.append(i)
        if _norm:
            body["inputs"] = _norm
    if seed is not None and v6_model in V6_SEED_MODELS:
        try:
            body["seed"] = int(seed) % 2147483647
        except Exception:
            pass
    if quality:
        body["quality"] = quality
    if count:
        body["count"] = int(count)
    if duration_seconds:
        body["duration_seconds"] = int(duration_seconds)
    resp = googler_request_v6("generations", method="POST", data=body)
    if resp.get("_error"):
        # Отказ на СОЗДАНИИ заказа — единственное место, где провайдер называет причину
        # («HTTP 401: invalid api key»). Дальше она терялась: наверх уходила обёртка
        # «v6 генерация не удалась», и в карточке с журналом оставалось «rc=3».
        _set_provider_refusal(str(resp["_error"]))
        print_user(f"❌ [{index + 1}] v6 generation отклонён: "
                   f"{_humanize_refusal(str(resp['_error']))[:300]}", "ERROR")
        return None
    gid = resp.get("id")
    if not gid:
        _set_provider_refusal(f"ответ без id операции: {str(resp)[:160]}")
        print_user(f"❌ [{index + 1}] v6: нет id в ответе ({str(resp)[:160]})", "ERROR")
        return None
    # 03.09.2026: заказ создан и уже оплачен — фиксируем id ДО поллинга. Именно здесь
    # раньше терялись деньги: падение/таймаут во время ожидания и кадр заказывали заново.
    # Вне генерации кадра (видео, ручной вызов) заявки нет и это no-op.
    _journal_note_order(gid)
    return poll_v6_generation(gid, index=index, timeout=timeout)


def poll_video_status(operation_id: str, index: int = 0) -> Optional[str]:
    """
    Опрашивает статус генерации видео до завершения

    Returns:
        base64 data URI видео или None при ошибке
    """
    start_time = time.time()

    while time.time() - start_time < VIDEO_POLL_TIMEOUT:
        result = googler_request_v2(f"videos/status/{operation_id}", method="GET")

        status = result.get("status", "unknown")

        if status == "success":
            video_data = result.get("result")
            elapsed = int(time.time() - start_time)
            if video_data:
                print_user(f"✅ [{index}] Видео готово! (время генерации: {elapsed}с)", "SUCCESS")
            else:
                print_user(f"⚠️ [{index}] Статус success, но результат пуст (время: {elapsed}с)", "WARNING")
            return video_data
        elif status == "error":
            error_msg = (result.get("error") or result.get("message") or
                         result.get("error_message") or result.get("details") or
                         json.dumps(result, ensure_ascii=False)[-300:])
            print_user(f"❌ v2 video operation {operation_id} status=error: {error_msg}", "ERROR")
            return None
        elif status in ("pending", "processing"):
            elapsed = int(time.time() - start_time)
            print_debug(f"[{index}] Статус: {status} ({elapsed}с)...")
            if elapsed % 10 == 0:  # Показываем прогресс каждые 10 секунд
                print_user(f"Генерация видео {index}: {elapsed}с...", "PROCESS")
            time.sleep(VIDEO_POLL_INTERVAL)
        else:
            print_debug(f"[{index}] Неизвестный статус: {status}")
            time.sleep(VIDEO_POLL_INTERVAL)

    print_error_user_friendly(f"Таймаут ожидания видео {index} ({VIDEO_POLL_TIMEOUT}с)")
    return None


# ============ ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ (v1) ============

def _prepare_hero_reference(text: str, style: str, subject_hint: Optional[str] = None) -> Optional[dict]:
    """Подготовить image-промпт героя.

    Это единственная LLM-часть авто-героя. Серверная задача ``prompts`` сохраняет
    результат в scenes.meta.json, а задача ``images --use_plan`` только рисует по
    готовому тексту и не получает credential LLM-провайдера.
    """
    if subject_hint and str(subject_hint).strip():
        subj = str(subject_hint).strip().strip('"\'[]')
        low_quality = any(k in style.lower() for k in [
            "bad quality", "grainy", "144p", "480p", "low quality", "poor quality",
            "vintage", "old photo", "retro", "distorted",
        ])
        quality = "" if low_quality else ", sharp focus, photorealistic, natural lighting, high detail"
        identity_body = (
            f"a clear reference photo of {subj}, the single subject centered "
            f"and prominent, natural pose, simple uncluttered background{quality}, "
            f"no text, no words, no letters, no numbers"
        )
        full_prompt, _style_trimmed = _fit_reference_identity_prompt(
            style, identity_body
        )
        return {"prompt": full_prompt, "aspect_ratio": "16:9", "subject": subj}

    system_prompt = """Analyze the FULL script. Describe the Main Protagonist visually in 1 detailed sentence (Age, clothes, face, distinctive features). Physical description only.
Use the full script context to understand character appearance, clothing, and distinctive features.
CRITICAL: The character's appearance MUST match their nationality/ethnicity mentioned in the script. If the script mentions a specific nationality (Japanese, Russian, American, etc.), describe facial features, skin tone, and physical characteristics that match that nationality.
CRITICAL: NEVER mention names of famous people, celebrities, historical figures, or real persons. Describe a GENERIC FICTIONAL character matching the nationality/ethnicity from the script.
CRITICAL: Use only generic descriptive terms (e.g., "Japanese man", "soldier", "young person") - avoid any specific details that could identify a real person.
CRITICAL: Focus on physical appearance only: age, build, clothing, facial features matching the nationality. Do NOT include names, ranks, or any identifying information.
Output ONLY the description, no explanations."""

    payload = {
        "model": GPT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"FULL SCRIPT:\n{text}"}
        ],
        "max_tokens": 200,
        "temperature": 0.7
    }

    for attempt in range(HERO_MAX_RETRIES):
        try:
            response = _llm_post(
                OPENAI_API_URL,
                headers=_openai_chat_headers(),
                json=payload,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code == 429:
                wait_time = RETRY_DELAY * (attempt + 1)
                print_user(f"⏳ Rate limit GPT, ждем {wait_time}с... (попытка {attempt + 1}/{MAX_RETRIES})", "WARNING")
                time.sleep(wait_time)
                continue

            if response.status_code != 200:
                try:
                    error_text = response.text[:200] if hasattr(response, 'text') else str(response.status_code)
                except (UnicodeDecodeError, AttributeError):
                    error_text = response.content.decode('utf-8', errors='replace')[:200] if hasattr(response, 'content') else str(response.status_code)
                print_user(f"⚠️ GPT вернул статус {response.status_code}: {error_text}", "WARNING")

            response.raise_for_status()

            try:
                result = response.json()
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                print_debug(f"Ошибка декодирования JSON: {e}")
                text_response = response.content.decode('utf-8', errors='replace')
                result = json.loads(text_response)

            hero_description = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
            hero_description = hero_description.strip('"\'[]')

            if not hero_description or len(hero_description) < 10:
                print_user("⚠️ Описание героя слишком короткое, пропускаем", "WARNING")
                return None

            is_low_quality_style = any(keyword in style.lower() for keyword in [
                "bad quality", "grainy", "144p", "480p", "low quality", "poor quality",
                "vintage", "old photo", "retro", "distorted",
            ])
            generic_character_hint = "generic fictional character, not a real person, not a celebrity, not a historical figure"
            if is_low_quality_style:
                visual_prompt = f"Portrait photo, {generic_character_hint}, {hero_description}, no text, no words, no letters, no numbers, no written content"
            else:
                visual_prompt = f"Portrait photo, {generic_character_hint}, {hero_description}, cinematic lighting, photorealistic, 8k, no text, no words, no letters, no numbers, no written content"

            full_prompt, _trimmed = _fit_reference_identity_prompt(
                style, visual_prompt
            )
            _orig_bytes = len(style.encode('utf-8')) + len(
                visual_prompt.encode('utf-8')
            ) + 2
            if _trimmed:
                print_user(f"⚠️ Промпт слишком длинный ({_orig_bytes} байт): стиль урезан, описание героя сохранено ({len(full_prompt.encode('utf-8'))} байт)", "WARNING")
            return {
                "prompt": full_prompt,
                "aspect_ratio": "9:16",
                "description": hero_description,
            }
        except requests.exceptions.RequestException as e:
            error_type = type(e).__name__
            print_user(f"⚠️ Ошибка LLM при подготовке авто-героя (попытка {attempt + 1}/{MAX_RETRIES}): {error_type}: {str(e)[:150]}", "WARNING")
            if attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                print_user(f"🔄 Повтор через {wait_time}с...", "WARNING")
                time.sleep(wait_time)

    print_user("❌ Не удалось подготовить промпт авто-героя", "ERROR")
    return None


def generate_hero_reference(text: str, project_dir: str,
                            style: str = "cinematic, photorealistic, portrait",
                            use_legacy: bool = False,
                            subject_hint: Optional[str] = None,
                            prepared: Optional[dict] = None) -> Optional[str]:
    """Нарисовать auto_hero.jpg по заранее подготовленному или локально созданному промпту."""
    del use_legacy  # сохранён в сигнатуре ради обратной совместимости
    ready = prepared if isinstance(prepared, dict) else _prepare_hero_reference(
        text, style, subject_hint=subject_hint
    )
    full_prompt = str((ready or {}).get("prompt") or "").strip()
    if not full_prompt:
        print_user("❌ Нет готового промпта авто-героя", "ERROR")
        return None
    ratio = str((ready or {}).get("aspect_ratio") or "9:16")
    generation_fingerprint = ref_contract.canonical_fingerprint({
        "schema_version": 1,
        "kind": "auto-hero",
        "prompt_contract": "auto-hero-reference-v1",
        "model": str(GOOGLER_MODEL).upper(),
        "prompt": full_prompt,
        "aspect_ratio": ratio,
        "subject_hint": subject_hint or "",
    })
    provider_prompt = _immutable_identity_provider_prompt(
        full_prompt, "Reference portrait"
    )
    prompt_sha256 = hashlib.sha256(provider_prompt.encode("utf-8")).hexdigest()
    hero_path = _generated_reference_path(
        project_dir, "auto-hero", generation_fingerprint
    )
    if _generated_reference_cache_hit(
        hero_path, kind="auto-hero", input_fingerprint=generation_fingerprint,
        prompt_sha256=prompt_sha256,
    ):
        print_user(f"✅ Авто-герой уже существует: {hero_path}", "INFO")
        return hero_path
    label = str((ready or {}).get("subject") or (ready or {}).get("description") or "")[:80]
    if label:
        print_user(f"🎭 Генерация авто-героя: {label}…", "PROCESS")
    try:
        _idx, hero_b64 = generate_image(
            scene_data={
                "visual_prompt": full_prompt,
                "camera_angle": "Reference portrait",
                "subject_type": "character",
                "immutable_identity_prompt": True,
            },
            style="",
            output_path=hero_path,
            aspect_ratio=("portrait" if ratio == "9:16" else "landscape"),
            index=0,
            reference_bundle={
                "selected": [],
                "capacity": ref_contract.reference_capacity(GOOGLER_MODEL),
                "identity_block": "",
            },
            # 03.09.2026: авто-герой идёт с index=0 и в журнале столкнулся бы с кадром 001.
            journal_unit="ref-hero",
        )
        if hero_b64 and _reference_image_is_valid(hero_path):
            _commit_generated_reference_receipt(
                hero_path,
                kind="auto-hero",
                input_fingerprint=generation_fingerprint,
                prompt_sha256=prompt_sha256,
            )
            print_user(f"✅ Авто-герой сохранён: {hero_path}", "SUCCESS")
            return hero_path
    except Exception as exc:
        print_debug(f"hero generation err: {exc}")
    print_user("❌ Не удалось сгенерировать авто-героя", "ERROR")
    return None


def generate_character_ref(appearance: str, style: str, out_path: str,
                           sheet: bool = True,
                           input_fingerprint: Optional[str] = None) -> Optional[str]:
    """Генерит реф-картинку персонажа ИЗ ГОТОВОГО appearance (без GPT-описания) → out_path.
    sheet=True: character-sheet turnaround (3 ракурса одного человека) — макс. консистентность (research D).
    Возвращает out_path или None (тогда пайплайн якорит героя только текстом).
    """
    if not appearance or len(appearance.strip()) < 8:
        return None
    expected_fingerprint = _character_reference_input_fingerprint(
        appearance, style, sheet, GOOGLER_MODEL
    )
    if input_fingerprint is not None and input_fingerprint != expected_fingerprint:
        raise ref_contract.ReferenceContractError(
            "story reference path fingerprint does not match its prompt contract"
        )
    input_fingerprint = expected_fingerprint
    reference_kind = (
        "story-character-sheet" if sheet else "story-character-portrait"
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    generic = "generic fictional character, not a real person, not a celebrity, not a historical figure"
    if sheet:
        body = (f"character reference sheet, three views of the SAME single person side by side — "
                f"front view, three-quarter view, profile view, identical face and identical clothing in all three views, "
                f"neutral grey studio background, even flat lighting, {generic}, {appearance.strip()}, "
                f"no text, no words, no letters, no numbers, no written content")
        aspect = "16:9"
    else:
        body = (f"Portrait photo, {generic}, {appearance.strip()}, cinematic lighting, photorealistic, 8k, "
                f"no text, no words, no letters, no numbers")
        aspect = "9:16"
    # Identity/appearance is immutable. Only the optional style prefix may be
    # shortened; byte-slicing the tail would silently remove the hero's face.
    full_prompt, style_trimmed = _fit_reference_identity_prompt(style, body)
    camera = "Character reference sheet" if sheet else "Reference portrait"
    provider_prompt = _immutable_identity_provider_prompt(full_prompt, camera)
    prompt_sha256 = hashlib.sha256(provider_prompt.encode("utf-8")).hexdigest()
    if _generated_reference_cache_hit(
        out_path, kind=reference_kind, input_fingerprint=input_fingerprint,
        prompt_sha256=prompt_sha256,
    ):
        print_debug(f"Реф персонажа уже есть: {out_path}")
        return out_path
    if style_trimmed:
        print_user(
            "⚠️ Стиль референса сокращён; полная identity-карта сохранена",
            "WARNING",
        )
    try:
        _idx, image_data = generate_image(
            scene_data={
                "visual_prompt": full_prompt,
                "camera_angle": camera,
                "subject_type": "character",
                "immutable_identity_prompt": True,
            },
            style="",
            output_path=out_path,
            aspect_ratio=("landscape" if aspect == "16:9" else "portrait"),
            index=0,
            reference_bundle={
                "selected": [],
                "capacity": ref_contract.reference_capacity(GOOGLER_MODEL),
                "identity_block": "",
            },
            # 03.09.2026: реф персонажа тоже идёт с index=0 — свой unit, чтобы не
            # смешаться в журнале с кадром 001.
            journal_unit=f"ref-{os.path.splitext(os.path.basename(out_path))[0]}",
        )
        if image_data and _reference_image_is_valid(out_path):
            _commit_generated_reference_receipt(
                out_path,
                kind=reference_kind,
                input_fingerprint=input_fingerprint,
                prompt_sha256=prompt_sha256,
            )
            print_user(f"✅ Реф персонажа сохранён: {os.path.basename(out_path)}", "SUCCESS")
            return out_path
    except Exception as e:
        err = str(e)[:160]
        if "InputIsImproper" in err or "content policy" in err.lower():
            print_user(f"❌ Реф персонажа отклонён (политика контента): {err}", "ERROR")
            return None
        print_debug(f"reference generation err: {err}")
    print_user(f"⚠️ Не удалось сгенерировать реф персонажа ({os.path.basename(out_path)}) — якорю текстом", "WARNING")
    return None


def _ref_to_datauri(image_path: str, maxdim: int = 768) -> str:
    """Референс для recrafter v4: API ждёт reference_images = список data-uri СТРОК (не словарей)
    и давится большими картинками (>~сотни КБ → 504 Gateway Timeout). Ужимаем до maxdim → JPEG → data-uri.
    Пустая строка = пропустить реф (генерация без него), чтобы не ронять весь запрос."""
    try:
        from PIL import Image
        import io as _io
        im = Image.open(image_path).convert("RGB")
        im.thumbnail((maxdim, maxdim))
        buf = _io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as _e:
        print_debug(f"_ref_to_datauri: не смог ужать {os.path.basename(image_path)}: {_e}")
        return ""


REFERENCE_RUNTIME_SCHEMA = 1
REFERENCE_RUNTIME_FILE = "references.runtime.v1.json"
IMAGE_RECEIPTS_FILE = "image_receipts.v1.json"
IMAGE_RECEIPTS_SCHEMA = 2
_IMAGE_RECEIPT_WRITE_LOCK = threading.Lock()
IMAGE_MUTATION_LOCK_FILE = ".image-mutation.v1.lock"
IMAGE_SET_QUARANTINE_DIR = ".image-set-quarantine"
IMAGE_SET_QUARANTINE_MANIFEST = "manifest.v1.json"
IMAGE_SET_RECEIPT_BACKUP = "receipt.before.v1.json"
IMAGE_SET_QUARANTINE_SCHEMA = 1
_REFERENCE_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_ARTIFACT_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CANONICAL_NUMERIC_IMAGE_RE = re.compile(
    r"^(?P<number>[0-9]{3,})\.(?P<extension>jpe?g|png|webp)$", re.IGNORECASE
)
_IMAGE_SET_TRANSACTION_RE = re.compile(r"^txn-[0-9]{16,20}-[0-9a-f]{16}$")


def _path_is_link_or_reparse(path: str) -> bool:
    """Reject Unix symlinks and Windows junction/reparse-point directories."""
    try:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        if callable(isjunction) and isjunction(path):
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(reparse_flag and attributes & reparse_flag)
    except FileNotFoundError:
        return False


def _confined_project_directory(project_dir: str, *relative_parts: str) -> str:
    """Create/resolve a real project-owned directory without link traversal."""
    if not project_dir:
        raise ref_contract.ReferenceContractError(
            "project-owned path requires a concrete project directory"
        )
    project_root = os.path.realpath(os.path.abspath(str(project_dir)))
    try:
        os.makedirs(project_root, exist_ok=True)
    except OSError as exc:
        raise ref_contract.ReferenceContractError(
            "project directory cannot be created"
        ) from exc
    if not os.path.isdir(project_root):
        raise ref_contract.ReferenceContractError("project path is not a directory")

    current = project_root
    for raw_part in relative_parts:
        part = str(raw_part or "")
        parsed = Path(part)
        if (
            not part or parsed.is_absolute() or len(parsed.parts) != 1
            or part in (".", "..")
        ):
            raise ref_contract.ReferenceContractError(
                f"invalid project-owned directory component: {part!r}"
            )
        current = os.path.abspath(os.path.join(current, part))
        try:
            if os.path.commonpath((project_root, current)) != project_root:
                raise ref_contract.ReferenceContractError(
                    "project-owned directory escapes the project"
                )
        except ValueError as exc:
            raise ref_contract.ReferenceContractError(
                "project-owned directory escapes the project"
            ) from exc
        if os.path.lexists(current) and _path_is_link_or_reparse(current):
            raise ref_contract.ReferenceContractError(
                f"project-owned directory must not be a symlink/reparse point: {current}"
            )
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ref_contract.ReferenceContractError(
                f"project-owned directory cannot be created: {current}"
            ) from exc
        if (
            _path_is_link_or_reparse(current)
            or not os.path.isdir(current)
            or os.path.normcase(os.path.realpath(current))
            != os.path.normcase(current)
        ):
            raise ref_contract.ReferenceContractError(
                f"project-owned directory is not confined: {current}"
            )
    return current


class _ProjectImageMutationLock:
    """Cross-process exclusive lock for one project's mutable image state."""

    def __init__(self, project_dir: str, command: str):
        project = os.path.realpath(os.path.abspath(str(project_dir or "")))
        if not project_dir or not project:
            raise ref_contract.ReferenceContractError(
                "image mutation requires a concrete project directory"
            )
        self.project_dir = project
        self.command = str(command or "image-command")
        asset_dir = _confined_project_directory(project, "_ASSETS")
        self.path = os.path.join(asset_dir, IMAGE_MUTATION_LOCK_FILE)
        self.handle = None
        self._backend = None
        self._acquired = False

    @staticmethod
    def _holder_description(handle) -> str:
        try:
            handle.seek(1)
            raw = handle.read(4096).strip(b"\x00\r\n ")
            value = json.loads(raw.decode("utf-8")) if raw else {}
            if isinstance(value, dict):
                pid = value.get("pid", "?")
                command = value.get("command", "unknown")
                started = value.get("started_at", "?")
                return f"pid={pid}, command={command}, started_at={started}"
        except Exception:
            pass
        return "holder metadata unavailable"

    def _unlock_and_close(self) -> None:
        handle, backend = self.handle, self._backend
        self.handle = None
        self._backend = None
        try:
            if handle is not None and self._acquired:
                if backend == "fcntl":
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif backend == "msvcrt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._acquired = False
            if handle is not None:
                handle.close()

    def __enter__(self):
        if os.path.lexists(self.path) and _path_is_link_or_reparse(self.path):
            raise ref_contract.ReferenceContractError(
                "image mutation lock must not be a symlink/reparse point"
            )
        open_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, open_flags, 0o600)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EACCES):
                raise ref_contract.ReferenceContractError(
                    "image mutation lock path is not a confined regular file"
                ) from exc
            raise
        if (
            _path_is_link_or_reparse(self.path)
            or not stat.S_ISREG(os.fstat(fd).st_mode)
        ):
            os.close(fd)
            raise ref_contract.ReferenceContractError(
                "image mutation lock path is not a confined regular file"
            )
        self.handle = os.fdopen(fd, "r+b", buffering=0)
        try:
            if os.path.getsize(self.path) < 1:
                self.handle.seek(0)
                self.handle.write(b"\x00")
                self.handle.flush()
            self.handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    self._backend = "msvcrt"
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    self._backend = "fcntl"
                    fcntl.flock(
                        self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                holder = self._holder_description(self.handle)
                raise ref_contract.ReferenceContractError(
                    "проект уже изменяется другой командой картинок; "
                    f"повторите после её завершения ({holder})"
                ) from exc
            self._acquired = True
            metadata = json.dumps({
                "schema_version": 1,
                "pid": os.getpid(),
                "command": self.command,
                "started_at": int(time.time()),
                "project_dir": self.project_dir,
            }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.handle.seek(1)
            self.handle.truncate()
            self.handle.write(metadata)
            self.handle.flush()
            os.fsync(self.handle.fileno())
            return self
        except Exception:
            self._unlock_and_close()
            raise

    def __exit__(self, exc_type, exc, traceback):
        self._unlock_and_close()
        return False


def _serialize_project_image_mutation():
    """Decorate top-level commands; nested generate_image calls never relock."""
    def decorate(function):
        @functools.wraps(function)
        def locked(args, *function_args, **function_kwargs):
            project_dir = getattr(args, "project_dir", None)
            with _ProjectImageMutationLock(project_dir, function.__name__):
                # A process may have stopped between atomic moves of a previous
                # authoritative-set commit.  Recover before *any* command can
                # read or overwrite receipts, scenes, or numbered outputs.
                output_dir = _confined_project_directory(
                    project_dir, GOOGLER_IMAGES_DIR
                )
                _recover_incomplete_image_set_transactions(
                    project_dir, output_dir
                )
                return function(args, *function_args, **function_kwargs)
        return locked
    return decorate


def _load_json_object(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="strict") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reference_image_is_valid(path: str, expected_sha256: Optional[str] = None,
                              required_format: Optional[str] = None) -> bool:
    try:
        if not os.path.isfile(path):
            return False
        actual = _sha256_file(path)
        if expected_sha256 is not None and actual != expected_sha256:
            return False
        from PIL import Image
        with Image.open(path) as image:
            if required_format and str(image.format or "").upper() != required_format.upper():
                return False
            image.load()
            if image.width <= 0 or image.height <= 0:
                return False
        return True
    except Exception:
        return False


def _manifest_reference_path(manifest_dir: str, relative_path: str) -> str:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ref_contract.ReferenceContractError("manifest reference path is empty")
    rel = Path(relative_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise ref_contract.ReferenceContractError(
            f"manifest reference path must stay inside its bundle: {relative_path!r}"
        )
    root = os.path.realpath(manifest_dir)
    resolved = os.path.realpath(os.path.join(root, *rel.parts))
    try:
        inside = os.path.commonpath([root, resolved]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ref_contract.ReferenceContractError(
            f"manifest reference escapes its bundle: {relative_path!r}"
        )
    if not os.path.isfile(resolved):
        raise ref_contract.ReferenceContractError(
            f"manifest reference is missing: {relative_path!r}"
        )
    filename = os.path.basename(resolved)
    stem, suffix = os.path.splitext(filename)
    if suffix.lower() != ".jpg" or not _REFERENCE_SHA_RE.fullmatch(stem):
        raise ref_contract.ReferenceContractError(
            f"manifest source must be named <sha256>.jpg: {relative_path!r}"
        )
    if os.path.getsize(resolved) > ref_contract.MAX_REFERENCE_BYTES:
        raise ref_contract.ReferenceContractError(
            f"manifest source exceeds upload limit: {relative_path!r}"
        )
    if _sha256_file(resolved) != stem:
        raise ref_contract.ReferenceContractError(
            f"manifest source hash mismatch: {relative_path!r}"
        )
    try:
        raw = Path(resolved).read_bytes()
        checked = ref_contract.canonicalize_reference_image(
            raw, declared_format="image/jpeg"
        )
    except (OSError, ref_contract.ReferenceContractError) as exc:
        raise ref_contract.ReferenceContractError(
            f"manifest source is not a readable JPEG: {relative_path!r}"
        ) from exc
    if checked.source_sha256 != stem:
        raise ref_contract.ReferenceContractError(
            f"manifest source content hash mismatch: {relative_path!r}"
        )
    return resolved


def _load_reference_manifest(path: str) -> dict:
    """Validate the immutable server bundle, including paths and content hashes."""
    manifest_path = os.path.abspath(path)
    payload = _load_json_object(manifest_path)
    if payload is None:
        raise ref_contract.ReferenceContractError(
            f"reference manifest is missing or invalid JSON: {manifest_path}"
        )
    if payload.get("schema_version") != 1:
        raise ref_contract.ReferenceContractError("unsupported reference manifest schema")
    if set(payload) != {
        "schema_version", "bundle_id", "characters", "style", "source_fingerprint"
    }:
        raise ref_contract.ReferenceContractError("reference manifest v1 has unknown/missing fields")
    bundle_id = payload.get("bundle_id")
    if not isinstance(bundle_id, str) or not _REFERENCE_SHA_RE.fullmatch(bundle_id):
        raise ref_contract.ReferenceContractError("reference manifest has invalid bundle_id")
    unsigned = dict(payload)
    unsigned.pop("bundle_id", None)
    if ref_contract.canonical_fingerprint(unsigned) != bundle_id:
        raise ref_contract.ReferenceContractError("reference manifest bundle_id mismatch")

    raw_characters = payload.get("characters")
    raw_style = payload.get("style")
    if not isinstance(raw_characters, list) or not isinstance(raw_style, list):
        raise ref_contract.ReferenceContractError(
            "reference manifest characters/style must be arrays"
        )
    if len(raw_characters) > ref_contract.MAX_HERO_GROUPS:
        raise ref_contract.ReferenceContractError(
            f"reference manifest accepts at most {ref_contract.MAX_HERO_GROUPS} hero groups"
        )
    manifest_dir = os.path.dirname(manifest_path)
    characters = []
    fingerprint_characters = []
    seen_groups = set()
    digest_owners = {}

    def _claim_digest(sha256: str, owner: str) -> None:
        previous = digest_owners.get(sha256)
        if previous is not None and previous != owner:
            raise ref_contract.ReferenceContractError(
                "one reference source cannot belong to multiple logical owners "
                f"({previous} vs {owner}; sha256={sha256})"
            )
        digest_owners[sha256] = owner

    for index, row in enumerate(raw_characters):
        if not isinstance(row, dict):
            raise ref_contract.ReferenceContractError(
                f"reference character group {index} must be an object"
            )
        if set(row) != {"group_id", "name", "files"}:
            raise ref_contract.ReferenceContractError(
                f"reference character group {index} has unknown/missing fields"
            )
        group_id = str(row.get("group_id") or "").strip()
        name = str(row.get("name") or "").strip()
        files = row.get("files")
        if not group_id or group_id in seen_groups or not name:
            raise ref_contract.ReferenceContractError(
                f"reference character group {index} has invalid id/name"
            )
        if not isinstance(files, list) or not 1 <= len(files) <= ref_contract.MAX_HERO_ANGLES:
            raise ref_contract.ReferenceContractError(
                f"reference character group {group_id!r} requires 1..{ref_contract.MAX_HERO_ANGLES} angles"
            )
        if files != sorted(files) or len(set(files)) != len(files):
            raise ref_contract.ReferenceContractError(
                f"reference character group {group_id!r} files must be sorted and unique"
            )
        paths = [_manifest_reference_path(manifest_dir, item) for item in files]
        hashes = [os.path.splitext(os.path.basename(item))[0] for item in paths]
        for sha256 in hashes:
            _claim_digest(sha256, f"hero:{group_id}")
        characters.append({"group_id": group_id, "name": name, "files": paths})
        fingerprint_characters.append(
            {"group_id": group_id, "name": name, "hashes": hashes}
        )
        seen_groups.add(group_id)

    if not 0 <= len(raw_style) <= ref_contract.MAX_STYLE_SOURCES:
        raise ref_contract.ReferenceContractError(
            f"reference style requires 0..{ref_contract.MAX_STYLE_SOURCES} sources"
        )
    if raw_style != sorted(raw_style) or len(set(raw_style)) != len(raw_style):
        raise ref_contract.ReferenceContractError(
            "reference style files must be sorted and unique"
        )
    style = [_manifest_reference_path(manifest_dir, item) for item in raw_style]
    style_hashes = [os.path.splitext(os.path.basename(item))[0] for item in style]
    for sha256 in style_hashes:
        _claim_digest(sha256, "style:moodboard")
    expected_source_fingerprint = ref_contract.canonical_fingerprint(
        {"characters": fingerprint_characters, "style": style_hashes}
    )
    if payload.get("source_fingerprint") != expected_source_fingerprint:
        raise ref_contract.ReferenceContractError(
            "reference manifest source_fingerprint mismatch"
        )
    return {
        "bundle_id": bundle_id,
        "source_fingerprint": expected_source_fingerprint,
        "characters": characters,
        "style": style,
        "source_count": sum(len(row["files"]) for row in characters) + len(style),
        "manifest_path": manifest_path,
    }


def _parse_legacy_reference_sources(user_refs: Optional[str], style_refs: Optional[str]) -> dict:
    """Deterministic compatibility adapter for desktop/old CLI callers."""
    characters = []
    if user_refs:
        parts = [part.strip() for part in str(user_refs).split("|") if part.strip()]
        for index, part in enumerate(parts):
            if ":" in part and not re.match(r"^[A-Za-z]:[\\/]", part):
                name, raw_paths = part.split(":", 1)
                name = name.strip()
            else:
                name, raw_paths = ("hero" if len(parts) == 1 else f"hero-{index + 1}"), part
            paths = [item.strip() for item in raw_paths.split(";") if item.strip()]
            if not name or not 1 <= len(paths) <= ref_contract.MAX_HERO_ANGLES:
                raise ref_contract.ReferenceContractError(
                    f"legacy hero group {index + 1} requires a name and 1..{ref_contract.MAX_HERO_ANGLES} paths"
                )
            missing = [item for item in paths if not os.path.isfile(item)]
            if missing:
                raise ref_contract.ReferenceContractError(
                    f"legacy hero reference is missing: {missing[0]}"
                )
            characters.append(
                {"group_id": f"legacy-{index + 1}", "name": name, "files": paths}
            )
    if len(characters) > ref_contract.MAX_HERO_GROUPS:
        raise ref_contract.ReferenceContractError(
            f"legacy references accept at most {ref_contract.MAX_HERO_GROUPS} hero groups"
        )
    style = [item.strip() for item in str(style_refs or "").split(";") if item.strip()]
    if len(style) > ref_contract.MAX_STYLE_SOURCES:
        raise ref_contract.ReferenceContractError(
            f"legacy style accepts at most {ref_contract.MAX_STYLE_SOURCES} sources"
        )
    missing_style = [item for item in style if not os.path.isfile(item)]
    if missing_style:
        raise ref_contract.ReferenceContractError(
            f"legacy style reference is missing: {missing_style[0]}"
        )
    content = {
        "characters": [
            {
                "group_id": row["group_id"],
                "name": row["name"],
                "hashes": [_sha256_file(path) for path in row["files"]],
            }
            for row in characters
        ],
        "style": [_sha256_file(path) for path in style],
    }
    source_fingerprint = ref_contract.canonical_fingerprint(content)
    return {
        "bundle_id": ref_contract.canonical_fingerprint(
            {"schema_version": 1, "adapter": "legacy-cli", **content}
        ),
        "source_fingerprint": source_fingerprint,
        "characters": characters,
        "style": style,
        "source_count": sum(len(row["files"]) for row in characters) + len(style),
        "manifest_path": None,
    }


def _snapshot_legacy_reference_sources(project_dir: str,
                                       user_refs: Optional[str],
                                       style_refs: Optional[str]) -> dict:
    """Import legacy external paths into one immutable project-local bundle."""
    parsed = _parse_legacy_reference_sources(user_refs, style_refs)
    canonical_by_sha = {}
    digest_owners = {}

    def _claim_digest(sha256: str, owner: str) -> None:
        previous = digest_owners.get(sha256)
        if previous is not None and previous != owner:
            raise ref_contract.ReferenceContractError(
                "one legacy reference source cannot belong to multiple logical "
                f"owners ({previous} vs {owner}; sha256={sha256})"
            )
        digest_owners[sha256] = owner

    def _canonicalize(path: str):
        try:
            raw = Path(path).read_bytes()
            canonical = ref_contract.canonicalize_reference_image(raw)
        except (OSError, ref_contract.ReferenceContractError) as exc:
            raise ref_contract.ReferenceContractError(
                f"legacy reference cannot be snapshotted: {path}"
            ) from exc
        canonical_by_sha[canonical.sha256] = canonical.jpeg_bytes
        return canonical.sha256

    character_rows = []
    fingerprint_characters = []
    for row in parsed.get("characters", []):
        hashes = sorted(_canonicalize(path) for path in row["files"])
        if len(set(hashes)) != len(hashes):
            raise ref_contract.ReferenceContractError(
                f"legacy hero group {row['name']!r} contains duplicate images"
            )
        for sha256 in hashes:
            _claim_digest(sha256, f"hero:{row['group_id']}")
        files = [f"sources/{sha256}.jpg" for sha256 in hashes]
        character_rows.append({
            "group_id": row["group_id"], "name": row["name"], "files": files,
        })
        fingerprint_characters.append({
            "group_id": row["group_id"], "name": row["name"], "hashes": hashes,
        })
    style_hashes = sorted(_canonicalize(path) for path in parsed.get("style", []))
    if len(set(style_hashes)) != len(style_hashes):
        raise ref_contract.ReferenceContractError(
            "legacy style references contain duplicate images"
        )
    for sha256 in style_hashes:
        _claim_digest(sha256, "style:moodboard")
    source_fingerprint = ref_contract.canonical_fingerprint({
        "characters": fingerprint_characters, "style": style_hashes,
    })
    unsigned = {
        "schema_version": 1,
        "characters": character_rows,
        "style": [f"sources/{sha256}.jpg" for sha256 in style_hashes],
        "source_fingerprint": source_fingerprint,
    }
    bundle_id = ref_contract.canonical_fingerprint(unsigned)
    source_dir = _confined_project_directory(
        project_dir, "_ASSETS", "reference_bundles", bundle_id, "sources"
    )
    bundle_dir = os.path.dirname(source_dir)
    for sha256, jpeg_bytes in canonical_by_sha.items():
        destination = os.path.join(source_dir, f"{sha256}.jpg")
        if not _reference_image_is_valid(
            destination, expected_sha256=sha256, required_format="JPEG"
        ):
            _atomic_write_bytes(destination, jpeg_bytes)
    manifest_path = os.path.join(bundle_dir, "manifest.json")
    _atomic_write_json(manifest_path, {**unsigned, "bundle_id": bundle_id})
    return _load_reference_manifest(manifest_path)


def _load_reference_sources(args) -> dict:
    manifest_raw = getattr(args, "reference_manifest", None)
    user_raw = getattr(args, "user_refs", None)
    style_raw = getattr(args, "style_refs", None)
    if manifest_raw is not None:
        manifest = str(manifest_raw or "").strip()
        if not manifest:
            raise ref_contract.ReferenceContractError(
                "--reference_manifest was provided without a manifest path"
            )
        if user_raw is not None or style_raw is not None:
            raise ref_contract.ReferenceContractError(
                "--reference_manifest cannot be combined with legacy reference flags"
            )
        return _load_reference_manifest(manifest)
    return _snapshot_legacy_reference_sources(
        getattr(args, "project_dir"), user_raw, style_raw
    )


def _artifact_from_json(
    value: dict, project_dir: Optional[str] = None
) -> ref_contract.ReferenceArtifact:
    expected_keys = {
        "path", "sha256", "width", "height", "source_hashes", "kind", "fingerprint",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ref_contract.ReferenceContractError("runtime artifact must be an object")
    try:
        artifact = ref_contract.ReferenceArtifact(
            path=str(value["path"]),
            sha256=str(value["sha256"]),
            width=int(value["width"]),
            height=int(value["height"]),
            source_hashes=tuple(value["source_hashes"]),
            kind=str(value["kind"]),
            fingerprint=str(value["fingerprint"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ref_contract.ReferenceContractError("runtime artifact is malformed") from exc
    if (
        not _REFERENCE_SHA_RE.fullmatch(artifact.sha256)
        or not _REFERENCE_ARTIFACT_KIND_RE.fullmatch(artifact.kind)
        or not artifact.source_hashes
        or any(not _REFERENCE_SHA_RE.fullmatch(value) for value in artifact.source_hashes)
    ):
        raise ref_contract.ReferenceContractError("runtime artifact metadata is invalid")
    if project_dir is not None:
        artifact_root = _confined_project_directory(
            project_dir, "_ASSETS", "reference_artifacts"
        )
        artifact_path = os.path.realpath(os.path.abspath(artifact.path))
        try:
            confined = os.path.commonpath((artifact_root, artifact_path)) == artifact_root
        except ValueError:
            confined = False
        if not confined or artifact_path == artifact_root:
            raise ref_contract.ReferenceContractError(
                "runtime reference artifact escapes the project artifact directory"
            )
    expected_name = f"{artifact.kind}-{artifact.sha256}.jpg"
    if os.path.basename(artifact.path) != expected_name:
        raise ref_contract.ReferenceContractError(
            "runtime reference artifact path is not content-addressed"
        )
    if not _reference_image_is_valid(
        artifact.path, artifact.sha256, required_format="JPEG"
    ):
        raise ref_contract.ReferenceContractError(
            f"runtime reference artifact is missing or changed: {artifact.path}"
        )
    try:
        from PIL import Image
        with Image.open(artifact.path) as image:
            image.load()
            actual_dimensions = (image.width, image.height)
    except Exception as exc:
        raise ref_contract.ReferenceContractError(
            "runtime reference artifact cannot be decoded"
        ) from exc
    if actual_dimensions != (artifact.width, artifact.height):
        raise ref_contract.ReferenceContractError(
            "runtime reference artifact dimensions were tampered"
        )
    expected_fingerprint = ref_contract.canonical_fingerprint({
        "schema_version": ref_contract.CONTRACT_SCHEMA_VERSION,
        "kind": artifact.kind,
        "sha256": artifact.sha256,
        "source_hashes": list(artifact.source_hashes),
        "width": artifact.width,
        "height": artifact.height,
    })
    if artifact.fingerprint != expected_fingerprint:
        raise ref_contract.ReferenceContractError(
            "runtime reference artifact fingerprint mismatch"
        )
    return artifact


def _materialize_reference_bundle(reference_bundle: Optional[dict]) -> Tuple[List[dict], List[str], str]:
    """Convert an already packed scene bundle; never allocates or drops slots."""
    bundle = reference_bundle or {}
    selected = bundle.get("selected") or []
    if not isinstance(selected, list):
        raise ref_contract.ReferenceContractError("scene reference bundle is malformed")
    registered_capacity = ref_contract.reference_capacity(GOOGLER_MODEL)
    capacity = int(bundle.get("capacity", registered_capacity))
    if capacity != registered_capacity:
        raise ref_contract.ReferenceContractError(
            "scene reference bundle capacity differs from the shared registry"
        )
    if "used" in bundle and int(bundle.get("used") or 0) != len(selected):
        raise ref_contract.ReferenceContractError("scene reference bundle used count mismatch")
    if len(selected) > capacity:
        raise ref_contract.ReferenceContractError(
            f"prebuilt scene bundle exceeds model capacity ({len(selected)} > {capacity})"
        )
    inputs = []
    hints = []
    seen_slot_ids = set()
    seen_artifact_hashes = set()
    for input_index, row in enumerate(selected, 1):
        if not isinstance(row, dict):
            raise ref_contract.ReferenceContractError("scene reference slot is malformed")
        artifact = _artifact_from_json(row.get("artifact"))
        slot_id = str(row.get("slot_id") or "")
        if not slot_id or slot_id in seen_slot_ids:
            raise ref_contract.ReferenceContractError(
                "scene reference bundle has an empty/duplicate slot id"
            )
        if artifact.sha256 in seen_artifact_hashes:
            raise ref_contract.ReferenceContractError(
                "scene reference bundle duplicates an artifact"
            )
        seen_slot_ids.add(slot_id)
        seen_artifact_hashes.add(artifact.sha256)
        # 1536 keeps each tile useful in a 2-3 column hero/style sheet while the
        # resulting JPEG remains below provider upload limits.
        data_uri = _ref_to_datauri(artifact.path, maxdim=1536)
        if not data_uri:
            raise ref_contract.ReferenceContractError(
                f"cannot materialize required reference {slot_id!r}"
            )
        kind = str(row.get("kind") or "reference")
        raw_identity = str(row.get("entity_id") or slot_id)
        safe_identity = re.sub(
            r"[^a-z0-9_-]+", "-", raw_identity.casefold()
        ).strip("-") or re.sub(r"[^a-z0-9_-]+", "-", kind.casefold()).strip("-")
        filename = f"ref-{input_index:02d}-{safe_identity[:40]}-{artifact.sha256[:16]}.jpg"
        inputs.append({"filename": filename, "input": data_uri})
        if kind == "style":
            hints.append(
                f"reference input #{input_index} filename {filename} = STYLE moodboard; "
                "copy palette, lighting and drawing technique only, never its people or objects"
            )
        else:
            name = str(row.get("canonical_name") or row.get("entity_id") or "main subject")
            hints.append(
                f"reference input #{input_index} filename {filename} = IDENTITY for {name}; "
                "preserve that face/species and clothing, never treat it as a style sample"
            )
    return inputs, hints, str(bundle.get("identity_block") or "")


def _reference_input_value(value):
    if isinstance(value, dict):
        return value.get("input") or value.get("image") or ""
    return value


def _hero_applies_to_scene(scene: dict, hero_subject: Optional[str]) -> bool:
    subject_type = str(scene.get("subject_type") or "").casefold()
    if subject_type == "character":
        return True
    if not hero_subject:
        return False
    scene_subject = str(scene.get("subject") or "").casefold().strip()
    hero = str(hero_subject).casefold().strip()
    if not scene_subject:
        # A custom/no-bible prompt has no reliable entity binding.  The caller
        # was allowed here only after proving a single-subject project.
        return True
    if scene_subject == hero or hero in scene_subject or scene_subject in hero:
        return True
    generic = {
        "bird", "birds", "small", "large", "brown", "black", "white",
        "grey", "gray", "dark", "pale", "light", "tiny", "little",
        "young", "adult", "male", "female", "animal", "creature", "with",
        "feathers", "feathered", "plumage", "beak", "bill", "wings",
        "tail", "eyes", "body", "head", "long", "short", "thin", "round",
        "bright", "colored", "coloured", "bird's",
    }
    hero_words = {
        word for word in re.findall(r"[a-zа-яё]+", hero)
        if len(word) > 3 and word not in generic
    }
    scene_words = {
        word for word in re.findall(r"[a-zа-яё]+", scene_subject)
        if len(word) > 3
    }
    return bool(hero_words and hero_words & scene_words)


def _scene_identity_block(scene: dict, bible: Optional[dict]) -> str:
    """Named identity clauses keep multiple entities from being blended."""
    if not bible:
        return ""
    entities = (bible.get("characters") or []) + (bible.get("locations") or [])
    by_id = {row.get("id"): row for row in entities if isinstance(row, dict)}
    char_ids = {
        row.get("id") for row in (bible.get("characters") or [])
        if isinstance(row, dict)
    }
    clauses = []
    matched = scene.get("matched_refs") or []
    for entity_id in sorted(matched, key=lambda value: (value not in char_ids, str(value))):
        row = by_id.get(entity_id)
        appearance = str((row or {}).get("appearance") or "").strip().rstrip(".")
        if not appearance:
            continue
        names = (row or {}).get("names") or []
        name = str(names[0] if isinstance(names, list) and names else entity_id).strip()
        clauses.append(f"{name}: {appearance}")
    return "; ".join(clauses)


def _generated_reference_path(project_dir: str, prefix: str, fingerprint: str) -> str:
    safe_prefix = re.sub(r"[^a-zA-Z0-9_-]+", "-", prefix).strip("-") or "reference"
    directory = _confined_project_directory(
        project_dir, "_ASSETS", "generated_references"
    )
    return os.path.join(directory, f"{safe_prefix}-{fingerprint}.jpg")


GENERATED_REFERENCE_RECEIPT_SCHEMA = 1


def _generated_reference_receipt_path(image_path: str) -> str:
    return f"{image_path}.receipt.v1.json"


def _generated_reference_cache_hit(image_path: str, *, kind: str,
                                   input_fingerprint: str,
                                   prompt_sha256: str) -> bool:
    receipt = _load_json_object(_generated_reference_receipt_path(image_path))
    expected_keys = {
        "schema_version", "kind", "model", "input_fingerprint", "prompt_sha256",
        "output_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_keys:
        return False
    if (
        receipt.get("schema_version") != GENERATED_REFERENCE_RECEIPT_SCHEMA
        or receipt.get("kind") != kind
        or str(receipt.get("model") or "").upper() != str(GOOGLER_MODEL).upper()
        or receipt.get("input_fingerprint") != input_fingerprint
        or receipt.get("prompt_sha256") != prompt_sha256
        or not isinstance(receipt.get("output_sha256"), str)
        or not _REFERENCE_SHA_RE.fullmatch(receipt["output_sha256"])
    ):
        return False
    return _reference_image_is_valid(
        image_path, expected_sha256=receipt["output_sha256"]
    )


def _commit_generated_reference_receipt(image_path: str, *, kind: str,
                                        input_fingerprint: str,
                                        prompt_sha256: str) -> None:
    if not _REFERENCE_SHA_RE.fullmatch(str(input_fingerprint or "")):
        raise ref_contract.ReferenceContractError(
            "generated reference input fingerprint is invalid"
        )
    if not _REFERENCE_SHA_RE.fullmatch(str(prompt_sha256 or "")):
        raise ref_contract.ReferenceContractError(
            "generated reference prompt digest is invalid"
        )
    if not _reference_image_is_valid(image_path):
        raise ref_contract.ReferenceContractError(
            "generated reference output is not a valid image"
        )
    _atomic_write_json(_generated_reference_receipt_path(image_path), {
        "schema_version": GENERATED_REFERENCE_RECEIPT_SCHEMA,
        "kind": kind,
        "model": str(GOOGLER_MODEL).upper(),
        "input_fingerprint": input_fingerprint,
        "prompt_sha256": prompt_sha256,
        "output_sha256": _sha256_file(image_path),
    })


def _character_reference_input_fingerprint(appearance: str, style: str,
                                           sheet: bool, model: str) -> str:
    return ref_contract.canonical_fingerprint({
        "schema_version": 1,
        "kind": "story-character-sheet" if sheet else "story-character-portrait",
        "prompt_contract": "story-character-v2-identity-budget",
        "model": str(model).upper(),
        "appearance": str(appearance or "").strip(),
        "style": str(style_for_index(style)[0] or ""),
    })


def _manual_reference_resolutions(sources: dict, bible: Optional[dict],
                                  single_subject: bool):
    groups = tuple(
        ref_contract.ManualHeroGroup(
            group_id=row["group_id"], aliases=(row["name"],)
        )
        for row in sources.get("characters", [])
    )
    resolutions = ref_contract.resolve_manual_hero_groups(
        groups, bible, single_subject=single_subject
    ) if groups else ()
    if bible:
        characters_by_id = {
            row.get("id"): row
            for row in (bible.get("characters") or [])
            if isinstance(row, dict)
        }
        unsafe = [
            resolution
            for resolution in resolutions
            if resolution.character_id
            and characters_by_id.get(resolution.character_id, {}).get(
                "manual_binding_safe", True
            ) is False
        ]
        if unsafe:
            raise ref_contract.ReferenceContractError(
                "manual hero alias matches a story identity merged from multiple "
                "raw candidates; explicit disambiguation is required"
            )
    return resolutions


def _reference_scene_demands(scenes: List[dict], sources: dict,
                             bible: Optional[dict], single_subject: bool,
                             hero_subject: Optional[str], auto_hero: bool,
                             allow_generated_story_refs: bool = True,
                             force_manual_scene_index: Optional[int] = None):
    """Return logical demands without generating/uploading a single reference."""
    resolutions = _manual_reference_resolutions(sources, bible, single_subject)
    manual_by_character = {
        row.character_id: row for row in resolutions if row.character_id is not None
    }
    manual_single = (
        resolutions[0] if len(resolutions) == 1 and resolutions[0].character_id is None
        else None
    )
    all_bible_characters = [
        row for row in ((bible or {}).get("characters") or [])
        if isinstance(row, dict) and row.get("id")
    ]
    all_bible_by_id = {row.get("id"): row for row in all_bible_characters}
    generated_story_characters = [
        row for row in ((bible or {}).get("characters") or [])
        if isinstance(row, dict)
        and row.get("role") in ("protagonist", "major")
        and str(row.get("appearance") or "").strip()
    ]
    bible_by_id = {row.get("id"): row for row in generated_story_characters}
    has_story_characters = bool((bible or {}).get("characters"))
    auto_active = bool(
        auto_hero and not resolutions and not has_story_characters
    )
    demands = []
    for scene_index, scene in enumerate(scenes):
        selected = []
        superseded = []
        text_only_story = []
        matched = set(scene.get("matched_refs") or [])
        for character_id in sorted(matched & set(all_bible_by_id), key=str):
            manual = manual_by_character.get(character_id)
            if manual is not None:
                selected.append(("manual", manual.group_id, character_id))
                if character_id in bible_by_id:
                    superseded.append((character_id, manual.group_id))
            elif character_id in bible_by_id and allow_generated_story_refs:
                selected.append(("story", character_id, character_id))
            elif character_id in bible_by_id:
                text_only_story.append(character_id)
        if force_manual_scene_index == scene_index and len(resolutions) == 1:
            forced = resolutions[0]
            selected = [("manual", forced.group_id, forced.character_id)]
            superseded = []
        elif manual_single is not None:
            # Exact custom-prompt contract: a single manually named subject is
            # the project identity and therefore applies to every scene.
            selected.append(("manual", manual_single.group_id, None))
        if auto_active and _hero_applies_to_scene(scene, hero_subject):
            selected.append(("auto", "main-subject", None))
        if sources.get("style"):
            selected.append(("style", "style", None))
        demands.append({
            "selected": selected,
            "superseded": superseded,
            "text_only_story": text_only_story,
        })
    return resolutions, bible_by_id, auto_active, demands


def _preflight_reference_demands(scenes: List[dict], demands: List[dict], model: str) -> int:
    capacity = ref_contract.reference_capacity(model)
    errors = []
    for index, demand in enumerate(demands):
        required = len(demand["selected"])
        if required > capacity:
            kinds = ", ".join(item[0] for item in demand["selected"])
            errors.append(
                f"scene {index + 1}: requires {required}/{capacity} slots ({kinds})"
            )
    if errors:
        raise ref_contract.ReferenceContractError(
            "reference preflight failed before generation: " + "; ".join(errors[:12])
        )
    if len(demands) != len(scenes):
        raise ref_contract.ReferenceContractError("reference demand/scene count mismatch")
    return capacity


def _reference_preflight_placeholder(slot_id: str, kind: str,
                                     occupied_hashes: set) -> ref_contract.ReferenceArtifact:
    """A deterministic structural-only artifact for refs not generated yet."""
    nonce = 0
    while True:
        sha256 = ref_contract.canonical_fingerprint({
            "schema_version": 1,
            "kind": "reference-structural-preflight",
            "slot_id": slot_id,
            "slot_kind": kind,
            "nonce": nonce,
        })
        if sha256 not in occupied_hashes:
            occupied_hashes.add(sha256)
            break
        nonce += 1
    return ref_contract.ReferenceArtifact(
        path=f"preflight:{slot_id}",
        sha256=sha256,
        width=1,
        height=1,
        source_hashes=(sha256,),
        kind="preflight",
        fingerprint=ref_contract.canonical_fingerprint({
            "schema_version": 1, "artifact_sha256": sha256, "kind": kind,
        }),
    )


def _logical_slots_for_scene_demand(
    demand: dict,
    resolutions,
    manual_artifacts: dict,
    story_artifacts: dict,
    style_artifact,
    auto_artifact,
    manual_names: dict,
    bible_names: dict,
    hero_subject: Optional[str],
):
    """Build the exact slot graph shared by structural preflight and final pack."""
    logical_slots = []
    slot_metadata = {}
    for kind, key, entity_id in demand["selected"]:
        if kind == "manual":
            slot_id = f"manual:{key}"
            artifact = manual_artifacts.get(key)
            slot_kind = "manual_character"
            canonical_name = manual_names.get(key, key)
        elif kind == "story":
            slot_id = f"story:{key}"
            artifact = story_artifacts.get(key)
            slot_kind = "story_character"
            canonical_name = bible_names.get(key, key)
        elif kind == "auto":
            slot_id = "auto:main-subject"
            artifact = auto_artifact
            slot_kind = "auto_hero"
            canonical_name = hero_subject or "main subject"
        else:
            slot_id = "style:moodboard"
            artifact = style_artifact
            slot_kind = "style"
            canonical_name = "project style"
        logical_slots.append(ref_contract.LogicalReferenceSlot(
            slot_id=slot_id, kind=slot_kind, artifact=artifact
        ))
        slot_metadata[slot_id] = {
            "entity_id": entity_id,
            "canonical_name": canonical_name,
        }

    active_slot_ids = {slot.slot_id for slot in logical_slots}
    superseded_story_ids = {
        str(character_id) for character_id, _manual_group_id in demand["superseded"]
    }
    for resolution in resolutions:
        slot_id = f"manual:{resolution.group_id}"
        if slot_id not in active_slot_ids:
            logical_slots.append(ref_contract.LogicalReferenceSlot(
                slot_id=slot_id,
                kind="manual_character",
                artifact=manual_artifacts.get(resolution.group_id),
                applicable=False,
            ))
    for character_id, artifact in story_artifacts.items():
        slot_id = f"story:{character_id}"
        if (
            slot_id not in active_slot_ids
            and str(character_id) not in superseded_story_ids
        ):
            logical_slots.append(ref_contract.LogicalReferenceSlot(
                slot_id=slot_id,
                kind="story_character",
                artifact=artifact,
                applicable=False,
            ))
    for character_id in demand.get("text_only_story", []):
        slot_id = f"story:{character_id}"
        if (
            slot_id not in active_slot_ids
            and str(character_id) not in superseded_story_ids
        ):
            logical_slots.append(ref_contract.LogicalReferenceSlot(
                slot_id=slot_id,
                kind="story_character",
                artifact=None,
                applicable=False,
            ))
    for character_id, manual_group_id in demand["superseded"]:
        logical_slots.append(ref_contract.LogicalReferenceSlot(
            slot_id=f"story:{character_id}",
            kind="story_character",
            artifact=None,
            superseded_by=f"manual:{manual_group_id}",
        ))
    return logical_slots, slot_metadata


def _scene_reference_fingerprint(pack_fingerprint: str, selected_rows: List[dict],
                                 identity_block: str) -> str:
    """Bind provider-visible logical metadata to the shared pack digest."""
    return ref_contract.canonical_fingerprint({
        "schema_version": 2,
        "pack_fingerprint": pack_fingerprint,
        "identity_block": str(identity_block or ""),
        "selected": [
            {
                "slot_id": row.get("slot_id"),
                "kind": row.get("kind"),
                "entity_id": row.get("entity_id"),
                "canonical_name": row.get("canonical_name"),
                "artifact_sha256": (row.get("artifact") or {}).get("sha256"),
                "artifact_fingerprint": (
                    row.get("artifact") or {}
                ).get("fingerprint"),
            }
            for row in selected_rows
        ],
    })


def _prepare_reference_runtime(project_dir: str, scenes: List[dict], model: str,
                               style: str, bible: Optional[dict], sources: dict,
                               *, single_subject: bool,
                               hero_subject: Optional[str], auto_hero: bool,
                               hero_prepared: Optional[dict],
                               force_manual_scene_index: Optional[int] = None,
                               plan_scenes_sha256: Optional[str] = None,
                               persist: bool = True) -> dict:
    """Build every scene pack and commit it only after whole-project preflight."""
    scenes_sha256 = (
        str(plan_scenes_sha256)
        if plan_scenes_sha256 is not None
        else hashlib.sha256(_json_bytes(scenes)).hexdigest()
    )
    if not _REFERENCE_SHA_RE.fullmatch(scenes_sha256):
        raise ref_contract.ReferenceContractError(
            "scene plan digest for reference runtime is malformed"
        )
    if bible is not None:
        bible = _validate_story_bible(bible)
        _validate_explicit_alias_entity_coverage(scenes, bible)
    runtime_dir = _confined_project_directory(project_dir, GOOGLER_IMAGES_DIR)
    model_capacity = ref_contract.reference_capacity(model)
    resolutions, bible_by_id, auto_active, demands = _reference_scene_demands(
        scenes, sources, bible, single_subject, hero_subject, auto_hero,
        allow_generated_story_refs=(model_capacity > 0),
        force_manual_scene_index=force_manual_scene_index,
    )
    capacity = _preflight_reference_demands(scenes, demands, model)

    artifact_dir = _confined_project_directory(
        project_dir, "_ASSETS", "reference_artifacts"
    )
    manual_artifacts = {}
    source_by_group = {
        row["group_id"]: row for row in sources.get("characters", [])
    }
    for resolution in resolutions:
        row = source_by_group[resolution.group_id]
        manual_artifacts[resolution.group_id] = ref_contract.build_contact_sheet(
            row["files"], artifact_dir, kind="hero"
        )
    identity_artifact_owners = {}
    for resolution in resolutions:
        artifact = manual_artifacts[resolution.group_id]
        identity_key = (
            f"story:{resolution.character_id}"
            if resolution.character_id else f"manual:{resolution.group_id}"
        )
        previous_owner = identity_artifact_owners.get(artifact.sha256)
        if previous_owner is not None and previous_owner != identity_key:
            raise ref_contract.ReferenceContractError(
                "one reference artifact cannot represent two different character "
                f"identities ({previous_owner} vs {identity_key})"
            )
        identity_artifact_owners[artifact.sha256] = identity_key
    style_artifact = None
    if sources.get("style"):
        style_artifact = ref_contract.build_contact_sheet(
            sources["style"], artifact_dir, kind="style"
        )

    manual_names = {
        row["group_id"]: row["name"] for row in sources.get("characters", [])
    }
    bible_names = {
        character_id: str(((row.get("names") or [character_id])[0]))
        for character_id, row in bible_by_id.items()
    }
    required_story_ids = sorted({
        item[1]
        for demand in demands for item in demand["selected"]
        if item[0] == "story"
    })

    # Count-only capacity is insufficient: the shared packer also rejects
    # duplicate slot ids and duplicate active content. Run that exact structure
    # with actual local hero/style artifacts and unique placeholders before a
    # story/auto provider can spend a credit.
    occupied_hashes = {
        artifact.sha256 for artifact in manual_artifacts.values()
    }
    if style_artifact is not None:
        occupied_hashes.add(style_artifact.sha256)
    preflight_story_artifacts = {
        character_id: _reference_preflight_placeholder(
            f"story:{character_id}", "story_character", occupied_hashes
        )
        for character_id in required_story_ids
    }
    preflight_auto_artifact = (
        _reference_preflight_placeholder(
            "auto:main-subject", "auto_hero", occupied_hashes
        ) if auto_active else None
    )
    for demand in demands:
        preflight_slots, _metadata = _logical_slots_for_scene_demand(
            demand, resolutions, manual_artifacts, preflight_story_artifacts,
            style_artifact, preflight_auto_artifact, manual_names, bible_names,
            hero_subject,
        )
        ref_contract.pack_scene_references(preflight_slots, model)

    story_artifacts = {}
    for character_id in required_story_ids:
        character = bible_by_id[character_id]
        generation_fingerprint = _character_reference_input_fingerprint(
            str(character.get("appearance") or ""), style, True, model
        )
        generated_path = _generated_reference_path(
            project_dir, f"story-{character_id}", generation_fingerprint
        )
        got = generate_character_ref(
            str(character.get("appearance") or ""), style, generated_path,
            sheet=True, input_fingerprint=generation_fingerprint,
        )
        if not got:
            raise ref_contract.ReferenceContractError(
                f"required story reference failed for character {character_id!r}"
            )
        story_artifacts[character_id] = ref_contract.persist_reference_artifact(
            got, artifact_dir, kind="story-character"
        )
        story_sha = story_artifacts[character_id].sha256
        previous_owner = identity_artifact_owners.get(story_sha)
        story_owner = f"story:{character_id}"
        if previous_owner is not None and previous_owner != story_owner:
            raise ref_contract.ReferenceContractError(
                "one generated artifact cannot represent two different character "
                f"identities ({previous_owner} vs {story_owner})"
            )
        identity_artifact_owners[story_sha] = story_owner

    auto_artifact = None
    if auto_active:
        if not isinstance(hero_prepared, dict) or not str(hero_prepared.get("prompt") or "").strip():
            raise ref_contract.ReferenceContractError(
                "explicit auto hero has no prepared deterministic prompt"
            )
        auto_path = generate_hero_reference(
            "", project_dir, style=style, subject_hint=hero_subject,
            prepared=hero_prepared,
        )
        if not auto_path:
            raise ref_contract.ReferenceContractError(
                "explicit auto hero generation failed; continuing without it is forbidden"
            )
        auto_artifact = ref_contract.persist_reference_artifact(
            auto_path, artifact_dir, kind="auto-hero"
        )

    scene_rows = []
    for index, (scene, demand) in enumerate(zip(scenes, demands)):
        logical_slots, slot_metadata = _logical_slots_for_scene_demand(
            demand, resolutions, manual_artifacts, story_artifacts,
            style_artifact, auto_artifact, manual_names, bible_names,
            hero_subject,
        )
        pack = ref_contract.pack_scene_references(logical_slots, model)
        audit_rows = [asdict(row) for row in pack.audit]
        text_only_slot_ids = {
            f"story:{character_id}"
            for character_id in demand.get("text_only_story", [])
        }
        for audit_row in audit_rows:
            if audit_row["slot_id"] in text_only_slot_ids:
                audit_row["reason"] = (
                    "model capacity is zero; named story identity is enforced by text only"
                )
        selected_rows = []
        for slot in pack.selected:
            metadata = slot_metadata.get(slot.slot_id, {})
            selected_rows.append({
                "slot_id": slot.slot_id,
                "kind": slot.kind,
                "entity_id": metadata.get("entity_id"),
                "canonical_name": metadata.get("canonical_name"),
                "artifact": asdict(slot.artifact),
            })
        identity_block = _scene_identity_block(scene, bible)
        scene_rows.append({
            "index": index,
            "bundle_id": sources["bundle_id"],
            "source_fingerprint": sources["source_fingerprint"],
            "fingerprint": _scene_reference_fingerprint(
                pack.fingerprint, selected_rows, identity_block
            ),
            "used": len(pack.selected),
            "capacity": pack.capacity,
            "identity_block": identity_block,
            "selected": selected_rows,
            "audit": audit_rows,
        })

    runtime_fingerprint = ref_contract.canonical_fingerprint({
        "schema_version": REFERENCE_RUNTIME_SCHEMA,
        "scenes_sha256": scenes_sha256,
        "bundle_id": sources["bundle_id"],
        "source_fingerprint": sources["source_fingerprint"],
        "model": str(model).upper(),
        "scenes": [
            {
                "index": row["index"],
                "fingerprint": row["fingerprint"],
                "identity_block": row["identity_block"],
            }
            for row in scene_rows
        ],
    })
    runtime = {
        "schema_version": REFERENCE_RUNTIME_SCHEMA,
        "scenes_sha256": scenes_sha256,
        "bundle_id": sources["bundle_id"],
        "source_fingerprint": sources["source_fingerprint"],
        "reference_fingerprint": runtime_fingerprint,
        "model": str(model).upper(),
        "capacity": capacity,
        "source_count": int(sources.get("source_count") or 0),
        "packed_slots": sum(row["used"] for row in scene_rows),
        "scenes": scene_rows,
    }
    runtime_path = os.path.join(runtime_dir, REFERENCE_RUNTIME_FILE)
    if persist:
        _atomic_write_json(runtime_path, runtime)
    runtime_state = (
        "контракт сохранён" if persist
        else "контракт подготовлен для атомарного commit"
    )
    print_user(
        f"📎 Референсы: {runtime['source_count']} исходников → "
        f"{runtime['packed_slots']} scene-slot, capacity {capacity}/сцену; {runtime_state}",
        "INFO",
    )
    return runtime


def _load_reference_runtime(project_dir: str) -> dict:
    runtime_path = os.path.join(
        _confined_project_directory(project_dir, GOOGLER_IMAGES_DIR),
        REFERENCE_RUNTIME_FILE,
    )
    runtime = _load_json_object(runtime_path)
    runtime_keys = {
        "schema_version", "bundle_id", "source_fingerprint", "reference_fingerprint",
        "scenes_sha256", "model", "capacity", "source_count", "packed_slots", "scenes",
    }
    if (
        runtime is None
        or set(runtime) != runtime_keys
        or runtime.get("schema_version") != REFERENCE_RUNTIME_SCHEMA
    ):
        raise ref_contract.ReferenceContractError(
            "reference runtime is missing; run the image pipeline preflight first"
        )
    for key in (
        "bundle_id", "source_fingerprint", "reference_fingerprint", "scenes_sha256"
    ):
        if not isinstance(runtime.get(key), str) or not _REFERENCE_SHA_RE.fullmatch(runtime[key]):
            raise ref_contract.ReferenceContractError(
                f"reference runtime {key} is malformed"
            )
    if str(runtime.get("model") or "").upper() != str(GOOGLER_MODEL).upper():
        raise ref_contract.ReferenceContractError(
            "reference runtime model differs from the selected regeneration model"
        )
    scenes = runtime.get("scenes")
    if not isinstance(scenes, list):
        raise ref_contract.ReferenceContractError("reference runtime scenes are malformed")
    registered_capacity = ref_contract.reference_capacity(GOOGLER_MODEL)
    if int(runtime.get("capacity", -1)) != registered_capacity:
        raise ref_contract.ReferenceContractError("reference runtime capacity mismatch")
    if (
        not isinstance(runtime.get("source_count"), int)
        or runtime["source_count"] < 0
        or not isinstance(runtime.get("packed_slots"), int)
        or runtime["packed_slots"] < 0
    ):
        raise ref_contract.ReferenceContractError("reference runtime counters are malformed")

    scene_keys = {
        "index", "bundle_id", "source_fingerprint", "fingerprint", "used",
        "capacity", "identity_block", "selected", "audit",
    }
    selected_row_keys = {
        "slot_id", "kind", "entity_id", "canonical_name", "artifact",
    }
    audit_row_keys = {
        "slot_id", "kind", "status", "artifact_sha256", "reason", "superseded_by",
    }
    total_selected = 0
    for index, scene in enumerate(scenes):
        if (
            not isinstance(scene, dict)
            or set(scene) != scene_keys
            or scene.get("index") != index
        ):
            raise ref_contract.ReferenceContractError("reference runtime scene index mismatch")
        if scene.get("bundle_id") != runtime.get("bundle_id"):
            raise ref_contract.ReferenceContractError("reference runtime scene bundle mismatch")
        if scene.get("source_fingerprint") != runtime.get("source_fingerprint"):
            raise ref_contract.ReferenceContractError("reference runtime source fingerprint mismatch")
        if int(scene.get("capacity", -1)) != registered_capacity:
            raise ref_contract.ReferenceContractError("reference runtime scene capacity mismatch")
        if not isinstance(scene.get("identity_block"), str):
            raise ref_contract.ReferenceContractError("reference runtime identity block is malformed")
        selected_rows = scene.get("selected")
        audit_rows = scene.get("audit")
        if not isinstance(selected_rows, list) or not isinstance(audit_rows, list):
            raise ref_contract.ReferenceContractError("reference runtime scene rows are malformed")
        if int(scene.get("used", -1)) != len(selected_rows):
            raise ref_contract.ReferenceContractError("reference runtime scene used count mismatch")
        selected_by_slot = {}
        for row in selected_rows:
            if not isinstance(row, dict) or set(row) != selected_row_keys:
                raise ref_contract.ReferenceContractError(
                    "reference runtime selected row is malformed"
                )
            slot_id = row.get("slot_id")
            kind = row.get("kind")
            if (
                not isinstance(slot_id, str) or not slot_id
                or slot_id in selected_by_slot
                or not isinstance(kind, str) or not kind
                or row.get("entity_id") is not None
                and not isinstance(row.get("entity_id"), str)
                or row.get("canonical_name") is not None
                and not isinstance(row.get("canonical_name"), str)
            ):
                raise ref_contract.ReferenceContractError(
                    "reference runtime selected metadata is malformed"
                )
            selected_by_slot[slot_id] = (
                row, _artifact_from_json(row.get("artifact"), project_dir)
            )

        primary_audits = []
        for row in audit_rows:
            if not isinstance(row, dict) or set(row) != audit_row_keys:
                raise ref_contract.ReferenceContractError(
                    "reference runtime audit row is malformed"
                )
            if (
                not isinstance(row.get("slot_id"), str) or not row["slot_id"]
                or not isinstance(row.get("kind"), str) or not row["kind"]
                or row.get("status") not in ref_contract.REFERENCE_AUDIT_STATUSES
                or row.get("artifact_sha256") is not None
                and not _REFERENCE_SHA_RE.fullmatch(str(row.get("artifact_sha256")))
                or not isinstance(row.get("reason"), str)
                or row.get("superseded_by") is not None
                and not isinstance(row.get("superseded_by"), str)
            ):
                raise ref_contract.ReferenceContractError(
                    "reference runtime audit metadata is malformed"
                )
            is_source_row = bool(
                row.get("status") == "superseded"
                and row.get("superseded_by")
                and row["slot_id"].startswith(f"{row['superseded_by']}:source:")
                and row["kind"].endswith("_source")
            )
            if not is_source_row:
                primary_audits.append(row)

        logical_slots = []
        for audit in primary_audits:
            status = audit["status"]
            slot_id = audit["slot_id"]
            selected_value = selected_by_slot.get(slot_id)
            if status == "selected":
                if selected_value is None:
                    raise ref_contract.ReferenceContractError(
                        "reference runtime selected audit has no selected row"
                    )
                selected_row, artifact = selected_value
                if (
                    selected_row["kind"] != audit["kind"]
                    or artifact.sha256 != audit["artifact_sha256"]
                ):
                    raise ref_contract.ReferenceContractError(
                        "reference runtime selected audit differs from artifact"
                    )
                logical_slots.append(ref_contract.LogicalReferenceSlot(
                    slot_id=slot_id, kind=audit["kind"], artifact=artifact,
                ))
            elif status == "not_applicable":
                artifact_sha = audit.get("artifact_sha256")
                placeholder = None
                if artifact_sha:
                    placeholder = ref_contract.ReferenceArtifact(
                        path=f"runtime-audit:{slot_id}", sha256=artifact_sha,
                        width=1, height=1, source_hashes=(artifact_sha,),
                        kind="preflight",
                        fingerprint=ref_contract.canonical_fingerprint({
                            "schema_version": 1, "runtime_audit": slot_id,
                            "sha256": artifact_sha,
                        }),
                    )
                logical_slots.append(ref_contract.LogicalReferenceSlot(
                    slot_id=slot_id, kind=audit["kind"], artifact=placeholder,
                    applicable=False,
                ))
            elif status == "superseded":
                logical_slots.append(ref_contract.LogicalReferenceSlot(
                    slot_id=slot_id, kind=audit["kind"], artifact=None,
                    superseded_by=audit.get("superseded_by"),
                ))
            else:
                raise ref_contract.ReferenceContractError(
                    "blocking reference audit cannot be persisted as runnable runtime"
                )

        repacked = ref_contract.pack_scene_references(logical_slots, GOOGLER_MODEL)
        repacked_selected = [
            (slot.slot_id, slot.kind, slot.artifact.sha256)
            for slot in repacked.selected
        ]
        persisted_selected = [
            (row["slot_id"], row["kind"], row["artifact"]["sha256"])
            for row in selected_rows
        ]
        if repacked_selected != persisted_selected:
            raise ref_contract.ReferenceContractError(
                "reference runtime selected slots do not repack exactly"
            )
        expected_audit = [asdict(row) for row in repacked.audit]
        for row in expected_audit:
            if (
                row["slot_id"].startswith("story:")
                and row["status"] == "not_applicable"
                and row["artifact_sha256"] is None
            ):
                row["reason"] = (
                    "model capacity is zero; named story identity is enforced by text only"
                )
        if expected_audit != audit_rows:
            raise ref_contract.ReferenceContractError(
                "reference runtime audit does not repack exactly"
            )
        expected_scene_fingerprint = _scene_reference_fingerprint(
            repacked.fingerprint, selected_rows, scene.get("identity_block") or ""
        )
        if scene.get("fingerprint") != expected_scene_fingerprint:
            raise ref_contract.ReferenceContractError(
                "reference runtime scene fingerprint mismatch"
            )
        total_selected += len(selected_rows)
    if runtime["packed_slots"] != total_selected:
        raise ref_contract.ReferenceContractError("reference runtime packed_slots mismatch")
    expected_fingerprint = ref_contract.canonical_fingerprint({
        "schema_version": REFERENCE_RUNTIME_SCHEMA,
        "scenes_sha256": runtime.get("scenes_sha256"),
        "bundle_id": runtime.get("bundle_id"),
        "source_fingerprint": runtime.get("source_fingerprint"),
        "model": str(runtime.get("model") or "").upper(),
        "scenes": [
            {
                "index": row.get("index"),
                "fingerprint": row.get("fingerprint"),
                "identity_block": row.get("identity_block") or "",
            }
            for row in scenes
        ],
    })
    if runtime.get("reference_fingerprint") != expected_fingerprint:
        raise ref_contract.ReferenceContractError("reference runtime fingerprint mismatch")
    try:
        _published_scenes, published_meta, _published_style = _load_regen_plan(
            project_dir, allow_legacy_no_refs=False
        )
    except ScenePlanError as exc:
        raise ref_contract.ReferenceContractError(
            "reference runtime has no coherent published scene plan"
        ) from exc
    if runtime.get("scenes_sha256") != published_meta.get("scenes_sha256"):
        raise ref_contract.ReferenceContractError(
            "reference runtime belongs to a different published scene plan"
        )
    return runtime


def _frame_generation_fingerprint(scene: dict, style: str, aspect_ratio: str,
                                  seed: Optional[int], model: str,
                                  reference_bundle: dict) -> str:
    """Content-only cache key for what this exact provider call consumes.

    Project bundle ids and non-applicable audit rows remain provenance, but are
    deliberately excluded: changing an unused hero must not invalidate a frame
    that never sent that hero to the provider.
    """
    selected_contract = []
    for row in (reference_bundle.get("selected") or []):
        artifact = row.get("artifact") or {}
        selected_contract.append({
            "slot_id": row.get("slot_id"),
            "kind": row.get("kind"),
            "entity_id": row.get("entity_id"),
            "canonical_name": row.get("canonical_name"),
            "artifact_sha256": artifact.get("sha256"),
            "artifact_fingerprint": artifact.get("fingerprint"),
        })
    payload = {
        "schema_version": 1,
        "prompt_contract": "image-reference-prompt-v2-selected-slots",
        "safety_retry_contract": IMAGE_PROMPT_CONTRACT_VERSION,
        "model": str(model).upper(),
        "scene_index": reference_bundle.get("index"),
        "style": style,
        "aspect_ratio": aspect_ratio,
        "seed": seed,
        "scene": {
            "visual_prompt": scene.get("visual_prompt") or scene.get("visual_description") or "",
            "camera_angle": scene.get("camera_angle") or "Wide Shot",
            "subject_type": scene.get("subject_type") or scene.get("subject_focus") or "",
            "visible_text": scene.get("visible_text"),
        },
        "selected_references": selected_contract,
        "identity_block": reference_bundle.get("identity_block") or "",
    }
    exact_plan = _resolve_preflight_exact_safety(
        scene.get("visual_prompt") or scene.get("visual_description") or "",
        identity_block=reference_bundle.get("identity_block") or "",
        ref_hints=("selected-reference",) if selected_contract else (),
        immutable_scene=bool(scene.get("immutable_identity_prompt", False)),
    ).fingerprint_plan(model=model, visible_text=scene.get("visible_text"))
    # Preserve every normal frame's historical fingerprint byte-for-byte. Only
    # an actually matched exact recovery records its conditional prompt plan.
    if exact_plan is not None:
        payload["exact_preflight_plan"] = exact_plan
    return ref_contract.canonical_fingerprint(payload)


def _receipt_reference_usage(reference_bundle: dict) -> List[dict]:
    return [
        {
            "slot_id": row.get("slot_id"),
            "kind": row.get("kind"),
            "entity_id": row.get("entity_id"),
            "canonical_name": row.get("canonical_name"),
            "artifact_sha256": (row.get("artifact") or {}).get("sha256"),
        }
        for row in (reference_bundle.get("selected") or [])
    ]


def _prepare_image_receipts(project_dir: str, runtime: dict, scenes: List[dict],
                            style: str, aspect_ratio: str,
                            seed: Optional[int], *,
                            output_names: Optional[List[str]] = None,
                            scene_plan_sha256: Optional[str] = None,
                            trust_legacy_cache: bool = False,
                            use_cache: bool = False,
                            persist: bool = True) -> Tuple[str, dict, List[str]]:
    """Mark stale/fresh frames before scheduling and atomically publish the audit."""
    expected_scenes_sha256 = (
        str(scene_plan_sha256)
        if scene_plan_sha256 is not None
        else hashlib.sha256(_json_bytes(scenes)).hexdigest()
    )
    if runtime.get("scenes_sha256") != expected_scenes_sha256:
        raise ref_contract.ReferenceContractError(
            "reference runtime belongs to a different scene plan"
        )
    output_dir = _confined_project_directory(project_dir, GOOGLER_IMAGES_DIR)
    path = os.path.join(output_dir, IMAGE_RECEIPTS_FILE)
    old = _load_json_object(path) or {}
    old_frames = (
        old.get("frames")
        if old.get("schema_version") == IMAGE_RECEIPTS_SCHEMA
        and isinstance(old.get("frames"), dict)
        else {}
    )
    frames = {}
    fingerprints = []
    runtime_scenes = runtime.get("scenes") or []
    if len(runtime_scenes) != len(scenes):
        raise ref_contract.ReferenceContractError(
            "reference runtime and scene plan have different lengths"
        )
    for index, (scene, bundle) in enumerate(zip(scenes, runtime_scenes)):
        key = f"{index + 1:03d}"
        generation_fingerprint = _frame_generation_fingerprint(
            scene, style, aspect_ratio, seed, GOOGLER_MODEL, bundle
        )
        fingerprints.append(generation_fingerprint)
        previous = old_frames.get(key) if isinstance(old_frames.get(key), dict) else {}
        output_name = (
            output_names[index] if output_names is not None else f"{key}.jpg"
        )
        output_path = os.path.join(output_dir, output_name)
        output_sha = _sha256_file(output_path) if os.path.isfile(output_path) else None
        completed = previous.get("completed_fingerprint")
        recorded_sha = previous.get("output_sha256")
        trusted_by_user = previous.get("trusted_by_user") is True
        legacy_trusted_at = previous.get("legacy_trusted_at")
        exact_previous_output = bool(
            output_sha and completed == generation_fingerprint
            and recorded_sha == output_sha
        )
        previous_status = previous.get("status")
        trusted_legacy = bool(
            previous_status == "legacy"
            and trusted_by_user
            and isinstance(legacy_trusted_at, int)
            and legacy_trusted_at > 0
        )
        if exact_previous_output and previous_status == "fresh":
            status = "fresh"
            trusted_by_user = False
            legacy_trusted_at = None
        elif exact_previous_output and trusted_legacy:
            status = "legacy"
        elif exact_previous_output and previous_status == "legacy":
            # A schema-v2 row is not proof of user consent by itself.  This also
            # prevents older/malformed ``legacy`` rows from being promoted to
            # ``fresh`` merely because their hashes happen to match.
            if trust_legacy_cache:
                status = "legacy"
                trusted_by_user = True
                legacy_trusted_at = int(time.time())
            elif use_cache:
                raise ref_contract.ReferenceContractError(
                    "legacy image cache has no explicit trust provenance "
                    f"({key}); rerun without --use_cache to regenerate or pass "
                    "--trust_legacy_cache after explicit user confirmation"
                )
            else:
                status = "stale"
                completed = None
                trusted_by_user = False
                legacy_trusted_at = None
        elif exact_previous_output:
            # A stale/unknown row must never become fresh just from matching old
            # fields.  Only _mark_receipt_success may publish a fresh result.
            status = "stale"
            trusted_by_user = False
            legacy_trusted_at = None
        elif output_sha and not previous:
            recorded_sha = output_sha
            if trust_legacy_cache:
                status = "legacy"
                completed = generation_fingerprint
                trusted_by_user = True
                legacy_trusted_at = int(time.time())
            else:
                if use_cache:
                    raise ref_contract.ReferenceContractError(
                        "receipt-less legacy image cache is unverified "
                        f"({key}); rerun without --use_cache to regenerate or pass "
                        "--trust_legacy_cache after explicit user confirmation"
                    )
                status = "stale"
                completed = None
                trusted_by_user = False
                legacy_trusted_at = None
        else:
            status = "stale"
            trusted_by_user = False
            legacy_trusted_at = None
        frames[key] = {
            "generation_fingerprint": generation_fingerprint,
            "completed_fingerprint": completed,
            "reference_fingerprint": bundle.get("fingerprint"),
            "status": status,
            "used": int(bundle.get("used") or 0),
            "capacity": int(bundle.get("capacity") or runtime.get("capacity") or 0),
            "references": _receipt_reference_usage(bundle),
            "output_sha256": recorded_sha,
            "trusted_by_user": trusted_by_user,
            "legacy_trusted_at": legacy_trusted_at,
        }
    receipts = {
        "schema_version": IMAGE_RECEIPTS_SCHEMA,
        "bundle_id": runtime.get("bundle_id"),
        "reference_fingerprint": runtime.get("reference_fingerprint"),
        "model": str(GOOGLER_MODEL).upper(),
        "frames": frames,
    }
    if persist:
        _atomic_write_json(path, receipts)
    return path, receipts, fingerprints


def _receipt_cache_hit(receipts: dict, frame_key: str, output_path: str) -> bool:
    if receipts.get("schema_version") != IMAGE_RECEIPTS_SCHEMA:
        return False
    frame = (receipts.get("frames") or {}).get(frame_key) or {}
    if frame.get("status") not in ("fresh", "legacy") or not os.path.isfile(output_path):
        return False
    if frame.get("status") == "legacy" and not (
        frame.get("trusted_by_user") is True
        and isinstance(frame.get("legacy_trusted_at"), int)
        and frame["legacy_trusted_at"] > 0
    ):
        return False
    try:
        return _sha256_file(output_path) == frame.get("output_sha256")
    except OSError:
        return False
def _invalidate_receipts_for_regeneration(receipt_path: str, receipts: dict,
                                          frame_keys) -> None:
    for frame_key in frame_keys:
        frame = (receipts.get("frames") or {}).get(frame_key)
        if isinstance(frame, dict):
            frame["status"] = "stale"
            frame["trusted_by_user"] = False
            frame["legacy_trusted_at"] = None
    _atomic_write_json(receipt_path, receipts)


def _mark_receipt_success(receipt_path: str, receipts: dict, frame_key: str,
                          output_path: str) -> None:
    with _IMAGE_RECEIPT_WRITE_LOCK:
        frame = receipts["frames"][frame_key]
        output_sha = _sha256_file(output_path)
        frame["completed_fingerprint"] = frame["generation_fingerprint"]
        frame["output_sha256"] = output_sha
        frame["status"] = "fresh"
        frame["trusted_by_user"] = False
        frame["legacy_trusted_at"] = None
        _atomic_write_json(receipt_path, receipts)


def _canonical_numeric_image_name(filename: str) -> bool:
    """Return True only for worker-owned sequence names: 001.jpg ... 1000.webp."""
    match = _CANONICAL_NUMERIC_IMAGE_RE.fullmatch(str(filename or ""))
    if not match:
        return False
    number_text = match.group("number")
    number = int(number_text)
    return number >= 1 and number_text == f"{number:03d}"


def _fsync_directory_path(path: str) -> None:
    """Make directory-entry moves durable where the platform supports it."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _regular_file_bytes(path: str, *, confined_to: str) -> bytes:
    """Read one non-link regular file whose direct path is confined to a directory."""
    root = os.path.abspath(confined_to)
    candidate = os.path.abspath(path)
    try:
        if os.path.commonpath((root, candidate)) != root:
            raise ref_contract.ReferenceContractError(
                "image-set artifact escapes its confined directory"
            )
    except ValueError as exc:
        raise ref_contract.ReferenceContractError(
            "image-set artifact escapes its confined directory"
        ) from exc
    if (
        not os.path.lexists(candidate)
        or _path_is_link_or_reparse(candidate)
    ):
        raise ref_contract.ReferenceContractError(
            f"image-set artifact is missing or linked: {os.path.basename(candidate)}"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(candidate, flags)
    except OSError as exc:
        raise ref_contract.ReferenceContractError(
            f"image-set artifact is not a readable regular file: {os.path.basename(candidate)}"
        ) from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ref_contract.ReferenceContractError(
                f"image-set artifact is not a regular file: {os.path.basename(candidate)}"
            )
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        current = os.lstat(candidate)
        if (
            not stat.S_ISREG(current.st_mode)
            or (opened.st_ino and current.st_ino and opened.st_ino != current.st_ino)
            or (opened.st_dev and current.st_dev and opened.st_dev != current.st_dev)
        ):
            raise ref_contract.ReferenceContractError(
                f"image-set artifact changed during validation: {os.path.basename(candidate)}"
            )
        return b"".join(chunks)
    finally:
        os.close(fd)


def _regular_file_sha256(path: str, *, confined_to: str) -> str:
    return hashlib.sha256(
        _regular_file_bytes(path, confined_to=confined_to)
    ).hexdigest()


def _expected_image_output_contract(expected_output_names: List[str]) -> List[Tuple[str, str]]:
    if not isinstance(expected_output_names, list) or not expected_output_names:
        raise ref_contract.ReferenceContractError(
            "authoritative image set must contain at least one expected frame"
        )
    contract = []
    for index, raw_name in enumerate(expected_output_names, 1):
        name = str(raw_name or "")
        expected_name = f"{index:03d}.jpg"
        if name != expected_name or not _canonical_numeric_image_name(name):
            raise ref_contract.ReferenceContractError(
                f"authoritative image name mismatch: expected {expected_name}, got {name!r}"
            )
        contract.append((f"{index:03d}", name))
    return contract


def _validate_authoritative_image_set(output_dir: str, receipts: dict,
                                      expected_output_names: List[str],
                                      skip_frames=()) -> dict:
    """Strict success gate for a complete, hash-bound schema-v2 image set.

    The returned mapping is keyed by the exact output filename and contains the
    verified receipt SHA.  Extra receipt rows are deliberately ignored here and
    are removed only by the coherent receipt republish in reconciliation.
    """
    output_root = os.path.abspath(output_dir)
    if (
        not os.path.isdir(output_root)
        or _path_is_link_or_reparse(output_root)
        or os.path.normcase(os.path.realpath(output_root)) != os.path.normcase(output_root)
    ):
        raise ref_contract.ReferenceContractError(
            "authoritative image directory is not a confined real directory"
        )
    expected = _expected_image_output_contract(expected_output_names)
    if not isinstance(receipts, dict) or receipts.get("schema_version") != IMAGE_RECEIPTS_SCHEMA:
        raise ref_contract.ReferenceContractError(
            "authoritative image receipt must use schema_version=2"
        )
    frames = receipts.get("frames")
    if not isinstance(frames, dict):
        raise ref_contract.ReferenceContractError(
            "authoritative image receipt frames must be an object"
        )

    verified = {}
    skip = set(skip_frames or ())
    for frame_key, filename in expected:
        if frame_key in skip:
            # Сцена отдана соседнему кадру (image_gaps.json): файла нет, квитанции нет —
            # проверять нечего. Есть ли устаревший файл — решает уборка в reconcile.
            continue
        frame = frames.get(frame_key)
        if not isinstance(frame, dict):
            raise ref_contract.ReferenceContractError(
                f"authoritative image receipt is missing frame {frame_key}"
            )
        generation = frame.get("generation_fingerprint")
        completed = frame.get("completed_fingerprint")
        if (
            not isinstance(generation, str)
            or not _REFERENCE_SHA_RE.fullmatch(generation)
            or completed != generation
        ):
            raise ref_contract.ReferenceContractError(
                f"authoritative image receipt is not exact for frame {frame_key}"
            )
        status = frame.get("status")
        if status == "fresh":
            if (
                frame.get("trusted_by_user") is not False
                or frame.get("legacy_trusted_at") is not None
            ):
                raise ref_contract.ReferenceContractError(
                    f"fresh frame {frame_key} has legacy trust markers"
                )
        elif status == "legacy":
            trusted_at = frame.get("legacy_trusted_at")
            if not (
                frame.get("trusted_by_user") is True
                and isinstance(trusted_at, int)
                and not isinstance(trusted_at, bool)
                and trusted_at > 0
            ):
                raise ref_contract.ReferenceContractError(
                    f"legacy frame {frame_key} has no explicit user trust"
                )
        else:
            raise ref_contract.ReferenceContractError(
                f"authoritative image receipt is nonfresh for frame {frame_key}"
            )
        recorded_sha = frame.get("output_sha256")
        if not isinstance(recorded_sha, str) or not _REFERENCE_SHA_RE.fullmatch(recorded_sha):
            raise ref_contract.ReferenceContractError(
                f"authoritative image receipt has invalid SHA for frame {frame_key}"
            )
        output_path = os.path.join(output_root, filename)
        actual_sha = _regular_file_sha256(output_path, confined_to=output_root)
        if actual_sha != recorded_sha:
            raise ref_contract.ReferenceContractError(
                f"authoritative image hash mismatch for frame {frame_key}"
            )
        verified[filename] = recorded_sha
    return verified


def _authoritative_receipt_projection(receipts: dict,
                                      expected_output_names: List[str],
                                      skip_frames=()) -> dict:
    expected = _expected_image_output_contract(expected_output_names)
    skip = set(skip_frames or ())
    projected = dict(receipts)
    projected["frames"] = {
        frame_key: dict(receipts["frames"][frame_key])
        for frame_key, _filename in expected
        if frame_key not in skip
    }
    return projected


def _quarantine_transaction_manifest(tx_dir: str) -> str:
    return os.path.join(tx_dir, IMAGE_SET_QUARANTINE_MANIFEST)


def _load_quarantine_manifest(tx_dir: str) -> dict:
    manifest_path = _quarantine_transaction_manifest(tx_dir)
    raw = _regular_file_bytes(manifest_path, confined_to=tx_dir)
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ref_contract.ReferenceContractError(
            "image-set quarantine manifest is damaged"
        ) from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != IMAGE_SET_QUARANTINE_SCHEMA
        or manifest.get("transaction_id") != os.path.basename(tx_dir)
        or manifest.get("status") not in ("prepared", "committed", "rolled_back")
        or not isinstance(manifest.get("files"), list)
        or not isinstance(manifest.get("expected_files"), list)
        or not isinstance(manifest.get("receipt_backup_present"), bool)
        or not isinstance(manifest.get("receipt_before_sha256"), str)
        or not _REFERENCE_SHA_RE.fullmatch(manifest["receipt_before_sha256"])
    ):
        raise ref_contract.ReferenceContractError(
            "image-set quarantine manifest violates schema"
        )
    expected_seen = set()
    for row in manifest["expected_files"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"name", "sha256"}
            or not _canonical_numeric_image_name(row.get("name"))
            or not str(row["name"]).endswith(".jpg")
            or row["name"] in expected_seen
            or not isinstance(row.get("sha256"), str)
            or not _REFERENCE_SHA_RE.fullmatch(row["sha256"])
        ):
            raise ref_contract.ReferenceContractError(
                "image-set quarantine expected-file row is unsafe"
            )
        expected_seen.add(row["name"])
    seen = set()
    for row in manifest["files"]:
        if not isinstance(row, dict) or set(row) != {
            "source_name", "quarantine_name", "sha256", "size"
        }:
            raise ref_contract.ReferenceContractError(
                "image-set quarantine file row violates schema"
            )
        source_name = row.get("source_name")
        if (
            source_name != row.get("quarantine_name")
            or not _canonical_numeric_image_name(source_name)
            or source_name in seen
            or not isinstance(row.get("sha256"), str)
            or not _REFERENCE_SHA_RE.fullmatch(row["sha256"])
            or not isinstance(row.get("size"), int)
            or isinstance(row.get("size"), bool)
            or row["size"] < 0
        ):
            raise ref_contract.ReferenceContractError(
                "image-set quarantine file row is unsafe"
            )
        seen.add(source_name)
    return manifest


def _atomic_quarantine_move(source: str, destination: str) -> None:
    os.replace(source, destination)


def _rollback_image_set_transaction(output_dir: str, tx_dir: str,
                                    manifest: dict) -> None:
    files_dir = os.path.join(tx_dir, "files")
    if (
        _path_is_link_or_reparse(tx_dir)
        or _path_is_link_or_reparse(files_dir)
        or not os.path.isdir(files_dir)
        or os.path.normcase(os.path.realpath(files_dir)) != os.path.normcase(files_dir)
    ):
        raise ref_contract.ReferenceContractError(
            "image-set quarantine rollback directory is unsafe"
        )
    for row in reversed(manifest["files"]):
        source = os.path.join(output_dir, row["source_name"])
        quarantined = os.path.join(files_dir, row["quarantine_name"])
        source_exists = os.path.lexists(source)
        quarantined_exists = os.path.lexists(quarantined)
        if source_exists and _path_is_link_or_reparse(source):
            raise ref_contract.ReferenceContractError(
                f"cannot roll back linked output {row['source_name']}"
            )
        if quarantined_exists and _path_is_link_or_reparse(quarantined):
            raise ref_contract.ReferenceContractError(
                f"cannot roll back linked quarantine {row['quarantine_name']}"
            )
        if quarantined_exists:
            if source_exists:
                raise ref_contract.ReferenceContractError(
                    f"cannot roll back quarantine over existing {row['source_name']}"
                )
            # Restore the exact bytes that are present even if an external actor
            # changed them after the move.  Leaving the canonical source absent
            # would be a more destructive failure than preserving changed bytes.
            _atomic_quarantine_move(quarantined, source)
        elif not source_exists:
            raise ref_contract.ReferenceContractError(
                f"quarantine rollback lost {row['source_name']}"
            )
    _fsync_directory_path(output_dir)
    _fsync_directory_path(files_dir)

    if manifest.get("receipt_backup_present") is True:
        backup_path = os.path.join(tx_dir, IMAGE_SET_RECEIPT_BACKUP)
        backup = _regular_file_bytes(backup_path, confined_to=tx_dir)
        if hashlib.sha256(backup).hexdigest() != manifest.get("receipt_before_sha256"):
            raise ref_contract.ReferenceContractError(
                "image-set receipt backup hash mismatch during rollback"
            )
        _atomic_write_bytes(os.path.join(output_dir, IMAGE_RECEIPTS_FILE), backup)
    manifest["status"] = "rolled_back"
    manifest["rolled_back_at"] = int(time.time())
    _atomic_write_json(_quarantine_transaction_manifest(tx_dir), manifest)


def _recover_incomplete_image_set_transactions(project_dir: str,
                                               output_dir: str) -> None:
    quarantine_root = _confined_project_directory(
        project_dir, GOOGLER_IMAGES_DIR, IMAGE_SET_QUARANTINE_DIR
    )
    with os.scandir(quarantine_root) as entries:
        transaction_names = sorted(
            entry.name for entry in entries
            if _IMAGE_SET_TRANSACTION_RE.fullmatch(entry.name)
        )
    for transaction_id in transaction_names:
        tx_dir = os.path.join(quarantine_root, transaction_id)
        if _path_is_link_or_reparse(tx_dir) or not os.path.isdir(tx_dir):
            raise ref_contract.ReferenceContractError(
                "image-set quarantine transaction directory is unsafe"
            )
        manifest_path = _quarantine_transaction_manifest(tx_dir)
        if not os.path.lexists(manifest_path):
            try:
                contents = os.listdir(tx_dir)
                files_dir = os.path.join(tx_dir, "files")
                if not contents:
                    os.rmdir(tx_dir)
                    continue
                if (
                    contents == ["files"]
                    and not _path_is_link_or_reparse(files_dir)
                    and os.path.isdir(files_dir)
                    and not os.listdir(files_dir)
                ):
                    os.rmdir(files_dir)
                    os.rmdir(tx_dir)
                    continue
            except OSError:
                pass
            raise ref_contract.ReferenceContractError(
                f"orphan image-set quarantine transaction: {transaction_id}"
            )
        manifest = _load_quarantine_manifest(tx_dir)
        if manifest["status"] == "prepared":
            _rollback_image_set_transaction(output_dir, tx_dir, manifest)


def _stale_numeric_image_rows(output_dir: str,
                              expected_output_names: List[str]) -> List[dict]:
    expected = set(expected_output_names)
    rows = []
    with os.scandir(output_dir) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            if entry.name in expected or not _canonical_numeric_image_name(entry.name):
                continue
            path = os.path.join(output_dir, entry.name)
            if _path_is_link_or_reparse(path) or not entry.is_file(follow_symlinks=False):
                raise ref_contract.ReferenceContractError(
                    f"stale canonical output is not a regular file: {entry.name}"
                )
            payload = _regular_file_bytes(path, confined_to=output_dir)
            rows.append({
                "source_name": entry.name,
                "quarantine_name": entry.name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            })
    return rows


def _reconcile_authoritative_image_set(project_dir: str, receipt_path: str,
                                       receipts: dict,
                                       expected_output_names: List[str],
                                       skip_frames=()) -> dict:
    """Publish one authoritative output set and quarantine older numbered files.

    No file is moved until all expected rows are exact fresh/trusted-legacy cache
    hits.  A durable prepared manifest makes interrupted multi-file moves
    recoverable; any synchronous failure restores both files and the prior receipt.
    """
    output_dir = _confined_project_directory(project_dir, GOOGLER_IMAGES_DIR)
    exact_receipt_path = os.path.join(output_dir, IMAGE_RECEIPTS_FILE)
    if os.path.abspath(receipt_path) != os.path.abspath(exact_receipt_path):
        raise ref_contract.ReferenceContractError(
            "image-set reconciliation requires the canonical receipt path"
        )
    _recover_incomplete_image_set_transactions(project_dir, output_dir)
    skip = set(skip_frames or ())
    verified = _validate_authoritative_image_set(
        output_dir, receipts, expected_output_names, skip
    )
    projected = _authoritative_receipt_projection(receipts, expected_output_names, skip)
    receipt_before = _regular_file_bytes(exact_receipt_path, confined_to=output_dir)
    try:
        receipt_on_disk = json.loads(receipt_before.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ref_contract.ReferenceContractError(
            "canonical image receipt is damaged before reconciliation"
        ) from exc
    if receipt_on_disk != receipts:
        raise ref_contract.ReferenceContractError(
            "canonical image receipt changed before reconciliation"
        )

    # Файл пропущенной сцены, если он есть, — устаревший (квитанции у него нет): в карантин,
    # иначе рендер показал бы его вместо соседнего кадра.
    stale_rows = _stale_numeric_image_rows(
        output_dir, [n for n in expected_output_names if n[:-4] not in skip]
    )
    if not stale_rows:
        try:
            _atomic_write_json(exact_receipt_path, projected)
        except OSError as exc:
            raise ref_contract.ReferenceContractError(
                "failed to republish authoritative image receipt"
            ) from exc
        receipts.clear()
        receipts.update(projected)
        return {"quarantined": 0, "transaction_id": None, "verified": verified}

    quarantine_root = _confined_project_directory(
        project_dir, GOOGLER_IMAGES_DIR, IMAGE_SET_QUARANTINE_DIR
    )
    transaction_id = f"txn-{time.time_ns():020d}-{os.urandom(8).hex()}"
    if not _IMAGE_SET_TRANSACTION_RE.fullmatch(transaction_id):
        raise ref_contract.ReferenceContractError(
            "could not allocate a safe image-set transaction id"
        )
    tx_dir = _confined_project_directory(
        project_dir, GOOGLER_IMAGES_DIR, IMAGE_SET_QUARANTINE_DIR, transaction_id
    )
    files_dir = _confined_project_directory(
        project_dir, GOOGLER_IMAGES_DIR, IMAGE_SET_QUARANTINE_DIR,
        transaction_id, "files",
    )
    _fsync_directory_path(quarantine_root)
    _fsync_directory_path(tx_dir)
    manifest = {
        "schema_version": IMAGE_SET_QUARANTINE_SCHEMA,
        "transaction_id": transaction_id,
        "status": "prepared",
        "created_at": int(time.time()),
        "reason": "outside-authoritative-image-set",
        "receipt_before_sha256": hashlib.sha256(receipt_before).hexdigest(),
        "receipt_backup_present": False,
        "expected_files": [
            {"name": name, "sha256": verified[name]}
            for name in expected_output_names
            if name in verified
        ],
        "files": stale_rows,
    }
    try:
        _atomic_write_json(_quarantine_transaction_manifest(tx_dir), manifest)
    except BaseException as exc:
        try:
            os.rmdir(files_dir)
            os.rmdir(tx_dir)
            _fsync_directory_path(quarantine_root)
        except OSError:
            pass
        raise ref_contract.ReferenceContractError(
            "failed to prepare image-set quarantine audit"
        ) from exc
    try:
        _atomic_write_bytes(
            os.path.join(tx_dir, IMAGE_SET_RECEIPT_BACKUP), receipt_before
        )
        manifest["receipt_backup_present"] = True
        _atomic_write_json(_quarantine_transaction_manifest(tx_dir), manifest)
        for row in stale_rows:
            source = os.path.join(output_dir, row["source_name"])
            destination = os.path.join(files_dir, row["quarantine_name"])
            if os.path.lexists(destination):
                raise ref_contract.ReferenceContractError(
                    f"quarantine destination already exists: {row['quarantine_name']}"
                )
            source_payload = _regular_file_bytes(source, confined_to=output_dir)
            if (
                len(source_payload) != row["size"]
                or hashlib.sha256(source_payload).hexdigest() != row["sha256"]
            ):
                raise ref_contract.ReferenceContractError(
                    f"stale output changed before quarantine: {row['source_name']}"
                )
            _atomic_quarantine_move(source, destination)
            if _regular_file_sha256(destination, confined_to=files_dir) != row["sha256"]:
                raise ref_contract.ReferenceContractError(
                    f"quarantined output hash mismatch: {row['quarantine_name']}"
                )
        _fsync_directory_path(output_dir)
        _fsync_directory_path(files_dir)
        _atomic_write_json(exact_receipt_path, projected)
        manifest["status"] = "committed"
        manifest["committed_at"] = int(time.time())
        _atomic_write_json(_quarantine_transaction_manifest(tx_dir), manifest)
        _fsync_directory_path(quarantine_root)
    except BaseException as exc:
        try:
            _rollback_image_set_transaction(output_dir, tx_dir, manifest)
        except BaseException as rollback_exc:
            raise ref_contract.ReferenceContractError(
                "image-set reconciliation failed and automatic rollback also failed; "
                f"transaction {transaction_id}: {rollback_exc}"
            ) from exc
        if isinstance(exc, ref_contract.ReferenceContractError):
            raise
        raise ref_contract.ReferenceContractError(
            f"image-set reconciliation failed; transaction {transaction_id} was rolled back"
        ) from exc

    receipts.clear()
    receipts.update(projected)
    return {
        "quarantined": len(stale_rows),
        "transaction_id": transaction_id,
        "verified": verified,
    }


# 16.09.2026: размеры сняты под "gpt-image-2" и НЕ перепроверены под GPT_IMAGE_MODEL
# ("gpt-image-2.5-sunburst") — поддержку размеров новой моделью нечем проверить без
# платного вызова. Не трогать без реального теста.
_GPT_IMAGE2_EXACT_SIZES = {
    "landscape": ("1536x864", (1536, 864)),
    "portrait": ("864x1536", (864, 1536)),
    "square": ("1024x1024", (1024, 1024)),
}


def _gpt_image2_size(aspect_ratio: str) -> Tuple[str, Tuple[int, int]]:
    value = str(aspect_ratio or "landscape")
    if value in (
        "portrait", "IMAGE_ASPECT_RATIO_PORTRAIT",
        "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
    ):
        return _GPT_IMAGE2_EXACT_SIZES["portrait"]
    if value in ("square", "IMAGE_ASPECT_RATIO_SQUARE"):
        return _GPT_IMAGE2_EXACT_SIZES["square"]
    return _GPT_IMAGE2_EXACT_SIZES["landscape"]


def _base64_image_size(data_uri: str) -> Tuple[int, int]:
    """Decode provider output before accepting a paid image as successful."""
    encoded = str(data_uri or "").split(",", 1)[-1]
    decoded = base64.b64decode(encoded)
    try:
        from PIL import Image
        import io as _io
        with Image.open(_io.BytesIO(decoded)) as image:
            image.load()
            return tuple(image.size)
    except Exception as exc:
        raise ValueError(f"ответ не является читаемым изображением: {exc}") from exc


# ============================================================================
# Журнал платных вызовов (03.09.2026)
# ============================================================================
# Порядок обязателен и держится именно здесь, на уровне кадра (номер сцены + промпт
# известны только тут, в HTTP-обёртке их уже нет):
#   1) reserve()  — ДО первого обращения к провайдеру: строка появляется раньше денег;
#   2) ordered()  — немедленно, как только известен id заказа, ДО ожидания результата;
#   3) delivered()— когда файл реально записан на диск.
# Между (1) и (2) и происходит потеря денег при падении/таймауте, ради этого окна всё и
# делается. Журнал не настроен → все функции ниже возвращают «делай как раньше».

_JOURNAL_FRAME = threading.local()   # состояние строки текущего кадра (поток = кадр)
_JOURNAL_SINGLETON = None
_JOURNAL_SINGLETON_LOCK = threading.Lock()


class _JournalOff:
    """Решение «журнала нет» — байт-в-байт прежнее поведение движка."""
    action = "off"
    call_id = 0
    remote_order_id = ""
    result_path = ""
    attempts = 0
    error = ""
    may_call = True


_JOURNAL_OFF = _JournalOff()


def _journal():
    global _JOURNAL_SINGLETON
    if CallJournal is None:
        return None
    if _JOURNAL_SINGLETON is None:
        with _JOURNAL_SINGLETON_LOCK:
            if _JOURNAL_SINGLETON is None:
                _JOURNAL_SINGLETON = CallJournal(log=print_user)
    return _JOURNAL_SINGLETON


def _journal_provider(model: Optional[str] = None) -> str:
    """Кто именно берёт деньги за этот кадр (цена и лимиты у них разные)."""
    model = model or GOOGLER_MODEL
    if model in IMAGE_MODEL_V6:
        return "recrafter"
    if model in VEO_BANANA_MODELS:
        return "veo"
    if model == "GPT_IMAGE2":
        return "openai"
    return "googler"


def _journal_reserve_frame(unit, prompt, seed, aspect_ratio, reference_bundle,
                           output_path, model: Optional[str] = None):
    """Заявка на платный вызов ДО обращения к провайдеру.

    В отпечаток входит ровно то, что определяет результат и цену: модель, финальный
    промпт (в нём уже собраны стиль, ракурс, identity-блок и контракт видимого текста),
    seed, соотношение сторон и sha256 отправляемых референсов. Ничего сверх этого:
    лишнее поле = тот же кадр будет оплачен заново.
    """
    _JOURNAL_FRAME.state = None
    journal = _journal()
    if journal is None or not journal.enabled:
        return _JOURNAL_OFF
    refs = []
    for row in ((reference_bundle or {}).get("selected") or []):
        artifact = row.get("artifact") or {}
        refs.append("{}:{}".format(
            row.get("slot_id") or "",
            artifact.get("sha256") or artifact.get("fingerprint") or "",
        ))
    # model — модель именно этого заказа: у кадра, ушедшего в откат после фильтра, она
    # другая, и строка журнала обязана называть ту, что реально взяла кредиты.
    model = model or GOOGLER_MODEL
    fingerprint = journal.fingerprint(
        model=model, prompt=prompt, seed=seed,
        aspect=aspect_ratio, refs=sorted(refs),
    )
    decision = journal.reserve(unit=str(unit), fingerprint=fingerprint, kind="images",
                               provider=_journal_provider(model), model=model)
    # skip — строка уже закрыта кабинетом; повторный отчёт о доставке ей не нужен.
    if decision.call_id and decision.action != "skip":
        _JOURNAL_FRAME.state = {
            "journal": journal, "call_id": decision.call_id, "order_id": "",
            "ordered": False, "closed": False, "path": output_path or "",
        }
    return decision


def _journal_note_order(order_id, post: bool = True) -> None:
    """Заказ создан. Пишем СРАЗУ, до ожидания результата — иначе журнал бесполезен.

    Кадр внутри своего цикла попыток может создать второй заказ; в строке должен
    остаться ПОСЛЕДНИЙ, потому что именно он в полёте, когда прогон падает.
    post=False — заказ создан прошлым прогоном (ветка verify): помечаем строку как
    оплаченную, но повторный отчёт не отправляем.
    """
    state = getattr(_JOURNAL_FRAME, "state", None)
    if not state or state["closed"] or not order_id:
        return
    if state["order_id"] == order_id:
        return
    state["order_id"] = order_id
    state["ordered"] = True
    if post:
        state["journal"].ordered(state["call_id"], order_id)


def _order_lost_reason(order_id, why: bool = False):
    """Один прямой вопрос провайдеру о заказе: «результата не будет никогда» — или "".

    Нужна не картинка, а причина: 404 (потерял) и status=failed (отклонил) отличаются от
    «ещё в работе» тем, что ждать больше нечего (`order_never_delivers`). С why=True
    отдаёт ещё и сырой ответ — для строки «кадр пропущен (…)».
    """
    probe, raw = None, ""
    try:
        probe = googler_request_v6(f"generations/{order_id}", method="GET")
        raw = str((probe or {}).get("_error") or "")
    except Exception as exc:                                       # noqa: BLE001
        raw = f"{type(exc).__name__}: {exc}"
    final = order_never_delivers(probe)
    return (final, raw) if why else final


def _journal_retire_lost(decision, final: str) -> None:
    """Закрыть строку журнала с потерянным заказом так, чтобы кадр можно было заказать
    снова: `lost=True` — оплачен ли он, решает провайдер, а результата не будет."""
    journal = _journal()
    if journal is not None:
        journal.failed(
            decision.call_id,
            f"{final} — заказ {decision.remote_order_id}; результата не будет, кадр "
            f"можно заказать заново",
            ordered=True, lost=True)


def _journal_close_frame(image_data, error: str = "") -> None:
    state = getattr(_JOURNAL_FRAME, "state", None)
    _JOURNAL_FRAME.state = None          # пул переиспользует потоки: чужой остаток недопустим
    if not state or state["closed"]:
        return
    state["closed"] = True
    path = state["path"]
    # «Готово» доказывается файлом. Кадр пишет его сам, а клип — нет: его сохраняет
    # батч уже после того, как поток завершился. Без этой записи успешный клип
    # закрывался бы как неудача, и следующий прогон оплатил бы его заново.
    # Байты уже в памяти, батч потом перезапишет тот же файл — цена одна лишняя
    # локальная запись на клип, и только когда журнал включён.
    if image_data and path and not os.path.exists(path):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            save_base64_to_file(image_data, path)
        except Exception as exc:                                   # noqa: BLE001
            print_debug(f"[журнал] результат не сохранён в {path}: {exc}")
    if image_data and (not path or os.path.exists(path)):
        state["journal"].delivered(state["call_id"], path)
    else:
        # Порядок «сначала настоящая причина, обёртка — запасной вариант». До правки
        # первым стоял `error`, и в колонку error журнала вызовов уезжала либо наша
        # обёртка, либо вовсе «кадр не получен»: ни кода, ни слов провайдера. По таким
        # строкам причину отказа установить нельзя, а именно за этим журнал и заведён.
        # Текст исключения (`error`) причину называет и остаётся первым — обёртку он
        # не содержит; безликие же варианты пропускают вперёд ответ провайдера.
        state["journal"].failed(
            state["call_id"],
            (error if not _is_wrapper_gen_error(error) else "")
            or _real_gen_reason() or error or "кадр не получен",
            ordered=state["ordered"],
        )


def _journal_clip_provider() -> str:
    """Кто берёт деньги за клип. Цена и лимиты у veo и v6 разные."""
    return "veo" if VIDEO_METHOD == "veo" else "googler"


def _journal_clip_poll():
    """Чем опросить уже созданный заказ клипа, не создавая новый."""
    return veo_poll_task if VIDEO_METHOD == "veo" else poll_v6_generation


def _file_sha256(path: str) -> str:
    """Отпечаток исходного кадра: именно он определяет результат i2v и его цену."""
    if not path or not os.path.exists(path):
        return ""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _journal_reserve_clip(unit, prompt, aspect_ratio, image_path, output_path):
    """Заявка на платный клип ДО обращения к провайдеру.

    В отпечаток входит то, что определяет результат и цену: бэкенд, промпт, соотношение
    сторон и sha256 исходного кадра (для t2v его нет). Единица — номер сцены, а не
    позиция в выборке: выборка меняется от запуска к запуску («первые 5 сцен» против
    «все»), и по позиции один клип оплачивался бы дважды под разными номерами.
    """
    _JOURNAL_FRAME.state = None
    journal = _journal()
    if journal is None or not journal.enabled:
        return _JOURNAL_OFF
    fingerprint = journal.fingerprint(
        method=VIDEO_METHOD, prompt=prompt, aspect=aspect_ratio,
        source=_file_sha256(image_path),
    )
    decision = journal.reserve(unit=str(unit), fingerprint=fingerprint, kind="video",
                               provider=_journal_clip_provider(), model=VIDEO_METHOD)
    if decision.call_id and decision.action != "skip":
        _JOURNAL_FRAME.state = {
            "journal": journal, "call_id": decision.call_id, "order_id": "",
            "ordered": False, "closed": False, "path": output_path or "",
        }
    return decision


def _journal_clip_gate(decision, index, output_path):
    """Что делать, когда платить нельзя. Возвращает (перехвачено, данные).

    Три случая различаются ценой ошибки, и путать их нельзя:
      skip    — клип уже оплачен и получен: отдаём файл, заказ не создаём;
      verify  — заказ создавался и результата мы не видели: опрашиваем ЕГО;
      unknown — кабинет не ответил: клип пропускается, его доберёт докачка. Пропустить
                клип дешевле, чем оплатить его второй раз.
    """
    label = index + 1
    if decision.action == "skip":
        done = decision.result_path or output_path or ""
        if done and os.path.exists(done):
            print_user(f"♻️ [{label}] Журнал: клип уже получен ({os.path.basename(done)}) — "
                       f"платный запрос не нужен", "INFO")
            try:
                with open(done, "rb") as fh:
                    return True, base64.b64encode(fh.read()).decode("ascii")
            except OSError as exc:
                print_user(f"⚠️ [{label}] Журнал: готовый файл не читается ({exc}) — "
                           f"клип пропущен без нового платного запроса", "ERROR")
                return True, None
        print_user(f"⛔ [{label}] Журнал: клип числится оплаченным и полученным, но файла "
                   f"«{done}» здесь нет — пропускаю без нового платного запроса", "ERROR")
        return True, None
    if decision.may_call:
        return False, None
    if decision.action == "verify" and decision.remote_order_id:
        _journal_note_order(decision.remote_order_id, post=False)
        print_user(f"🔎 [{label}] Журнал: заказ {decision.remote_order_id} уже создан — "
                   f"опрашиваю его, нового не создаю", "WARNING")
        data = _journal_clip_poll()(decision.remote_order_id, index,
                                    timeout=VEO_POLL_TIMEOUT)
        if data:
            if output_path:
                save_base64_to_file(data, output_path)
            print_user(f"✅ [{label}] Журнал: результат забран у уже оплаченного заказа",
                       "SUCCESS")
            return True, data
        print_user(f"❌ [{label}] Журнал: оплаченный заказ {decision.remote_order_id} "
                   f"результата не дал — клип пропущен без новой оплаты", "ERROR")
        return True, None
    why = ("заказ уже создавался, id неизвестен" if decision.action == "verify"
           else f"кабинет не ответил ({decision.error or 'причина неизвестна'})")
    print_user(f"⛔ [{label}] Журнал: {why} — пропускаю клип без повторной оплаты", "ERROR")
    return True, None


def _journal_frame_call(function):
    """Закрывает строку журнала на ЛЮБОМ выходе из generate_image.

    Декоратор, а не правка полутора десятков `return` внутри функции: пропущенная точка
    выхода означала бы вечно открытую строку и кадр, который больше никогда не повторят.
    """
    @functools.wraps(function)
    def _wrapped(*args, **kwargs):
        # Причина ПРЕДЫДУЩЕГО кадра этого же потока не имеет права закрыть нынешнюю
        # строку журнала. Сброс стоял внутри цикла попыток, а ранние выходы
        # (_v6_generic_terminal_frame_blocked, _account_limit_hit) возвращают None ДО
        # цикла: кадр, к провайдеру вообще не ходивший, закрывался убедительным
        # «HTTP 401: api key expired» от чужого кадра — пул потоков переиспользует
        # thread-local. Чистим на ВХОДЕ в заявку: одна заявка — одна причина.
        _set_provider_refusal("")
        _set_last_gen_error("")
        try:
            result = function(*args, **kwargs)
        except BaseException as exc:
            _journal_close_frame(None, error=f"{type(exc).__name__}: {str(exc)[:200]}")
            raise
        _journal_close_frame(
            result[1] if isinstance(result, tuple) and len(result) > 1 else None
        )
        return result
    return _wrapped


def _model_fallback_call(function):
    """Фактическая модель кадра — на выходе из generate_image (см. IMAGE_SAFETY_FALLBACK_MODEL).

    Кадр, полученный откатом, записывается в image_model_fallbacks.json; кадр, полученный
    основной моделью, оттуда убирается (его перерисовали — пометка устарела).
    """
    @functools.wraps(function)
    def _wrapped(*args, **kwargs):
        _FRAME_FALLBACK.info = None      # пул переиспользует потоки: чужой откат недопустим
        result = function(*args, **kwargs)
        info = getattr(_FRAME_FALLBACK, "info", None)
        _FRAME_FALLBACK.info = None
        data = result[1] if isinstance(result, tuple) and len(result) > 1 else None
        output_path = kwargs.get("output_path") or (args[4] if len(args) > 4 else None)
        if (data and output_path and isinstance(result[0], int)
                and _canonical_numeric_image_name(os.path.basename(str(output_path)))):
            try:
                record_model_fallback(output_path, result[0] + 1, info)
            except Exception as exc:                               # noqa: BLE001
                print_debug(f"[откат модели] пометка не записана: {exc}")
        return result
    return _wrapped


@_model_fallback_call  # 05.10.2026: фактическая модель кадра после отката фильтра
@_journal_frame_call   # 03.09.2026: журнал платных вызовов, см. блок выше
def generate_image(scene_data: dict, style: str, auto_hero_path: Optional[str] = None,
                   user_refs: List[str] = None, output_path: str = None,
                   aspect_ratio: str = "landscape", seed: Optional[int] = None, index: int = 0, use_legacy: bool = False,
                   full_text: Optional[str] = None, previous_images: Optional[List[str]] = None,
                   style_refs: Optional[List[str]] = None,
                   bible: Optional[dict] = None, ref_image_map: Optional[dict] = None,
                   hero_subject: Optional[str] = None,
                   reference_bundle: Optional[dict] = None,
                   journal_unit: Optional[str] = None) -> Tuple[int, Optional[str]]:
    """
    Генерирует изображение через Googler API v3 с поддержкой референсов и моделью GEM_PIX_2.

    Args:
        scene_data: dict с полями visual_prompt (или visual_description), camera_angle, subject_type (или subject_focus)
        style: Стиль изображения (всегда первым в промпте)
        auto_hero_path: Путь к авто-герою (если есть)
        user_refs: Список путей к пользовательским референсам
        output_path: Путь для сохранения (опционально)
        aspect_ratio: Соотношение сторон (landscape, portrait, square)
        seed: Seed для генерации (int, не str!)
        index: Индекс изображения
        full_text: Полный текст сценария (для контекста в Smart Mode)
        previous_images: Список путей к предыдущим изображениям (для визуального контекста в Smart Mode)
    """
    # Модель ЭТОГО кадра. Обычно это GOOGLER_MODEL, но после отказа фильтра кадр один раз
    # заказывается другой моделью (IMAGE_SAFETY_FALLBACK_MODEL), а кадры рисуются параллельно —
    # глобальную менять нельзя, поэтому внутри функции модель только локальная.
    _model = GOOGLER_MODEL
    print_user(f"🎨 [{index+1}] Генерация изображения...", "PROCESS")
    
    # Поддержка старого и нового формата scene_data
    visual_prompt = scene_data.get('visual_prompt') or scene_data.get('visual_description', '')
    visible_text_suffix = _visible_text_contract_suffix(scene_data)
    camera_angle = scene_data.get('camera_angle', 'Wide Shot')
    subject_type = scene_data.get('subject_type', '')
    if not subject_type:
        # Конвертируем старый формат subject_focus
        subject_focus = scene_data.get('subject_focus', '').lower()
        if 'hero' in subject_focus or 'character' in subject_focus:
            subject_type = 'character'
        elif 'environment' in subject_focus or 'scene' in subject_focus:
            subject_type = 'environment'
        else:
            subject_type = 'object'
    
    legacy_reference_args = any((
        auto_hero_path, user_refs, previous_images, style_refs, bible,
        ref_image_map, hero_subject,
    ))
    if reference_bundle is None and legacy_reference_args:
        raise ref_contract.ReferenceContractError(
            "reference-bearing generate_image calls require a prebuilt scene bundle"
        )
    reference_images, ref_hints, identity_block = _materialize_reference_bundle(
        reference_bundle
    )

    exact_preflight = _resolve_preflight_exact_safety(
        visual_prompt,
        identity_block=identity_block,
        ref_hints=ref_hints,
        immutable_scene=bool(scene_data.get("immutable_identity_prompt", False)),
    )
    
    # Очищаем visual_prompt от описаний стиля, которые могут конфликтовать с заданным стилем
    # Убираем упоминания качества, разрешения, стиля из visual_prompt, так как стиль задан отдельно
    visual_prompt_clean = exact_preflight.scene_prose
    style_keywords = ["quality", "resolution", "grainy", "cinematic", "photorealistic", "8k", "4k", "hd", "sd", "144p", "480p", "720p", "1080p", "vintage", "old", "bad quality", "good quality"]
    style_lower = style.lower()
    for keyword in (() if exact_preflight.matched else style_keywords):
        # Если ключевое слово уже есть в заданном стиле, убираем ТОЛЬКО само слово
        # (а не всю клаузу вокруг него). Старый паттерн r'\b[^,.]*kw[^,.]*\b' сжирал
        # сабжект: "a cinematic photo of an ancient roman senate" → удалялось целиком
        # из-за слова "cinematic". Узкий \bkw\b убирает дубль стиля, сохраняя предмет.
        if keyword in style_lower:
            # НЕ импортировать re здесь: локальный import делает имя `re` локальным для ВСЕЙ
            # функции, и обращение выше по коду (ветка совпадения героя с сабжектом) падало с
            # «local variable 're' referenced before assignment». Тихо убивало кадр за кадром
            # с 23.07: за одну ночь 332 кадра в 5 задачах. re импортирован на уровне модуля.
            pattern = r'\b' + re.escape(keyword) + r'\b'
            visual_prompt_clean = re.sub(pattern, '', visual_prompt_clean, flags=re.IGNORECASE)
            # Подчищаем осиротевшие артикли/предлоги перед запятой и двойные пробелы/запятые
            visual_prompt_clean = re.sub(r'\s+', ' ', visual_prompt_clean).strip()
            visual_prompt_clean = re.sub(r'[,\s]+,', ',', visual_prompt_clean).strip()
            visual_prompt_clean = re.sub(r'^[,\s]+', '', visual_prompt_clean).strip()
    
    # Стиль-серия → стиль ОДНОГО кадра: перечень регистров превратился бы в коллаж
    # внутри кадра (стиль подставляется первым и доминирующим — см. ниже).
    style, _reg_note = style_for_index(style, index)
    if _reg_note:
        print_debug(f"[{index+1}] стиль-серия → {_reg_note}")

    # Определяем, является ли стиль черно-белым/монохромным
    style_lower = style.lower()
    is_monochrome_style = any(keyword in style_lower for keyword in [
        "1940", "1940s", "vintage", "old photo", "old photograph", "retro", 
        "black and white", "black white", "monochrome", "grayscale", "b&w",
        "bad quality", "grainy", "144p", "480p"
    ])

    # Proven whole-scene failures are selected from the raw scene before any
    # optional style cleanup. A live one-call A/B canary proved that the neutral
    # proxy succeeds raw but fails when the segmented camera/monochrome/SCENE
    # envelope is reattached. Therefore only unprotected exact matches use the
    # proxy as the provider prompt; exact visible-copy bytes remain appended.
    if exact_preflight.matched:
        print_user(
            f"🩹 [{index+1}] Доказанный exact-отказ: безопасный промпт выбран "
            "до первого запроса",
            "WARNING",
        )
    
    if exact_preflight.raw_provider_prompt:
        # Live A/B on p_cf8b6f3367: the raw exact proxy succeeds, while appending
        # the generic negative ``Render NO visible text`` suffix makes the same
        # OPENAI/openai-image operation terminate generic-failed.  Empty
        # ``visible_text=[]`` carries no required bytes, so omit only that negative
        # suffix in this rare raw recovery branch.  Non-empty exact copy remains
        # protected byte-for-byte; normal/protected prompts keep the full contract.
        omit_openai_empty_copy_suffix = (
            str(_model).strip().upper() == "OPENAI"
            and scene_data.get("visible_text") == []
        )
        raw_visible_text_suffix = (
            "" if omit_openai_empty_copy_suffix else visible_text_suffix
        )
        prompt, _trimmed = _fit_prompt_with_visible_text_contract(
            visual_prompt_clean, "", raw_visible_text_suffix, max_bytes=8000,
        )
        prompt_contract = {
            "style": "",
            "camera": "",
            "scene_prose": visual_prompt_clean,
            "identity_block": "",
            "ref_hints": [],
            "visible_suffix": raw_visible_text_suffix,
            "monochrome": False,
            "immutable_scene": False,
            "max_bytes": 8000,
            "raw_provider_prompt": True,
        }
    else:
        # Normal and protected scenes retain the segmented prompt contract.
        provider_style = (
            style
            if exact_preflight.style_override is None
            else exact_preflight.style_override
        )
        prompt, _trimmed, prompt_contract = _compose_image_prompt_contract(
            style=provider_style,
            camera=camera_angle,
            scene_prose=visual_prompt_clean,
            identity_block=identity_block,
            ref_hints=ref_hints,
            visible_suffix=visible_text_suffix,
            monochrome=is_monochrome_style,
            immutable_scene=bool(scene_data.get("immutable_identity_prompt", False)),
        )
    
    # Конвертируем aspect_ratio в формат API
    ar_map = {
        'landscape': 'IMAGE_ASPECT_RATIO_LANDSCAPE',
        'portrait': 'IMAGE_ASPECT_RATIO_PORTRAIT',
        'square': 'IMAGE_ASPECT_RATIO_SQUARE'
    }
    ar = ar_map.get(aspect_ratio.lower(), 'IMAGE_ASPECT_RATIO_LANDSCAPE')
    
    if _trimmed:
        print_user(
            f"⚠️ [{index+1}] Опциональный стиль/ракурс сокращён; "
            f"scene+identity+reference mapping сохранены ({len(prompt.encode('utf-8'))} байт)",
            "WARNING",
        )
    
    # Логируем финальный промпт и референсы
    print_debug(f"[{index}] Промпт: {prompt[:150]}...")
    if reference_images:
        print_debug(f"[{index}] Референсов: {len(reference_images)} ({', '.join(ref_hints)})")

    # 03.09.2026: заявка в журнал платных вызовов ДО первого обращения к провайдеру.
    # Номер кадра — тот же ключ, которым уже живут receipts (f"{index+1:03d}"), чтобы
    # строка журнала и квитанция кадра указывали на одну и ту же сцену.
    _jrn = _journal_reserve_frame(
        unit=(journal_unit or f"{index + 1:03d}"),
        prompt=prompt, seed=seed, aspect_ratio=aspect_ratio,
        reference_bundle=reference_bundle, output_path=output_path, model=_model,
    )
    if _jrn.action == "skip":
        # Кадр уже оплачен и получен раньше — второй заказ создавать нельзя.
        _jrn_done = _jrn.result_path or output_path or ""
        if _jrn_done and os.path.exists(_jrn_done):
            print_user(f"♻️ [{index+1}] Журнал: кадр уже получен ({os.path.basename(_jrn_done)}) — "
                       f"платный запрос не нужен", "INFO")
            try:
                with open(_jrn_done, "rb") as _jf:
                    return (index, base64.b64encode(_jf.read()).decode("ascii"))
            except OSError as _je:
                print_user(f"⚠️ [{index+1}] Журнал: готовый файл не читается ({_je}) — "
                           f"кадр пропущен без нового платного запроса", "ERROR")
                return (index, None)
        print_user(f"⛔ [{index+1}] Журнал: кадр числится оплаченным и полученным, но файла "
                   f"«{_jrn_done}» здесь нет — пропускаю без нового платного запроса", "ERROR")
        return (index, None)
    if not _jrn.may_call:
        if _jrn.action == "verify":
            # Заказ по этому кадру уже создавался. Второй заказ = вторая оплата, поэтому
            # либо опрашиваем существующий id, либо пропускаем кадр (его доберут две
            # существующие добивки пропущенных).
            _jrn_poll = (
                poll_v6_generation if _model in IMAGE_MODEL_V6
                else poll_v4_operation if (_model in FLOW_GENERATE_MODELS
                                           or _model in ("FLOWER", "GROK",
                                                                "GROK_SPEED", "OPENAI"))
                else None
            )
            if _jrn.remote_order_id and _jrn_poll is not None:
                _journal_note_order(_jrn.remote_order_id, post=False)
                print_user(f"🔎 [{index+1}] Журнал: заказ {_jrn.remote_order_id} уже создан — "
                           f"опрашиваю его, нового не создаю", "WARNING")
                _jrn_data = _jrn_poll(_jrn.remote_order_id, index, timeout=IMAGE_POLL_TIMEOUT)
                if _jrn_data:
                    if output_path:
                        save_base64_to_file(_jrn_data, output_path)
                    print_user(f"✅ [{index+1}] Журнал: результат забран у уже оплаченного заказа", "SUCCESS")
                    return (index, _jrn_data)
                # Спрашиваем ОДИН раз напрямую: нам нужна не картинка, а причина. 404
                # здесь — это «провайдер потерял заказ», и он отличается от «заказ ещё
                # не готов» тем, что ждать больше нечего никогда.
                _jrn_final, _jrn_why = _order_lost_reason(_jrn.remote_order_id, why=True)
                if _jrn_final:
                    # Результата по этому заказу не будет никогда (потерян или отклонён).
                    # Повтор дешевле, чем держать остальные оплаченные кадры проекта.
                    _journal_retire_lost(_jrn, _jrn_final)
                    # 05.10.2026: заказываем заново сразу, в этом же прогоне (владелец: «чтобы
                    # студия сама запрашивала кадры, которые не дослали») — обычным путём,
                    # где на отказ фильтра смягчается промпт.
                    _jrn_old = _jrn.remote_order_id
                    _jrn = _journal_reserve_frame(
                        unit=(journal_unit or f"{index + 1:03d}"),
                        prompt=prompt, seed=seed, aspect_ratio=aspect_ratio,
                        reference_bundle=reference_bundle, output_path=output_path,
                        model=_model,
                    )
                    if not _jrn.may_call:
                        print_user(f"🧹 [{index+1}] Журнал: {_jrn_final} ({_jrn_old}). Новый "
                                   f"заказ журнал сейчас не разрешил ({_jrn.error or _jrn.action}) "
                                   f"— кадр закажет следующий прогон", "WARNING")
                        return (index, None)
                    print_user(f"🔁 [{index+1}] Журнал: {_jrn_final} ({_jrn_old}) — заказываю "
                               f"кадр заново в этом же прогоне: это дешевле, чем держать "
                               f"остальные оплаченные кадры", "WARNING")
                else:
                    print_user(f"❌ [{index+1}] Журнал: оплаченный заказ {_jrn.remote_order_id} "
                               f"результата не дал — кадр пропущен без нового платного запроса"
                               + (f" ({_jrn_why[:90]})" if _jrn_why else ""), "ERROR")
                    return (index, None)
            else:
                print_user(f"⛔ [{index+1}] Журнал: заказ уже создавался"
                           f"{' (id ' + str(_jrn.remote_order_id) + ')' if _jrn.remote_order_id else ' (id неизвестен)'}, "
                           f"опросить его нечем — пропускаю кадр без повторной оплаты", "ERROR")
                return (index, None)
        else:
            # unknown: кабинет не ответил, и мы не знаем, не оплачен ли кадр уже. Платить нельзя.
            print_user(f"⛔ [{index+1}] Журнал недоступен ({_jrn.error or _jrn.action}) — "
                       f"кадр пропущен: повторная оплата дороже пропуска", "ERROR")
            return (index, None)

    # v2 endpoint dead — only v3/v4 attempts, no legacy fallback
    # VEO_BANANA (NonStop) — столько же попыток, сколько у veo-видео (VEO_MAX_ATTEMPTS); остальным моделям 5.
    # Per-key оверрайд из кабинета (GOOGLER_MAX_RETRIES) заменяет дефолт, если задан.
    max_attempts = _gen_attempts(VEO_MAX_ATTEMPTS if _model in VEO_BANANA_MODELS else 5)
    use_legacy_now = False  # v2 endpoint dead, no fallback
    _soften_rounds = 0  # раундов GPT-смягчения промпта после контент-фильтра (макс 3)
    _acct_limit_hits = 0  # сколько раз ЭТОТ кадр получил «лимит аккаунта исчерпан»
    _acct_limit_owner = False  # этот кадр — первооткрыватель: делает подтверждающую вторую попытку
    _provider_group = _model_provider_group(_model)
    # FastGen/openai-image generic terminal failures are paid terminal operations, not
    # transport retries.  Keep their budget per frame/job separate from the generic
    # max_attempts setting (which may be 15 in the cabinet).
    _v6_generic_terminal_hits = 0
    _v6_generic_frame_key = (
        _v6_generic_terminal_frame_key(_model, prompt, output_path, index)
        if _model == "OPENAI" else None
    )
    # «Смягчение последним шансом»: часть отказов приходит БЕЗ признака контент-фильтра
    # (recrafter v6: «Generation failed, please try again later»), и кадр терялся, ни разу
    # не пройдя смягчение. Даём ровно ОДНУ дополнительную попытку со смягчённым промптом.
    _last_chance_used = False
    _extra_attempts = 0
    # Откат на другую модель после отказа фильтра (см. IMAGE_SAFETY_FALLBACK_MODEL): один
    # заказ, и он последний — откатной модели повторов нет.
    _fallback_used = False
    _fallback_orders = 0
    _fallback_reason = ""

    # while вместо for: последний шанс добавляет попытку уже ПОСЛЕ входа в цикл, а range()
    # фиксируется на старте. attempt увеличивается первым делом, поэтому все `continue`
    # внутри тела продолжают работать как раньше и цикл гарантированно завершается.
    attempt = -1
    while True:
        attempt += 1
        if attempt >= max_attempts + _extra_attempts:
            break
        if _fallback_used:
            _fallback_orders += 1
            if _fallback_orders > 1:
                _fb_last = " ".join(str(_last_gen_error() or "результата нет").split())[:120]
                _fallback_gave_up(index, _model, _fallback_reason, _fb_last,
                                  output_path or f"#{index}")
                return (index, None)
        # Блок — для основной модели; откатный заказ этого же кадра им не останавливается.
        if not _fallback_used and _v6_generic_terminal_frame_blocked(_v6_generic_frame_key):
            print_user(
                f"⛔ [{index+1}] FastGen/openai-image: кадр уже получил два generic "
                "terminal отказа в этой задаче — refill без нового платного запроса",
                "ERROR",
            )
            return (index, None)
        # Аккаунт провайдера ПОДТВЕРЖДЁННО исчерпан другим кадром этого прогона → быстрый
        # отказ БЕЗ сетевого запроса (иначе каждый кадр крутил бы свои max_attempts попыток).
        # Первооткрыватель исключён: его вторая попытка и есть подтверждение.
        if not _acct_limit_owner and _account_limit_hit(_provider_group):
            print_user(f"⛔ [{index+1}] {ACCOUNT_LIMIT_MARKER}: пропускаю кадр без запроса "
                       f"(лимит уже зафиксирован в этом прогоне)", "ERROR")
            return (index, None)
        # Часовая квота ключа исчерпана — до сброса окна запросы бессмысленны (каждый
        # вернёт тот же 429). Ждём вместе со всеми потоками, а не долбим API каждым кадром.
        _hl_left = _hourly_limit_left()
        if _hl_left > 0:
            print_user(f"⏳ [{index+1}] Часовая квота ключа исчерпана — жду сброса окна "
                       f"({_hl_left // 60} мин {_hl_left % 60} с)", "WARNING")
            time.sleep(min(_hl_left, HOURLY_LIMIT_MAX_WAIT))
            _extra_attempts += 1          # ожидание не должно съедать попытку кадра
            continue
        _set_last_gen_error("")  # чистим перед попыткой (thread-local; пул переиспользует потоки)
        _set_provider_refusal("")  # причина прошлой попытки не должна выдаваться за нынешнюю
        # Legacy fallback убран — endpoint /api/v2/images возвращает 404 (FastGen его убил)
        pass  # use_legacy_now stays False для всех попыток
        
        if use_legacy_now:
            # Legacy режим через API v2
            if reference_images:
                # Используем remix операцию через v2
                params = {
                    "prompt": prompt,
                    "aspect_ratio": ar,
                    "reference_images": reference_images
                }
                if seed is not None:
                    params["seed"] = seed
                
                data = {
                    "provider": "google_fx",
                    "operation": "remix",
                    "parameters": params
                }
                result = googler_request_v2("images", data=data)
            else:
                # Используем generate операцию через v2
                params = {
                    "prompt": prompt,
                    "aspect_ratio": ar
                }
                if seed is not None:
                    params["seed"] = seed
                
                data = {
                    "provider": "google_fx",
                    "operation": "generate",
                    "parameters": params
                }
                result = googler_request_v2("images", data=data)
        elif _model in IMAGE_MODEL_V6:
            # v6 unified: ВСЕ recrafter-картинки через POST /api/v6/generations (миграция 2026-06-27,
            # старые /api/v4/*/image/* = 404). inputs=reference_images (data-uri), seed теперь работает.
            v6_ar = "16:9" if ar in ("landscape", "IMAGE_ASPECT_RATIO_LANDSCAPE", "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE") else \
                    "9:16" if ar in ("portrait", "IMAGE_ASPECT_RATIO_PORTRAIT", "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR") else "1:1"
            v6_q = "speed" if _model == "GROK_SPEED" else None
            _lbl = GOOGLER_MODEL_LABELS.get(_model, _model)
            # Live v6 capability: grok-image accepts 1..5 named inputs, but
            # aspect_ratio is valid only with exactly one input.
            v6_request_ar = (
                None if _model in ("GROK", "GROK_SPEED")
                and len(reference_images) > 1 else v6_ar
            )
            _v6_image_call = functools.partial(
                googler_v6_generate,
                IMAGE_MODEL_V6[_model],
                prompt,
                aspect_ratio=v6_request_ar,
                inputs=(reference_images or None),
                seed=seed,
                quality=v6_q,
                index=index,
                timeout=IMAGE_POLL_TIMEOUT,
            )
            if _model == "OPENAI" and exact_preflight.raw_provider_prompt:
                image_data = _run_openai_exact_raw_serialized(_v6_image_call)
            else:
                image_data = _v6_image_call()
            if image_data:
                if output_path:
                    save_base64_to_file(image_data, output_path)
                    print_user(f"✅ [{index+1}] Изображение через {_lbl} (v6)", "SUCCESS")
                return (index, image_data)
            # Обёртка «v6 генерация не удалась» больше не побеждает правду провайдера:
            # именно она печаталась в «Попытка N неудачна» и «Все N попыток исчерпаны»,
            # и по этим строкам причину восьми боевых отказов установить было нельзя.
            result = {"success": False, "error": _gen_failure_error(_lbl)}
            api_label = _lbl

        elif _model == "WHISK_IMAGEN4":
            # Whisk API (Imagen 4) через v3/image/from-text (без model param)
            if reference_images:
                request_data = {
                    "provider": "google_fx",
                    "prompt": prompt,
                    "reference_images": [_reference_input_value(row) for row in reference_images],
                    "aspect_ratio": ar,
                }
                if seed is not None:
                    request_data["seed"] = seed
                result = googler_request_v3("image/remix", data=request_data)
            else:
                request_data = {
                    "provider": "google_fx",
                    "prompt": prompt,
                    "aspect_ratio": ar,
                }
                if seed is not None:
                    request_data["seed"] = seed
                result = googler_request_v3("image/from-text", data=request_data)
            api_label = "Whisk (Imagen 4)"
        
        elif _model == "FLOWER":
            # Flower API (v4, async) - Nano Banana 2 via Flower
            flower_ar = "16:9" if ar in ("landscape", "IMAGE_ASPECT_RATIO_LANDSCAPE", "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE") else \
                        "9:16" if ar in ("portrait", "IMAGE_ASPECT_RATIO_PORTRAIT", "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR") else "16:9"
            request_data = {"prompt": prompt, "aspect_ratio": flower_ar}
            if reference_images:
                # Flower accepts single reference_image (use first one)
                first_ref = reference_images[0]
                ref_img = _reference_input_value(first_ref)
                request_data["reference_image"] = ref_img
            v4_result = googler_request_v4("flower/image/generate", data=request_data)
            op_id = v4_result.get("operation_id")
            _journal_note_order(op_id)   # 03.09.2026: заказ оплачен — пишем id ДО поллинга
            if op_id:
                image_data = poll_v4_operation(op_id, index, timeout=IMAGE_POLL_TIMEOUT)
                if image_data:
                    if output_path:
                        save_base64_to_file(image_data, output_path)
                        print_user(f"✅ [{index+1}] Изображение через Flower", "SUCCESS")
                    return (index, image_data)
            error = v4_result.get("error", "No operation_id")
            result = {"success": False, "error": error}
            api_label = "Flower (Nano Banana 2)"

        elif _model in ("GROK", "GROK_SPEED"):
            # Grok image. quality='quality' (4 variant) или 'speed' (6 variant).
            # If reference provided → grok/image/edit (img2img).
            # Otherwise → grok/image/generate (text-to-image).
            grok_quality = "speed" if _model == "GROK_SPEED" else "quality"
            grok_ar = "16:9" if ar in ("landscape", "IMAGE_ASPECT_RATIO_LANDSCAPE", "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE") else \
                      "9:16" if ar in ("portrait", "IMAGE_ASPECT_RATIO_PORTRAIT", "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR") else "16:9"
            if reference_images:
                first_ref = reference_images[0]
                ref_img = _reference_input_value(first_ref)
                request_data = {"prompt": prompt, "image": ref_img, "quality": grok_quality}
                v4_result = googler_request_v4("grok/image/edit", data=request_data)
                _grok_label = f"Grok edit ({grok_quality})"
            else:
                request_data = {"prompt": prompt, "aspect_ratio": grok_ar, "quality": grok_quality}
                v4_result = googler_request_v4("grok/image/generate", data=request_data)
                _grok_label = f"Grok generate ({grok_quality})"
            op_id = v4_result.get("operation_id")
            _journal_note_order(op_id)   # 03.09.2026: заказ оплачен — пишем id ДО поллинга
            if op_id:
                image_data = poll_v4_operation(op_id, index, timeout=IMAGE_POLL_TIMEOUT)
                if image_data:
                    if output_path:
                        save_base64_to_file(image_data, output_path)
                        print_user(f"✅ [{index+1}] Изображение через {_grok_label}", "SUCCESS")
                    return (index, image_data)
            error = v4_result.get("error", "No operation_id")
            result = {"success": False, "error": error}
            api_label = _grok_label

        elif _model in FLOW_GENERATE_MODELS:
            # Flow v4 (async): NARWHAL / GEM_PIX_2 (Nano Banana Pro) / IMAGEN_3_5 (Imagen 4).
            # Все три — один эндпоинт flow/image/generate с параметром model=<ID>.
            # v3-эндпоинт эти модели не принимает (мёртв) — только v4 flow. Generate-only.
            flow_label = GOOGLER_MODEL_LABELS.get(_model, _model)
            request_data = {"prompt": prompt, "aspect_ratio": ar, "model": _model}
            if reference_images:
                ref_list = [_reference_input_value(row) for row in reference_images]
                request_data["reference_images"] = ref_list
            if seed is not None:
                request_data["seed"] = seed
            v4_result = googler_request_v4("flow/image/generate", data=request_data)
            op_id = v4_result.get("operation_id")
            _journal_note_order(op_id)   # 03.09.2026: заказ оплачен — пишем id ДО поллинга
            if op_id:
                image_data = poll_v4_operation(op_id, index, timeout=IMAGE_POLL_TIMEOUT)
                if image_data:
                    if output_path:
                        save_base64_to_file(image_data, output_path)
                        print_user(f"✅ [{index+1}] Изображение через {flow_label}", "SUCCESS")
                    return (index, image_data)
            error = v4_result.get("error", "No operation_id")
            result = {"success": False, "error": error}
            api_label = flow_label

        elif _model in VEO_BANANA_MODELS:
            # VeoNonStop Nano Banana (veononstop.org, ключ veo). СИНХРОННО: ответ сразу
            # содержит data.media[].fifeUrl (без operation_id-поллинга как у recrafter).
            banana_ar = "16:9" if ar in ("landscape", "IMAGE_ASPECT_RATIO_LANDSCAPE", "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE") else \
                        "9:16" if ar in ("portrait", "IMAGE_ASPECT_RATIO_PORTRAIT", "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR") else "16:9"
            request_data = {"prompt": prompt, "aspect_ratio": banana_ar}
            _bkey = VEO_BANANA_MODEL_KEYS.get(_model)
            if _bkey:
                request_data["model_key"] = _bkey   # HARBOR_SEAL(Lite)/NARWHAL(2)/GEM_PIX_2(Pro)
            if reference_images:
                # banana ждёт reference_images=[{name,image_base64,mime_type}] (до 5)
                _refs = []
                for _ri, _r in enumerate(reference_images):
                    _raw = _reference_input_value(_r)
                    _b64 = _raw.split(",", 1)[-1] if isinstance(_raw, str) and _raw.startswith("data:") else _raw
                    _name = (_r.get("filename") if isinstance(_r, dict) else None) or f"ref{_ri+1}.jpg"
                    _refs.append({"name": _name, "image_base64": _b64, "mime_type": "image/jpeg"})
                request_data["reference_images"] = _refs
                request_data["use_all_ref_images"] = True
            # banana синхронный и медленный (видел 5–53с, иногда >120с) → отдельный запрос
            # с таймаутом VEO_BANANA_TIMEOUT, БЕЗ внутреннего ретрая veo_request (ретрай по таймауту = новая
            # генерация = лишний кредит; внешний цикл generate_image и так ретраит до max_attempts).
            v_res = {"success": False, "error": "Nano Banana (Veo): нет ответа"}
            try:
                _bhdr = {"X-API-Key": VEO_API_KEY, "Content-Type": "application/json; charset=utf-8"}
                _bresp = requests.post(f"{VEO_BASE_URL}/image/banana/generate", headers=_bhdr, json=request_data, timeout=VEO_BANANA_TIMEOUT)
                if _bresp.status_code in (401, 403):
                    v_res = {"success": False, "error": f"Veo auth {_bresp.status_code}: ключ неверный/просрочен", "skip_retry": True}
                elif _bresp.status_code == 200:
                    v_res = _bresp.json()
                elif _is_account_limit_response(_bresp.status_code, _bresp.text):
                    # Исчерпан аккаунт провайдера (не троттл нашей параллельности): ретраи это
                    # не лечат, ждать надо 20-30 мин → отдельный признак account_limit.
                    v_res = {"success": False,
                             "error": _account_limit_error("Nano Banana (Veo)", _bresp.text),
                             "account_limit": True}
                else:
                    v_res = {"success": False, "error": f"banana HTTP {_bresp.status_code}: {_bresp.text[:200]}"}
            except requests.exceptions.Timeout:
                # на повторном таймауте не крутим зря (провайдер тормозит, каждый заход = кредит)
                v_res = {"success": False, "error": f"Nano Banana (Veo): таймаут {VEO_BANANA_TIMEOUT}с (провайдер тормозит)", "skip_retry": attempt >= 1}
            except Exception as _berr:
                v_res = {"success": False, "error": f"Nano Banana (Veo): {str(_berr)[:200]}"}
            if isinstance(v_res, dict) and v_res.get("success"):
                _media = ((v_res.get("data") or {}).get("media")) or []
                _fife = _media[0].get("fifeUrl") if _media else None
                _bytes = veo_fetch_url(_fife) if _fife else None
                if _bytes:
                    image_data = base64.b64encode(_bytes).decode("ascii")
                    if output_path:
                        save_base64_to_file(image_data, output_path)
                    # Аккаунт ответил картинкой → он жив: снимаем метку/заявку (иначе секундная
                    # просадка провайдера продолжала бы душить прогон и после выздоровления).
                    _clear_account_limit(_provider_group)
                    print_user(f"✅ [{index+1}] Изображение через Nano Banana (Veo)", "SUCCESS")
                    return (index, image_data)
                result = {"success": False, "error": "Nano Banana (Veo): пустой media/fifeUrl или не скачалось"}
            else:
                result = {"success": False,
                          "error": (v_res.get("error") if isinstance(v_res, dict) else "Nano Banana (Veo): нет ответа"),
                          "skip_retry": (v_res.get("skip_retry") if isinstance(v_res, dict) else False),
                          "account_limit": bool(v_res.get("account_limit")) if isinstance(v_res, dict) else False}
            api_label = "Nano Banana (Veo)"

        elif _model == "GPT_IMAGE2":
            # OpenAI GPT_IMAGE_MODEL ("gpt-image-2.5-sunburst") НАПРЯМУЮ — 💲ПЛАТНО
            # с твоего openai-ключа. Отдельно от "OPENAI" (тот идёт через recrafter-кредиты).
            # quality=low, output jpeg. Цена ~$0.006/шт снята под старую "gpt-image-2" и
            # НЕ перепроверена под 2.5 (платного вызова для проверки не делали).
            api_label = "GPT Image 2.5 (💲платно)"
            gi_size, gi_expected_dimensions = _gpt_image2_size(ar)
            v_res = {"success": False, "error": "GPT Image 2.5: нет ответа"}
            if not OPENAI_API_KEY or not str(OPENAI_API_KEY).strip():
                v_res = {"success": False, "error": "GPT Image 2.5: нет openai-ключа (~/video_studio_api_keys.json['openai'])", "skip_retry": True}
            else:
                gi_body = {"model": GPT_IMAGE_MODEL, "prompt": prompt, "size": gi_size, "quality": "low", "n": 1, "output_format": "jpeg"}
                try:
                    # НЕ через _openai_chat_headers(): это картинки у самого OpenAI,
                    # а не чат. Адрес здесь жёсткий, значит и ключ должен быть ключом
                    # OpenAI — ключ чужого чат-провайдера сюда не подходит.
                    _gh = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
                    _gr = requests.post("https://api.openai.com/v1/images/generations", headers=_gh, json=gi_body, timeout=180)
                    if _gr.status_code == 200:
                        v_res = _gr.json()
                    elif _gr.status_code in (401, 403):
                        v_res = {"success": False, "error": f"OpenAI auth {_gr.status_code}: ключ неверный/без доступа", "skip_retry": True}
                    else:
                        v_res = {"success": False, "error": f"OpenAI HTTP {_gr.status_code}: {_gr.text[:200]}"}
                except requests.exceptions.Timeout:
                    v_res = {"success": False, "error": "GPT Image 2.5: таймаут 180с", "skip_retry": attempt >= 1}
                except Exception as _ge:
                    v_res = {"success": False, "error": f"GPT Image 2.5: {str(_ge)[:200]}"}
            _gdata = v_res.get("data") if isinstance(v_res, dict) else None
            if _gdata and _gdata[0].get("b64_json"):
                image_data = _gdata[0]["b64_json"]
                try:
                    actual_dimensions = _base64_image_size(image_data)
                except ValueError as exc:
                    result = {"success": False, "error": f"GPT Image 2.5: {exc}",
                              "skip_retry": True}
                else:
                    if actual_dimensions != gi_expected_dimensions:
                        result = {
                            "success": False,
                            "error": (
                                "GPT Image 2.5: неверное разрешение ответа "
                                f"{actual_dimensions[0]}x{actual_dimensions[1]} вместо "
                                f"{gi_expected_dimensions[0]}x{gi_expected_dimensions[1]}"
                            ),
                            "skip_retry": True,
                        }
                    elif output_path and not save_base64_to_file(image_data, output_path):
                        result = {"success": False,
                                  "error": "GPT Image 2.5: изображение прошло проверку, но не сохранилось",
                                  "skip_retry": True}
                    else:
                        print_user(
                            f"✅ [{index+1}] Изображение через GPT Image 2.5 "
                            f"{actual_dimensions[0]}x{actual_dimensions[1]} "
                            "(💲ПЛАТНО, цена не перепроверена под 2.5)",
                            "SUCCESS",
                        )
                        return (index, image_data)
            else:
                result = {"success": False,
                          "error": (v_res.get("error") if isinstance(v_res, dict) else "GPT Image 2.5: пустой data/b64_json"),
                          "skip_retry": (v_res.get("skip_retry") if isinstance(v_res, dict) else False)}

        elif _model == "OPENAI":
            # OpenAI ChatGPT image flow (v4, async)
            openai_ar = "16:9" if ar in ("landscape", "IMAGE_ASPECT_RATIO_LANDSCAPE", "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE") else \
                        "9:16" if ar in ("portrait", "IMAGE_ASPECT_RATIO_PORTRAIT", "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR") else "1:1"
            request_data = {"prompt": prompt, "aspect_ratio": openai_ar}
            if reference_images:
                ref_list = [_reference_input_value(row) for row in reference_images]
                request_data["reference_images"] = ref_list
            v4_result = googler_request_v4("openai/image/generate", data=request_data)
            op_id = v4_result.get("operation_id")
            _journal_note_order(op_id)   # 03.09.2026: заказ оплачен — пишем id ДО поллинга
            if op_id:
                image_data = poll_v4_operation(op_id, index, timeout=IMAGE_POLL_TIMEOUT)
                if image_data:
                    if output_path:
                        save_base64_to_file(image_data, output_path)
                        print_user(f"✅ [{index+1}] Изображение через ChatGPT Image", "SUCCESS")
                    return (index, image_data)
            error = v4_result.get("error", "No operation_id")
            result = {"success": False, "error": error}
            api_label = "ChatGPT Image"

        else:
            # Flow API v3 (GEM_PIX_2 / IMAGEN_3_5)
            flow_model = _model
            if reference_images:
                request_data = {
                    "provider": "google_fx",
                    "prompt": prompt,
                    "reference_images": reference_images,
                    "aspect_ratio": ar,
                    "model": flow_model
                }
                if seed is not None:
                    request_data["seed"] = seed
                result = googler_request_v3("image/remix", data=request_data)
            else:
                request_data = {
                    "provider": "google_fx",
                    "prompt": prompt,
                    "aspect_ratio": ar,
                    "model": flow_model
                }
                if seed is not None:
                    request_data["seed"] = seed
                result = googler_request_v3("image/from-text-flow", data=request_data)
            api_label = GOOGLER_MODEL_LABELS.get(flow_model, flow_model)
        
        # Проверяем результат
        image_data = None
        if result.get("success"):
            image_data = result.get("image") or result.get("result")
            if image_data:
                if output_path:
                    if save_base64_to_file(image_data, output_path):
                        print_user(f"✅ [{index+1}] Изображение через {api_label}", "SUCCESS")
                    else:
                        print_user(f"⚠️ [{index+1}] Изображение через {api_label}, но не удалось сохранить", "WARNING")
                else:
                    print_user(f"✅ [{index+1}] Изображение через {api_label}", "SUCCESS")
                return (index, image_data)
        else:
            error = result.get("error", "Unknown error")
            if _fallback_used:
                _fb_last = " ".join(str(_last_gen_error() or error).split())[:120]
                _fallback_gave_up(index, _model, _fallback_reason, _fb_last,
                                  output_path or f"#{index}")
                return (index, None)
            if "InputIsImproper" in str(error):
                _soften_rounds += 1
                _softened = (
                    _soften_image_prompt_contract(prompt_contract, _soften_rounds)
                    if _soften_rounds <= 3 else None
                )
                if _softened and _softened[0].strip() != prompt:
                    prompt, prompt_contract = _softened
                    prompt = prompt.strip()
                    print_user(
                        f"🩹 [{index+1}] InputIsImproper: промпт смягчён "
                        f"(раунд {_soften_rounds}/3), пробую снова",
                        "WARNING",
                    )
                    continue
                print_user(f"❌ [{index+1}] API отклонил запрос: InputIsImproper (политика контента)", "ERROR")
                return (index, None)
            # Лимит аккаунта провайдера исчерпан (не троттл). Метку на весь прогон ставим НЕ
            # сразу: первооткрыватель делает подтверждающую вторую попытку с длинной паузой,
            # и только если она снова упёрлась в лимит — прогон помечается (мигнувший троттл
            # не должен убивать 242 кадра). Остальные кадры при подтверждённой метке отвалятся
            # мгновенно (см. верх цикла), а пока идёт проверка — ретраятся как при обычном сбое.
            if result.get("account_limit") or _is_account_limit_error(error):
                _acct_limit_hits += 1
                if not _acct_limit_owner:
                    # заявка свободна (первооткрыватель не появился/умер) → берём проверку на себя
                    _acct_limit_owner = _claim_account_limit_probe(_provider_group)
                if _acct_limit_owner:
                    if _acct_limit_hits < ACCOUNT_LIMIT_ATTEMPTS and attempt < max_attempts - 1:
                        print_user(f"⏳ [{index+1}] Похоже на лимит аккаунта — проверю ещё раз через "
                                   f"{ACCOUNT_LIMIT_RETRY_DELAY}с "
                                   f"(попытка {_acct_limit_hits + 1}/{ACCOUNT_LIMIT_ATTEMPTS})", "WARNING")
                        time.sleep(ACCOUNT_LIMIT_RETRY_DELAY)
                        continue
                    # Подтвердилось → метка на прогон (живёт ACCOUNT_LIMIT_TTL, снимается успехом)
                    if _mark_account_limit(_provider_group, error):
                        print_user(f"⛔ {str(error)[:300]}", "ERROR")
                    print_user(f"⛔ [{index+1}] {ACCOUNT_LIMIT_MARKER}: генерация остановлена "
                               f"(попыток {_acct_limit_hits}, ретраи лимит не лечат)", "ERROR")
                    return (index, None)
                if _account_limit_hit(_provider_group):
                    # Лимит уже подтверждён (кадр летел в момент отметки) → без лишних запросов.
                    print_user(f"⛔ [{index+1}] {ACCOUNT_LIMIT_MARKER}: лимит подтверждён — кадр пропущен", "ERROR")
                    return (index, None)
                # Проверку ведёт другой кадр. До её исхода аккаунт НЕ долбим коротким backoff
                # (25 потоков × ретрай раз в 3с = сотни запросов за минуту): ждём столько же,
                # сколько первооткрыватель, и возвращаемся в цикл. Подтвердится — кадр отвалится
                # сверху без запроса; окажется мигнувшим троттлом — ретраится дальше как обычно.
                if attempt < max_attempts - 1:
                    print_user(f"⏳ [{index+1}] Похоже на лимит аккаунта — жду результат проверки "
                               f"({ACCOUNT_LIMIT_RETRY_DELAY}с)", "WARNING")
                    time.sleep(ACCOUNT_LIMIT_RETRY_DELAY)
                    continue
                print_user(f"⛔ [{index+1}] {ACCOUNT_LIMIT_MARKER}: попытки исчерпаны на проверке лимита", "ERROR")
                return (index, None)
            if result.get("skip_retry"):
                print_user(f"❌ [{index+1}] API отклонил запрос: {str(error)[:200]}", "ERROR")
                return (index, None)
            # Часовая квота: не ошибка кадра и не повод жечь попытки — ставим метку окна,
            # ждать будет проверка в начале следующего витка (общая для всех потоков).
            if _is_hourly_limit_error(error) or _is_hourly_limit_error(_last_gen_error()):
                if _note_hourly_limit():
                    print_user(f"⏳ Часовая квота ключа исчерпана ({str(error)[:120]}) — "
                               f"потоки ждут сброса окна, запросы приостановлены", "WARNING")
                _extra_attempts += 1
                continue
            # Контент-фильтр (safety filter и т.п.) детерминирован для ТОГО ЖЕ промпта →
            # не повторяем его как есть, а применяем локальный deterministic safety-pass
            # (до 3 раундов), и только потом пропускаем картинку.
            #
            # 14.10.2026, «Všední Den Středověku»: openai-image три прогона подряд молча
            # отклонял одни и те же 11 кадров из 481 («Generation failed»), без признака
            # фильтра. Откат на другую модель был только у фильтра, кадры терялись, и ролик
            # падал на правиле 2%. Второй такой отказ кадра теперь идёт тем же откатом:
            # смягчений больше нет (одно уже было), один заказ другой моделью ключа.
            _generic_again = (
                _model == "OPENAI" and not _fallback_used
                and _v6_generic_terminal_hits >= 1
                and _is_generic_v6_terminal_error(_last_gen_error())
                and bool(safety_fallback_model(_model))
            )
            if (_generic_again or _is_fatal_gen_error(error)
                    or _is_fatal_gen_error(_last_gen_error())):
                _changed = False
                while _soften_rounds < 3 and not _changed and not _generic_again:
                    _soften_rounds += 1
                    _softened = _soften_image_prompt_contract(
                        prompt_contract, _soften_rounds
                    )
                    if (
                        _softened and _softened[0].strip()
                        and _softened[0].strip() != prompt
                    ):
                        prompt, prompt_contract = _softened
                        prompt = prompt.strip()
                        _changed = True
                if _changed:
                    print_user(f"🩹 [{index+1}] Контент-фильтр: промпт смягчён локально (раунд {_soften_rounds}/3), пробую снова", "WARNING")
                    continue
                if not _generic_again:
                    _note_soften_exhausted()   # копим статистику: сюжет режется устойчиво
                _fb_model = safety_fallback_model(_model)
                if _fb_model and claim_safety_fallback(output_path or f"#{index}"):
                    _fb_from = _model
                    _fb_label = GOOGLER_MODEL_LABELS.get(_fb_from, _fb_from)
                    if _generic_again:
                        # Основную модель для этого кадра больше не зовём: добивка
                        # пропущенных не должна снова платить за тот же отказ.
                        _block_v6_generic_terminal_frame(_v6_generic_frame_key)
                        _fallback_reason = (f"модель {_fb_from} дважды отказала без причины "
                                            f"(generic terminal)")
                        _fb_why = f"{_fb_label} дважды отказала без причины"
                    else:
                        _fallback_reason = (f"фильтр модели {_fb_from} после смягчений "
                                            f"({_soften_rounds})")
                        _fb_why = f"фильтр модели {_fb_label}"
                    print_user(f"🛟 [{index+1}] Сцена {index+1}: {_fb_why} — заказ моделью "
                               f"{GOOGLER_MODEL_LABELS.get(_fb_model, _fb_model)}", "WARNING")
                    # Заказ основной модели оплачен и кадра не дал — его строка журнала
                    # закрывается отказом; откат пойдёт своей строкой со своей моделью.
                    _journal_close_frame(None, error=(
                        (f"провайдер дважды вернул generic terminal status=failed "
                         if _generic_again else
                         f"провайдер отказал фильтром безопасности (смягчений: {_soften_rounds}) ")
                        + f"— кадр заказан моделью {_fb_model}"))
                    _model = _fb_model
                    _fallback_used = True
                    _provider_group = _model_provider_group(_model)
                    _fb_inputs = IMAGE_FALLBACK_MAX_INPUTS.get(_model)
                    if _fb_inputs and reference_images and len(reference_images) > _fb_inputs:
                        reference_images = list(reference_images)[:_fb_inputs]
                    _FRAME_FALLBACK.info = {"model": _model, "instead_of": _fb_from,
                                            "reason": _fallback_reason, "at": int(time.time())}
                    _jrn = _journal_reserve_frame(
                        unit=(journal_unit or f"{index + 1:03d}"),
                        prompt=prompt, seed=seed, aspect_ratio=aspect_ratio,
                        reference_bundle=reference_bundle, output_path=output_path,
                        model=_model,
                    )
                    if _jrn.action == "skip":
                        _fb_done = _jrn.result_path or output_path or ""
                        if _fb_done and os.path.exists(_fb_done):
                            print_user(f"♻️ [{index+1}] Журнал: откатный кадр уже получен — "
                                       f"платный запрос не нужен", "INFO")
                            try:
                                with open(_fb_done, "rb") as _fbf:
                                    return (index, base64.b64encode(_fbf.read()).decode("ascii"))
                            except OSError as _fbe:
                                print_user(f"⚠️ [{index+1}] Журнал: готовый откатный файл не "
                                           f"читается ({_fbe})", "ERROR")
                    elif (_jrn.action == "verify" and _jrn.remote_order_id
                          and _model in IMAGE_MODEL_V6):
                        # Откатный заказ с этим же промптом уже создавался прошлым прогоном.
                        # Второй не создаём: забираем результат первого или сдаёмся.
                        _journal_note_order(_jrn.remote_order_id, post=False)
                        _fb_data = poll_v6_generation(_jrn.remote_order_id, index,
                                                      timeout=IMAGE_POLL_TIMEOUT)
                        if _fb_data:
                            if output_path:
                                save_base64_to_file(_fb_data, output_path)
                            print_user(f"✅ [{index+1}] Журнал: откатный кадр забран у уже "
                                       f"оплаченного заказа", "SUCCESS")
                            return (index, _fb_data)
                        # 09.10.2026, владелец: «не хватило картинок, и мы не пошли в сервис
                        # ещё раз». Провайдер теряет и откатные заказы (404 на опрос), а эта
                        # ветка, в отличие от обычной, на потерю сдавалась: строка журнала
                        # оставалась «заказ был» и запрещала новый — кадр пропускался в
                        # каждом прогоне навсегда. Теперь как у обычного пути: потерянный
                        # заказ закрывается «результата не будет», и откат заказывается заново.
                        _fb_final = _order_lost_reason(_jrn.remote_order_id)
                        if _fb_final:
                            _fb_old = _jrn.remote_order_id
                            _journal_retire_lost(_jrn, _fb_final)
                            _jrn = _journal_reserve_frame(
                                unit=(journal_unit or f"{index + 1:03d}"),
                                prompt=prompt, seed=seed, aspect_ratio=aspect_ratio,
                                reference_bundle=reference_bundle, output_path=output_path,
                                model=_model,
                            )
                            if _jrn.may_call:
                                print_user(f"🔁 [{index+1}] Журнал: {_fb_final} ({_fb_old}) — "
                                           f"заказываю откатный кадр заново", "WARNING")
                    if not _jrn.may_call:
                        _note_frame_giveup(index, GAP_FILTER,
                                           f"{_fallback_reason}; откатный заказ журнал не разрешил "
                                           f"({_jrn.error or _jrn.action})")
                        print_user(f"⛔ [{index+1}] Журнал не разрешил откатный заказ "
                                   f"({_jrn.error or _jrn.action}) — пропускаю картинку", "ERROR")
                        return (index, None)
                    _extra_attempts += 1          # откату — свой заказ сверх исчерпанных попыток
                    continue
                if _generic_again:
                    # Отката этому кадру в прогоне уже не положено — как было до 14.10.
                    _block_v6_generic_terminal_frame(_v6_generic_frame_key)
                    print_user(
                        f"⛔ [{index+1}] FastGen/openai-image дважды вернул generic terminal "
                        "status=failed — останавливаю кадр и исключаю его из refill",
                        "ERROR",
                    )
                    return (index, None)
                _note_frame_giveup(index, GAP_FILTER,
                                   f"провайдер отказал фильтром безопасности (смягчений: {_soften_rounds})")
                print_user(f"⛔ [{index+1}] Контент-фильтр провайдера — пропускаю картинку (смягчений: {_soften_rounds})", "ERROR")
                return (index, None)

            # FastGen/openai-image sometimes returns a paid terminal ``status=failed``
            # without a safety or transport reason.  Repeating the identical operation
            # max_attempts=15 times cannot heal it.  Give the frame exactly one local,
            # deterministic rewrite and one confirmation generation; after the second
            # generic terminal result remember this frame so both refill rounds are free.
            if (
                _model == "OPENAI"
                and _is_generic_v6_terminal_error(_last_gen_error())
            ):
                _v6_generic_terminal_hits += 1
                if _v6_generic_terminal_hits == 1:
                    _round = min(3, max(1, _soften_rounds + 1))
                    _softened = _soften_image_prompt_contract(prompt_contract, _round)
                    if (
                        _softened and _softened[0].strip()
                        and _softened[0].strip() != prompt
                    ):
                        prompt, prompt_contract = _softened
                        prompt = prompt.strip()
                        _soften_rounds = _round
                    print_user(
                        f"🩹 [{index+1}] FastGen/openai-image вернул generic terminal "
                        "status=failed: один детерминированный rewrite и последняя попытка",
                        "WARNING",
                    )
                    continue
                _block_v6_generic_terminal_frame(_v6_generic_frame_key)
                print_user(
                    f"⛔ [{index+1}] FastGen/openai-image дважды вернул generic terminal "
                    "status=failed — останавливаю кадр и исключаю его из refill",
                    "ERROR",
                )
                return (index, None)
            
            if attempt < max_attempts - 1:
                if _model in VEO_BANANA_MODELS:
                    wait_time = VEO_BANANA_RETRY_DELAY   # капча у VeoNonStop, backoff не помогает → плоско 3с
                else:
                    wait_time = RETRY_DELAY * (attempt + 1)
                print_user(f"⚠️ [{index+1}] Попытка {attempt + 1} неудачна ({api_label}): {error[:150]}", "WARNING")
                print_user(f"🔄 Повтор через {wait_time}с...", "WARNING")
                time.sleep(wait_time)
            else:
                # Последний шанс перед тем, как бросить кадр: провайдер отказал без признака
                # фильтра, промпт ни разу не смягчался, сбой не похож на инфраструктурный →
                # один раунд смягчения и одна попытка. Дешевле, чем дырка в видеоряде.
                # транзиентность смотрим и по нашей обёртке, и по сырому тексту провайдера
                # (в `error` часто лежит только «… v6 генерация не удалась»)
                if (not _last_chance_used and _soften_rounds == 0
                        and not _is_transient_gen_error(error)
                        and not _is_transient_gen_error(_last_gen_error())):
                    _last_chance_used = True
                    _softened = _soften_image_prompt_contract(prompt_contract, 1)
                    if (
                        _softened and _softened[0].strip()
                        and _softened[0].strip() != prompt
                    ):
                        prompt, prompt_contract = _softened
                        prompt = prompt.strip()
                        _soften_rounds = 1
                        _extra_attempts = 1
                        print_user(f"🩹 [{index+1}] Последний шанс: отказ без признака фильтра "
                                   f"({str(error)[:80]}) — пробую со смягчённым промптом", "WARNING")
                        time.sleep(RETRY_DELAY)
                        continue
                _note_frame_giveup(index, _giveup_kind(error), error)
                print_user(f"❌ [{index+1}] Все {max_attempts + _extra_attempts} попыток исчерпаны "
                           f"({api_label}): {error[:200]}", "ERROR")
                return (index, None)

    return (index, None)


def generate_images_batch(
    prompts: List[str],
    aspect_ratio: str,
    output_dir: str,
    image_threads: Optional[int] = None,
) -> List[str]:
    """
    Генерирует изображения для списка промтов (legacy функция для старой логики).
    Для новой логики используйте cmd_full_pipeline.
    """
    os.makedirs(output_dir, exist_ok=True)
    
    total = len(prompts)
    try:
        threads = int(image_threads) if image_threads is not None else GOOGLER_IMAGE_THREADS
    except (TypeError, ValueError):
        threads = GOOGLER_IMAGE_THREADS
    threads = max(1, min(25, threads))
    print_user(f"🎨 Начинаю генерацию {total} изображений...", "PROCESS")
    print_debug(f"Потоков: {threads}")
    print_progress(0, total, "изображений", show_bar=True)
    
    saved_files = []
    results: List[Optional[str]] = [None] * total
    completed = 0
    last_progress_time = time.time()
    batch_start = time.time()

    with ThreadPoolExecutor(max_workers=threads) as executor:
        # Используем старую сигнатуру generate_image для обратной совместимости
        # Создаем простые scene_data объекты из промтов
        futures = {}
        for i, prompt in enumerate(prompts):
            if prompt:
                # Создаем простой scene_data для обратной совместимости
                scene_data = {
                    "text_segment": prompt,
                    "visual_description": prompt,
                    "camera_angle": "Wide Shot",
                    "subject_focus": "Environment"
                }
                future = executor.submit(
                    generate_image,
                    scene_data=scene_data,
                    style="cinematic, photorealistic",
                    auto_hero_path=None,
                    user_refs=None,
                    output_path=None,
                    aspect_ratio=aspect_ratio,
                    seed=None,
                    index=i
                )
                futures[future] = i
        
        for future in as_completed(futures):
            try:
                index, data_uri = future.result()
                if data_uri and isinstance(data_uri, str):
                    results[index] = data_uri
                completed += 1
                
                # Обновляем прогресс чаще для больших батчей
                update_interval = 1 if total < 50 else (2 if total < 200 else 5)
                current_time = time.time()
                time_since_last = current_time - last_progress_time
                
                # Обновляем прогресс каждые N элементов или каждые 3 секунды
                if completed % update_interval == 0 or time_since_last >= 3:
                    print_progress(completed, total, "изображений", show_bar=True)
                    print_eta(completed, total, batch_start, "изображений")
                    last_progress_time = current_time
                    
            except Exception as e:
                print_debug(f"Ошибка в потоке: {e}")
                import traceback
                print_debug(traceback.format_exc())
                completed += 1
                if completed % 5 == 0:
                    print_progress(completed, total, "изображений", show_bar=True)
    
    # Финальный прогресс-бар (100%)
    print_progress(total, total, "изображений", show_bar=True)
    
    for i, data_uri in enumerate(results):
        if data_uri:
            filename = f"{i + 1}.jpg"
            filepath = os.path.join(output_dir, filename)
            if save_base64_to_file(data_uri, filepath):
                saved_files.append(filepath)
                print_debug(f"Сохранено: {filename}")
    
    print_user(f"Сохранено изображений: {len(saved_files)} из {total}", "SUCCESS")
    return saved_files


# ============ ТРАНСФОРМАЦИЯ ИЗОБРАЖЕНИЙ (v1) ============

def transform_image(prompt: str, image_path: str, index: int = 0) -> Tuple[int, Optional[str]]:
    """Трансформирует изображение через выбранный бэкенд API"""
    print_debug(f"[{index}] Трансформация {os.path.basename(image_path)}...")
    
    image_base64 = image_to_base64(image_path)
    if not image_base64:
        print_debug(f"[{index}] Не удалось загрузить изображение")
        return (index, None)

    # img2img надёжно поддерживают только модели из TRANSFORM_CAPABLE_MODELS (OPENAI/FLOWER).
    # Остальные — generate-only: GROK/GROK_SPEED edit ловят анти-бот; flow-модели
    # (NARWHAL/GEM_PIX_2/IMAGEN_3_5) для transform нестабильны. Откат на OPENAI (дефолт).
    # Ветки ниже используют локальную `model`, а не глобал GOOGLER_MODEL — это и есть
    # "пометка": гейт сверху, реализация снизу (добавишь модель в set — ветка готова).
    model = GOOGLER_MODEL
    if model not in TRANSFORM_CAPABLE_MODELS:
        print_user(f"ℹ️ [{index}] Модель {GOOGLER_MODEL} только для generate; transform выполняю через OPENAI", "INFO")
        model = "OPENAI"
    api_label = GOOGLER_MODEL_LABELS.get(model, model)
    # v6 unified transform = generate с исходником в inputs. Надёжный img2img = openai/flower;
    # прочие модели на transform нестабильны → openai-image (модель уже откачена выше на OPENAI).
    v6_model = IMAGE_MODEL_V6.get(model) or "openai-image"
    for attempt in range(3):
        try:
            image_data = googler_v6_generate(v6_model, prompt, aspect_ratio="16:9",
                                             inputs=[image_base64], index=index, timeout=IMAGE_POLL_TIMEOUT)
            if image_data:
                print_debug(f"[{index}] Трансформировано через {api_label} (v6)")
                return (index, image_data)
        except Exception as e:
            err = str(e)[:150]
            if "InputIsImproper" in err or "content policy" in err.lower():
                print_user(f"Изображение {os.path.basename(image_path)} пропущено (политика контента)", "WARNING")
                return (index, None)
            print_debug(f"[{index}] transform v6 ошибка (попытка {attempt + 1}): {err}")
        if attempt < 2:
            time.sleep(RETRY_DELAY * (attempt + 1))
    return (index, None)


def transform_images_batch(prompt: str, image_paths: List[str], output_dir: str) -> List[str]:
    """Трансформирует список изображений"""
    os.makedirs(output_dir, exist_ok=True)
    
    total = len(image_paths)
    print_user(f"🔄 Начинаю трансформацию {total} изображений...", "PROCESS")
    print_user(f"📝 Промт: {prompt[:80]}{'...' if len(prompt) > 80 else ''}", "INFO")
    print_debug(f"Потоков: {GOOGLER_IMAGE_THREADS}")
    print_progress(0, total, "изображений", show_bar=True)
    
    saved_files = []
    results: List[Optional[str]] = [None] * total
    completed = 0
    skipped = 0
    last_progress_time = time.time()
    batch_start = time.time()

    with ThreadPoolExecutor(max_workers=GOOGLER_IMAGE_THREADS) as executor:
        futures = {
            executor.submit(transform_image, prompt, img_path, i): i
            for i, img_path in enumerate(image_paths)
        }
        
        for future in as_completed(futures):
            try:
                index, data_uri = future.result()
                if data_uri and isinstance(data_uri, str):
                    results[index] = data_uri
                else:
                    skipped += 1
                completed += 1
                
                # Обновляем прогресс чаще для больших батчей
                update_interval = 1 if total < 50 else (2 if total < 200 else 5)
                current_time = time.time()
                time_since_last = current_time - last_progress_time
                
                # Обновляем прогресс каждые N элементов или каждые 3 секунды
                if completed % update_interval == 0 or time_since_last >= 3:
                    print_progress(completed, total, "изображений", show_bar=True)
                    print_eta(completed, total, batch_start, "изображений")
                    last_progress_time = current_time
                    
            except Exception as e:
                print_debug(f"Ошибка в потоке: {e}")
                import traceback
                print_debug(traceback.format_exc())
                skipped += 1
                completed += 1
                # Обновляем прогресс даже при ошибке
                if completed % 5 == 0:
                    print_progress(completed, total, "изображений", show_bar=True)
    
    # Финальный прогресс-бар (100%)
    print_progress(total, total, "изображений", show_bar=True)
    
    for i, data_uri in enumerate(results):
        if data_uri:
            base_name = os.path.splitext(os.path.basename(image_paths[i]))[0]
            filename = f"{base_name}_transformed.jpg"
            filepath = os.path.join(output_dir, filename)
            if save_base64_to_file(data_uri, filepath):
                saved_files.append(filepath)
                print_debug(f"Сохранено: {filename}")
    
    if skipped > 0:
        print_user(f"Трансформировано изображений: {len(saved_files)} из {total} (пропущено: {skipped})", "SUCCESS")
    else:
        print_user(f"Трансформировано изображений: {len(saved_files)} из {total}", "SUCCESS")
    return saved_files


# ============ ГЕНЕРАЦИЯ ВИДЕО (v2 - асинхронный) ============

# Метод генерации видео
# v4 flow/flower/grok — recrafter.fun (один X-API-Key, base app.recrafter.fun).
# veo — VeoNonStop (veononstop.org, отдельный ключ veo_..., async task_id→poll→download).
VIDEO_METHOD = "veo"  # Veo NonStop (Google Veo 3.1) — дефолт
VIDEO_AVAILABLE_METHODS = ["veo", "flower", "flow", "grok"]
VIDEO_METHOD_LABELS = {
    "veo":    "Veo NonStop (3.1)",
    "flower": "Flower (Veo 3.1)",
    "flow":   "Flow (VEO)",
    "grok":   "Grok",
}


# ============ VeoNonStop (veononstop.org) ============
# Отдельный провайдер: base https://veononstop.org/api/v1, auth X-API-Key: veo_...
# Async: POST /video/{text-to-video|image-to-video} -> {data:{task_id}};
#   poll GET /video/status/:id (каждые 10с) -> data.status; на completed data.videos[];
#   скачать MP4 GET /video/download/:id?video_index=0 (X-API-Key) -> bytes.
# Возвращаем base64 data-uri, чтобы save_base64_to_file/batch работали без изменений.

def veo_request(path: str, data: Optional[dict] = None, method: str = "POST") -> dict:
    """Запрос к VeoNonStop API (X-API-Key). Возвращает распарсенный JSON либо {success:False,error:...}."""
    if not VEO_API_KEY or not str(VEO_API_KEY).strip():
        return {"success": False, "error": "VEO_API_KEY не задан (~/video_studio_api_keys.json['veo'] или env VEO_API_KEY)", "skip_retry": True}
    url = f"{VEO_BASE_URL}/{path.lstrip('/')}"
    headers = {"X-API-Key": VEO_API_KEY, "Content-Type": "application/json; charset=utf-8"}
    response = None
    for attempt in range(MAX_RETRIES):
        if attempt == 0 and data is not None:
            try:
                preview = {k: (v[:60] + '…' if isinstance(v, str) and len(v) > 60 else v) for k, v in data.items()}
                print_debug(f"veo {method} {url} body~{json.dumps(preview, ensure_ascii=False)[:200]}")
            except Exception:
                pass
        try:
            if method == "GET":
                response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            else:
                response = requests.post(url, headers=headers, json=data, timeout=REQUEST_TIMEOUT)

            if response.status_code == 429:
                wait_time = min(60, RETRY_DELAY * (2 ** attempt))
                print_user(f"⏳ Rate limit Veo, ждём {wait_time}с... (exp backoff)", "WARNING")
                time.sleep(wait_time)
                continue
            if response.status_code in (401, 403):
                txt = ""
                try:
                    txt = response.text[:300]
                except Exception:
                    pass
                return {"success": False, "error": f"Veo auth {response.status_code}: ключ неверный/без доступа. {txt}", "skip_retry": True}

            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError:
            code = getattr(response, 'status_code', '?')
            txt = ""
            try:
                txt = response.text[:400]
            except Exception:
                pass
            print_debug(f"HTTP {code} от veo ({path}) — {txt}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
        except requests.exceptions.Timeout:
            print_user(f"⏱️ Таймаут veo ({path})", "WARNING")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
        except requests.exceptions.RequestException as e:
            print_user(f"❌ Ошибка veo ({path}): {str(e)[:200]}", "WARNING")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
    return {"success": False, "error": f"Veo: не удалось выполнить запрос после {MAX_RETRIES} попыток"}


def veo_download_mp4(task_id: str, video_index: int = 0) -> Optional[bytes]:
    """Скачивает готовый MP4 через /video/download/:id (X-API-Key). Возвращает bytes или None."""
    if not VEO_API_KEY:
        return None
    url = f"{VEO_BASE_URL}/video/download/{task_id}"
    headers = {"X-API-Key": VEO_API_KEY}
    try:
        r = requests.get(url, headers=headers, params={"video_index": video_index}, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200 and r.content and len(r.content) > 1000:
            return r.content
        print_debug(f"veo download HTTP {r.status_code}, len={len(r.content) if r.content else 0}")
    except Exception as e:
        print_debug(f"veo download исключение: {str(e)[:150]}")
    return None


def veo_fetch_url(url: str) -> Optional[bytes]:
    """Фолбэк: скачивает MP4 напрямую с публичного URL (fifeUrl, googleapis storage)."""
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200 and r.content and len(r.content) > 1000:
            return r.content
        print_debug(f"veo fetch_url HTTP {r.status_code}")
    except Exception as e:
        print_debug(f"veo fetch_url исключение: {str(e)[:150]}")
    return None


def veo_poll_task(task_id: str, index: int = 0, timeout: int = None) -> Optional[str]:
    """Опрашивает VeoNonStop задачу до completed/failed.
    На completed скачивает MP4 и возвращает base64 data-uri (для save_base64_to_file)."""
    if timeout is None:
        timeout = VEO_POLL_TIMEOUT
    total_label = f"{index + 1}"
    start = time.time()
    last_status = None
    while time.time() - start < timeout:
        res = veo_request(f"video/status/{task_id}", method="GET")
        data = res.get("data") if isinstance(res, dict) else None
        if not data:
            err = (res.get("error") if isinstance(res, dict) else None) or "нет data в ответе статуса"
            print_debug(f"veo status [{total_label}] проблема: {err}")
            if isinstance(res, dict) and res.get("skip_retry"):
                return None  # фатальная ошибка (нет ключа / auth) — не крутим зря
            time.sleep(VEO_POLL_INTERVAL)
            continue
        status = data.get("status", "unknown")
        elapsed = int(time.time() - start)
        if status != last_status:
            print_debug(f"🎬 veo [{total_label}] status={status} ({elapsed}с)")
            last_status = status
        # Живой хартбит для оператора (фаза тянется до 20 мин, иначе видно тишину)
        print_user(f"Veo: {elapsed // 60}:{elapsed % 60:02d} / ~{timeout // 60}:{timeout % 60:02d} · {status}", "PROCESS")
        if status == "completed":
            videos = data.get("videos") or []
            if not videos:
                print_user(f"❌ veo [{total_label}] completed без videos", "ERROR")
                return None
            mp4 = veo_download_mp4(task_id, video_index=0)
            if not mp4:
                url = videos[0].get("fifeUrl") or videos[0].get("servingBaseUri")
                if url:
                    mp4 = veo_fetch_url(url)
            if mp4:
                elapsed = int(time.time() - start)
                print_debug(f"✅ veo [{total_label}] видео скачано ({len(mp4)//1024} КБ, {elapsed}с)")
                return "data:video/mp4;base64," + base64.b64encode(mp4).decode("ascii")
            print_user(f"❌ veo [{total_label}] не удалось скачать готовое видео", "ERROR")
            return None
        if status == "failed":
            err = data.get("error") or data.get("message") or "failed"
            _set_last_gen_error(err)  # для детекта фатальных (DANGER_FILTER) в ретрай-циклах
            print_user(f"❌ veo [{total_label}] задача провалена: {str(err)[:200]}", "ERROR")
            return None
        time.sleep(VEO_POLL_INTERVAL)
    print_user(f"⏱️ veo [{total_label}] таймаут ожидания ({timeout}с)", "WARNING")
    return None


@_journal_frame_call   # 03.09.2026: журнал платных вызовов — клип стоит дороже кадра
def generate_video(prompt: str, image_path: str, index: int = 0, use_legacy: bool = False,
                   journal_unit: Optional[str] = None,
                   journal_output: Optional[str] = None) -> Tuple[int, Optional[str]]:
    """
    Генерирует видео из изображения через выбранный бэкенд.
    
    VIDEO_METHOD (живые v4-эндпоинты):
    - flower: v4/flower/video/from-image (Veo 3.1, 1кр) — дефолт
    - flow:   v4/flow/video/from-ingredients (VEO, 1кр)
    - grok:   v4/grok/video/from-image (2-4кр)
    """
    api_label = VIDEO_METHOD_LABELS.get(VIDEO_METHOD, VIDEO_METHOD)
    total_label = f"{index + 1}"  # индекс для человека (1..N)
    log(f"🎬 [{total_label}] {_('googler_generating_video')} {os.path.basename(image_path)} ({api_label})...")
    
    image_base64 = image_to_base64(image_path)
    if not image_base64:
        return (index, None)

    v_ar = GROK_ASPECT_RATIOS.get("landscape", "16:9")  # видео-кадр обычно 16:9
    # 03.09.2026: заявка в журнал ДО первого обращения к провайдеру. Единица — имя
    # исходного кадра: из него же строится имя выходного файла, поэтому строка журнала
    # и файл на диске указывают на одну сцену.
    _jrn = _journal_reserve_clip(
        unit=(journal_unit or os.path.splitext(os.path.basename(image_path))[0]),
        prompt=prompt, aspect_ratio=v_ar, image_path=image_path,
        output_path=journal_output,
    )
    _handled, _data = _journal_clip_gate(_jrn, index, journal_output)
    if _handled:
        return (index, _data)

    max_attempts = _gen_attempts(VEO_MAX_ATTEMPTS if VIDEO_METHOD == "veo" else 3)
    _soften_rounds = 0  # раундов GPT-смягчения промпта после контент-фильтра (макс 3)

    for attempt in range(max_attempts):
        _set_last_gen_error("")  # чистим перед попыткой (thread-local)
        _set_provider_refusal("")
        if max_attempts > 1:
            log(f"🔁 [{total_label}] Попытка {attempt + 1}/{max_attempts} ({api_label})")
        try:
            if VIDEO_METHOD == "veo":
                # VeoNonStop: image_base64 БЕЗ data:-префикса + mime_type отдельно
                raw_b64 = image_base64
                mime = "image/jpeg"
                if isinstance(image_base64, str) and image_base64.startswith("data:") and "," in image_base64:
                    header, raw_b64 = image_base64.split(",", 1)
                    if ":" in header and ";" in header:
                        mime = header.split(":", 1)[1].split(";", 1)[0] or "image/jpeg"
                veo_body = {"prompt": prompt, "image_base64": raw_b64, "mime_type": mime, "aspect_ratio": v_ar}
                veo_res = veo_request("video/image-to-video", data=veo_body)
                task = veo_res.get("data") if isinstance(veo_res, dict) else None
                task_id = task.get("task_id") if isinstance(task, dict) else None
                _journal_note_order(task_id)   # 03.09.2026: заказ оплачен — id ДО поллинга
                if task_id:
                    log(f"📋 [{total_label}] veo task_id: {task_id}")
                    video_data = veo_poll_task(task_id, index, timeout=VEO_POLL_TIMEOUT)
                    if video_data:
                        return (index, video_data)
                else:
                    err = (veo_res.get('error') if isinstance(veo_res, dict) else None) or 'No task_id'
                    _set_last_gen_error(err)
                    _set_provider_refusal(err)   # та же правда — в журнал вызовов
                    log(f"❌ [{total_label}] Ошибка veo: {err}")
                    if isinstance(veo_res, dict) and veo_res.get("skip_retry"):
                        return (index, None)  # нет ключа/auth — не ретраим зря
            else:
                # v6 unified video (flower-video/flow-video-fast/grok-video). Исходный кадр → inputs[0].
                v6vid = VIDEO_METHOD_V6.get(VIDEO_METHOD, "flower-video")
                video_data = googler_v6_generate(v6vid, prompt, aspect_ratio=v_ar, inputs=[image_base64],
                                                 index=index, timeout=VIDEO_POLL_TIMEOUT)
                if video_data:
                    return (index, video_data)
                # Клип — самый дорогой вызов, и до правки его отказ читался как
                # «Ошибка flower: v6 video не удалось»: ни кода, ни слов провайдера.
                log(f"❌ [{total_label}] Ошибка "
                    f"{_gen_failure_error(api_label, 'v6 video не удалось')}")
        except Exception as e:
            log(f"❌ [{total_label}] Исключение {api_label}: {str(e)[:150]}")
        # Контент-фильтр (DANGER_FILTER и т.п.) детерминирован для того же промпта → GPT смягчает
        # промпт (до 3 раундов) и пробуем снова; не помогло — пропуск, НЕ крутим 20 попыток.
        if _is_fatal_gen_error(_last_gen_error()):
            _changed = False
            while _soften_rounds < 3 and not _changed:
                _soften_rounds += 1
                _new_p = soften_prompt(prompt, _soften_rounds)
                if _new_p and _new_p.strip() and _new_p.strip() != prompt:
                    prompt = _new_p.strip()
                    _changed = True
            if _changed:
                log(f"🩹 [{total_label}] Контент-фильтр: GPT смягчил промпт (раунд {_soften_rounds}/3), пробую снова")
                continue
            log(f"⛔ [{total_label}] Контент-фильтр провайдера — пропускаю клип (смягчений: {_soften_rounds})")
            return (index, None)
        if attempt < max_attempts - 1:
            # кап паузы 60с: линейный рост до 380с растягивал хвост одного клипа до ~1.5-2ч
            _delay = min(60, (VEO_RETRY_DELAY if VIDEO_METHOD == "veo" else RETRY_DELAY) * (attempt + 1))
            if VIDEO_METHOD == "veo":
                log(f"⏳ [{total_label}] Пауза {_delay}с перед повтором (cooldown reCAPTCHA)...")
            time.sleep(_delay)

    # Итоговая строка клипа — аналог «Все N попыток исчерпаны» у картинок: именно её
    # человек и читает, когда сцена осталась без видео. Причина последней попытки в ней
    # обязана быть, иначе отказ ключа на клипах по логу не устанавливается вообще.
    _why = _real_gen_reason()
    log(f"❌ [{total_label}] Не удалось запустить генерацию видео после {max_attempts} "
        f"попыток ({api_label})" + (f": {_why}" if _why else ""))
    return (index, None)


def generate_video_from_prompt_only(prompt: str, aspect_ratio: str = "landscape", index: int = 0) -> Tuple[int, Optional[str]]:
    """
    Генерирует видео только из текстового промта (без изображения).
    Поддерживает VIDEO_METHOD: flower (дефолт), flow, grok (все v4 from-text).
    """
    api_label = VIDEO_METHOD_LABELS.get(VIDEO_METHOD, VIDEO_METHOD)
    total_label = f"{index + 1}"
    log(f"🎬 [{total_label}] {_('googler_generating_video')} промта ({api_label})...")
    
    v_ar = GROK_ASPECT_RATIOS.get(aspect_ratio.lower(), "16:9")

    if VIDEO_METHOD == "veo":
        veo_res = veo_request("video/text-to-video", data={"prompt": prompt, "aspect_ratio": v_ar})
        task = veo_res.get("data") if isinstance(veo_res, dict) else None
        task_id = task.get("task_id") if isinstance(task, dict) else None
        _journal_note_order(task_id)   # 03.09.2026: заказ оплачен — id ДО поллинга
        if task_id:
            log(f"📋 [{total_label}] veo task_id: {task_id}")
            return (index, veo_poll_task(task_id, index, timeout=VEO_POLL_TIMEOUT))
        err = (veo_res.get('error') if isinstance(veo_res, dict) else None) or 'No task_id'
        # Здесь _set_last_gen_error НЕ ставим: в t2v-ветке его читает ретрай-логика
        # (_is_fatal_gen_error), и подмена её входа поменяла бы решения о повторах.
        # А журналу и человеку правда нужна — для этого и заведён отдельный слот.
        _set_provider_refusal(err)
        log(f"❌ [{total_label}] Ошибка veo: {err}")
        return (index, None)

    # v6 unified text→video (без inputs)
    v6vid = VIDEO_METHOD_V6.get(VIDEO_METHOD, "flower-video")
    video_data = googler_v6_generate(v6vid, prompt, aspect_ratio=v_ar, index=index, timeout=VIDEO_POLL_TIMEOUT)
    if video_data:
        return (index, video_data)
    log(f"❌ [{total_label}] Ошибка "
        f"{_gen_failure_error(api_label, 'v6 text→video не удалось')}")
    return (index, None)


@_journal_frame_call   # 03.09.2026: журнал платных вызовов, заявка на весь цикл попыток
def _t2v_generate_with_retry(prompt: str, aspect_ratio: str, index: int,
                             journal_unit: Optional[str] = None,
                             journal_output: Optional[str] = None) -> Tuple[int, Optional[str]]:
    """t2v с ретраями — как у i2v generate_video: VEO_MAX_ATTEMPTS для veo (капча/слоты флапают),
    паузы VEO_RETRY_DELAY*attempt (кап 120с)."""
    # Заявка одна на весь цикл: двадцать попыток одной сцены — это одна единица работы,
    # и если журнал завести на каждую попытку, повтор после таймаута будет выглядеть
    # новым кадром и оплатится заново.
    _jrn = _journal_reserve_clip(
        unit=(journal_unit if journal_unit is not None else f"{index + 1:03d}"),
        prompt=prompt, aspect_ratio=aspect_ratio, image_path="",
        output_path=journal_output,
    )
    _handled, _data = _journal_clip_gate(_jrn, index, journal_output)
    if _handled:
        return (index, _data)
    max_attempts = _gen_attempts(VEO_MAX_ATTEMPTS if VIDEO_METHOD == "veo" else 3)
    _soften_rounds = 0  # раундов GPT-смягчения промпта после контент-фильтра (макс 3)
    for attempt in range(max_attempts):
        _set_last_gen_error("")
        _set_provider_refusal("")
        try:
            _, data = generate_video_from_prompt_only(prompt, aspect_ratio=aspect_ratio, index=index)
        except Exception as e:
            print_debug(f"[{index+1}] t2v исключение: {e}")
            data = None
        if data:
            return (index, data)
        if _is_fatal_gen_error(_last_gen_error()):
            _changed = False
            while _soften_rounds < 3 and not _changed:
                _soften_rounds += 1
                _new_p = soften_prompt(prompt, _soften_rounds)
                if _new_p and _new_p.strip() and _new_p.strip() != prompt:
                    prompt = _new_p.strip()
                    _changed = True
            if _changed:
                log(f"🩹 [{index+1}] Контент-фильтр: GPT смягчил промпт (раунд {_soften_rounds}/3), пробую снова")
                continue
            log(f"⛔ [{index+1}] Контент-фильтр провайдера — пропускаю клип (смягчений: {_soften_rounds})")
            return (index, None)
        if attempt < max_attempts - 1:
            _delay = min(60, (VEO_RETRY_DELAY if VIDEO_METHOD == "veo" else RETRY_DELAY) * (attempt + 1))
            log(f"⏳ [{index+1}] Пауза {_delay}с перед повтором t2v (попытка {attempt+1}/{max_attempts})...")
            time.sleep(_delay)
    # Цикл кончился молча: сцена остаётся без клипа, и до правки в логе на этом месте
    # не было НИ ОДНОЙ строки с причиной — только отдельные строки попыток выше.
    _why = _real_gen_reason()
    log(f"❌ [{index+1}] t2v: клип не получен после {max_attempts} попыток"
        + (f": {_why}" if _why else ""))
    return (index, None)


def generate_videos_from_prompts_batch(scene_prompts: List[Tuple[int, str]], output_dir: str,
                                       aspect_ratio: str = "landscape",
                                       video_threads: Optional[int] = None) -> List[str]:
    """t2v-батч: scene_prompts = [(номер_сцены_1-based, промпт), ...] → googler video/NNN_video.mp4.
    Сохраняет КАЖДЫЙ клип сразу по готовности (resume-friendly, как i2v-батч)."""
    os.makedirs(output_dir, exist_ok=True)
    try:
        threads = int(video_threads) if video_threads is not None else GOOGLER_VIDEO_THREADS
    except (TypeError, ValueError):
        threads = GOOGLER_VIDEO_THREADS
    threads = max(1, min(20, threads))
    total = len(scene_prompts)
    api_label = VIDEO_METHOD_LABELS.get(VIDEO_METHOD, VIDEO_METHOD)
    print_user(f"🎬 Генерация {total} видео ИЗ ПРОМПТОВ сцен ({api_label}, t2v)...", "PROCESS")
    print_debug(f"Потоков: {threads}, aspect: {aspect_ratio}")
    print_progress(0, total, "видео")
    saved_files = []
    completed = 0
    batch_start = time.time()
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {
            executor.submit(_t2v_generate_with_retry, prompt, aspect_ratio, i,
                            f"{scene_num:03d}",
                            os.path.join(output_dir, f"{scene_num:03d}_video.mp4")): scene_num
            for i, (scene_num, prompt) in enumerate(scene_prompts)
        }
        for future in as_completed(futures):
            scene_num = futures[future]
            try:
                _, data_uri = future.result()
                completed += 1
                if data_uri and isinstance(data_uri, str):
                    filename = f"{scene_num:03d}_video.mp4"
                    filepath = os.path.join(output_dir, filename)
                    if save_base64_to_file(data_uri, filepath):
                        saved_files.append(filepath)
                        print_user(f"✅ [{completed}/{total}] Видео сохранено: {filename}", "SUCCESS")
                    else:
                        print_user(f"⚠️ [{completed}/{total}] Ошибка сохранения: {filename}", "WARNING")
                else:
                    print_user(f"❌ [{completed}/{total}] Видео не сгенерировано (сцена {scene_num})", "ERROR")
                print_progress(completed, total, "видео")
                print_eta(completed, total, batch_start, "видео")
            except Exception as e:
                print_debug(f"Ошибка в t2v потоке: {e}")
    print_user(f"✅ Готово! Сохранено видео: {len(saved_files)} из {total}", "SUCCESS")
    return saved_files


def _cmd_generate_videos_from_prompts(args, output_dir):
    """videos --from_prompts: t2v по промптам сцен из scenes.json — первые N сцен (или все при 0).
    Промпт сцены = как у картинок: style, camera_angle, visual_prompt. Если scenes.json нет —
    генерирует план из --source_file (полный обход стадии картинок). Картинки покрытых сцен
    (NNN.jpg) merge'ятся в googler_video_references.json → рендер их исключит."""
    project_dir = args.project_dir
    scenes_path = os.path.join(project_dir, GOOGLER_IMAGES_DIR, "scenes.json")
    scenes = None
    if os.path.isfile(scenes_path):
        try:
            with open(scenes_path, 'r', encoding='utf-8', errors='replace') as f:
                scenes = json.load(f)
        except Exception as e:
            print_debug(f"scenes.json не читается: {e}")
    if not scenes:
        src = getattr(args, 'source_file', None)
        if not src or not os.path.isfile(src):
            print_error_user_friendly("Нет scenes.json и не передан --source_file — сначала нужен план сцен")
            sys.exit(1)
        _assert_openai_key()
        with open(src, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
        _style = (getattr(args, 'style', '') or '').strip() or "cinematic, photorealistic"
        print_user("🎬 scenes.json нет — генерирую план сцен из текста...", "PROCESS")
        scenes = generate_scene_plan(text, style=_style,
                                     sentences_per_prompt=getattr(args, 'sentences_per_prompt', '3') or '3',
                                     split_long=getattr(args, 'split_long_sentences', False),
                                     max_solo_scene_seconds=getattr(
                                         args, 'max_solo_scene_seconds',
                                         DEFAULT_MAX_SOLO_SCENE_SECONDS),
                                     project_title=os.path.basename(getattr(args, 'project_dir', '') or ''))
        if not scenes:
            print_error_user_friendly("План сцен не сгенерировался")
            sys.exit(1)
        os.makedirs(os.path.dirname(scenes_path), exist_ok=True)
        _tmp = scenes_path + ".tmp"
        with open(_tmp, 'w', encoding='utf-8', errors='replace') as f:
            json.dump(scenes, f, ensure_ascii=False, indent=2)
        os.replace(_tmp, scenes_path)
        print_user(f"✅ План из {len(scenes)} сцен сохранен в scenes.json", "SUCCESS")

    n = int(getattr(args, 'first_n_scenes', 0) or 0)
    sel = scenes[:n] if n > 0 else scenes
    style = (getattr(args, 'style', '') or '').strip()
    print_user(f"🎥 t2v из промптов: {len(sel)} сцен из {len(scenes)}" + (f" (первые {n})" if n > 0 else " (все)"), "INFO")

    items = []  # (номер сцены 1-based, промпт)
    for i, sc in enumerate(sel):
        sc = sc if isinstance(sc, dict) else {}
        vp = str(sc.get('visual_prompt') or sc.get('text_segment') or '').strip()
        cam = str(sc.get('camera_angle') or '').strip()
        parts = [p for p in (style, cam, vp) if p]
        if parts:
            items.append((i + 1, ", ".join(parts)))
    if not items:
        print_error_user_friendly("Пустые сцены — нечего генерировать")
        sys.exit(1)

    # картинки покрытых сцен исключаем из рендера (union-merge, как у i2v)
    imgs_dir = os.path.join(project_dir, GOOGLER_IMAGES_DIR)
    covered = []
    for i, _sc in enumerate(sel):
        for ext in ('.jpg', '.jpeg', '.png', '.webp'):
            p = os.path.join(imgs_dir, f"{i+1:03d}{ext}")
            if os.path.isfile(p):
                covered.append(os.path.normpath(p))
                break
    if covered:
        refs_file = os.path.join(project_dir, "googler_video_references.json")
        try:
            old = []
            if os.path.exists(refs_file):
                try:
                    with open(refs_file, 'r', encoding='utf-8', errors='replace') as f:
                        old = json.load(f) or []
                except Exception:
                    old = []
            merged = list(dict.fromkeys([os.path.normpath(p) for p in list(old) + covered]))
            with open(refs_file, 'w', encoding='utf-8', errors='replace') as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            print_debug(f"refs merge: +{len(covered)} картинок исключены из рендера (итого {len(merged)})")
        except Exception as e:
            print_debug(f"refs не сохранились: {e}")

    # resume: докачиваем только недостающие NNN_video.mp4
    if args.use_cache and os.path.isdir(output_dir):
        existing = {f for f in os.listdir(output_dir) if f.endswith('.mp4')}
        _before = len(items)
        items = [(num, pr) for num, pr in items if f"{num:03d}_video.mp4" not in existing]
        if _before - len(items):
            print_user(f"Кэш видео: {_before - len(items)} уже готовы, догенерирую {len(items)}", "INFO")
        if not items:
            print_user("Все t2v-видео уже в кэше. Пропускаем генерацию.", "INFO")
            return

    saved = generate_videos_from_prompts_batch(items, output_dir,
                                               aspect_ratio=(getattr(args, 'aspect_ratio', 'landscape') or 'landscape'),
                                               video_threads=getattr(args, 'video_threads', None))
    if not saved and items:
        print_error_user_friendly(f"Ни одно t2v-видео не сгенерировано (0 из {len(items)}) — детали в ошибках выше")
        sys.exit(1)
    print_user(f"Генерация t2v-видео завершена! Сохранено: {len(saved)}", "SUCCESS")


def generate_videos_batch(prompt: str, image_paths: List[str], output_dir: str, use_legacy: bool = False, video_threads: Optional[int] = None) -> List[str]:
    """Генерирует видео для списка изображений"""
    os.makedirs(output_dir, exist_ok=True)
    
    # Используем переданное значение или значение по умолчанию
    try:
        threads = int(video_threads) if video_threads is not None else GOOGLER_VIDEO_THREADS
    except (TypeError, ValueError):
        threads = GOOGLER_VIDEO_THREADS
    threads = max(1, min(20, threads))
    
    total = len(image_paths)
    api_label = VIDEO_METHOD_LABELS.get(VIDEO_METHOD, VIDEO_METHOD)
    print_user(f"🎬 Генерация {total} видео ({api_label})...", "PROCESS")
    print_debug(f"Промт: {prompt[:120]}...")
    print_debug(f"Потоков: {threads}")
    print_debug(f"Метод видео: {VIDEO_METHOD}")
    # Стартовый прогрессбар
    print_progress(0, total, "видео")
    
    saved_files = []
    completed = 0
    batch_start = time.time()

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {
            executor.submit(
                generate_video, prompt, img_path, i, use_legacy,
                os.path.splitext(os.path.basename(img_path))[0],
                os.path.join(output_dir,
                             os.path.splitext(os.path.basename(img_path))[0] + "_video.mp4")): i
            for i, img_path in enumerate(image_paths)
        }

        for future in as_completed(futures):
            try:
                index, data_uri = future.result()
                completed += 1
                # СРАЗУ сохраняем на диск по готовности (раньше base64 копился в памяти
                # до конца ВСЕГО батча: папка пустовала, гигабайты в RAM, а стоп/краш
                # посреди прогона терял все уже сгенерированные видео + кредиты).
                if data_uri and isinstance(data_uri, str):
                    base_name = os.path.splitext(os.path.basename(image_paths[index]))[0]
                    filename = f"{base_name}_video.mp4"
                    filepath = os.path.join(output_dir, filename)
                    if save_base64_to_file(data_uri, filepath):
                        saved_files.append(filepath)
                        print_user(f"✅ [{index+1}/{total}] Видео сохранено: {filename}", "SUCCESS")
                    else:
                        print_user(f"⚠️ [{index+1}/{total}] Ошибка сохранения: {filename}", "WARNING")
                else:
                    print_user(f"❌ [{index+1}/{total}] Видео не сгенерировано для {os.path.basename(image_paths[index])}", "ERROR")
                # Прогрессбар после каждого завершённого видео
                print_progress(completed, total, "видео")
                print_eta(completed, total, batch_start, "видео")
            except Exception as e:
                print_debug(f"Ошибка в потоке: {e}")
                import traceback
                print_debug(traceback.format_exc())

    print_user(f"✅ Готово! Сохранено видео: {len(saved_files)} из {total}", "SUCCESS")
    return saved_files


def check_usage() -> dict:
    """Проверяет лимиты использования API"""
    return googler_request_v2("usage", method="GET")


# ============ ГЛАВНЫЕ КОМАНДЫ ============

@_serialize_project_image_mutation()
def cmd_generate_prompts(args):
    """Команда: генерация промтов из текстового файла"""
    print_user("Начинаю генерацию промптов...", "PROCESS")
    _assert_openai_key()  # нет OpenAI-ключа → фатально ДО любой работы
    
    try:
        with open(args.source_file, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read().strip()
    except FileNotFoundError:
        print_error_user_friendly(f"Файл не найден: {args.source_file}")
        return
    
    if not text:
        print_error_user_friendly("Файл пуст!")
        return
    
    sentences = split_text_to_sentences(text)
    print_debug(f"Найдено предложений: {len(sentences)}")
    
    groups = group_sentences(sentences, args.sentences_per_prompt)
    print_user(f"Создано групп: {len(groups)}", "INFO")
    
    # Всегда автоматически генерируем Visual Context для единого стиля
    print_user("Анализирую текст для создания визуального стиля...", "PROCESS")
    visual_context = generate_visual_context(text)
    
    if visual_context:
        print_debug(f"Visual Context: {visual_context}")
        print_user("Визуальный стиль создан", "SUCCESS")
    else:
        print_user("Продолжаю без визуального контекста", "WARNING")
        visual_context = None
    
    prompts = generate_prompts_batch(groups, args.style, visual_context)
    
    output_dir = _confined_project_directory(
        args.project_dir, GOOGLER_IMAGES_DIR
    )
    
    prompts_file = os.path.join(output_dir, "prompts.json")
    # visual_context уже установлен выше (автоматически сгенерирован)
    prompts_data = {
        "style": args.style,
        "sentences_per_prompt": args.sentences_per_prompt,
        "visual_context": visual_context,  # Автоматически сгенерированный визуальный контекст
        "prompts": [
            {"index": i, "text": groups[i], "prompt": prompts[i]}
            for i in range(len(prompts))
        ]
    }

    with open(prompts_file, 'w', encoding='utf-8', errors='replace') as f:
        json.dump(prompts_data, f, indent=2, ensure_ascii=False)

    print_user(f"Промпты сохранены: {prompts_file}", "SUCCESS")
    print_user("Генерация промптов завершена!", "SUCCESS")


@_serialize_project_image_mutation()
def cmd_generate_images(args):
    """Custom-prompt images with the same reference runtime/receipts as pipeline."""
    print_user("Начинаю генерацию изображений...", "PROCESS")
    _assert_image_generation_key(getattr(args, "model", None) or GOOGLER_MODEL)

    output_dir = _confined_project_directory(
        args.project_dir, GOOGLER_IMAGES_DIR
    )
    prompts_file = os.path.join(output_dir, "prompts.json")
    prompt_style = ""
    if getattr(args, "prompts_json", None):
        prompts = json.loads(args.prompts_json)
        print_debug(f"Получено явных промтов: {len(prompts)}")
    elif os.path.exists(prompts_file):
        with open(prompts_file, 'r', encoding='utf-8', errors='replace') as f:
            prompts_data = json.load(f)
        prompts = [p["prompt"] for p in prompts_data.get("prompts", []) if p.get("prompt")]
        prompt_style = str(prompts_data.get("style") or "")
        print_debug(f"Загружено промтов из файла: {len(prompts)}")
    else:
        print_error_user_friendly("Не указаны промты! Используйте --prompts_json или сначала сгенерируйте промты")
        return

    if (
        not isinstance(prompts, list) or not prompts
        or any(not isinstance(prompt, str) or not prompt.strip() for prompt in prompts)
    ):
        print_error_user_friendly("Список промтов пуст!")
        return

    style = str(getattr(args, "style", None) or prompt_style or "cinematic, photorealistic")
    scenes = [
        {
            "text_segment": prompt,
            "visual_description": prompt,
            "camera_angle": "Wide Shot",
            "subject_type": "environment",
        }
        for prompt in prompts
    ]
    sources = _load_reference_sources(args)
    runtime = _prepare_reference_runtime(
        args.project_dir, scenes, GOOGLER_MODEL, style, None, sources,
        single_subject=(len(sources.get("characters") or []) == 1),
        hero_subject=None,
        auto_hero=False,
        hero_prepared=None,
        persist=False,
    )
    scene_bytes = _json_bytes(scenes)
    custom_plan_fingerprint = ref_contract.canonical_fingerprint({
        "schema_version": 1,
        "kind": "custom-prompts",
        "prompts": prompts,
        "style": style,
        "model": str(GOOGLER_MODEL).upper(),
    })
    _atomic_write_bytes(os.path.join(output_dir, "scenes.json"), scene_bytes)
    _atomic_write_json(os.path.join(output_dir, "scenes.meta.json"), {
        "plan_version": "custom-prompts-v1",
        "model": str(GOOGLER_MODEL).upper(),
        "style": style,
        "scene_count": len(scenes),
        "scenes_sha256": hashlib.sha256(scene_bytes).hexdigest(),
        "custom_prompts_fingerprint": custom_plan_fingerprint,
        "analyze_story": False,
        "auto_hero": False,
        "manual_identity_present": bool(sources.get("characters")),
        "story_identity_present": False,
        "auto_hero_prompt_required": False,
        "prompt_artifacts": {
            "subject_anchor": {}, "story_bible": None, "hero_reference": None,
        },
    })
    _atomic_write_json(os.path.join(output_dir, REFERENCE_RUNTIME_FILE), runtime)
    fixed_seed = (
        args.seed if getattr(args, "seed", None) is not None
        else int(hashlib.md5(style.encode("utf-8")).hexdigest()[:8], 16) % 1000000 + 1
    )
    output_names = [f"{index + 1:03d}.jpg" for index in range(len(scenes))]
    receipt_path, receipts, _fingerprints = _prepare_image_receipts(
        args.project_dir, runtime, scenes, style, args.aspect_ratio, fixed_seed,
        output_names=output_names,
        trust_legacy_cache=bool(getattr(args, "trust_legacy_cache", False)),
        use_cache=bool(getattr(args, "use_cache", False)),
    )
    if not getattr(args, "use_cache", False):
        _invalidate_receipts_for_regeneration(
            receipt_path, receipts,
            [f"{index + 1:03d}" for index in range(len(scenes))],
        )
    try:
        threads = int(getattr(args, "image_threads", None) or GOOGLER_IMAGE_THREADS)
    except (TypeError, ValueError):
        threads = GOOGLER_IMAGE_THREADS
    threads = max(1, min(25, threads))

    def _pending_indices():
        return [
            index for index, output_name in enumerate(output_names)
            if not (
                getattr(args, "use_cache", False)
                and _receipt_cache_hit(
                    receipts, f"{index + 1:03d}", os.path.join(output_dir, output_name)
                )
            )
        ]

    pending = _pending_indices()
    if not pending:
        reconciliation = _reconcile_authoritative_image_set(
            args.project_dir, receipt_path, receipts, output_names
        )
        if reconciliation["quarantined"]:
            print_user(
                f"Старых нумерованных кадров перемещено в карантин: "
                f"{reconciliation['quarantined']}",
                "INFO",
            )
        print_user(f"Кэш подтверждён receipts для {len(scenes)} custom prompts", "INFO")
        return

    def _run_indices(indices):
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = {
                executor.submit(
                    generate_image,
                    scene_data=scenes[index], style=style,
                    output_path=os.path.join(output_dir, output_names[index]),
                    aspect_ratio=args.aspect_ratio, seed=fixed_seed, index=index,
                    reference_bundle=runtime["scenes"][index],
                ): index
                for index in indices
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    _idx, data_uri = future.result()
                    output_path = os.path.join(output_dir, output_names[index])
                    if data_uri and save_base64_to_file(data_uri, output_path):
                        _mark_receipt_success(
                            receipt_path, receipts, f"{index + 1:03d}", output_path
                        )
                except Exception as exc:
                    print_debug(f"custom image [{index + 1}] failed: {exc}")

    _run_indices(pending)
    for refill_round in (1, 2):
        missing = [
            index for index, output_name in enumerate(output_names)
            if not _receipt_cache_hit(
                receipts, f"{index + 1:03d}", os.path.join(output_dir, output_name)
            )
        ]
        if not missing or _account_limit_hit(_model_provider_group(GOOGLER_MODEL)):
            break
        print_user(f"🩹 Custom prompts refill {refill_round}/2: {len(missing)}", "WARNING")
        time.sleep(15)
        _run_indices(missing)

    have = sum(
        _receipt_cache_hit(
            receipts, f"{index + 1:03d}", os.path.join(output_dir, output_name)
        )
        for index, output_name in enumerate(output_names)
    )
    _account_limit_raise_if_hit(GOOGLER_MODEL, have, len(prompts))
    reconciliation = _reconcile_authoritative_image_set(
        args.project_dir, receipt_path, receipts, output_names
    )
    if reconciliation["quarantined"]:
        print_user(
            f"Старых нумерованных кадров перемещено в карантин: "
            f"{reconciliation['quarantined']}",
            "INFO",
        )
    print_user(
        f"Генерация изображений завершена! Подтверждено receipts: {have}/{len(prompts)}",
        "SUCCESS",
    )


def _load_regen_plan(project_dir: str, *,
                     allow_legacy_no_refs: bool = False) -> Tuple[List[dict], dict, str]:
    """Load and validate the exact published scene snapshot for regeneration."""
    googler_dir = _confined_project_directory(project_dir, GOOGLER_IMAGES_DIR)
    scenes_path = os.path.join(googler_dir, "scenes.json")
    meta_path = os.path.join(googler_dir, "scenes.meta.json")
    if os.path.isfile(scenes_path):
        try:
            raw = Path(scenes_path).read_bytes()
            scenes = json.loads(raw.decode("utf-8"))
            meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ScenePlanTerminalError(
                "scenes.json/scenes.meta.json для regen отсутствует или повреждён"
            ) from exc
        if not isinstance(scenes, list) or any(not isinstance(row, dict) for row in scenes):
            raise ScenePlanTerminalError("scenes.json для regen должен быть массивом объектов")
        required_meta = {
            "plan_version", "model", "style", "scene_count", "scenes_sha256",
            "analyze_story", "auto_hero", "manual_identity_present",
            "story_identity_present", "auto_hero_prompt_required", "prompt_artifacts",
        }
        if not isinstance(meta, dict) or not required_meta.issubset(meta):
            raise ScenePlanTerminalError(
                "scenes.meta.json для regen не содержит полный plan contract"
            )
        if meta.get("scene_count") != len(scenes):
            raise ScenePlanTerminalError("scene_count regen snapshot не совпадает")
        digest = hashlib.sha256(raw).hexdigest()
        if meta.get("scenes_sha256") != digest:
            raise ScenePlanTerminalError("digest regen scenes.json не совпадает")
        if (
            not isinstance(meta.get("style"), str)
            or not isinstance(meta.get("model"), str) or not meta["model"]
            or any(
                not isinstance(meta.get(key), bool)
                for key in (
                    "analyze_story", "auto_hero", "manual_identity_present",
                    "story_identity_present", "auto_hero_prompt_required",
                )
            )
        ):
            raise ScenePlanTerminalError("scenes.meta.json plan dependencies malformed")
        artifacts = meta.get("prompt_artifacts")
        if (
            not isinstance(artifacts, dict)
            or not isinstance(artifacts.get("subject_anchor"), dict)
        ):
            raise ScenePlanTerminalError("scenes.meta.json prompt_artifacts malformed")
        story = artifacts.get("story_bible")
        if meta["analyze_story"]:
            if not isinstance(story, dict):
                raise ScenePlanTerminalError(
                    "analyze_story plan has no validated story_bible"
                )
            story = _validate_story_bible(story)
        elif story is not None:
            raise ScenePlanTerminalError(
                "analyze_story=off plan unexpectedly contains story_bible"
            )
        story_present = bool(story and story.get("characters"))
        if meta["story_identity_present"] != story_present:
            raise ScenePlanTerminalError(
                "story_identity_present differs from prompt_artifacts"
            )
        auto_required = bool(
            meta["auto_hero"]
            and not meta["manual_identity_present"]
            and not meta["story_identity_present"]
        )
        if meta["auto_hero_prompt_required"] != auto_required:
            raise ScenePlanTerminalError("auto-hero plan dependency mismatch")
        hero_reference = artifacts.get("hero_reference")
        if auto_required and (
            not isinstance(hero_reference, dict)
            or not str(hero_reference.get("prompt") or "").strip()
        ):
            raise ScenePlanTerminalError("auto-hero plan has no prepared prompt")
        if story is not None:
            _validate_explicit_alias_entity_coverage(scenes, story)
        return scenes, meta, str(meta.get("style") or "")

    if not allow_legacy_no_refs:
        raise ScenePlanTerminalError(
            "для regen нет согласованного scenes.json/scenes.meta.json"
        )
    prompts = _load_json_object(os.path.join(googler_dir, "prompts.json"))
    rows = (prompts or {}).get("prompts")
    if not isinstance(rows, list):
        raise ScenePlanTerminalError("для regen нет scenes.json/prompts.json")
    scenes = [
        {"visual_prompt": str((row or {}).get("prompt") or "")}
        for row in rows if isinstance(row, dict)
    ]
    raw = _json_bytes(scenes)
    style = str((prompts or {}).get("style") or "")
    return scenes, {
        "plan_version": "legacy-prompts-no-refs-v1",
        "model": str(GOOGLER_MODEL).upper(),
        "style": style,
        "scene_count": len(scenes),
        "scenes_sha256": hashlib.sha256(raw).hexdigest(),
        "analyze_story": False,
        "auto_hero": False,
        "manual_identity_present": False,
        "story_identity_present": False,
        "auto_hero_prompt_required": False,
        "prompt_artifacts": {
            "subject_anchor": {}, "story_bible": None, "hero_reference": None,
        },
        "legacy_no_refs": True,
    }, style


def _canonical_regen_output_path(project_dir: str, image_file: str) -> Tuple[str, str, int]:
    """Resolve only the canonical ``project/googler/{N:03d}.jpg`` target."""
    raw = str(image_file or "")
    if not raw or "\x00" in raw:
        raise ScenePlanTerminalError("regen image filename must be canonical NNN.jpg")
    project_root = os.path.realpath(os.path.abspath(str(project_dir)))
    googler_dir = os.path.join(project_root, GOOGLER_IMAGES_DIR)
    if os.path.lexists(googler_dir) and os.path.islink(googler_dir):
        raise ScenePlanTerminalError("regen googler directory must not be a symlink")
    canonical_dir = os.path.realpath(googler_dir)
    try:
        if os.path.commonpath([project_root, canonical_dir]) != project_root:
            raise ScenePlanTerminalError("regen googler directory escapes project")
    except ValueError as exc:
        raise ScenePlanTerminalError("regen googler directory escapes project") from exc

    if os.path.isabs(raw):
        supplied_path = os.path.abspath(raw)
        filename = os.path.basename(supplied_path)
    else:
        # A relative CLI value is a filename, not another path language.
        if raw != os.path.basename(raw) or "/" in raw or "\\" in raw:
            raise ScenePlanTerminalError(
                "regen image_file must be a bare canonical filename"
            )
        filename = raw
        supplied_path = os.path.join(canonical_dir, filename)

    match = re.fullmatch(r"([0-9]{3,})\.jpg", filename)
    if not match:
        raise ScenePlanTerminalError("regen image filename must be canonical NNN.jpg")
    frame_number = int(match.group(1))
    if frame_number <= 0:
        raise ScenePlanTerminalError("regen image filename must be canonical NNN.jpg")
    canonical_name = f"{frame_number:03d}.jpg"
    if filename != canonical_name:
        raise ScenePlanTerminalError("regen image filename must be canonical NNN.jpg")
    expected = os.path.join(canonical_dir, canonical_name)
    if supplied_path != expected:
        raise ScenePlanTerminalError("regen image path does not match project/googler")
    if os.path.lexists(expected) and os.path.islink(expected):
        raise ScenePlanTerminalError("regen image target must not be a symlink")
    if os.path.realpath(expected) != expected:
        raise ScenePlanTerminalError("regen image target escapes project/googler")
    try:
        if os.path.commonpath([canonical_dir, expected]) != canonical_dir:
            raise ScenePlanTerminalError("regen image target escapes project/googler")
    except ValueError as exc:
        raise ScenePlanTerminalError("regen image target escapes project/googler") from exc
    return canonical_dir, expected, frame_number - 1


@_serialize_project_image_mutation()
def cmd_regen_image(args):
    """Regenerate one frame with the same immutable reference contract as pipeline."""
    googler_dir, output_path, scene_index = _canonical_regen_output_path(
        args.project_dir, args.image_file
    )
    filename = os.path.basename(output_path)
    # Preserve the command-wide provider credential preflight before validating
    # plan-specific switches.  This is read-only, spends no credit, and keeps
    # every image command deterministic under isolated server credentials.
    _assert_image_generation_key(getattr(args, "model", None) or GOOGLER_MODEL)
    requested_analyze_story = getattr(args, "analyze_story", None)
    if not isinstance(requested_analyze_story, bool):
        raise ScenePlanTerminalError(
            "regen requires exactly one current setting: "
            "--analyze_story or --no_analyze_story"
        )

    manifest_raw = getattr(args, "reference_manifest", None)
    legacy_user_raw = getattr(args, "user_refs", None)
    legacy_style_raw = getattr(args, "style_refs", None)
    manifest_provided = manifest_raw is not None
    legacy_user_provided = legacy_user_raw is not None
    legacy_style_provided = legacy_style_raw is not None
    manifest_path = str(manifest_raw or "").strip()
    if manifest_provided and not manifest_path:
        raise ref_contract.ReferenceContractError(
            "--reference_manifest was provided without a manifest path"
        )
    if manifest_provided and (legacy_user_provided or legacy_style_provided):
        raise ref_contract.ReferenceContractError(
            "--reference_manifest cannot be combined with legacy reference flags"
        )
    explicit_legacy_sources = legacy_user_provided or legacy_style_provided
    allow_legacy_no_refs = bool(
        requested_analyze_story is False
        and not manifest_provided
        and explicit_legacy_sources
        and not str(legacy_user_raw or "").strip()
        and not str(legacy_style_raw or "").strip()
        and not bool(getattr(args, "auto_hero", False))
    )

    scenes, plan_meta, plan_style = _load_regen_plan(
        args.project_dir, allow_legacy_no_refs=allow_legacy_no_refs
    )
    if scene_index >= len(scenes):
        raise ScenePlanTerminalError(
            f"scene {scene_index + 1} is outside the saved plan ({len(scenes)})"
        )
    effective_scenes = [dict(row) for row in scenes]
    custom_prompt = str(getattr(args, "prompt", None) or "").strip()
    if custom_prompt:
        effective_scenes[scene_index]["visual_prompt"] = custom_prompt
    scene_data = effective_scenes[scene_index]
    if not (scene_data.get("visual_prompt") or scene_data.get("visual_description")):
        raise ScenePlanTerminalError(f"prompt for {filename} is empty")

    style = str(getattr(args, "style", None) or plan_style or "").strip()
    aspect_ratio = str(getattr(args, "aspect_ratio", "landscape") or "landscape")
    if getattr(args, "seed", None) is not None:
        fixed_seed = args.seed
    else:
        fixed_seed = int(hashlib.md5(style.encode("utf-8")).hexdigest()[:8], 16) % 1000000 + 1

    requested_auto_hero = bool(getattr(args, "auto_hero", False))
    plan_analyze_story = plan_meta.get("analyze_story")
    if not isinstance(plan_analyze_story, bool):
        raise ScenePlanTerminalError("regen plan has no explicit analyze_story setting")
    runtime_changed = bool(
        manifest_provided or explicit_legacy_sources or plan_meta.get("legacy_no_refs")
    )
    if requested_analyze_story and not plan_analyze_story:
        raise ScenePlanTerminalError(
            "analyze_story включён для regen, но plan bundle создан без него; "
            "сначала пересоздайте план"
        )
    if (
        not requested_analyze_story
        and plan_analyze_story
        and not runtime_changed
    ):
        raise ScenePlanTerminalError(
            "analyze_story выключен, но сохранённый план содержит story refs; "
            "передайте текущий reference manifest (включая пустой) для безопасной пересборки"
        )
    artifacts = plan_meta.get("prompt_artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    saved_bible = artifacts.get("story_bible")
    bible = (
        _validate_story_bible(saved_bible)
        if requested_analyze_story and isinstance(saved_bible, dict)
        else None
    )
    if requested_analyze_story and bible is None:
        raise ScenePlanTerminalError(
            "analyze_story включён, но plan bundle не содержит story_bible; требуется replan"
        )
    subject_anchor = artifacts.get("subject_anchor")
    subject_anchor = subject_anchor if isinstance(subject_anchor, dict) else {}
    saved_hero_prepared = artifacts.get("hero_reference")
    saved_hero_prepared = (
        saved_hero_prepared if isinstance(saved_hero_prepared, dict) else None
    )
    if requested_auto_hero and plan_meta.get("auto_hero") is not True:
        raise ScenePlanTerminalError(
            "auto_hero включён для regen, но план создан без него; "
            "сначала пересоздайте plan bundle"
        )

    if runtime_changed:
        sources = (
            _load_reference_manifest(manifest_path) if manifest_path
            else _snapshot_legacy_reference_sources(
                args.project_dir, legacy_user_raw, legacy_style_raw
            )
        )
        auto_required_now = bool(
            requested_auto_hero
            and not sources.get("characters")
            and not (bible and bible.get("characters"))
        )
        if auto_required_now and (
            plan_meta.get("auto_hero_prompt_required") is not True
            or not isinstance(saved_hero_prepared, dict)
            or not str(saved_hero_prepared.get("prompt") or "").strip()
        ):
            raise ScenePlanTerminalError(
                "текущий regen требует auto-hero, но в plan bundle нет "
                "согласованного hero prompt; требуется replan"
            )
        if custom_prompt and len(sources.get("characters") or []) > 1:
            raise ref_contract.ReferenceContractError(
                "custom regen prompt cannot bind multiple manual heroes; select one named hero"
            )
        custom_project_identity = bool(
            plan_meta.get("plan_version") == "custom-prompts-v1"
            and not bible
            and len(sources.get("characters") or []) == 1
        )
        manual_continuity_claim = bool(
            requested_auto_hero
            and not bible
            and len(sources.get("characters") or []) == 1
        )
        single_subject = bool(
            subject_anchor.get("single_subject") and subject_anchor.get("main_subject")
        ) or custom_project_identity or manual_continuity_claim
        reference_runtime = _prepare_reference_runtime(
            args.project_dir, effective_scenes, GOOGLER_MODEL, style, bible, sources,
            single_subject=single_subject,
            hero_subject=subject_anchor.get("main_subject") if single_subject else None,
            auto_hero=requested_auto_hero,
            hero_prepared=saved_hero_prepared if requested_auto_hero else None,
            force_manual_scene_index=(
                scene_index
                if custom_project_identity and custom_prompt
                else None
            ),
            plan_scenes_sha256=plan_meta["scenes_sha256"],
            persist=False,
        )
    else:
        reference_runtime = _load_reference_runtime(args.project_dir)
        if reference_runtime.get("scenes_sha256") != plan_meta.get("scenes_sha256"):
            raise ref_contract.ReferenceContractError(
                "saved reference runtime belongs to a different scene plan"
            )
        if len(reference_runtime.get("scenes") or []) != len(effective_scenes):
            raise ref_contract.ReferenceContractError(
                "saved reference runtime does not match the scene plan"
            )
        if custom_prompt:
            identity_refs = [
                row for row in reference_runtime["scenes"][scene_index].get("selected", [])
                if row.get("kind") != "style"
            ]
            if len(identity_refs) > 1:
                raise ref_contract.ReferenceContractError(
                    "custom regen prompt is ambiguous with multiple identity references"
                )
        runtime_has_auto = any(
            row.get("kind") == "auto_hero"
            for scene_bundle in (reference_runtime.get("scenes") or [])
            for row in (scene_bundle.get("selected") or [])
        )
        runtime_has_story = any(
            row.get("kind") == "story_character"
            for scene_bundle in (reference_runtime.get("scenes") or [])
            for row in (scene_bundle.get("audit") or [])
        )
        if runtime_has_story and not requested_analyze_story:
            raise ref_contract.ReferenceContractError(
                "saved runtime contains story refs but current analyze_story is off; "
                "pass the current reference manifest to rebuild it"
            )
        if runtime_has_auto and not requested_auto_hero:
            raise ref_contract.ReferenceContractError(
                "saved runtime contains auto-hero but current regen toggle is off; "
                "pass the current reference manifest so runtime can be rebuilt without it"
            )
        runtime_has_manual = any(
            row.get("kind") == "manual_character"
            for scene_bundle in (reference_runtime.get("scenes") or [])
            for row in (scene_bundle.get("audit") or [])
        )
        if (
            requested_auto_hero
            and not runtime_has_auto
            and not runtime_has_manual
            and not (bible and bible.get("characters"))
        ):
            raise ref_contract.ReferenceContractError(
                "current auto-hero is absent from saved runtime; pass the current "
                "reference manifest after rebuilding the matching plan"
            )

    bundle = reference_runtime["scenes"][scene_index]
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    runtime_path = os.path.join(googler_dir, REFERENCE_RUNTIME_FILE)
    receipt_file_path = os.path.join(googler_dir, IMAGE_RECEIPTS_FILE)
    original_output = Path(output_path).read_bytes() if os.path.isfile(output_path) else None
    original_runtime = Path(runtime_path).read_bytes() if os.path.isfile(runtime_path) else None
    original_receipt = Path(receipt_file_path).read_bytes() if os.path.isfile(receipt_file_path) else None
    commit_started = False
    fd, temporary_output = tempfile.mkstemp(
        prefix=f".{filename}.regen.", suffix=".tmp", dir=os.path.dirname(output_path)
    )
    os.close(fd)
    os.unlink(temporary_output)
    try:
        prompt_preview = scene_data.get("visual_prompt") or scene_data.get("visual_description") or ""
        print_user(f"🔄 Перегенерация {filename}: {prompt_preview[:120]}", "PROCESS")
        _idx, image_data = generate_image(
            scene_data=scene_data,
            style=style,
            output_path=temporary_output,
            aspect_ratio=aspect_ratio,
            seed=fixed_seed,
            index=scene_index,
            reference_bundle=bundle,
        )
        if image_data is None or not save_base64_to_file(
            image_data, temporary_output, validate_image=True
        ):
            _account_limit_raise_if_hit(GOOGLER_MODEL, 0, 1)
            raise RuntimeError("provider result did not pass local image validation")
        commit_started = True
        os.replace(temporary_output, output_path)
        temporary_output = None

        if runtime_changed:
            _atomic_write_json(runtime_path, reference_runtime)
        receipt_path, receipts, _fingerprints = _prepare_image_receipts(
            args.project_dir, reference_runtime, effective_scenes, style,
            aspect_ratio, fixed_seed,
            scene_plan_sha256=plan_meta["scenes_sha256"], persist=False,
        )
        _mark_receipt_success(receipt_path, receipts, f"{scene_index + 1:03d}", output_path)
        print_user(
            f"✅ Перегенерировано: {filename}; refs {bundle.get('used', 0)}/{bundle.get('capacity', 0)}",
            "SUCCESS",
        )
    except Exception:
        if commit_started:
            for path, original in (
                (output_path, original_output),
                (runtime_path, original_runtime),
                (receipt_file_path, original_receipt),
            ):
                try:
                    if original is None:
                        if os.path.isfile(path):
                            os.unlink(path)
                    else:
                        _atomic_write_bytes(path, original)
                except Exception as rollback_error:
                    print_debug(f"regen rollback failed for {path}: {rollback_error}")
        print_error_user_friendly(
            f"Не удалось перегенерировать {filename} — старый кадр и receipt сохранены"
        )
        raise
    finally:
        if temporary_output:
            try:
                os.unlink(temporary_output)
            except FileNotFoundError:
                pass


def cmd_generate_videos(args):
    """Команда: генерация видео из изображений"""
    print_user("Начинаю генерацию видео...", "PROCESS")
    
    # Предупреждение о deprecated параметре
    if getattr(args, 'loop', False):
        print_debug("Параметр --loop больше не поддерживается в API v2")
    
    output_dir = os.path.join(args.project_dir, GOOGLER_VIDEO_DIR)

    # t2v из промптов сцен — отдельная ветка, картинки не нужны
    if getattr(args, 'from_prompts', False):
        return _cmd_generate_videos_from_prompts(args, output_dir)

    if args.image_paths_file:
        with open(args.image_paths_file, 'r', encoding='utf-8', errors='replace') as f:
            image_paths = json.load(f)
        
        # Сохраняем список изображений, использованных для генерации видео, чтобы исключить их из рендера.
        # MERGE (union), не перезапись: частичный прогон (перегенерация одного клипа) не должен
        # затирать список — иначе рендер перестанет исключать остальные заменённые картинки.
        video_refs_file = os.path.join(args.project_dir, "googler_video_references.json")
        try:
            old_refs = []
            if os.path.exists(video_refs_file):
                try:
                    with open(video_refs_file, 'r', encoding='utf-8', errors='replace') as f:
                        old_refs = json.load(f) or []
                except Exception:
                    old_refs = []
            merged = list(dict.fromkeys([os.path.normpath(p) for p in list(old_refs) + list(image_paths)]))
            with open(video_refs_file, 'w', encoding='utf-8', errors='replace') as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            print_debug(f"Список референсов для видео сохранен (merge {len(old_refs)}+{len(image_paths)}→{len(merged)}): {video_refs_file}")
        except Exception as e:
            print_debug(f"Не удалось сохранить список референсов: {e}")
    else:
        images_dir = os.path.join(args.project_dir, GOOGLER_IMAGES_DIR)
        if not os.path.exists(images_dir):
            print_error_user_friendly(f"Папка не найдена: {images_dir}")
            return
        image_paths = sorted([
            os.path.join(images_dir, f) 
            for f in os.listdir(images_dir) 
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
        ])
    
    if not image_paths:
        print_error_user_friendly("Не найдено изображений для генерации видео!")
        return
    
    print_user(f"Изображений для обработки: {len(image_paths)}", "INFO")
    
    if not args.prompt:
        print_error_user_friendly("Не указан промт для видео! Используйте --prompt")
        return
    
    if args.use_cache and os.path.exists(output_dir):
        # Докачиваем ТОЛЬКО недостающие (раньше: пропуск только если готовы ВСЕ — рестарт после
        # обрыва перегенерировал всё заново). Вместе с сохранением по-готовности это даёт resume.
        existing = {f for f in os.listdir(output_dir) if f.endswith('.mp4')}
        if existing:
            _before = len(image_paths)
            image_paths = [p for p in image_paths
                           if f"{os.path.splitext(os.path.basename(p))[0]}_video.mp4" not in existing]
            _skipped = _before - len(image_paths)
            if _skipped:
                print_user(f"Кэш видео: {_skipped} уже готовы, догенерирую {len(image_paths)}", "INFO")
            if not image_paths:
                print_user(f"Все видео уже в кэше ({len(existing)}). Пропускаем генерацию.", "INFO")
                return
    
    # Примечание: параметр loop больше не поддерживается
    use_legacy_flag = getattr(args, 'use_legacy', False)
    video_threads = getattr(args, 'video_threads', None)
    saved = generate_videos_batch(args.prompt, image_paths, output_dir, use_legacy=use_legacy_flag, video_threads=video_threads)
    if not saved and image_paths:
        # 0 из N — полный провал: честный rc=1, а не «✅ завершено, сохранено 0» (ложный done у сервера).
        # Частичный результат (saved>0) остаётся rc=0 — continue-on-error.
        print_error_user_friendly(f"Ни одно видео не сгенерировано (0 из {len(image_paths)}) — провайдер отказал, детали в ошибках выше")
        sys.exit(1)
    print_user(f"Генерация видео завершена! Сохранено: {len(saved)}", "SUCCESS")


SCENE_PLAN_VERSION = 3


def _plan_contract_fields(args, full_text: str) -> dict:
    _split_enabled = bool(getattr(args, "split_long_sentences", False))
    _spp = str(getattr(args, "sentences_per_prompt", "") or "")
    _semantic_enabled = _semantic_split_enabled(_spp, _split_enabled)
    _split_seconds = (_validate_solo_scene_seconds(getattr(
        args, "max_solo_scene_seconds", DEFAULT_MAX_SOLO_SCENE_SECONDS
    )) if _split_enabled else DEFAULT_MAX_SOLO_SCENE_SECONDS)
    source_bytes = (full_text or "").encode("utf-8", "replace")
    return {
        "plan_version": SCENE_PLAN_VERSION,
        "model": str(getattr(args, "model", "") or GOOGLER_MODEL),
        "style": str(getattr(args, "style", "") or ""),
        "sentences_per_prompt": _spp,
        "split_long": _split_enabled,
        "split_long_algorithm": (
            SPLIT_LONG_ALGORITHM_VERSION if _semantic_enabled
            else LEGACY_SPLIT_LONG_ALGORITHM_VERSION
        ) if _split_enabled else None,
        "max_solo_scene_seconds": _split_seconds if _split_enabled else None,
        "max_solo_scene_chars": _solo_scene_chars(_split_seconds) if _split_enabled else None,
        "analyze_story": bool(getattr(args, "analyze_story", False)),
        "smart_mode": bool(getattr(args, "smart_mode", False)),
        "auto_hero": bool(getattr(args, "auto_hero", False)),
        "source_sha1": hashlib.sha1(source_bytes).hexdigest(),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
    }


def _build_plan_meta(args, full_text: str, scene_bytes: bytes, scene_count: int,
                     prompt_artifacts: Optional[dict] = None,
                     reference_dependencies: Optional[dict] = None) -> dict:
    artifacts = dict(prompt_artifacts or {})
    story = artifacts.get("story_bible")
    dependencies = dict(reference_dependencies or {})
    manual_identity_present = bool(dependencies.get("manual_identity_present", False))
    story_identity_present = bool(
        dependencies.get(
            "story_identity_present",
            isinstance(story, dict) and bool(story.get("characters")),
        )
    )
    auto_hero_prompt_required = bool(
        dependencies.get(
            "auto_hero_prompt_required",
            bool(getattr(args, "auto_hero", False))
            and not manual_identity_present
            and not story_identity_present,
        )
    )
    meta = {
        **_plan_contract_fields(args, full_text),
        "scene_count": int(scene_count),
        "scenes_sha256": hashlib.sha256(scene_bytes).hexdigest(),
        "manual_identity_present": manual_identity_present,
        "story_identity_present": story_identity_present,
        "auto_hero_prompt_required": auto_hero_prompt_required,
        "prompt_artifacts": artifacts,
    }
    return meta


def _publish_scene_plan(output_dir: str, args, full_text: str, scenes: List[dict],
                        prompt_artifacts: Optional[dict] = None,
                        reference_dependencies: Optional[dict] = None) -> dict:
    """Логически атомарная публикация пары: scenes first, meta commit-marker last.

    Digest/count в meta делают промежуточное состояние непринимаемым: после crash
    consumer увидит либо прежний согласованный bundle, либо digest mismatch, но
    никогда не запустит картинки по новой/старой split-brain паре.
    """
    scene_bytes = _json_bytes(scenes)
    meta = _build_plan_meta(
        args, full_text, scene_bytes, len(scenes), prompt_artifacts,
        reference_dependencies,
    )
    _atomic_write_bytes(os.path.join(output_dir, "scenes.json"), scene_bytes)
    _atomic_write_json(os.path.join(output_dir, "scenes.meta.json"), meta)
    return meta


def _canon_names_from_bible(bible: Optional[dict]) -> List[str]:
    if not isinstance(bible, dict):
        return []
    characters = [c for c in bible.get("characters", [])
                  if isinstance(c, dict) and c.get("names")]
    locations = [loc for loc in bible.get("locations", [])
                 if isinstance(loc, dict) and loc.get("names")]
    return [c["names"][0] for c in characters] + [loc["names"][0] for loc in locations]


def _load_plan_bundle(output_dir: str, args, full_text: str):
    """Один coherent read/validation для ``--use_plan`` → (scenes, artifacts, meta)."""
    meta_path = os.path.join(output_dir, "scenes.meta.json")
    scenes_path = os.path.join(output_dir, "scenes.json")
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        with open(scenes_path, "rb") as f:
            scene_bytes = f.read()
        scenes = json.loads(scene_bytes.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise ScenePlanTerminalError(f"готовый plan bundle не читается: {exc}") from exc
    want = _plan_contract_fields(args, full_text)
    # "model" СОЗНАТЕЛЬНО не сверяется: модель картинок не меняет текст промптов.
    want.pop("model", None)
    legacy_semantic_v3 = bool(
        isinstance(meta, dict)
        and want.get("split_long_algorithm") == SPLIT_LONG_ALGORITHM_VERSION
        and meta.get("split_long_algorithm") == 3
        and all(
            meta.get(key) == value
            for key, value in want.items()
            if key != "split_long_algorithm"
        )
    )
    if (not isinstance(meta, dict)
            or (not legacy_semantic_v3
                and any(meta.get(key) != value for key, value in want.items()))):
        raise ScenePlanTerminalError("готовый план устарел или создан с другими параметрами")
    if not isinstance(scenes, list) or not scenes:
        raise ScenePlanTerminalError("готовый scenes.json пуст или не является списком")
    if meta.get("scene_count") != len(scenes):
        raise ScenePlanTerminalError("scene_count не совпадает с scenes.json")
    if meta.get("scenes_sha256") != hashlib.sha256(scene_bytes).hexdigest():
        raise ScenePlanTerminalError("digest scenes.json не совпадает с commit-marker")
    artifacts = meta.get("prompt_artifacts")
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get("subject_anchor"), dict):
        raise ScenePlanTerminalError("готовый план не содержит обязательные prompt_artifacts")

    _split_enabled = bool(getattr(args, "split_long_sentences", False))
    _semantic_enabled = _semantic_split_enabled(
        getattr(args, "sentences_per_prompt", 3), _split_enabled
    )
    if bool(getattr(args, "smart_mode", False)):
        checked = _validate_smart_scene_plan(
            scenes, full_text,
            canon_names=_canon_names_from_bible(artifacts.get("story_bible")),
        )
    elif _semantic_enabled:
        checked = _validate_semantic_scene_plan(
            scenes, full_text,
            _canon_names_from_bible(artifacts.get("story_bible")),
            getattr(args, "sentences_per_prompt", 3), True,
            getattr(args, "max_solo_scene_seconds", DEFAULT_MAX_SOLO_SCENE_SECONDS),
            enforce_cadence=not legacy_semantic_v3,
        )
    else:
        text_chunks = _build_scene_text_chunks(
            full_text,
            sentences_per_prompt=getattr(args, "sentences_per_prompt", 3),
            split_long=bool(getattr(args, "split_long_sentences", False)),
            max_solo_scene_seconds=getattr(
                args, "max_solo_scene_seconds", DEFAULT_MAX_SOLO_SCENE_SECONDS
            ),
        )
        checked = _validate_complete_scene_plan(
            scenes, text_chunks,
            _canon_names_from_bible(artifacts.get("story_bible")),
        )
    return checked, artifacts, meta


def _load_plan_artifacts(output_dir: str, args, full_text: str) -> dict:
    """Compatibility wrapper; validation всё равно читает coherent bundle."""
    _scenes, artifacts, _meta = _load_plan_bundle(output_dir, args, full_text)
    return artifacts


def _load_ready_plan(output_dir: str, args=None, full_text: Optional[str] = None) -> Optional[List[dict]]:
    """Готовый строго проверенный plan bundle; legacy v2 fail-closed."""
    if args is None or full_text is None:
        raise ScenePlanTerminalError("для проверки готового плана нужны args и исходный текст")
    scenes, _artifacts, _meta = _load_plan_bundle(output_dir, args, full_text)
    return scenes


@_serialize_project_image_mutation()
def cmd_full_pipeline(args):
    """
    Команда: полный пайплайн с новой логикой (план сцен → генерация изображений)
    Поддерживает авто-героя и пользовательские референсы
    """
    print_user("🎬 Запуск полного пайплайна с планированием сцен...", "PROCESS")
    print_debug("="*60)

    # Определяем режим до любых ранних проверок: plan-only не имеет права вернуть rc=0,
    # если полный план не был опубликован.
    plan_only = bool(getattr(args, 'plan_only', False))
    use_plan = bool(getattr(args, 'use_plan', False))
    if plan_only and use_plan:
        raise ScenePlanTerminalError("--plan_only и --use_plan взаимоисключающие")
    if (bool(getattr(args, 'smart_mode', False))
            and bool(getattr(args, 'split_long_sentences', False))):
        raise ScenePlanTerminalError(
            "--smart_mode и --split_long_sentences взаимоисключающие режимы планирования"
        )

    # Читаем исходный текст
    source_file = args.source_file
    if not source_file or not os.path.exists(source_file):
        raise ScenePlanTerminalError(f"Файл не найден: {source_file}")
    
    with open(source_file, 'r', encoding='utf-8', errors='replace') as f:
        full_text = f.read()
    
    if not full_text.strip():
        raise ScenePlanTerminalError("Файл пуст — план сцен не создан")
    
    project_dir = args.project_dir
    output_dir = _confined_project_directory(project_dir, GOOGLER_IMAGES_DIR)
    
    style = args.style
    aspect_ratio = args.aspect_ratio
    
    # --plan_only: пишем ТОЛЬКО план сцен (LLM) и выходим — картинки рисует отдельная задача.
    # --use_plan: берём готовый scenes.json от такой задачи вместо повторной работы LLM.
    # Так этапы разных проектов идут конвейером (промпты одного ‖ картинки другого), см. tasks/prompts_queue_plan.md.
    if not use_plan:
        # Prompt-job (либо legacy монолитный pipeline) обязан иметь свой LLM-канал.
        _assert_openai_key()
    if use_plan:
        scenes, prompt_artifacts, _loaded_plan_meta = _load_plan_bundle(
            output_dir, args, full_text
        )
    else:
        scenes, prompt_artifacts, _loaded_plan_meta = None, {}, None
    try:
        _pipeline_image_threads = int(getattr(args, "image_threads", None) or GOOGLER_IMAGE_THREADS)
    except (TypeError, ValueError):
        _pipeline_image_threads = GOOGLER_IMAGE_THREADS
    _pipeline_image_threads = max(1, min(25, _pipeline_image_threads))

    # These three analyses depend only on the immutable source/config and may run
    # together. Their nested requests share _LLM_CONCURRENCY_GATE, so aggregate
    # live calls never exceed VS_SCENE_THREADS. Phase 2 starts only after every
    # future has joined; no partial scene bundle can be published.
    smart_mode = bool(getattr(args, 'smart_mode', False))
    sentences_per_prompt = getattr(args, 'sentences_per_prompt', 3)
    _split_long = bool(getattr(args, 'split_long_sentences', False))
    _split_seconds = getattr(
        args, 'max_solo_scene_seconds', DEFAULT_MAX_SOLO_SCENE_SECONDS
    )
    _semantic_mode = _semantic_split_enabled(sentences_per_prompt, _split_long)
    _artifact_cadence = {
        "sentences_per_prompt": str(sentences_per_prompt),
        "split_long": _split_long,
        "max_solo_scene_seconds": (
            float(_split_seconds) if _split_long else None
        ),
        "smart_mode": smart_mode,
    }
    _precomputed_semantic_beats = _PRECOMPUTE_MISSING
    _precomputed_visual_context = _PRECOMPUTE_MISSING
    _precomputed_story_bible = _PRECOMPUTE_MISSING
    _use_prompt_cache = bool(getattr(args, 'use_cache', False))

    if not use_plan and not smart_mode:
        def _precompute_visual_context():
            try:
                return _get_visual_context(
                    full_text, use_cache=_use_prompt_cache
                )
            except OpenAIKeyError:
                raise
            except Exception as exc:
                print_debug(
                    f"Visual Bible не сгенерён, чанки без story-контекста: {exc}"
                )
                return None

        precompute_jobs = {"visual_context": _precompute_visual_context}
        if getattr(args, 'analyze_story', False):
            precompute_jobs["story_bible"] = lambda: analyze_story(
                full_text,
                project_dir,
                use_cache=_use_prompt_cache,
                cadence=_artifact_cadence,
            )
        if _semantic_mode:
            precompute_jobs["semantic_beats"] = lambda: _plan_semantic_beats(
                full_text,
                _split_seconds,
                sentences_per_prompt=sentences_per_prompt,
                use_cache=_use_prompt_cache,
            )

        print_user(
            "⚡ Параллельно готовлю независимые анализы: "
            + ", ".join(precompute_jobs),
            "INFO",
        )
        with ThreadPoolExecutor(
            max_workers=min(
                len(precompute_jobs), max(1, GPT_SCENE_PARALLEL_THREADS)
            )
        ) as precompute_executor:
            precompute_futures = {
                name: precompute_executor.submit(job)
                for name, job in precompute_jobs.items()
            }
            # Explicit join is the phase-2 barrier. Values remain private until
            # every future is complete and the final scene plan passes validation.
            precomputed = {
                name: future.result()
                for name, future in precompute_futures.items()
            }
        _precomputed_visual_context = precomputed["visual_context"]
        if "story_bible" in precomputed:
            _precomputed_story_bible = precomputed["story_bible"]
        if "semantic_beats" in precomputed:
            _precomputed_semantic_beats = _validate_semantic_beat_plan(
                precomputed["semantic_beats"], full_text,
                sentences_per_prompt, True, _split_seconds,
            )

    # ШАГ 1: Анализ истории → персонажи+локации (story_bible) + реф-картинки персонажей (если включён)
    bible = None
    if getattr(args, 'analyze_story', False):
        print_user("🔍 ШАГ 1: Анализ текста → персонажи и локации...", "INFO")
        if use_plan:
            # Полный snapshot лежит в паспорте prompt-job: не читаем потенциально
            # устаревший cache и ни при каких условиях не вызываем LLM.
            bible = prompt_artifacts.get("story_bible")
            if bible is not None and not isinstance(bible, dict):
                raise RuntimeError("story_bible в prompt_artifacts повреждён")
        elif _precomputed_story_bible is not _PRECOMPUTE_MISSING:
            bible = _precomputed_story_bible
        else:
            bible = analyze_story(
                full_text,
                project_dir,
                use_cache=_use_prompt_cache,
                cadence=_artifact_cadence,
            )
        if bible is None:
            raise ScenePlanTransientError(
                "включённый analyze_story не вернул полный story_bible"
            )
        bible = _validate_story_bible(bible)
        # Image artifacts are intentionally deferred until *all* scenes have
        # passed the exact per-scene capacity preflight below.

    # ── Якорь субъекта: считаем ОДИН раз тут (для авто-героя) и переиспользуем в плане сцен.
    #    Активен только на дефолт-пути (нет story-bible); при мульти-протагонисте → single_subject=false.
    _subj_anchor: dict = dict(prompt_artifacts.get("subject_anchor") or {}) if use_plan else {}
    if not use_plan and not (bible and bible.get('characters')):
        try:
            _subj_anchor = extract_main_subject(full_text, title=os.path.basename(project_dir or '')) or {}
        except Exception as _e:
            print_debug(f"subject anchor (pipeline) err: {_e}")
            _subj_anchor = {}
    _is_single_subject = bool(_subj_anchor.get("single_subject") and _subj_anchor.get("main_subject"))
    _hero_subject = _subj_anchor.get("main_subject") if _is_single_subject else None
    _reference_sources = _load_reference_sources(args)
    _has_manual_hero = bool(_reference_sources.get("characters"))
    _hero_needed = bool(
        getattr(args, 'auto_hero', False)
        and not _has_manual_hero
        and not (bible and bible.get('characters'))
    )
    if use_plan:
        expected_dependencies = {
            "manual_identity_present": _has_manual_hero,
            "story_identity_present": bool(bible and bible.get("characters")),
            "auto_hero_prompt_required": _hero_needed,
        }
        if any(
            not isinstance(_loaded_plan_meta.get(key), bool)
            or _loaded_plan_meta.get(key) != expected
            for key, expected in expected_dependencies.items()
        ):
            raise ScenePlanTerminalError(
                "готовый план создан для другого состава "
                "manual/story/auto identity; требуется replan"
            )
    if getattr(args, 'auto_hero', False) and not _hero_needed:
        reason = "ручной референс героя" if _has_manual_hero else "story_bible"
        print_user(f"🎭 Авто-герой подавлен: активен {reason}", "INFO")
    if use_plan:
        _saved_hero_prepared = prompt_artifacts.get("hero_reference")
        if _hero_needed and not (
            isinstance(_saved_hero_prepared, dict)
            and str(_saved_hero_prepared.get("prompt") or "").strip()
        ):
            raise RuntimeError("готовый план не содержит промпт обязательного auto-hero")
        # An old prepared artifact is never implicitly revived when the current
        # explicit toggle is off or when manual/story identity suppresses auto.
        _hero_prepared = _saved_hero_prepared if _hero_needed else None
    else:
        _hero_prepared = None
        if _hero_needed:
            _hero_prepared = _prepare_hero_reference(
                full_text, style, subject_hint=_hero_subject
            )
            if not _hero_prepared:
                raise RuntimeError("prompt-job не смог подготовить обязательный auto-hero")

    # The actual auto/story images are generated by _prepare_reference_runtime,
    # after every scene is known and the whole project has passed preflight.

    # ШАГ 2: Генерация плана сцен.
    # КРИТИЧНО (фикс 27.06): если анализ дал персонажей (bible) — scene_plan ОБЯЗАН идти
    # канон-aware (canon=bible), иначе entities/ref-тег не проставляются и весь матчинг
    # деградирует до бэкапа. Раньше canon уходил только в редкий smart-fallback → теги не работали в бою.
    if scenes:
        print_user(f"⚡ План сцен готов заранее ({len(scenes)} сцен из scenes.json) — LLM не вызываю", "SUCCESS")
    elif smart_mode:
        print_user("🎬 Smart Cinematic Mode: Семантический анализ сцен...", "INFO")
        scenes = _internal_generate_smart_scene_plan(
            full_text, style=style, canon=bible
        )
    elif bible and bible.get('characters'):
        print_user("🎬 План сцен (ref-aware: канон персонажей/локаций активен)...", "INFO")
        scenes = generate_scene_plan(full_text, style=style,
                                     sentences_per_prompt=sentences_per_prompt, canon=bible,
                                     split_long=_split_long,
                                     max_solo_scene_seconds=_split_seconds,
                                     precomputed_semantic_beats=_precomputed_semantic_beats,
                                     precomputed_visual_context=_precomputed_visual_context,
                                     use_cache=_use_prompt_cache)
    else:
        print_user("⏱ Standard Uniform Mode: Линейное разбиение текста...", "INFO")
        scenes = generate_scene_plan(full_text, style=style, sentences_per_prompt=sentences_per_prompt, split_long=_split_long,
                                     max_solo_scene_seconds=_split_seconds,
                                     project_title=os.path.basename(project_dir or ''),
                                     precomputed_subject=_subj_anchor,
                                     precomputed_semantic_beats=_precomputed_semantic_beats,
                                     precomputed_visual_context=_precomputed_visual_context,
                                     use_cache=_use_prompt_cache)
    
    if not scenes:
        raise ScenePlanTerminalError("Не удалось создать полный план сцен")

    # Последний acceptance-gate находится в orchestration, а не только внутри
    # конкретного generator: monkeypatch/cache/Smart/use_plan не могут обойти его.
    # Persisted legacy plans may predate visible_text. Fresh director output must
    # make an explicit []/strings decision and therefore stays fail-closed.
    _require_visible_text = not use_plan
    if smart_mode:
        scenes = _validate_smart_scene_plan(
            scenes, full_text,
            require_visible_text=_require_visible_text,
            canon_names=_canon_names_from_bible(bible),
        )
    elif _semantic_split_enabled(sentences_per_prompt, _split_long):
        scenes = _validate_semantic_scene_plan(
            scenes, full_text, _canon_names_from_bible(bible),
            sentences_per_prompt, True, _split_seconds,
            require_visible_text=_require_visible_text,
        )
    else:
        scenes = _validate_complete_scene_plan(
            scenes,
            _build_scene_text_chunks(
                full_text,
                sentences_per_prompt=sentences_per_prompt,
                split_long=_split_long,
                max_solo_scene_seconds=_split_seconds,
            ),
            _canon_names_from_bible(bible),
            require_visible_text=_require_visible_text,
        )

    # ШАГ 2.5: детерминированный матчинг рефов на каждую сцену (если есть story_bible).
    # Reference authority is only the strict exact ``entities`` enum. Prose,
    # prefixes and appearance heuristics may never select a visual identity.
    if bible:
        _validate_explicit_alias_entity_coverage(scenes, bible)
        matched_total = 0
        for sc in scenes:
            mids = match_refs_for_scene(sc, sc.get('text_segment', ''), bible)
            sc['matched_refs'] = mids
            matched_total += len(mids)
        print_user(f"🔗 Матчинг рефов: {matched_total} привязок на {len(scenes)} сцен", "INFO")

    if not use_plan:
        # scenes.json пишется первым, meta с digest/count — последним commit-marker.
        # Любой crash между replace даёт digest mismatch и не запускает image-job.
        _publish_scene_plan(
            output_dir, args, full_text,
            scenes,
            prompt_artifacts={
                "subject_anchor": _subj_anchor,
                "story_bible": bible,
                "hero_reference": _hero_prepared,
            },
            reference_dependencies={
                "manual_identity_present": _has_manual_hero,
                "story_identity_present": bool(bible and bible.get("characters")),
                "auto_hero_prompt_required": _hero_needed,
            },
        )
        print_user(
            f"✅ Полный plan bundle из {len(scenes)} сцен опубликован с digest/count",
            "SUCCESS",
        )

    if plan_only:
        # Задача «промпты» на этом закончена: план лежит в scenes.json, картинки нарисует
        # отдельная задача (она запустит этот же pipeline с --use_plan).
        print_user(f"✅ Промпты готовы: {len(scenes)} сцен в scenes.json (картинки — отдельной задачей)", "SUCCESS")
        print_llm_usage_summary(f"{len(scenes)} сцен")
        return

    _assert_image_generation_key(getattr(args, "model", None) or GOOGLER_MODEL)

    # One immutable runtime is the only source for initial, refill and regen.
    # It performs exact all-scene preflight before generating story/auto refs.
    reference_runtime = _prepare_reference_runtime(
        project_dir, scenes, GOOGLER_MODEL, style, bible, _reference_sources,
        single_subject=bool(
            _is_single_subject
            or (
                bool(getattr(args, "auto_hero", False))
                and not bible
                and len(_reference_sources.get("characters") or []) == 1
            )
        ),
        hero_subject=_hero_subject,
        auto_hero=bool(getattr(args, "auto_hero", False)),
        hero_prepared=_hero_prepared,
    )
    
    # ШАГ 5: Генерация изображений по сценам
    print_user(f"🎨 ШАГ 3: Генерация {len(scenes)} изображений...", "INFO")
    
    # Используем один и тот же seed для всех изображений, чтобы сохранить единый стиль
    # Если seed указан в аргументах, используем его, иначе вычисляем из стиля
    if hasattr(args, 'seed') and args.seed is not None:
        fixed_seed = args.seed
        print_user(f"🎲 Используется фиксированный seed: {fixed_seed}", "INFO")
    else:
        import hashlib
        style_hash = int(hashlib.md5(style.encode('utf-8')).hexdigest()[:8], 16) % 1000000
        fixed_seed = style_hash + 1  # Один seed для всех изображений проекта
        print_user(f"🎲 Автоматический seed из стиля: {fixed_seed}", "INFO")

    receipt_path, image_receipts, _frame_fingerprints = _prepare_image_receipts(
        project_dir, reference_runtime, scenes, style, aspect_ratio, fixed_seed,
        trust_legacy_cache=bool(getattr(args, "trust_legacy_cache", False)),
        use_cache=bool(getattr(args, "use_cache", False)),
    )
    if not getattr(args, "use_cache", False):
        _invalidate_receipts_for_regeneration(
            receipt_path, image_receipts,
            [f"{index + 1:03d}" for index in range(len(scenes))],
        )
    if getattr(args, "use_cache", False):
        all_fresh = all(
            _receipt_cache_hit(
                image_receipts, f"{index + 1:03d}",
                os.path.join(output_dir, f"{index + 1:03d}.jpg"),
            )
            for index in range(len(scenes))
        )
        if all_fresh:
            reconciliation = _reconcile_authoritative_image_set(
                project_dir, receipt_path, image_receipts,
                [f"{index + 1:03d}.jpg" for index in range(len(scenes))],
            )
            if reconciliation["quarantined"]:
                print_user(
                    f"Старых нумерованных кадров перемещено в карантин: "
                    f"{reconciliation['quarantined']}",
                    "INFO",
                )
            print_user(
                f"✅ Кэш подтверждён receipts для {len(scenes)} кадров; "
                f"bundle {reference_runtime['bundle_id'][:12]}",
                "INFO",
            )
            return
    
    saved_files = []
    total = len(scenes)
    batch_start = time.time()
    print_progress(0, total, "изображений", show_bar=True)
    
    # Используем переданное значение или значение по умолчанию
    threads = _pipeline_image_threads
    print_debug(f"Параллельных потоков для изображений: {threads}")
    
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {}
        for i, scene in enumerate(scenes):
            output_path = os.path.join(output_dir, f"{i+1:03d}.jpg")

            frame_key = f"{i + 1:03d}"
            if (
                getattr(args, "use_cache", False)
                and _receipt_cache_hit(image_receipts, frame_key, output_path)
            ):
                print_debug(f"[{i+1}] Пропущено (receipt=fresh): {os.path.basename(output_path)}")
                saved_files.append(output_path)
                continue

            future = executor.submit(
                generate_image,
                scene_data=scene,
                style=style,
                output_path=output_path,
                aspect_ratio=aspect_ratio,
                seed=fixed_seed,
                index=i,
                use_legacy=getattr(args, "use_legacy", False),
                full_text=full_text if smart_mode else None,
                reference_bundle=reference_runtime["scenes"][i],
            )
            futures[future] = i
        
        completed = 0
        for future in as_completed(futures):
            index = futures[future]
            try:
                index, image_data = future.result()
                if image_data is not None:
                    output_path = os.path.join(output_dir, f"{index+1:03d}.jpg")
                    # Re-save through the verified atomic writer.  Some provider
                    # branches return data even when their first local write failed.
                    if save_base64_to_file(image_data, output_path):
                        saved_files.append(output_path)
                        _mark_receipt_success(
                            receipt_path, image_receipts, f"{index + 1:03d}", output_path
                        )
                        print_debug(f"[{index+1}] Файл+receipt сохранены: {os.path.basename(output_path)}")
                    else:
                        print_user(f"⚠️ [{index+1}] Не удалось проверить/сохранить файл: {os.path.basename(output_path)}", "WARNING")
                else:
                    if index is not None:
                        print_debug(f"[{index+1}] image_data is None - изображение не сгенерировано")
                completed += 1
                
                # Обновляем прогресс
                if completed % max(1, total // 20) == 0 or completed == total:
                    print_progress(completed, total, "изображений", show_bar=True)
                    print_eta(completed, total, batch_start, "изображений")
                    
            except Exception as e:
                index_str = f"[{index+1}]" if index is not None else "[?]"
                print_user(f"❌ Ошибка в потоке генерации {index_str}: {e}", "ERROR")
                print_debug(f"Ошибка в потоке генерации: {e}")
                import traceback
                print_debug(traceback.format_exc())
                completed += 1
                
                # Примечание: В Smart Mode мы используем только уже существующие изображения из кэша
                # как референсы, так как при параллельной генерации новые изображения еще не готовы
    
    # ДОБИВКА ПРОПУЩЕННЫХ КАДРОВ (2026-07-26). Провайдер роняет сцены пачками: одна
    # 15-секундная просадка = 16 подряд идущих номеров без файла (волна 7, Dosarele_24/25:
    # «Все 12 попыток исчерпаны … v6 генерация не удалась» в одну минуту на 200-215/242-257).
    # Рендер отдаёт время сцены без картинки ПРЕДЫДУЩЕЙ существующей
    # (render_worker._collapse_missing_images) → дыра = застывший кадр на минуты.
    # Поэтому перед финалом добиваем пропуски ещё двумя раундами с паузой.
    # НО: если аккаунт провайдера исчерпан — добивка бессмысленна (её кадры отвалятся по метке),
    # только жжёт минуты паузами. Пропускаем и сразу идём к внятной ошибке задачи.
    for _refill_round in (() if _account_limit_hit(_model_provider_group(GOOGLER_MODEL)) else (1, 2)):
        _missing = [i for i in range(total)
                    if not _receipt_cache_hit(
                        image_receipts, f"{i + 1:03d}",
                        os.path.join(output_dir, f"{i + 1:03d}.jpg"),
                    )]
        if not _missing:
            break
        _prev = ", ".join(str(i + 1) for i in _missing[:12]) + ("…" if len(_missing) > 12 else "")
        print_user(f"🩹 Добивка пропущенных кадров (раунд {_refill_round}/2): {len(_missing)} шт — сцены {_prev}", "WARNING")
        time.sleep(15)   # просадки провайдера транзиентны — даём им закончиться
        with ThreadPoolExecutor(max_workers=max(1, min(4, threads))) as _ex2:
            _futs = {}
            for _i in _missing:
                _kw = dict(scene_data=scenes[_i], style=style,
                           output_path=os.path.join(output_dir, f"{_i+1:03d}.jpg"),
                           aspect_ratio=aspect_ratio, seed=fixed_seed, index=_i,
                           use_legacy=getattr(args, 'use_legacy', False),
                           reference_bundle=reference_runtime["scenes"][_i])
                if smart_mode:
                    _kw['full_text'] = full_text
                _futs[_ex2.submit(generate_image, **_kw)] = _i
            for _f in as_completed(_futs):
                _i = _futs[_f]
                try:
                    _idx, _data = _f.result()
                    _out = os.path.join(output_dir, f"{_idx+1:03d}.jpg")
                    if _data is not None and save_base64_to_file(_data, _out):
                        _mark_receipt_success(
                            receipt_path, image_receipts, f"{_idx + 1:03d}", _out
                        )
                        saved_files.append(_out)
                        print_user(f"🩹 [{_idx+1}] Пропущенный кадр добит", "SUCCESS")
                except Exception as _e:
                    print_debug(f"Добивка [{_i+1}] не удалась: {_e}")

    # ПОВТОР ПОЗЖЕ (владелец 05.10.2026): «попробуйте позже» и прочие сбои повторяем через
    # 15 минут, до трёх раз — но только когда недостаёт немного (не больше 2% ролика) и
    # аккаунт провайдера жив. Отказ фильтра после смягчений позже не повторяем: он уйдёт
    # соседнему кадру ниже. Место очереди картинок на это время занято — дешевле, чем
    # новый прогон, который ждал бы очереди часами.
    if not _account_limit_hit(_model_provider_group(GOOGLER_MODEL)):
        for _late_round in range(1, IMAGE_LATE_RETRY_ROUNDS + 1):
            _gaps_now = [i for i in range(total)
                         if not _receipt_cache_hit(
                             image_receipts, f"{i + 1:03d}",
                             os.path.join(output_dir, f"{i + 1:03d}.jpg"),
                         )]
            _later = late_retry_indices(_gaps_now)
            if not _later or not image_gaps_acceptable(_gaps_now, total):
                break
            print_user(f"⏳ Повтор позже (раунд {_late_round}/{IMAGE_LATE_RETRY_ROUNDS}): "
                       f"{len(_later)} кадр(ов) — сцены "
                       f"{', '.join(str(i + 1) for i in _later[:12])}; жду "
                       f"{IMAGE_LATE_RETRY_SEC // 60} мин", "WARNING")
            # Молча ждать нельзя: сервер считает gen-воркер без вывода дольше GEN_LEASE
            # (те же 15 мин) зависшим, убивает и запускает заново — с раунда 1, по кругу
            # (13.10.2026: Tichá Obloha и Zaklęta Knieja так держали очередь картинок 3 часа).
            _wake = time.time() + IMAGE_LATE_RETRY_SEC
            while time.time() < _wake:
                time.sleep(min(IMAGE_LATE_WAIT_PULSE_SEC, max(0.0, _wake - time.time())))
                if time.time() < _wake:
                    print_user(f"⏳ Жду повтора: ещё {int(_wake - time.time()) // 60 + 1} мин", "INFO")
            for _i in _later:
                _kw = dict(scene_data=scenes[_i], style=style,
                           output_path=os.path.join(output_dir, f"{_i+1:03d}.jpg"),
                           aspect_ratio=aspect_ratio, seed=fixed_seed, index=_i,
                           use_legacy=getattr(args, 'use_legacy', False),
                           reference_bundle=reference_runtime["scenes"][_i])
                if smart_mode:
                    _kw['full_text'] = full_text
                try:
                    _idx, _data = generate_image(**_kw)
                    _out = os.path.join(output_dir, f"{_idx+1:03d}.jpg")
                    if _data is not None and save_base64_to_file(_data, _out):
                        _mark_receipt_success(
                            receipt_path, image_receipts, f"{_idx + 1:03d}", _out
                        )
                        saved_files.append(_out)
                        print_user(f"✅ [{_idx+1}] Кадр получен при повторе позже", "SUCCESS")
                except Exception as _e:
                    print_debug(f"Повтор позже [{_i+1}] не удался: {_e}")

    # Финальный прогресс — по ФАКТУ сохранённого, а не «всегда 100%»
    # (раньше при 269/287 в лог всё равно уходило «100% — изображений: 287/287»,
    #  и потеря 18 кадров нигде не всплывала)
    _have = len([i for i in range(total)
                 if _receipt_cache_hit(
                     image_receipts, f"{i + 1:03d}",
                     os.path.join(output_dir, f"{i + 1:03d}.jpg"),
                 )])
    print_progress(_have, total, "изображений", show_bar=True)
    if _have < total:
        _gaps = [i + 1 for i in range(total)
                 if not _receipt_cache_hit(
                     image_receipts, f"{i + 1:03d}",
                     os.path.join(output_dir, f"{i + 1:03d}.jpg"),
                 )]
        _prev = ", ".join(str(g) for g in _gaps[:20]) + ("…" if len(_gaps) > 20 else "")
        print_user(f"⚠️ Не хватает {total - _have} кадров из {total}: сцены {_prev}", "WARNING")

    # Account-limit reporting remains after metadata and per-frame receipts, so
    # a partial run never loses its synchronization/audit state.

    # Smart Mode: Сохраняем метаданные для синхронизации
    if smart_mode:
        metadata_file = os.path.join(output_dir, "scenes_metadata.json")
        scenes_metadata = []
        for i, scene in enumerate(scenes):
            image_path = os.path.join(output_dir, f"{i+1:03d}.jpg")
            if os.path.exists(image_path):
                scene_meta = {
                    "image_index": i + 1,
                    "image_path": image_path,
                    "text_segment": scene.get('text_segment', ''),
                    "character_start": scene.get('character_start', 0),
                    "character_end": scene.get('character_end', 0),
                    "includes_main_character": scene.get('includes_main_character', False),
                    "visual_prompt": scene.get('visual_prompt', ''),
                    "camera_angle": scene.get('camera_angle', 'Wide Shot'),
                    "subject_type": scene.get('subject_type', 'environment')
                }
                scenes_metadata.append(scene_meta)
        
        try:
            with open(metadata_file, 'w', encoding='utf-8', errors='replace') as f:
                json.dump({
                    "smart_mode": True,
                    "total_text_length": len(full_text),
                    "scenes": scenes_metadata
                }, f, ensure_ascii=False, indent=2)
            print_user(f"✅ Метаданные для синхронизации сохранены: {metadata_file}", "SUCCESS")
        except Exception as e:
            print_user(f"⚠️ Не удалось сохранить метаданные: {e}", "WARNING")
            print_debug(f"Ошибка сохранения метаданных: {e}")
    
    # Аккаунт провайдера исчерпан → задача завершается ПОНЯТНОЙ ошибкой (а не тихим
    # «сохранено 0/242»), чтобы человек увидел причину и сменил модель/подождал.
    # Строго последним шагом: метаданные и receipts уже записаны; content-addressed
    # reference artifacts intentionally remain available for refill/regen.
    _account_limit_raise_if_hit(GOOGLER_MODEL, _have, total)

    _final_gaps = [i for i in range(total)
                   if not _receipt_cache_hit(
                       image_receipts, f"{i + 1:03d}",
                       os.path.join(output_dir, f"{i + 1:03d}.jpg"),
                   )]
    _accepted = []
    if _final_gaps and image_gaps_stretchable(_final_gaps, total):
        _accepted = _final_gaps
        for _row in write_image_gaps(output_dir, _accepted):
            print_user(f"🧩 [{_row['scene']}] Сцена {_row['scene']} заменена кадром предыдущей сцены: "
                       f"{_row['reason']}", "WARNING")
    else:
        write_image_gaps(output_dir, [])
        if _final_gaps:
            _filt = len(_final_gaps) - len(late_retry_indices(_final_gaps))
            print_user(f"⛔ Недостающих кадров {len(_final_gaps)} из {total} (из них отказ фильтра: "
                       f"{_filt}), подряд пропущено {longest_gap_run(_final_gaps)} сцен — "
                       f"больше {IMAGE_GAP_MAX_RUN}: растянуть готовые кадры нельзя, "
                       f"картинка висела бы минутами; нужен разбор", "ERROR")
    reconciliation = _reconcile_authoritative_image_set(
        project_dir, receipt_path, image_receipts,
        [f"{index + 1:03d}.jpg" for index in range(total)],
        skip_frames={f"{index + 1:03d}" for index in _accepted},
    )
    if reconciliation["quarantined"]:
        print_user(
            f"Старых нумерованных кадров перемещено в карантин: "
            f"{reconciliation['quarantined']}",
            "INFO",
        )

    print_user(f"✅ Полный пайплайн завершен! Подтверждено receipts: {_have}/{total} изображений"
               + (f"; заменено кадрами предыдущих сцен: {len(_accepted)}" if _accepted else ""), "SUCCESS")


def cmd_check_usage(args):
    """Команда: проверка лимитов API"""
    print_user("Проверка лимитов Googler API v2...", "PROCESS")
    usage = check_usage()
    print_debug(json.dumps(usage, indent=2))


def cmd_transform(args):
    """Команда: трансформация изображений"""
    print_user("Начинаю трансформацию изображений...", "PROCESS")
    _assert_googler_key()  # нет Googler-ключа → чёткая ошибка, не None→молчаливый 401
    
    with open(args.image_paths_file, 'r', encoding='utf-8', errors='replace') as f:
        image_paths = json.load(f)
    
    if not image_paths:
        print_error_user_friendly("Список изображений пуст!")
        return
    
    print_user(f"Изображений для трансформации: {len(image_paths)}", "INFO")
    
    saved = transform_images_batch(args.prompt, image_paths, args.output_dir)
    print_user(f"Трансформация завершена! Сохранено: {len(saved)}", "SUCCESS")


# ============ MAIN ============

def main():
    parser = argparse.ArgumentParser(description="Googler Worker v2.1 - генерация изображений и видео")
    subparsers = parser.add_subparsers(dest="command", help="Команды")
    
    # Команда: prompts
    p_prompts = subparsers.add_parser("prompts", help="Генерация промтов через GPT")
    p_prompts.add_argument("--source_file", required=True, help="Путь к текстовому файлу")
    p_prompts.add_argument("--project_dir", required=True, help="Папка проекта")
    p_prompts.add_argument("--style", required=True, help="Стиль изображений")
    p_prompts.add_argument("--sentences_per_prompt", type=int, default=3, help="Предложений на один промт")
    # Примечание: Visual Context теперь всегда генерируется автоматически из текста
    
    # Команда: pipeline (обновленная с новой логикой)
    p_pipeline = subparsers.add_parser("pipeline", help="Полный пайплайн: план сцен → изображения")
    p_pipeline.add_argument("--source_file", required=True, help="Путь к текстовому файлу")
    p_pipeline.add_argument("--project_dir", required=True, help="Папка проекта")
    p_pipeline.add_argument("--style", required=True, help="Стиль изображений")
    p_pipeline.add_argument("--sentences_per_prompt", type=str, default="3", help="Предложений на чанк. Число (напр. 3) ИЛИ переменный шаг 'HxB,T' (напр. 1x40,3 = первые 40 предложений по 1 на картинку, дальше по 3)")
    p_pipeline.add_argument("--split_long_sentences", action="store_true", help="В позициях «по 1» делить длинные предложения по смысловым паузам; «по N» не трогать")
    p_pipeline.add_argument("--max_solo_scene_seconds", type=_solo_scene_seconds_arg,
                            default=DEFAULT_MAX_SOLO_SCENE_SECONDS,
                            help="Примерный лимит одиночной сцены ДО Whisper (1–60 с; дефолт 6). Внутри переводится в символы по production-темпу")
    p_pipeline.add_argument("--aspect_ratio", default="landscape", help="Соотношение сторон")
    p_pipeline.add_argument("--auto_hero", action="store_true", help="Авто-генерация главного героя")
    p_pipeline.add_argument("--user_refs", help="Пути к пользовательским референсам (разделитель: ;)")
    p_pipeline.add_argument("--style_refs", help="Пути к референсам стиля, стабильно в КАЖДУЮ генерацию (разделитель: ;)")
    p_pipeline.add_argument("--reference_manifest", help="Immutable reference bundle manifest v1 (server authority)")
    p_pipeline.add_argument("--use_cache", action="store_true", help="Использовать кэш (пропускать уже сгенерированные изображения)")
    p_pipeline.add_argument(
        "--trust_legacy_cache", action="store_true",
        help="Явно принять существующие картинки без receipts как legacy-кэш",
    )
    p_pipeline.add_argument("--use_legacy", action="store_true", help="Использовать Legacy API v2 для всех изображений (вместо v3)")
    p_pipeline.add_argument("--seed", type=int, default=None, help="Фиксированный seed для генерации (если не указан, вычисляется из стиля)")
    p_pipeline.add_argument("--image_threads", type=int, default=None, help="Количество параллельных потоков для изображений (по умолчанию: 20)")
    p_pipeline.add_argument("--smart_mode", action="store_true", help="Smart Cinematic Mode: семантический анализ сцен, единообразие персонажей, синхронизация с текстом")
    p_pipeline.add_argument("--analyze_story", action="store_true", help="Анализ текста → персонажи (карта внешности) + локации (story_bible); ref-aware промпты + привязка референсов к сценам")
    p_pipeline.add_argument("--model", type=str, default=None, choices=GOOGLER_AVAILABLE_MODELS, help="Модель для генерации изображений")
    # Разделение этапов (очередь кабинета): промпты и картинки — разные задачи, идут конвейером
    p_pipeline.add_argument("--plan_only", action="store_true", help="Только план сцен (scenes.json) — картинки НЕ генерировать")
    p_pipeline.add_argument("--use_plan", action="store_true", help="Взять готовый scenes.json (от задачи промптов) вместо повторной работы LLM")
    
    # Команда: images
    p_images = subparsers.add_parser("images", help="Генерация изображений")
    p_images.add_argument("--project_dir", required=True, help="Папка проекта")
    p_images.add_argument("--prompts_json", help="JSON список промтов (опционально)")
    p_images.add_argument("--aspect_ratio", default="landscape", help="Соотношение сторон")
    p_images.add_argument("--style", default=None, help="Стиль (по умолчанию из prompts.json)")
    p_images.add_argument("--seed", type=int, default=None, help="Фиксированный seed")
    p_images.add_argument("--user_refs", help="Legacy именованные hero refs")
    p_images.add_argument("--style_refs", help="Legacy style refs")
    p_images.add_argument("--reference_manifest", help="Immutable reference bundle manifest v1")
    p_images.add_argument("--image_threads", type=int, default=None, help="Количество параллельных потоков для изображений (по умолчанию: 20)")
    p_images.add_argument("--use_cache", action="store_true", help="Использовать кэш")
    p_images.add_argument(
        "--trust_legacy_cache", action="store_true",
        help="Явно принять существующие картинки без receipts как legacy-кэш",
    )
    p_images.add_argument("--model", type=str, default=None, choices=GOOGLER_AVAILABLE_MODELS, help="Модель для генерации изображений")
    
    # Команда: regen_image — перегенерация одной картинки
    p_regen = subparsers.add_parser("regen_image", help="Перегенерация одной картинки (опц. со своим промптом)")
    p_regen.add_argument("--project_dir", required=True, help="Папка проекта")
    p_regen.add_argument("--image_file", required=True, help="Имя файла в googler/ (например 007.jpg) или полный путь")
    p_regen.add_argument("--prompt", default=None, help="Свой промпт (если не указан — исходный из scenes.json/prompts.json)")
    p_regen.add_argument("--style", default=None, help="Стиль (если не указан — из prompts.json)")
    p_regen.add_argument("--aspect_ratio", default="landscape", help="Соотношение сторон")
    p_regen.add_argument("--seed", type=int, default=None, help="Seed (по умолчанию — из стиля, как в pipeline)")
    p_regen.add_argument("--auto_hero", action="store_true", help="Явно включить auto-hero из согласованного plan bundle")
    regen_analyze = p_regen.add_mutually_exclusive_group()
    regen_analyze.add_argument(
        "--analyze_story", dest="analyze_story", action="store_true", default=None,
        help="Текущий режим: story-bible включён (нужен согласованный plan bundle)",
    )
    regen_analyze.add_argument(
        "--no_analyze_story", dest="analyze_story", action="store_false",
        help="Текущий режим: story-bible выключен; старые story refs запрещены",
    )
    p_regen.add_argument("--user_refs", help="Legacy именованные hero refs")
    p_regen.add_argument("--style_refs", help="Пути к референсам стиля (разделитель: ;)")
    p_regen.add_argument("--reference_manifest", help="Текущий immutable reference bundle manifest v1")
    p_regen.add_argument("--model", type=str, default=None, choices=GOOGLER_AVAILABLE_MODELS, help="Модель для генерации")

    # Команда: videos
    p_videos = subparsers.add_parser("videos", help="Генерация видео (API v3 с fallback на v2)")
    p_videos.add_argument("--project_dir", required=True, help="Папка проекта")
    p_videos.add_argument("--prompt", required=True, help="Промт для анимации")
    p_videos.add_argument("--image_paths_file", help="JSON файл со списком путей к изображениям")
    p_videos.add_argument("--loop", action="store_true", help="[DEPRECATED] Не поддерживается, игнорируется")
    p_videos.add_argument("--use_legacy", action="store_true", help="Использовать Legacy API v2 для всех видео (вместо v3)")
    p_videos.add_argument("--video_threads", type=int, default=None, help="Количество параллельных потоков для видео (по умолчанию: 10)")
    p_videos.add_argument("--use_cache", action="store_true", help="Использовать кэш")
    p_videos.add_argument("--video_method", type=str, default=None, choices=VIDEO_AVAILABLE_METHODS, help="Метод генерации видео: veo (Veo NonStop, дефолт), flower (Veo 3.1 recrafter), flow (VEO), grok")
    # t2v из промптов сцен (04.07): видео генерятся НАПРЯМУЮ по промптам scenes.json, без картинок
    p_videos.add_argument("--from_prompts", action="store_true", help="Text-to-video по промптам сцен (scenes.json), картинки-исходники не нужны")
    p_videos.add_argument("--first_n_scenes", type=int, default=0, help="Сколько первых сцен генерить как t2v (0 = все)")
    p_videos.add_argument("--aspect_ratio", type=str, default="landscape", help="Соотношение сторон для t2v")
    p_videos.add_argument("--style", type=str, default="", help="Стиль (в промпт t2v-сцены; и для плана, если scenes.json нет)")
    p_videos.add_argument("--source_file", type=str, default=None, help="Исходный текст — для генерации плана сцен, если scenes.json отсутствует")
    p_videos.add_argument("--sentences_per_prompt", type=str, default="3", help="Для плана при отсутствии scenes.json")
    p_videos.add_argument("--split_long_sentences", action="store_true", help="Для плана при отсутствии scenes.json")
    p_videos.add_argument("--max_solo_scene_seconds", type=_solo_scene_seconds_arg,
                          default=DEFAULT_MAX_SOLO_SCENE_SECONDS,
                          help="Примерный лимит одиночной сцены для fallback-плана (дефолт 6 с)")
    
    # Команда: transform
    p_transform = subparsers.add_parser("transform", help="Трансформация изображений")
    p_transform.add_argument("--prompt", required=True, help="Промт для трансформации")
    p_transform.add_argument("--image_paths_file", required=True, help="JSON файл со списком путей")
    p_transform.add_argument("--output_dir", required=True, help="Папка для сохранения")
    p_transform.add_argument("--model", type=str, default=None, choices=GOOGLER_AVAILABLE_MODELS, help="Модель для трансформации изображений")
    
    # Команда: pipeline
    # Примечание: Visual Context теперь всегда генерируется автоматически из текста
    
    # Команда: usage
    p_usage = subparsers.add_parser("usage", help="Проверка лимитов API")
    
    args = parser.parse_args()
    
    # Применяем выбранную модель, если указана
    global GOOGLER_MODEL, VIDEO_METHOD
    if hasattr(args, 'model') and args.model:
        GOOGLER_MODEL = args.model
        label = GOOGLER_MODEL_LABELS.get(args.model, args.model)
        print_user(f"🔧 Выбрана модель: {label}", "INFO")
    
    # Применяем метод генерации видео, если указан
    if hasattr(args, 'video_method') and args.video_method:
        VIDEO_METHOD = args.video_method
        label = VIDEO_METHOD_LABELS.get(args.video_method, args.video_method)
        print_user(f"🎬 Метод видео: {label}", "INFO")
    
    # Диспетч обёрнут в loud-fail: мёртвый/отсутствующий ключ → чёткая ошибка + exit(2),
    # ДО (или вместо) тихой деградации и расхода recrafter-кредитов.
    try:
        # Команды использующие OpenAI
        if args.command == "prompts":
            cmd_generate_prompts(args)

        # Команды использующие Googler
        elif args.command == "images":
            cmd_generate_images(args)

        elif args.command == "regen_image":
            cmd_regen_image(args)

        elif args.command == "videos":
            cmd_generate_videos(args)

        elif args.command == "transform":
            cmd_transform(args)

        # Pipeline использует оба API
        elif args.command == "pipeline":
            cmd_full_pipeline(args)

        elif args.command == "usage":
            cmd_check_usage(args)

        else:
            parser.print_help()
    except ScenePlanTransientError as e:
        print_error_user_friendly(f"Временный сбой плана сцен: {e}")
        sys.exit(EX_TEMPFAIL)
    # Обе ветки ниже стоят ПЕРЕД общим ScenePlanTerminalError намеренно: они его подклассы,
    # и только по ним кабинет умеет выключить виноватую галочку и переставить задачу.
    except ScenePlanCanonConflictError as e:
        print_error_user_friendly(f"План сцен отклонён: {e}")
        sys.exit(EX_PLAN_CANON_CONFLICT)
    except ScenePlanSemanticExhaustedError as e:
        print_error_user_friendly(f"План сцен отклонён: {e}")
        sys.exit(EX_PLAN_SEMANTIC_EXHAUSTED)
    except ScenePlanTerminalError as e:
        print_error_user_friendly(f"План сцен отклонён: {e}")
        sys.exit(1)
    except ref_contract.ReferenceContractError as e:
        print_error_user_friendly(f"Контракт референсов отклонён: {e}")
        sys.exit(1)
    except ProviderAccountLimitError as e:
        # Лимит аккаунта провайдера: код 3, чтобы отличать от «нет/битый ключ» (2) и от
        # обычного провала генерации (1). Текст с маркером уходит в лог задачи.
        print_error_user_friendly(str(e))
        sys.exit(3)
    except (OpenAIKeyError, GooglerKeyError) as e:
        print_error_user_friendly(str(e))
        sys.exit(2)


if __name__ == "__main__":
    main()
