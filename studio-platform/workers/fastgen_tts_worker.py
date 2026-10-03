# -*- coding: utf-8 -*-
"""Воркер озвучки кабинета через FastGen (модель Gemini TTS) — движок «fastgen».

ЧТО ДЕЛАЕТ
  Берёт весь текст проекта (source.txt), режет его на блоки не длиннее --max_chars знаков
  жадно по границам абзацев; слишком длинный абзац — по концам предложений, слишком длинное
  предложение — по пробелам. Слово не рвётся никогда, порядок блоков строго как в тексте.
  Выравнивается только хвост (короткий последний блок делится с предпоследним), поэтому
  правка одного абзаца меняет 1–2 блока, а не все.
  Блок = один заказ FastGen: POST /generations → опрос GET /generations/{id} до «succeeded» →
  звук из results[0].data (base64) или results[0].download_url. Каждый блок перекодируется
  в mp3 44,1 кГц / 128 кбит/с / моно, затем все склеиваются (ffmpeg concat) в ОДИН файл
  <project_dir>/<--audio_dir_name>/1.mp3. Темп (--speed ≠ 1.0) — atempo при склейке.

КОНТРАКТ (как у tts_worker.py в режиме «весь текст»)
  Аргументы: --source_file --project_dir --speed --use_cache --full_text (принимается для
  совместимости, режим всегда «весь текст») и свои: --voice --model --scene --max_chars
  --parallel --audio_dir_name --reorder_unconfirmed_minutes. Кабинет передаёт всё аргументами:
  env службы до воркера не доходит (кроме ключа и номера задачи).
  Ключ: env GOOGLER_API_KEY (так кабинет отдаёт ключ семейства «googler»), запасные имена
  FASTGEN_API_KEY и GOOGLER_API_TOKEN. Ключ нигде не печатается.
  Номер задачи: env VS_QUEUE_JOB_ID (gen-кабинет) или VS_JOB_ID (студия) — пишется в квитанции.
  Результат: ровно один <audio_dir>/1.mp3 и рядом квитанция 1.mp3.fastgen.json.
  Блоки лежат в <audio_dir>/.blocks/: папку с точкой пропускает и обход рендера, и проверки
  кабинета (glob скрытое не видит) — звук не задваивается; уборка папок озвучки её видит.
  Выход 0 — 1.mp3 готов (в логе строка «✅ … завершена»). Выход 1 — не готов, причина в строке ❌.
  Если часовой лимит ключа не освободился за время ожидания, строка ошибки содержит маркер
  «ЛИМИТ АККАУНТА ПРОВАЙДЕРА» — кабинет показывает её причиной в карточке задачи.

ЗАКАЗЫ И ПОВТОРЫ (квитанция .blocks/block_NNNN.mp3.fastgen.json на каждый блок)
  - квитанция пишется ДО отправки заказа (creating), после ответа — created + номер
    генерации; звук скачан и проверен — done; генерация закрыта неудачей FastGen — terminal;
  - готовые блоки и 1.mp3 с точной квитанцией берутся без заказа, если задан --use_cache
    («Кэш: докачивать недостающее») ИЛИ квитанцию написала эта же задача (тот же номер
    задачи: кабинет перезапустил её после рестарта, снятия по lease или автоповтора).
    Без --use_cache новая задача делает новый дубль — это «переозвучить всё»;
  - квитанция с номером — ждём и забираем СВОЮ генерацию, новую не создаём (и без
    --use_cache тоже: отправленный заказ не бросаем). Номер от другого ключа пула сперва
    пробуем забрать текущим ключом; 401/403/404/410 — заказ заново;
  - сбой на нашей стороне после успешной генерации (звук не скачался за 5 попыток, ffmpeg
    не перекодировал, ffprobe не прочитал) — НЕ повод заказывать заново: квитанция остаётся
    с номером, повтор задачи скачает звук бесплатно (FastGen хранит готовое ~40 мин);
  - квитанция creating без номера (обрыв или 5xx уже после отправки — создалась ли генерация,
    не узнать: списка генераций у FastGen нет). Новый заказ — когда с отправки прошло
    --reorder_unconfirmed_minutes (15; воркер дожидается этого сам, с пульсом в журнале),
    или сразу, если это новая задача без --use_cache («переозвучить всё»). Худший случай —
    один лишний заказ, 10 единиц часа. 0 — никогда не перезаказывать сам (тогда выход 1,
    разблокирует запуск с «Кэш: переозвучить всё»). Если запрос до FastGen не дошёл
    (нет соединения, DNS, TLS) или отвергнут 4xx/429, квитанция снимается сразу — заказа нет;
  - потолок: не больше 4 заказов на блок в одной задаче (счётчик orders в квитанции
    переживает перезапуски задачи) — дальше выход 1 «проверьте текст блока»;
  - отпечаток блока = текст + голос + модель + сцена (как в эталоне). Скорость в отпечаток
    блока не входит: FastGen её не получает, темп накладывается на склейку. Она входит в
    отпечаток итоговой квитанции 1.mp3 — смена скорости пересобирает 1.mp3 из готовых блоков
    без новых заказов;
  - старый 1.mp3, не совпавший с текстом, и посторонние аудиофайлы в папке озвучки не
    удаляются, а переносятся в .blocks/previous/ — рендер их не возьмёт.

ПРОВЕРКА ЗВУКА
  Каждый блок: темп 60–200 слов в минуту (иначе обрубок или тишина/повторы — заказ заново).
  После готовности всех блоков — сверка темпа между блоками: блок от 200 слов, читающийся
  быстрее 1,2 × медианы остальных (у одиночного блока — быстрее 170 сл/мин), считается
  оборванным: заказывается заново один раз. Повтор той же длины (±5 %) — это естественный
  темп текста, блок принимается; иначе выход 1 с номером блока.

ЛИМИТЫ КЛЮЧА (живой /usage 03.10.2026)
  600 единиц в час, заказ речи — 10 единиц → 60 заказов в час на ключ; до 20 потоков речи.
  Тот же ключ кормит картинки кабинета и обложки. Проверка ключа (GET /usage, /models) —
  один раз перед первым заказом, а не до сбора уже оплаченных генераций. На 429:
  - «все потоки заняты» — короткая пауза и повтор;
  - час исчерпан (подтверждается бесплатным GET /usage) — ждём начала следующего часа UTC,
    но не дольше 90 мин, затем выход 1 с маркером лимита. 429 приходит до создания заказа,
    поэтому повтор безопасен.
  Отказ сервиса при отправке (5xx, обрыв, 401/403) — новые заказы в этом запуске не шлём,
  уже отправленные дожидаемся.

ОТКУДА ЛОГИКА
  Боевой модуль манхвы voice_fastgen.py (замеры 29.09.2026): API, модель, квитанции,
  ожидание лимита часа, скачивание звука (ключ — только своему хосту), перекодировка
  wav → mp3, проверка «звук похож на текст» по темпу 60–200 слов в минуту, две попытки
  на блок, если генерация закрылась неудачей, перезаказ неподтверждённой отправки через
  15 мин. Отличия от эталона: голос по умолчанию algieba, сцена — нейтральный рассказчик
  документального ролика; нет общего файла-перерыва между процессами (очередь кабинета и так
  перезапускает задачу); редирект на чужой хост идёт без ключа; только стандартная
  библиотека + ffmpeg/ffprobe, без модулей манхвы.

Для ручного запуска (в кабинете до воркера не доходят): FASTGEN_TTS_API_BASE
(https://googler.fast-gen.ai/api/v6), FASTGEN_TTS_POLL_SECONDS (5), FASTGEN_TTS_HEARTBEAT_SECONDS
(120; кабинет снимает молчащий воркер через 900 с), FASTGEN_TTS_MAX_WAIT_MINUTES (30; только если
FastGen не прислал expires_at), FASTGEN_TTS_QUOTA_WAIT_MINUTES (90),
FASTGEN_TTS_REORDER_UNCONFIRMED_MINUTES (15; аргумент важнее).
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

# Система логирования кабинета (как в tts_worker.py); без неё — простой print.
try:
    from logger_config import print_user, print_debug, print_progress, print_error_user_friendly
except ImportError:
    def print_user(msg, msg_type="INFO"):
        emoji_map = {"INFO": "ℹ️", "SUCCESS": "✅", "WARNING": "⚠️", "ERROR": "❌", "PROCESS": "⏳"}
        emoji = emoji_map.get(str(msg_type).upper(), "ℹ️")
        print(f"{emoji} {msg}", flush=True)

    def print_debug(msg):
        pass

    def print_progress(current, total, item_name="элементов", show_bar=True):
        percentage = int((current / total) * 100) if total > 0 else 0
        print_user(f"Обработка {item_name}: {current} из {total} ({percentage}%)", "PROCESS")

    def print_error_user_friendly(error_msg, show_debug_hint=True):
        print_user(error_msg, "ERROR")


# ---------- настройки ----------
_ENV_WARNINGS: List[str] = []


def _int_env(name: str, default: int, minimum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _ENV_WARNINGS.append(f"{name}={raw!r} — не целое число, беру {default}")
        return default
    if value < minimum:
        _ENV_WARNINGS.append(f"{name}={value} — меньше {minimum}, беру {minimum}")
        return minimum
    return value


DEFAULT_BASE_URL = "https://googler.fast-gen.ai/api/v6"
BASE_URL = (os.environ.get("FASTGEN_TTS_API_BASE") or DEFAULT_BASE_URL).strip().rstrip("/")
DEFAULT_MODEL = "aistudio/gemini-3.8-flash-tts"
DEFAULT_VOICE = "algieba"          # голос владельца для Второй мировой (03.10.2026)
# Сцена манхвы («живой рассказчик рекапа») переписана под документальный ролик: движок общий
# для всех ниш кабинета (война, еда, Средневековье, птицы), поэтому без темы — серьёзно и ровно,
# без театральщины. Тему ниши задаёт --scene (tts_scene пресета). Язык речи — из самого текста.
DEFAULT_SCENE = ("A calm, experienced narrator records a documentary for YouTube in a quiet studio. "
                 "Serious tone, steady natural pacing, clear diction, short measured pauses between "
                 "thoughts. No theatrical acting, no shouting, no dramatic character voices, "
                 "no sound effects.")
DEFAULT_AUDIO_DIR = "_FINAL_AUDIO_FASTGEN"
# 6000 знаков ≈ 900 русских / 1000 английских слов ≈ 7 мин речи при замеренных 124–142 сл/мин —
# как блок эталона (420 с темпа). Длиннее 9,7 мин (1300 слов, 29.09) заказ не проверяли, а обрыв
# хвоста длинного блока проверка темпа 200 сл/мин не ловит.
DEFAULT_MAX_CHARS = 6000
DEFAULT_PARALLEL = 4
MAX_PARALLEL = 20                  # speech_threads_allowed ключа
KEY_ENV_NAMES = ("GOOGLER_API_KEY", "FASTGEN_API_KEY", "GOOGLER_API_TOKEN")
FINAL_NAME = "1.mp3"
BLOCKS_DIR_NAME = ".blocks"
PREVIOUS_DIR_NAME = "previous"
PROVIDER = "fastgen"
LIMIT_MARKER = "ЛИМИТ АККАУНТА ПРОВАЙДЕРА"   # кабинет показывает такую строку причиной ошибки

POLL_SECONDS = _int_env("FASTGEN_TTS_POLL_SECONDS", 5, 1)
HEARTBEAT_SECONDS = min(_int_env("FASTGEN_TTS_HEARTBEAT_SECONDS", 120, 10), 600)
MAX_WAIT_MINUTES = _int_env("FASTGEN_TTS_MAX_WAIT_MINUTES", 30, 1)   # если нет expires_at
QUOTA_WAIT_MINUTES = _int_env("FASTGEN_TTS_QUOTA_WAIT_MINUTES", 90, 0)
# Отправка без ответа: через столько минут — новый заказ (как PAUSE_MINUTES эталона).
DEFAULT_REORDER_UNCONFIRMED_MINUTES = _int_env("FASTGEN_TTS_REORDER_UNCONFIRMED_MINUTES", 15, 0)
# Номер задачи кабинета: gen отдаёт VS_QUEUE_JOB_ID, студия — VS_JOB_ID. Перезапуск ТОЙ ЖЕ
# задачи (рестарт службы, снятие по lease, автоповтор) добирает недостающее, а не заказывает заново.
RUN_ID = (os.environ.get("VS_QUEUE_JOB_ID") or os.environ.get("VS_JOB_ID") or "").strip()

ATTEMPTS = 2           # заказов на блок за запуск: второй — только если первый закрылся неудачей
MAX_ORDERS_PER_BLOCK = 4   # заказов на блок за задачу (переживает перезапуски той же задачи)
CREDITS = 10           # единиц часа за заказ речи (каталог /models, 29.09)
MIN_AUDIO_BYTES = 2048
DOWNLOAD_PAUSES = (5, 15, 30, 60)   # паузы между попытками скачать готовый звук, с
EXPIRES_MARGIN_SECONDS = 120        # ждём генерацию до expires_at минус 2 мин
# Звук не похож на текст: быстрее 200 сл/мин — обрубок, медленнее 60 — тишина или повторы.
FASTEST_WPM, SLOWEST_WPM = 200.0, 60.0
# Сверка темпа между блоками (обрыв хвоста длинного блока): блок от 200 слов быстрее
# 1,2 × медианы остальных — оборван; без соседей для сравнения — быстрее 170 сл/мин.
TEMPO_MIN_WORDS, TEMPO_OUTLIER, FASTEST_WPM_SINGLE = 200, 1.2, 170.0
TEMPO_SAME_LENGTH = 0.05            # повтор той же длины (±5 %) — естественный темп текста
SUCCESS = {"succeeded"}
TERMINAL_FAILURE = {"failed", "cancelled", "canceled", "expired"}
AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")   # что собирает рендер
_VOICE_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_AUDIO_DIR_RE = re.compile(r"^_FINAL_AUDIO_[A-Z0-9_]{1,40}$")


# ---------- журнал (из нескольких потоков — строки не должны слипаться) ----------
_LOG_LOCK = threading.Lock()


def say(msg: str, kind: str = "INFO") -> None:
    with _LOG_LOCK:
        print_user(msg, kind)


def say_progress(current: int, total: int, what: str) -> None:
    with _LOG_LOCK:
        print_progress(current, total, what)


def say_error(msg: str) -> None:
    with _LOG_LOCK:
        print_error_user_friendly(msg, show_debug_hint=False)


def _clock_text(ts: float) -> str:
    return time.strftime("%d.%m %H:%M:%S", time.localtime(ts))


def _sleep(seconds: float, what: str = "") -> None:
    """Долгое ожидание кусками, с отметкой в журнале: молчащий воркер кабинет снимает."""
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        chunk = min(remaining, float(HEARTBEAT_SECONDS))
        time.sleep(chunk)
        remaining -= chunk
        if remaining > 0 and what:
            say(f"FastGen: {what} — осталось ~{max(1, round(remaining / 60))} мин", "PROCESS")


# ---------- ошибки ----------
class ConfigError(RuntimeError):
    """Выясняется до любого заказа: ключ, ffmpeg, голос, модель, лимит речи, текст."""


class APIError(RuntimeError):
    def __init__(self, status: int, action: str, payload: dict, retry_after: Optional[float] = None):
        self.status = int(status)
        self.retry_after = retry_after
        self.payload = payload
        detail = payload.get("detail") if isinstance(payload.get("detail"), (str, dict, list)) else None
        message = str(payload.get("message") or payload.get("error") or detail or payload or "неизвестная ошибка")
        super().__init__(f"FastGen: {action}: HTTP {self.status}: {message[:300]}")


class NetworkError(RuntimeError):
    """sent=False — запрос до FastGen не дошёл (соединение, DNS, TLS): заказа точно нет."""

    def __init__(self, message: str, sent: bool = True):
        self.sent = sent
        super().__init__(message)


class Skipped(RuntimeError):
    """Блок не заказан (рубильник запуска или лимит часа): ни заказа, ни квитанции creating."""


class QuotaSkipped(Skipped):
    pass


class OrderCap(Skipped):
    """Блок в этой задаче заказан уже MAX_ORDERS_PER_BLOCK раз — дальше только человек."""


class KeyCheckFailed(Skipped):
    """Проверка ключа перед первым заказом не прошла — заказы в этом запуске не шлём."""


class UnconfirmedOrder(RuntimeError):
    """Прошлая отправка заказа осталась без ответа, а перезаказ выключен (порог 0)."""


class Terminal(RuntimeError):
    """Генерация окончательно не дала годного звука — блок можно заказать заново."""

    def __init__(self, gid, status, message=""):
        self.gid, self.status = gid, status
        super().__init__(f"генерация {gid} — {status}" + (f": {str(message)[:200]}" if message else ""))


# ---------- HTTP ----------
class _KeySafeRedirect(urllib.request.HTTPRedirectHandler):
    """urllib переносит заголовки на адрес редиректа; ключ чужому хосту не отдаём."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (urllib.parse.urlsplit(newurl).netloc
                                != urllib.parse.urlsplit(req.full_url).netloc):
            for name in list(new.headers):
                if name.lower() == "x-api-key":
                    new.remove_header(name)
        return new


_OPENER = urllib.request.build_opener(_KeySafeRedirect)


def _http(method: str, url: str, headers: dict, body: Optional[bytes], timeout: float):
    """(код, заголовки, тело). Единственная точка выхода в сеть."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001 — тело ошибки не главное
            raw = b""
        return e.code, dict(e.headers or {}), raw
    except urllib.error.URLError as e:
        # urllib заворачивает в URLError только сбой соединения/отправки (до ответа):
        # запрос целиком до сервера не дошёл.
        raise NetworkError(f"FastGen: нет связи: {e.reason}", sent=False) from e
    except (OSError, http.client.HTTPException) as e:
        # Обрыв/таймаут уже при ожидании ответа: запрос мог быть принят.
        raise NetworkError(f"FastGen: ответ не получен: {e.__class__.__name__}: {e}", sent=True) from e


def _api(key: str, method: str, path: str, body: Optional[dict] = None, timeout: float = 60) -> dict:
    headers = {"X-API-Key": key, "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    status, resp_headers, raw = _http(method, BASE_URL + path, headers, data, timeout)
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        payload = {"error": raw[:200].decode("utf-8", "replace")} if raw else {}
    payload = payload if isinstance(payload, dict) else {"data": payload}
    if status >= 400:
        retry_after = {str(k).lower(): v for k, v in (resp_headers or {}).items()}.get("retry-after")
        try:
            retry_after = max(0.0, float(retry_after)) if retry_after not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise APIError(status, f"{method} {path.split('?')[0]}", payload, retry_after)
    return payload


def _transient(e: Exception) -> bool:
    return isinstance(e, NetworkError) or (
        isinstance(e, APIError) and (e.status in (408, 429) or e.status >= 500))


def _service_down(e: Exception) -> bool:
    """Отказ всего сервиса, а не одного текста: следующий заказ с тем же ключом получит то же."""
    if isinstance(e, NetworkError):
        return True
    return isinstance(e, APIError) and (e.status >= 500 or e.status in (401, 403, 408))


def _api_retry(key: str, method: str, path: str, attempts: int = 4) -> dict:
    """Повторы только для бесплатных запросов (опрос, /usage, /models). Не для заказа."""
    for attempt in range(attempts):
        try:
            return _api(key, method, path)
        except Exception as e:
            if not _transient(e) or attempt >= attempts - 1:
                raise
            delay = min(60.0, getattr(e, "retry_after", None) or POLL_SECONDS * 2 ** attempt)
            say(f"FastGen: временная ошибка ({e}) — повтор через {delay:g} с", "WARNING")
            time.sleep(delay)
    raise RuntimeError("недостижимо")


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _num(value, cast=int):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def usage(key: str) -> dict:
    """Бесплатный GET /usage: лимит единиц часа, израсходовано, начало часа, потоки речи.
    limit/threads = None, если поля нет (формат ответа сменился) — тогда не судим."""
    data = _api_retry(key, "GET", "/usage")
    limits = _dict(data.get("account_limits"))
    cur = _dict(data.get("current_usage"))
    hour = _dict(_dict(cur.get("hourly_usage")).get("aistudio_request_credit"))
    now = time.time()
    limit = limits.get("aistudio_request_credits_per_hour_limit",
                       limits.get("aistudio_request_credits_per_hour"))
    return {"limit": _num(limit),
            "threads": _num(limits.get("speech_threads_allowed")),
            "used": _num(hour.get("current_usage")) or 0,
            "window_start": _num(hour.get("window_start"), float) or (now - now % 3600),
            "busy": _num(_dict(cur.get("active_threads")).get("speech_threads")) or 0}


def check_key(key: str, model: str, voice: str) -> str:
    """Бесплатные GET до заказа: ключ принят, у него есть лимит речи, модель знает голос."""
    try:
        u = usage(key)
    except APIError as e:
        if e.status in (401, 403):
            raise ConfigError(f"FastGen: ключ отвергнут (HTTP {e.status}); заказ не создан") from None
        raise
    if (u["limit"] is not None and u["limit"] < CREDITS) or (u["threads"] is not None and u["threads"] < 1):
        raise ConfigError(f"FastGen: у ключа нет лимита речи (единиц в час {u['limit']}, "
                          f"потоков {u['threads']}); заказ не создан")
    try:
        models = _api_retry(key, "GET", "/models")
        items = models.get("data") if isinstance(models.get("data"), list) else models.get("models") or []
        card = next((m for m in items if isinstance(m, dict) and m.get("id") == model), None)
    except Exception as e:  # noqa: BLE001 — каталог не главное: голос проверит сам заказ
        say(f"FastGen: каталог моделей не прочитан ({e}) — голос не сверяю", "WARNING")
        card = {}
    if card is None:
        raise ConfigError(f"FastGen: модели {model} нет в каталоге — проверьте --model; заказ не создан")
    allowed = _dict(_dict(_dict(card).get("options")).get("voice")).get("enum") or []
    if allowed and voice not in allowed:
        raise ConfigError(f"FastGen: модель {model} не знает голос {voice} — проверьте --voice; "
                          "заказ не создан")
    if u["limit"] is None:
        return "лимит часа не прочитан из /usage"
    left = max(0, u["limit"] - u["used"]) // CREDITS
    threads = f"{u['busy']} из {u['threads']}" if u["threads"] is not None else f"{u['busy']}"
    return (f"лимит ключа {u['limit'] // CREDITS} заказов речи в час, в этом часе осталось {left}, "
            f"потоков речи занято {threads}")


# ---------- часовой лимит ключа (общий на все потоки запуска) ----------
_QUOTA_LOCK = threading.Lock()
_QUOTA = {"until": 0.0}


def _is_busy(e: APIError) -> bool:
    text = json.dumps(e.payload, ensure_ascii=False).lower()
    return "concurrency" in text or "thread" in text or "concurrent" in text


def _after_429(key: str, e: APIError, attempt: int, idx: int) -> float:
    """Сколько ждать перед повтором после 429. «Час исчерпан» — только если /usage подтвердил."""
    short = min(float(HEARTBEAT_SECONDS), e.retry_after or min(60.0, float(POLL_SECONDS * 2 ** min(attempt, 4))))
    if _is_busy(e):
        say(f"FastGen: блок {idx}: все потоки речи заняты — повтор через {short:g} с", "PROCESS")
        return short
    try:
        u = usage(key)
    except Exception as err:  # noqa: BLE001
        say(f"FastGen: 429, а /usage не прочитан ({err}) — повтор через {short:g} с", "WARNING")
        return short
    if u["limit"] is not None and u["used"] + CREDITS > u["limit"]:
        until = u["window_start"] + 3600 + 5
        with _QUOTA_LOCK:
            _QUOTA["until"] = max(_QUOTA["until"], until)
        say(f"FastGen: лимит часа исчерпан ({u['used']} из {u['limit']} единиц) — жду до "
            f"{_clock_text(until)}, заказ не создан", "PROCESS")
        return max(1.0, until - time.time())
    say(f"FastGen: 429 ({e}), по /usage час не исчерпан ({u['used']} из {u['limit']}) — "
        f"повтор через {short:g} с", "PROCESS")
    return short


def _quota_skipped(idx: int, until: float) -> QuotaSkipped:
    return QuotaSkipped(f"блок {idx}: не заказан — лимит часа FastGen держится до {_clock_text(until)}, "
                        f"дольше {QUOTA_WAIT_MINUTES} мин ожидания")


def _wait_quota(deadline: float, idx: int) -> None:
    """Другой поток уже упёрся в лимит часа — не стучимся, ждём его конца."""
    with _QUOTA_LOCK:
        until = _QUOTA["until"]
    now = time.time()
    if until <= now:
        return
    if until > deadline:
        raise _quota_skipped(idx, until)
    say(f"FastGen: блок {idx}: жду лимит часа до {_clock_text(until)}", "PROCESS")
    _sleep(until - now, f"блок {idx}: жду лимит часа")


# ---------- блоки и квитанции ----------
@dataclass
class Block:
    idx: int
    text: str
    path: str = ""
    ident: str = ""
    seconds: float = 0.0
    orders: int = 0          # заказов этого блока в этой задаче (из квитанции, тот же отпечаток)
    tempo_ok: bool = False   # темп выше соседей уже признан естественным (повтор дал ту же длину)

    @property
    def words(self) -> int:
        return len(self.text.split())


class Job:
    def __init__(self, key: str, model: str, voice: str, scene: str,
                 use_cache: bool = False, reorder_minutes: int = DEFAULT_REORDER_UNCONFIRMED_MINUTES):
        self.key, self.model, self.voice, self.scene = key, model, voice, scene
        self.use_cache, self.reorder_minutes = use_cache, reorder_minutes
        self.scope = api_scope(key)
        self.service_down = threading.Event()   # рубильник запуска: после отказа сервиса POST не шлём
        self._check_lock = threading.Lock()
        self._checked = False
        self.check_error: Optional[str] = None

    def ensure_checked(self) -> None:
        """Проверка ключа и каталога — один раз за запуск, перед первым заказом. Уже оплаченные
        генерации забираются и без неё: сбой /usage или /models не должен их бросать."""
        with self._check_lock:
            if not self._checked:
                self._checked = True
                try:
                    say(f"FastGen: {check_key(self.key, self.model, self.voice)}", "INFO")
                except ConfigError as e:
                    self.check_error = str(e)
                except Exception as e:  # noqa: BLE001 — /usage недоступен после повторов
                    self.check_error = f"FastGen недоступен, заказы не отправлены: {e}"
            if self.check_error:
                raise KeyCheckFailed(self.check_error)


def api_scope(key: str) -> str:
    return hashlib.sha256(f"{BASE_URL}\0{key}".encode("utf-8")).hexdigest()


def scene_mark(scene: str) -> str:
    return hashlib.sha256(scene.encode("utf-8")).hexdigest()[:8] if scene else ""


def identity(text: str, voice: str, model: str, scene: str) -> str:
    """Отпечаток блока — как в эталоне: текст, модель, голос, сцена."""
    raw = json.dumps({"text": text, "model": model, "voice": voice, "scene": scene},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def receipt_path(mp3_path: str) -> str:
    return mp3_path + ".fastgen.json"


def atomic_json(path: str, data: dict) -> None:
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def read_receipt(mp3_path: str) -> dict:
    try:
        with open(receipt_path(mp3_path), encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise RuntimeError(f"квитанция {os.path.basename(receipt_path(mp3_path))} повреждена ({e}); "
                           "новый заказ автоматически не создан — разберите её вручную") from None


def save_receipt(job: Job, b: Block, state: str, generation_id: str = "", status: str = "", **extra) -> None:
    atomic_json(receipt_path(b.path), {
        "provider": PROVIDER, "model": job.model, "voice": job.voice, "scene_mark": scene_mark(job.scene),
        "identity": b.ident, "api_scope": job.scope, "state": state, "status": status,
        "generation_id": generation_id, "block": b.idx, "chars": len(b.text), "words": b.words,
        "job": RUN_ID, "orders": b.orders, "tempo_ok": b.tempo_ok, "updated_at": time.time(), **extra})


def _drop_receipt(mp3_path: str) -> None:
    try:
        os.remove(receipt_path(mp3_path))
    except FileNotFoundError:
        pass


def _restore_receipt(mp3_path: str, previous: dict) -> None:
    """Заказ точно не создан — вернуть квитанцию, какой она была до отправки (со счётчиком)."""
    if previous:
        atomic_json(receipt_path(mp3_path), previous)
    else:
        _drop_receipt(mp3_path)


def _same_job(r: dict) -> bool:
    return str(r.get("job") or "") == RUN_ID


def orders_so_far(b: Block) -> int:
    """Счётчик заказов блока в этой задаче: из квитанции, если отпечаток и задача те же."""
    try:
        r = read_receipt(b.path)
    except RuntimeError:
        return 0
    if r.get("identity") != b.ident or not _same_job(r):
        return 0
    return max(0, _num(r.get("orders")) or 0)


def _wall_time(value) -> Optional[float]:
    """expires_at из ответа FastGen → секунды эпохи: число (с или мс) или ISO-8601. None — не понял."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or re.fullmatch(r"\s*\d+(\.\d+)?\s*", str(value)):
        t = float(value)
        return t / 1000.0 if t > 1e12 else (t if t > 1e9 else None)
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


# ---------- резка текста ----------
_SENTENCE_END = re.compile(r"[.!?…]+[\"'»”’)\]]*\s+")


def _sentences(paragraph: str) -> List[str]:
    out, start = [], 0
    for m in _SENTENCE_END.finditer(paragraph):
        piece = paragraph[start:m.end()].strip()
        if piece:
            out.append(piece)
        start = m.end()
    tail = paragraph[start:].strip()
    if tail:
        out.append(tail)
    return out


def _pack_words(sentence: str, max_chars: int) -> List[str]:
    out, cur = [], ""
    for w in sentence.split():
        if len(w) > max_chars:
            raise ConfigError(f"в тексте «слово» длиннее {max_chars} знаков ({w[:40]}…) — "
                              "разбейте его; заказ не создан")
        cand = f"{cur} {w}" if cur else w
        if len(cand) > max_chars:
            out.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def split_text(paragraphs: List[str], max_chars: int) -> List[str]:
    """Блоки ≤ max_chars: целые абзацы через перевод строки; длинный абзац — по предложениям,
    длинное предложение — по словам. Порядок текста сохраняется строго.

    Упаковка жадная: граница блока зависит только от текста ДО неё, поэтому правка концовки
    перезаказывает 1–2 последних блока, а не весь ролик (выравнивание по средней длине
    сдвигало все границы). Выравнивается только хвост: короткий последний блок (меньше трети
    предела) делится поровну с предпоследним — без отдельного заказа ради пары строк."""
    units: List[Tuple[str, bool]] = []          # (кусок, начинает ли он абзац)
    for para in paragraphs:
        if len(para) <= max_chars:
            units.append((para, True))
            continue
        pieces: List[str] = []
        for sent in _sentences(para):
            pieces.extend([sent] if len(sent) <= max_chars else _pack_words(sent, max_chars))
        units.extend((p, i == 0) for i, p in enumerate(pieces))

    def join(group: List[Tuple[str, bool]]) -> str:
        out = group[0][0]
        for text, starts_paragraph in group[1:]:
            out += ("\n" if starts_paragraph else " ") + text
        return out

    def pack(items: List[Tuple[str, bool]], soft: float) -> List[List[Tuple[str, bool]]]:
        groups: List[List[Tuple[str, bool]]] = []
        cur: List[Tuple[str, bool]] = []
        cur_len = 0
        for text, starts_paragraph in items:
            if cur and (cur_len + 1 + len(text) > max_chars or cur_len >= soft):
                groups.append(cur)
                cur, cur_len = [], 0
            cur_len = cur_len + 1 + len(text) if cur else len(text)
            cur.append((text, starts_paragraph))
        if cur:
            groups.append(cur)
        return groups

    groups = pack(units, math.inf)
    if len(groups) > 1 and len(join(groups[-1])) < max_chars // 3:
        pair = groups[-2] + groups[-1]
        redone = pack(pair, len(join(pair)) / 2)
        if len(redone) == 2:            # лишний заказ ради ровности не нужен
            groups = groups[:-2] + redone
    blocks = [join(g) for g in groups]
    # Защита от ошибки резки: последовательность слов обязана совпасть с исходной.
    if " ".join(blocks).split() != " ".join(paragraphs).split():
        raise ConfigError("резка текста на блоки потеряла или переставила слова — заказ не создан")
    if any(len(b) > max_chars for b in blocks):
        raise ConfigError("резка текста дала блок длиннее предела — заказ не создан")
    return blocks


# ---------- звук ----------
def _duration(path: str) -> float:
    """Длина звука по ffprobe; 0.0 — не прочитана (не звук, или сбой/таймаут самого ffprobe)."""
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        value = float((p.stdout or "0").strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0
    return value if value == value and value > 0 else 0.0


def _run_ffmpeg(args: List[str], what: str, timeout_s: float) -> Tuple[int, str]:
    """ffmpeg с отметками в журнале, пока работает (долгая склейка не должна молчать)."""
    proc = subprocess.Popen(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, errors="replace")
    started = time.monotonic()
    while True:
        try:
            _out, err = proc.communicate(timeout=HEARTBEAT_SECONDS)
            return proc.returncode, (err or "").strip()
        except subprocess.TimeoutExpired:
            if time.monotonic() - started > timeout_s:
                proc.kill()
                proc.communicate()
                return -9, f"ffmpeg не уложился в {timeout_s / 60:.0f} мин"
            say(f"{what}: ffmpeg ещё работает…", "PROCESS")


def audio_problem(text: str, seconds: float) -> Optional[str]:
    """Длина звука против числа слов: обрубок или тишина/повторы. None — годится."""
    words = len(text.split())
    if words == 0 or len(text) / words > 20:
        return None if seconds > 0.5 else f"пустой звук: {seconds:.1f} с"   # письмо без пробелов: слов не счесть
    if seconds < words / FASTEST_WPM * 60:
        return f"обрубок: {seconds:.1f} с на {words} слов (быстрее {FASTEST_WPM:g} сл/мин)"
    if seconds > words / SLOWEST_WPM * 60 + 10:
        return f"затянуто: {seconds:.1f} с на {words} слов (медленнее {SLOWEST_WPM:g} сл/мин)"
    return None


def _atempo_chain(speed: float) -> str:
    factors, rem = [], speed
    while rem > 2.0:
        factors.append(2.0)
        rem /= 2.0
    while rem < 0.5:
        factors.append(0.5)
        rem /= 0.5
    factors.append(rem)
    return ",".join("atempo=%.4f" % f for f in factors)


def _canonical_speed(raw) -> Tuple[float, str]:
    try:
        d = Decimal(str(raw if raw not in (None, "") else "1").strip())
    except (InvalidOperation, ValueError):
        raise ConfigError(f"--speed: нужно число, а не {raw!r}") from None
    if not d.is_finite() or d <= 0:
        raise ConfigError(f"--speed: нужно положительное число, а не {raw!r}")
    value = float(d)
    if abs(value - 1.0) < 0.01:          # как apply_audio_speed кабинета: почти 1 — без atempo
        return 1.0, "1"
    return value, format(d.normalize(), "f")


# ---------- один блок ----------
def block_ready(b: Block) -> bool:
    """Готовый звук блока с точной квитанцией — заказывать не нужно."""
    try:
        r = read_receipt(b.path)
    except RuntimeError:
        return False
    if not (r.get("provider") == PROVIDER and r.get("state") == "done" and r.get("identity") == b.ident
            and os.path.isfile(b.path) and os.path.getsize(b.path) > MIN_AUDIO_BYTES):
        return False
    seconds = _duration(b.path)
    if seconds <= 0:
        # Оплаченный готовый блок с точной квитанцией: ffprobe не прочитал его — это наш сбой
        # (ffprobe, диск, нагрузка), а не плохой звук. Новый заказ тут ничего не исправит.
        raise RuntimeError(f"блок {b.idx}: ffprobe не прочитал готовый блок {os.path.basename(b.path)} — "
                           "новый заказ не создаю; проверьте ffprobe и диск и повторите задачу")
    if audio_problem(b.text, seconds) is not None:
        return False
    b.seconds = seconds
    b.tempo_ok = bool(r.get("tempo_ok"))
    return True


def generation_to_resume(job: Job, b: Block, audio_dir: str) -> Tuple[str, bool]:
    """(номер своей отправленной генерации, которую надо дождаться; заказана ли она другим
    ключом). '' — можно заказывать. Квитанцию creating без номера ждём до порога и
    перезаказываем; UnconfirmedOrder — только если перезаказ выключен (порог 0)."""
    while True:
        r = read_receipt(b.path)
        if not r:
            return "", False
        gid = str(r.get("generation_id") or "").strip()
        state = r.get("state")
        if not (state == "creating" and not gid):
            break
        if r.get("identity") != b.ident:
            say(f"блок {b.idx}: неподтверждённая прошлая отправка была для другого текста или голоса — "
                "заказываю новый", "WARNING")
            return "", False
        sent = _num(r.get("ended_at"), float) or _num(r.get("sent_at"), float) \
            or os.path.getmtime(receipt_path(b.path))
        age = max(0.0, time.time() - sent)
        lost = f"если та всё же создалась, лишний заказ — {CREDITS} единиц часа"
        if not job.use_cache and not (RUN_ID and _same_job(r)):
            say(f"блок {b.idx}: прошлая отправка ({_clock_text(sent)}) осталась без ответа; задача "
                f"«переозвучить всё» — заказываю заново ({lost})", "WARNING")
            return "", False
        limit = job.reorder_minutes * 60
        if limit <= 0:
            rel = os.path.relpath(receipt_path(b.path), os.path.dirname(audio_dir))
            raise UnconfirmedOrder(
                f"блок {b.idx}: прошлая отправка заказа ({_clock_text(sent)}) осталась без ответа FastGen — "
                f"создалась ли генерация, узнать нельзя ({rel}). Новый заказ сам не создаю (порог 0). "
                "Разблокировать: запустить озвучку с «Кэш: переозвучить всё»")
        if age >= limit:
            say(f"блок {b.idx}: отправка {age / 60:.0f} мин назад осталась без ответа — заказываю "
                f"заново ({lost})", "WARNING")
            return "", False
        say(f"блок {b.idx}: прошлая отправка {age / 60:.0f} мин назад осталась без ответа — жду до "
            f"{job.reorder_minutes} мин с отправки, потом закажу заново", "PROCESS")
        _sleep(limit - age, f"блок {b.idx}: жду порог перезаказа неподтверждённой отправки")
        # перечитываем: вдруг процесс, который отправлял, всё-таки дописал номер
    if r.get("identity") != b.ident:
        return "", False   # текст блока, модель, голос или сцена изменились — нужен другой звук
    if not gid or state in ("terminal", "done"):
        return "", False   # закрыта неудачей, или звук испорчен/удалён, или новый дубль
    if r.get("api_scope") != job.scope:
        say(f"блок {b.idx}: генерация {gid} заказана другим ключом пула — пробую забрать её этим "
            "ключом (опрос бесплатный); не отдаст — закажу заново", "WARNING")
        return gid, True
    return gid, False


def create_generation(job: Job, b: Block) -> str:
    """POST /generations с ожиданием лимита часа и занятых потоков. Квитанция «creating»
    лежит ровно на время отправки: отказ 4xx (в том числе 429) или недошедший запрос —
    генерации нет, квитанция возвращается к прежней; обрыв после отправки, 5xx и ответ без
    номера — «creating» остаётся с временем отправки."""
    job.ensure_checked()
    body = {"model": job.model, "prompt": b.text, "voice": job.voice}
    if job.scene:
        body["scene"] = job.scene
    deadline = time.time() + QUOTA_WAIT_MINUTES * 60
    attempt = 0
    while True:
        _wait_quota(deadline, b.idx)
        if job.service_down.is_set():
            raise Skipped(f"блок {b.idx}: не заказан — FastGen отказал в этом запуске")
        if b.orders >= MAX_ORDERS_PER_BLOCK:
            raise OrderCap(f"блок {b.idx}: в этой задаче заказан уже {b.orders} раз(а), годного звука нет — "
                           "новый заказ не создаю; проверьте текст блока")
        previous = read_receipt(b.path)
        sent_at = time.time()
        b.orders += 1
        save_receipt(job, b, "creating", sent_at=sent_at)
        try:
            data = _api(job.key, "POST", "/generations", body, timeout=120)
        except APIError as e:
            if e.status == 429:
                b.orders -= 1
                _restore_receipt(b.path, previous)
                wait = _after_429(job.key, e, attempt, b.idx)
                if time.time() + wait > deadline:
                    with _QUOTA_LOCK:
                        until = max(_QUOTA["until"], time.time() + wait)
                    raise _quota_skipped(b.idx, until) from e
                _sleep(wait, f"блок {b.idx}: жду лимит")
                attempt += 1
                continue
            if e.status < 500 and e.status != 408:
                b.orders -= 1
                _restore_receipt(b.path, previous)
            else:
                save_receipt(job, b, "creating", sent_at=sent_at, ended_at=time.time(), error=str(e)[:300])
                e.order_unconfirmed = True
            raise
        except NetworkError as e:
            if not e.sent:
                b.orders -= 1
                _restore_receipt(b.path, previous)
            else:
                save_receipt(job, b, "creating", sent_at=sent_at, ended_at=time.time(), error=str(e)[:300])
                e.order_unconfirmed = True
            raise
        gid = str(data.get("id") or "").strip()
        if not gid:
            save_receipt(job, b, "creating", sent_at=sent_at, ended_at=time.time(),
                         error="ответ без номера генерации")
            err = NetworkError("FastGen: POST /generations: ответ без номера генерации — "
                               "создана ли она, неизвестно")
            err.order_unconfirmed = True
            raise err
        save_receipt(job, b, "created", gid, str(data.get("status") or ""),
                     sent_at=sent_at, expires_at=data.get("expires_at"))
        return gid


def _wait_deadline(b: Block, start: float) -> Tuple[float, str]:
    """До какого момента ждать генерацию: expires_at из квитанции минус 2 мин (FastGen хранит
    готовое ~40 мин), без него — MAX_WAIT_MINUTES от начала ожидания."""
    try:
        expires = _wall_time(read_receipt(b.path).get("expires_at"))
    except RuntimeError:
        expires = None
    if expires:
        return min(expires - EXPIRES_MARGIN_SECONDS, start + 3 * 3600), f"до {_clock_text(expires)} (срок хранения)"
    return start + MAX_WAIT_MINUTES * 60, f"{MAX_WAIT_MINUTES} мин"


def wait_generation(job: Job, gid: str, b: Block, foreign: bool = False) -> dict:
    start = time.time()
    deadline, deadline_text = _wait_deadline(b, start)
    last_beat, last_status, last_err = start, None, 0.0
    path = f"/generations/{urllib.parse.quote(gid, safe='')}"
    while True:                      # хотя бы один опрос, даже если срок уже вышел
        try:
            gen = _api(job.key, "GET", path)
        except (APIError, NetworkError) as e:
            if isinstance(e, APIError) and e.status in (404, 410):
                raise Terminal(gid, "нет у FastGen (истекла или не найдена)") from None
            if foreign and isinstance(e, APIError) and e.status in (401, 403):
                raise Terminal(gid, f"текущий ключ её не видит (HTTP {e.status})") from None
            if not _transient(e):
                raise
            if time.time() - last_err >= 60:
                say(f"FastGen: блок {b.idx}: опрос генерации {gid} не удался ({e}) — жду и повторяю", "WARNING")
                last_err = time.time()
            if time.time() >= deadline:
                break
            time.sleep(min(float(HEARTBEAT_SECONDS), getattr(e, "retry_after", None) or POLL_SECONDS))
            continue
        status = str(gen.get("status") or "").strip().lower()
        if status in SUCCESS:
            return gen
        if status in TERMINAL_FAILURE:
            raise Terminal(gid, status, gen.get("error") or gen.get("error_code") or "")
        now = time.time()
        if now >= deadline:
            break
        if status != last_status or now - last_beat >= HEARTBEAT_SECONDS:
            say(f"FastGen: блок {b.idx}: генерация {gid} — {status or '?'}, ждём {(now - start) / 60:.1f} мин",
                "PROCESS")
            last_status, last_beat = status, now
        time.sleep(POLL_SECONDS)
    raise TimeoutError(f"блок {b.idx}: генерация {gid} не готова {deadline_text}; квитанция сохранена — "
                       "повтор задачи продолжит ждать её, а не закажет заново")


def _result_bytes(key: str, gen: dict) -> bytes:
    res = gen.get("results") or []
    r0 = res[0] if isinstance(res, list) and res and isinstance(res[0], dict) else {}
    data = r0.get("data")
    if data:
        data = str(data)
        data = data.split(",", 1)[1] if data.startswith("data:") else data
        try:
            return base64.b64decode(data)
        except (binascii.Error, ValueError) as e:
            raise Terminal(gen.get("id"), f"звук пришёл битым base64 ({e})") from None
    url = str(r0.get("download_url") or "").strip()
    if not url:
        raise Terminal(gen.get("id"), "готова без звука (нет ни data, ни download_url)")
    host = BASE_URL.split("/api/", 1)[0]
    if url.startswith("/"):
        url = host + url
    # Ключ — только своему хосту; подписанная ссылка хранилища в ключе не нуждается.
    own = urllib.parse.urlsplit(url).netloc == urllib.parse.urlsplit(host).netloc
    status, _h, raw = _http("GET", url, {"X-API-Key": key} if own else {}, None, 300)
    if status >= 400:
        raise APIError(status, "скачивание звука", {})
    return raw


def download(job: Job, gen: dict, b: Block) -> None:
    """Звук готовой генерации → mp3 блока (44,1 кГц, 128 кбит/с, моно).
    Terminal — только если виноват FastGen (нет звука, не звук, генерация удалена). Наш сбой
    (сеть, ffmpeg) — RuntimeError: квитанция остаётся с номером, повтор скачает бесплатно."""
    gid = gen.get("id")
    raw, last = None, None
    tries = len(DOWNLOAD_PAUSES) + 1
    for attempt in range(tries):
        try:
            raw = _result_bytes(job.key, gen)
            break
        except (APIError, NetworkError) as e:
            last = e
            if attempt + 1 >= tries:
                break
            pause = DOWNLOAD_PAUSES[attempt]
            say(f"FastGen: блок {b.idx}: звук генерации {gid} не скачан ({e}) — попытка {attempt + 2} "
                f"из {tries} через {pause} с", "WARNING")
            time.sleep(pause)
            try:   # подписанная ссылка живёт недолго — берём свежую карточку генерации
                gen = _api_retry(job.key, "GET", f"/generations/{urllib.parse.quote(str(gid), safe='')}")
            except APIError as err:
                if err.status in (404, 410):
                    raise Terminal(gid, "звук не скачан, генерация уже удалена") from None
            except NetworkError:
                pass
    if raw is None:
        raise RuntimeError(f"блок {b.idx}: звук готовой генерации {gid} не скачан за {tries} попыток ({last}); "
                           "номер сохранён в квитанции — повтор задачи скачает его без нового заказа")
    if len(raw) <= MIN_AUDIO_BYTES:
        raise Terminal(gid, f"скачан пустой звук ({len(raw)} байт)")
    wav, part = b.path + ".part.wav", b.path + ".part.mp3"
    try:
        with open(wav, "wb") as f:
            f.write(raw)
        if _duration(wav) <= 0:
            raise Terminal(gid, f"FastGen прислал не звук ({len(raw)} байт, ffprobe не читает длину)")
        rc, err = _run_ffmpeg(["-i", wav, "-vn", "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame",
                               "-b:a", "128k", part], f"блок {b.idx}: перекодировка", 900)
        if rc != 0 or not os.path.exists(part) or os.path.getsize(part) <= MIN_AUDIO_BYTES:
            raise RuntimeError(f"блок {b.idx}: звук генерации {gid} не перекодирован в mp3 на нашей стороне "
                               f"({err[:200] or 'пустой результат'}); номер сохранён — повтор задачи "
                               "скачает его без нового заказа")
        os.replace(part, b.path)
    finally:
        for leftover in (wav, part):
            try:
                os.remove(leftover)
            except FileNotFoundError:
                pass


def process_block(job: Job, b: Block, audio_dir: str) -> None:
    last: Optional[Exception] = None
    for attempt in range(ATTEMPTS):
        gid, foreign = generation_to_resume(job, b, audio_dir)
        if gid:
            say(f"FastGen: блок {b.idx}: заказ уже отправлен — жду свою генерацию {gid}", "PROCESS")
        else:
            if job.service_down.is_set():
                raise Skipped(f"блок {b.idx}: не заказан — FastGen отказал в этом запуске")
            try:
                gid = create_generation(job, b)
            except Skipped:
                raise
            except Exception as e:
                if _service_down(e) and not job.service_down.is_set():
                    job.service_down.set()
                    say(f"FastGen отказал при отправке заказа ({e}) — новые заказы в этом запуске не "
                        "отправляю, уже отправленные дожидаюсь", "WARNING")
                later = (f"повтор задачи закажет блок заново не раньше чем через {job.reorder_minutes} мин "
                         "после отправки" if job.reorder_minutes > 0
                         else "сам не перезакажу — разблокирует запуск с «Кэш: переозвучить всё»")
                raise RuntimeError(f"блок {b.idx}: " + (
                    f"отправка заказа без ответа ({e}) — создан ли он, неизвестно; {later}"
                    if getattr(e, "order_unconfirmed", False) else f"заказ не создан: {e}")) from e
            foreign = False
            say(f"FastGen: блок {b.idx}: генерация {gid} создана ({b.words} слов)", "PROCESS")
        try:
            gen = wait_generation(job, gid, b, foreign)
            download(job, gen, b)
            seconds = _duration(b.path)
            if seconds <= 0:
                raise RuntimeError(f"блок {b.idx}: ffprobe не прочитал скачанный звук генерации {gid}; номер "
                                   "сохранён в квитанции — повтор задачи скачает его без нового заказа")
            problem = audio_problem(b.text, seconds)
            if problem:
                raise Terminal(gid, "звук не похож на текст", problem)
        except Terminal as e:
            save_receipt(job, b, "terminal", gid, str(e.status), error=str(e)[:300])
            last = e
            say(f"FastGen: блок {b.idx}: {e}" + (" — заказываю заново" if attempt + 1 < ATTEMPTS else ""),
                "WARNING")
            continue
        save_receipt(job, b, "done", gid, "succeeded", seconds=round(seconds, 3))
        b.seconds = seconds
        return
    raise RuntimeError(f"блок {b.idx}: {last}")


# ---------- папка озвучки ----------
def _move_aside(path: str, previous_dir: str) -> str:
    os.makedirs(previous_dir, exist_ok=True)
    dest = os.path.join(previous_dir, os.path.basename(path))
    os.replace(path, dest)
    return dest


def prepare_audio_dir(audio_dir: str, blocks_dir: str) -> None:
    """Свои огрызки прошлых запусков — удалить; посторонний звук — в .blocks/previous/."""
    previous_dir = os.path.join(blocks_dir, PREVIOUS_DIR_NAME)
    for name in os.listdir(audio_dir):
        full = os.path.join(audio_dir, name)
        if not os.path.isfile(full):
            continue
        if name.startswith(".") and name.endswith(".part.mp3"):
            os.remove(full)                       # наша недоделанная склейка
            continue
        if name == FINAL_NAME or name.startswith("."):
            continue
        if name.lower().endswith(AUDIO_EXTENSIONS):
            _move_aside(full, previous_dir)
            say(f"в папке озвучки лежал посторонний звук {name} — перенёс в "
                f"{BLOCKS_DIR_NAME}/{PREVIOUS_DIR_NAME}/, иначе рендер склеил бы его с озвучкой", "WARNING")
    for name in os.listdir(blocks_dir):
        if name.endswith((".part.wav", ".part.mp3")):
            os.remove(os.path.join(blocks_dir, name))


def final_identity(blocks: List[Block], speed_text: str) -> str:
    raw = json.dumps({"mode": "fastgen_full_text_v1", "blocks": [b.ident for b in blocks],
                      "speed": speed_text}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def final_ok(final_path: str, ident: str) -> bool:
    try:
        r = read_receipt(final_path)
    except RuntimeError:
        return False
    return (r.get("provider") == PROVIDER and r.get("state") == "done" and r.get("identity") == ident
            and os.path.isfile(final_path) and os.path.getsize(final_path) > MIN_AUDIO_BYTES)


def build_final(blocks: List[Block], final_path: str, blocks_dir: str, speed: float) -> float:
    """Склейка блоков в один 1.mp3 (44,1 кГц, моно, 128 кбит/с); темп — atempo. → длина, с."""
    lst = os.path.join(blocks_dir, "concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for b in blocks:
            f.write("file '" + os.path.abspath(b.path).replace("'", "'\\''") + "'\n")
    # Имя с точкой: ни рендер, ни проверки кабинета недоделанный файл не увидят.
    tmp = os.path.join(os.path.dirname(final_path), "." + os.path.basename(final_path) + ".part.mp3")
    args = ["-f", "concat", "-safe", "0", "-i", lst, "-vn"]
    if speed == 1.0:
        args += ["-c", "copy", tmp]           # блоки уже 44,1 кГц / моно / 128k — без пережатия
    else:
        args += ["-af", _atempo_chain(speed), "-ac", "1", "-ar", "44100",
                 "-c:a", "libmp3lame", "-b:a", "128k", tmp]
    size_mb = sum(os.path.getsize(b.path) for b in blocks) / (1024.0 * 1024.0)
    try:
        rc, err = _run_ffmpeg(args, "склейка озвучки", min(7200.0, max(900.0, size_mb * 12.0)))
        if rc != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) <= MIN_AUDIO_BYTES:
            raise RuntimeError(f"склейка звука не удалась: {err[:300]}")
        got = _duration(tmp)
        want = sum(b.seconds for b in blocks) / speed
        if got <= 0 or abs(got - want) > max(5.0, 0.02 * want):
            raise RuntimeError(f"склейка звука дала {got:.1f} с вместо ожидаемых {want:.1f} с")
        os.replace(tmp, final_path)
        return got
    finally:
        for leftover in (tmp, lst):
            try:
                os.remove(leftover)
            except FileNotFoundError:
                pass


# ---------- сверка темпа между блоками ----------
def _wpm(b: Block) -> float:
    return b.words / (b.seconds / 60.0) if b.seconds > 0 else 0.0


def tempo_suspects(blocks: List[Block]) -> List[Tuple[Block, float, float]]:
    """Блоки, похожие на оборванные: (блок, его темп, порог). Длинный блок, у которого модель
    оборвала хвост, проходит проверку 200 сл/мин (1300 слов за 10 мин = 130 сл/мин), но читается
    заметно быстрее соседей. Сравнение — с медианой ОСТАЛЬНЫХ блоков (у двух блоков медиана всех
    прятала бы обрыв); без соседей от 200 слов — с 170 сл/мин."""
    rates = {b.idx: _wpm(b) for b in blocks if b.words >= TEMPO_MIN_WORDS and b.seconds > 0}
    out = []
    for b in blocks:
        if b.idx not in rates or b.tempo_ok:
            continue
        others = [v for i, v in rates.items() if i != b.idx]
        limit = statistics.median(others) * TEMPO_OUTLIER if others else FASTEST_WPM_SINGLE
        if rates[b.idx] > limit:
            out.append((b, rates[b.idx], limit))
    return out


def _mark_tempo_ok(b: Block) -> None:
    b.tempo_ok = True
    try:
        r = read_receipt(b.path)
    except RuntimeError:
        return
    if r:
        r["tempo_ok"] = True
        atomic_json(receipt_path(b.path), r)


# ---------- запуск ----------
def _fail(msg: str) -> None:
    say_error(msg)
    sys.exit(1)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Воркер озвучки кабинета через FastGen (Gemini TTS): весь текст → один 1.mp3.")
    p.add_argument("--source_file", required=True, help="Текст проекта (source.txt).")
    p.add_argument("--project_dir", required=True, help="Папка проекта.")
    p.add_argument("--speed", default=os.environ.get("TTS_SPEED", "1.0"),
                   help="Темп речи (atempo на итоге): 1.0 — норма, меньше — медленнее, больше — быстрее.")
    p.add_argument("--use_cache", action="store_true",
                   help="Брать готовые блоки и 1.mp3 с точной квитанцией и от прошлых задач "
                        "(без флага — только написанные этой же задачей).")
    p.add_argument("--full_text", action="store_true",
                   help="Принимается для совместимости: режим всегда «весь текст».")
    p.add_argument("--voice", default=DEFAULT_VOICE, help=f"Голос FastGen (по умолчанию {DEFAULT_VOICE}).")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Модель FastGen (по умолчанию {DEFAULT_MODEL}).")
    p.add_argument("--scene", default=None,
                   help="Инструкция стиля для модели. По умолчанию — спокойный рассказчик "
                        "документального ролика (без темы); off — без инструкции.")
    p.add_argument("--max_chars", type=int, default=DEFAULT_MAX_CHARS,
                   help=f"Предел знаков в одном заказе (по умолчанию {DEFAULT_MAX_CHARS} ≈ 900 слов ≈ 7 мин речи).")
    p.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL,
                   help=f"Одновременных заказов (по умолчанию {DEFAULT_PARALLEL}, не больше {MAX_PARALLEL}).")
    p.add_argument("--audio_dir_name", default=DEFAULT_AUDIO_DIR,
                   help=f"Папка озвучки внутри проекта (по умолчанию {DEFAULT_AUDIO_DIR}).")
    p.add_argument("--reorder_unconfirmed_minutes", type=int, default=DEFAULT_REORDER_UNCONFIRMED_MINUTES,
                   help="Отправка заказа без ответа: через сколько минут после неё заказать блок заново "
                        f"(по умолчанию {DEFAULT_REORDER_UNCONFIRMED_MINUTES}; 0 — никогда, сам не перезаказывать).")
    return p.parse_args(argv)


def run_blocks(job: Job, todo: List[Block], audio_dir: str, parallel: int, total: int,
               done: int) -> Tuple[Dict[int, Exception], int]:
    errors: Dict[int, Exception] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(todo)))) as pool:
        futures = {pool.submit(process_block, job, b, audio_dir): b for b in todo}
        for fut in as_completed(futures):
            b = futures[fut]
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001 — собираем все, решаем в конце
                errors[b.idx] = e
                say(f"блок {b.idx} из {total} не готов: {e}", "WARNING")
                continue
            done += 1
            say(f"блок {b.idx} из {total} готов: {b.seconds / 60:.1f} мин речи", "INFO")
            say_progress(done, total, "блоков озвучки")
    return errors, done


def fail_blocks(job: Job, errors: Dict[int, Exception], total: int) -> None:
    ok_n = total - len(errors)
    kept = (f"; готовые блоки ({ok_n} из {total}) сохранены — повтор этой задачи и запуск с «Кэш: "
            "докачивать недостающее» их не закажут заново") if ok_n else ""
    if job.check_error:
        _fail(f"Озвучка FastGen не завершена: {job.check_error}{kept}")
    unconfirmed = [e for e in errors.values() if isinstance(e, UnconfirmedOrder)]
    quota = [e for e in errors.values() if isinstance(e, QuotaSkipped)]
    capped = [e for e in errors.values() if isinstance(e, OrderCap)]
    if unconfirmed:
        _fail(f"Озвучка FastGen не завершена: {unconfirmed[0]}{kept}")
    if capped:
        _fail(f"Озвучка FastGen не завершена: {capped[0]}{kept}")
    if quota:
        _fail(f"{LIMIT_MARKER} FastGen: часовой лимит заказов речи на ключе исчерпан и не освободился "
              f"за {QUOTA_WAIT_MINUTES} мин ожидания; не заказано блоков {len(quota)}{kept}. "
              "Повторите озвучку в следующем часе")
    first = errors[min(errors)]
    _fail(f"Озвучка FastGen не завершена: не готово блоков {len(errors)} из {total}; "
          f"первая причина — {first}{kept}")


def main(argv=None) -> None:
    args = parse_args(argv)
    say("Озвучка FastGen: начинаю…", "PROCESS")
    for w in _ENV_WARNINGS:
        say(f"настройка окружения: {w}", "WARNING")

    # --- всё, что можно проверить без сети и без заказа ---
    try:
        speed, speed_text = _canonical_speed(args.speed)
        voice = str(args.voice or "").strip().lower()
        if not _VOICE_RE.match(voice):
            raise ConfigError(f"--voice: не похоже на голос FastGen: {args.voice!r}")
        model = str(args.model or "").strip()
        if not model:
            raise ConfigError("--model: пусто")
        scene_raw = DEFAULT_SCENE if args.scene is None else str(args.scene).strip()
        scene = "" if scene_raw.lower() in ("off", "none", "-") else scene_raw
        if not 500 <= args.max_chars <= 20000:
            raise ConfigError(f"--max_chars: от 500 до 20000, а не {args.max_chars}")
        if not 0 <= args.reorder_unconfirmed_minutes <= 24 * 60:
            raise ConfigError(f"--reorder_unconfirmed_minutes: от 0 до 1440, а не {args.reorder_unconfirmed_minutes}")
        parallel = max(1, min(MAX_PARALLEL, int(args.parallel)))
        if not _AUDIO_DIR_RE.match(args.audio_dir_name or ""):
            raise ConfigError(f"--audio_dir_name: нужна папка вида _FINAL_AUDIO_ИМЯ, а не {args.audio_dir_name!r}")
        key = next((os.environ.get(n, "").strip() for n in KEY_ENV_NAMES if os.environ.get(n, "").strip()), "")
        if not key:
            raise ConfigError("FastGen: нет ключа (GOOGLER_API_KEY) — тот же ключ, что у картинок FastGen; "
                              "заказ не создан")
        missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
        if missing:
            raise ConfigError(f"не найден {', '.join(missing)} — без него звук не собрать; заказ не создан")
        try:
            with open(args.source_file, "rb") as f:
                source = f.read().decode("utf-8", errors="replace")
        except OSError as e:
            raise ConfigError(f"текст для озвучки не прочитан: {args.source_file}: {e.strerror or e}") from None
        paragraphs = [p.strip() for p in source.splitlines() if p.strip()]
        if not paragraphs:
            raise ConfigError("Файл для озвучки пуст.")
        texts = split_text(paragraphs, args.max_chars)
        if not os.path.isdir(args.project_dir):
            raise ConfigError(f"нет папки проекта: {args.project_dir}")
    except ConfigError as e:
        _fail(str(e))

    audio_dir = os.path.join(args.project_dir, args.audio_dir_name)
    blocks_dir = os.path.join(audio_dir, BLOCKS_DIR_NAME)
    final_path = os.path.join(audio_dir, FINAL_NAME)
    os.makedirs(blocks_dir, exist_ok=True)
    prepare_audio_dir(audio_dir, blocks_dir)

    job = Job(key, model, voice, scene, use_cache=args.use_cache,
              reorder_minutes=args.reorder_unconfirmed_minutes)
    blocks = [Block(idx=i, text=t, path=os.path.join(blocks_dir, f"block_{i:04d}.mp3"),
                    ident=identity(t, voice, model, scene))
              for i, t in enumerate(texts, 1)]
    for b in blocks:
        b.orders = orders_so_far(b)
    total = len(blocks)
    final_ident = final_identity(blocks, speed_text)

    def reuse(path: str) -> bool:
        """Готовое брать без заказа: «докачивать недостающее» или это написала эта же задача."""
        if args.use_cache:
            return True
        try:
            return bool(RUN_ID) and _same_job(read_receipt(path))
        except RuntimeError:
            return False

    if reuse(final_path) and final_ok(final_path, final_ident):
        say(f"Озвучка FastGen уже готова (кэш): {args.audio_dir_name}/{FINAL_NAME} совпадает с текстом, "
            "голосом и скоростью — новых заказов нет", "SUCCESS")
        return
    if os.path.isfile(final_path) and not final_ok(final_path, final_ident):
        # Старый 1.mp3 озвучивал другой текст/голос/скорость: рендер не должен взять его,
        # пока новый не готов. Не удаляем — переносим.
        previous_dir = os.path.join(blocks_dir, PREVIOUS_DIR_NAME)
        _move_aside(final_path, previous_dir)
        if os.path.isfile(receipt_path(final_path)):
            _move_aside(receipt_path(final_path), previous_dir)
        say(f"прежний {FINAL_NAME} не совпадает с текущим текстом, голосом или скоростью — перенёс в "
            f"{BLOCKS_DIR_NAME}/{PREVIOUS_DIR_NAME}/", "INFO")

    try:
        ready = [b for b in blocks if reuse(b.path) and block_ready(b)]
    except RuntimeError as e:
        _fail(f"Озвучка FastGen не завершена: {e}")
    ready_idx = {b.idx for b in ready}
    todo = [b for b in blocks if b.idx not in ready_idx]
    words = sum(b.words for b in blocks)
    say(f"Озвучка FastGen: {total} блоков (~{words} слов, {sum(len(b.text) for b in blocks)} знаков), "
        f"голос {voice}, модель {model}, стиль "
        + ("свой (--scene)" if scene and scene != DEFAULT_SCENE else "документальный рассказчик" if scene
           else "без инструкции")
        + f"; готово {len(ready)}, заказать или дождаться {len(todo)}"
        + ("" if args.use_cache else " (без «докачивать недостающее»: готовое берётся только от этой задачи)"),
        "INFO")

    done = len(ready)
    if todo:
        errors, done = run_blocks(job, todo, audio_dir, parallel, total, done)
        if errors:
            fail_blocks(job, errors, total)

    # Обрыв хвоста длинного блока: темп заметно выше соседей → один перезаказ.
    suspects = tempo_suspects(blocks)
    if suspects:
        before = {}
        for b, wpm, limit in suspects:
            say(f"блок {b.idx}: {wpm:.0f} сл/мин при пороге {limit:.0f} — похоже, модель оборвала хвост; "
                "заказываю блок заново один раз", "WARNING")
            before[b.idx] = b.seconds
            r = read_receipt(b.path)
            save_receipt(job, b, "terminal", str(r.get("generation_id") or ""), "обрубок по темпу",
                         error=f"{wpm:.0f} сл/мин при пороге {limit:.0f}")
        errors, done = run_blocks(job, [b for b, _w, _l in suspects], audio_dir, parallel, total,
                                  total - len(suspects))
        if errors:
            fail_blocks(job, errors, total)
        still = {b.idx: (wpm, limit) for b, wpm, limit in tempo_suspects(blocks)}
        for b, _w, _l in suspects:
            if b.idx not in still:
                continue
            old, new = before[b.idx], b.seconds
            if abs(new - old) <= TEMPO_SAME_LENGTH * max(new, old):
                say(f"блок {b.idx}: повтор дал ту же длину ({old:.0f} → {new:.0f} с) — это естественный "
                    "темп текста, а не обрыв; принимаю", "INFO")
                _mark_tempo_ok(b)
                continue
            wpm, limit = still[b.idx]
            _fail(f"Озвучка FastGen не завершена: блок {b.idx} дважды вышел быстрее соседей ({wpm:.0f} сл/мин "
                  f"при пороге {limit:.0f}; {old:.0f} с и {new:.0f} с) — похоже, модель обрывает его хвост. "
                  "Сократите или разбейте абзацы этого блока и повторите")

    try:
        seconds = build_final(blocks, final_path, blocks_dir, speed)
    except Exception as e:  # noqa: BLE001
        _fail(f"Озвучка FastGen: блоки готовы, но итоговый файл не собран: {e}")
    atomic_json(receipt_path(final_path), {
        "provider": PROVIDER, "kind": "full_text", "state": "done", "identity": final_ident,
        "model": model, "voice": voice, "scene_mark": scene_mark(scene), "speed": speed_text,
        "seconds": round(seconds, 3), "api_scope": job.scope, "job": RUN_ID, "created_at": time.time(),
        "blocks": [{"idx": b.idx, "identity": b.ident, "seconds": round(b.seconds, 3),
                    "generation_id": str(read_receipt(b.path).get("generation_id") or "")} for b in blocks]})
    say(f"Озвучка FastGen завершена: {args.audio_dir_name}/{FINAL_NAME}, {seconds / 60:.1f} мин, "
        f"блоков {total}", "SUCCESS")


if __name__ == "__main__":
    main()
