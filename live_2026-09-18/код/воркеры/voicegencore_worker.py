#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VoiceGenCore API Worker — озвучка текста через https://api.qw1voicegencore.pro/api/v1
Async flow: create task → poll → download mp3.
Паттерн: voicer_worker.py.
"""
import os
import sys
import time
import json
import requests
import argparse
import subprocess
import hashlib
import threading
import queue
from remote_journal_vgc import VGCJournal, JournalError, CredentialRequired, validate_pin, read_json, atomic_json, file_lock
from pathlib import Path

try:
    from i18n import _
except ImportError:
    def _(key, *args, **kwargs):
        return key

try:
    from logger_config import print_user, print_debug, print_progress, print_error_user_friendly
except ImportError:
    def print_user(msg, msg_type="INFO"):
        emoji_map = {"INFO": "ℹ️", "SUCCESS": "✅", "WARNING": "⚠️", "ERROR": "❌", "PROCESS": "⏳"}
        emoji = emoji_map.get(msg_type.upper(), "ℹ️")
        print(f"{emoji} {msg}", flush=True)
    def print_debug(msg):
        pass
    def print_progress(current, total, item_name="элементов"):
        percentage = int((current / total) * 100) if total > 0 else 0
        print_user(f"Обработка {item_name}: {current} из {total} ({percentage}%)", "PROCESS")
    def print_error_user_friendly(error_msg, show_debug_hint=True):
        print_user(error_msg, "ERROR")

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

# --- VoiceGenCore API ---
API_BASE = "https://api.qw1voicegencore.pro/api/v1"

MAX_RETRIES = 8            # попыток на абзац (poll/download ретраятся свободно) — для ночных батчей
MAX_CREATE_ATTEMPTS = 3    # но СОЗДАНИЕ задачи — максимум 3 раза (при таймауте задача могла создаться
                           # на сервере → больше попыток = дубли задач = слив квоты)
RETRY_DELAY_SECONDS = 5
# 03.09.2026: провайдер держит свою очередь — при занятых аккаунтах задача висит в
# queued, и прежние 45 мин срезали ИМЕННО ожидание очереди (147 «Таймаут задачи…»
# в логах кабинета). Потолок поднят ×10 по запросу владельца; env — чтобы крутить
# без правки кода.
MAX_WAIT_TIME_MINUTES = max(1, int(os.environ.get("VGC_MAX_WAIT_MINUTES", "450") or 450))
POLL_INTERVAL = 3
HEARTBEAT_INTERVAL_SECONDS = 30
QUEUE_RECOVERY_PROBE_INTERVAL_SECONDS = 30
AUDIO_DIR_NAME = "_FINAL_AUDIO_VGC"

DEFAULT_VOICE_ENGINE = "elevenLabsV3"
DEFAULT_STABILITY = 0.5
DEFAULT_THREAD_COUNT = 10


def current_vgc_token():
    """Из env VGC_API_TOKEN (ставится APIKeysManager из JSON['voicegencore'])."""
    tok = os.environ.get("VGC_API_TOKEN", "").strip()
    return tok if tok else None


def _headers(token):
    return {
        "X-API-Key": token,
        "Content-Type": "application/json",
    }


def _http_error_snippet(exc):
    try:
        r = exc.response
        if r is not None and r.text:
            return (r.text[:400] + "…") if len(r.text) > 400 else r.text
    except Exception:
        pass
    return ""


# ---------- API functions ----------

def check_auth(token):
    """GET /client/me — проверка ключа."""
    url = f"{API_BASE}/client/me"
    r = requests.get(url, headers=_headers(token), timeout=15)
    r.raise_for_status()
    return r.json()


# 15.09.2026: сразу после сброса дневного лимита провайдера (~02:00) он отвечает медленно —
# один GET /client/me с таймаутом 15 с обрывался («Read timed out»), и задача уходила в error
# без единого повтора (j15120, j15103 тенанта 14), хотя днём обычный повтор проходит за 40 с.
# Стартовую проверку повторяем при обрывах связи / таймаутах / 5xx (и 429) с нарастающей
# паузой; прочие 4xx (ключ отвергнут и т.п.) — сразу стоп. Весь цикл — не дольше ~4 мин.
AUTH_RETRY_ATTEMPTS = 5
AUTH_RETRY_DELAYS = (10, 20, 40, 60)     # паузы между попытками, с
AUTH_RETRY_BUDGET_SECONDS = 240          # верхняя граница всего стартового цикла


class VGCStartupUnavailable(Exception):
    """Провайдер не ответил на стартовую проверку за все отведённые попытки."""


def _startup_error_is_transient(exc):
    """Что повторяем: обрывы связи, таймауты, 5xx и 429. Остальные 4xx — нет."""
    if isinstance(exc, requests.exceptions.HTTPError):
        code = exc.response.status_code if exc.response is not None else None
        return code is not None and (code >= 500 or code == 429)
    return isinstance(exc, requests.exceptions.RequestException)


def _startup_error_brief(exc):
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.exceptions.Timeout):
        return "таймаут"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "нет соединения"
    return type(exc).__name__


def check_auth_with_retry(token, attempts=None, delays=None, budget_seconds=None, sleep=None):
    """check_auth с повторами при временных сбоях. Пауза — через _pause, отмена задачи работает."""
    attempts = AUTH_RETRY_ATTEMPTS if attempts is None else int(attempts)
    delays = tuple(AUTH_RETRY_DELAYS if delays is None else delays)
    budget_seconds = float(AUTH_RETRY_BUDGET_SECONDS if budget_seconds is None else budget_seconds)
    sleep = _pause if sleep is None else sleep
    started = time.monotonic()
    deadline = started + budget_seconds
    last_exc = None
    made = 0
    for attempt in range(1, attempts + 1):
        made = attempt
        try:
            return check_auth(token)
        except Exception as exc:
            if not _startup_error_is_transient(exc):
                raise
            last_exc = exc
        if attempt >= attempts:
            break
        delay = delays[min(attempt - 1, len(delays) - 1)]
        brief = _startup_error_brief(last_exc)
        if time.monotonic() + delay >= deadline:
            print_user(
                f"VoiceGenCore не отвечает ({brief}); лимит ожидания {int(budget_seconds)} с "
                f"исчерпан после {attempt} попыток.",
                "WARNING",
            )
            break
        print_user(
            f"VoiceGenCore не отвечает ({brief}), повтор через {delay} с (попытка {attempt}/{attempts})",
            "WARNING",
        )
        sleep(delay)
    elapsed = int(time.monotonic() - started)
    raise VGCStartupUnavailable(f"после {made} попыток за {elapsed} с: {last_exc}") from last_exc


def get_voice_catalog(token):
    """GET /client/voice-catalog — каталог голосов."""
    url = f"{API_BASE}/client/voice-catalog"
    r = requests.get(url, headers=_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def _voice_field(voice_id):
    """API принимает либо voice_id (каталог), либо elevenlabs_voice_id."""
    vid = str(voice_id or "").strip()
    if not vid:
        return {}
    if len(vid) == 32 and all(c in "0123456789abcdefABCDEF" for c in vid):
        return {"voice_id": vid}
    return {"elevenlabs_voice_id": vid}


def create_task(token, text, voice_id, voice_engine=DEFAULT_VOICE_ENGINE,
                stability=DEFAULT_STABILITY, filename="out.mp3",
                thread_count=DEFAULT_THREAD_COUNT, auto_start=True):
    """POST /client/tasks — создать задачу озвучки."""
    url = f"{API_BASE}/client/tasks"
    payload = {
        "text": text,
        **_voice_field(voice_id),
        "voice_engine": voice_engine,
        "voice_engine_settings": {"stability": float(stability)},
        "filename": filename,
        "thread_count": int(thread_count),
        "auto_start": auto_start,
    }
    # Read-timeout 180с: сервер может долго переваривать большой текст (весь скрипт одной задачей).
    # connect 15с — быстро падаем если сети нет. (Раньше было 60с → read timeout на длинных текстах.)
    r = requests.post(url, headers=_headers(token), json=payload, timeout=(15, 180))
    if r.status_code >= 400:
        detail = ""
        try:
            body = r.json()
            detail = body.get("detail") or body.get("message") or r.text[:300]
        except Exception:
            detail = r.text[:300]
        raise requests.exceptions.HTTPError(
            f"{r.status_code}: {detail}", response=r
        )
    data = r.json()
    task_obj = data.get("task") or {}
    task_id = task_obj.get("task_id") or task_obj.get("id") or data.get("task_id") or data.get("id")
    if not task_id:
        raise Exception(f"API не вернул task_id: {json.dumps(data, ensure_ascii=False)[:300]}")
    return task_id


def check_task_status(token, task_id):
    """GET /client/tasks/{task_id} — статус задачи."""
    url = f"{API_BASE}/client/tasks/{task_id}"
    r = requests.get(url, headers=_headers(token), timeout=30)
    r.raise_for_status()
    data = r.json()
    task_obj = data.get("task") or data
    status = (task_obj.get("status") or data.get("status") or "unknown").strip().lower()
    progress = task_obj.get("progress", data.get("progress"))
    return status, progress, data


def get_queue_snapshot(token):
    """Лёгкий GET /client/runtime — состояние очереди без списка чужих задач."""
    url = f"{API_BASE}/client/runtime"
    r = requests.get(url, headers=_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def start_queue(token):
    """Idempotent POST /client/queue/start для уже созданных задач."""
    url = f"{API_BASE}/client/queue/start"
    r = requests.post(url, headers=_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def _task_status_message(data):
    task_obj = (data.get("task") or data) if isinstance(data, dict) else {}
    if not isinstance(task_obj, dict):
        return ""
    return str(task_obj.get("status_message") or data.get("status_message") or "").strip()


def _is_zero_progress(progress):
    try:
        return float(progress) == 0.0
    except (TypeError, ValueError):
        return False


def _waiting_for_provider_accounts(data):
    message = _task_status_message(data).casefold()
    return (
        "ожидает свободные аккаунты" in message
        or "waiting for free accounts" in message
    )


def _waiting_for_daily_limit(data):
    """Провайдер сам держит задачу до сброса дневного лимита символов.

    Это не зависание: задача продолжится после сброса (у VoiceGenCore около 00:00 UTC).
    13.09.2026 так ждала озвучка тенанта 14: провайдер после сброса взял её в работу
    (8% → 18%), а наш дедлайн в 450 минут от создания снял её в ту же минуту.
    """
    message = _task_status_message(data).casefold()
    return "дневной лимит" in message or "daily limit" in message


def _maybe_start_stopped_queue(token, task_id, status, progress, data, state, persist_before_start=None):
    """Один recovery-POST на lifetime уже созданной provider task.

    `state` принадлежит одному вызову process_text. Флаг ставится ДО POST: даже если
    ответ потеряется по сети, повторный poll не отправит второй start и не устроит
    циклический спам. GET-пробы ограничены одним разом в 30 секунд.
    """
    if state.get("start_attempted") or status != "queued" or not _is_zero_progress(progress):
        return False

    task_obj = (data.get("task") or data) if isinstance(data, dict) else {}
    if not isinstance(task_obj, dict):
        return False
    reported_id = str(task_obj.get("task_id") or task_obj.get("id") or "").strip()
    # Не запускаем глобальную очередь по неполному/чужому status-ответу.
    if not reported_id or reported_id != str(task_id):
        return False

    now = time.monotonic()
    last_probe_at = state.get("last_probe_at")
    if (last_probe_at is not None
            and now - last_probe_at < QUEUE_RECOVERY_PROBE_INTERVAL_SECONDS):
        return False
    state["last_probe_at"] = now

    try:
        snapshot = get_queue_snapshot(token)
    except Exception as exc:
        # Recovery необязателен: сбой лёгкого runtime-probe не должен съедать
        # MAX_RETRIES основной task и обрывать poll уже оплаченной генерации.
        print_debug(f"VoiceGenCore: не удалось проверить состояние очереди: {exc}")
        return False
    accounts = snapshot.get("accounts") if isinstance(snapshot, dict) else None
    if isinstance(accounts, list):
        state["provider_accounts_empty"] = not accounts
    queue = snapshot.get("queue") if isinstance(snapshot, dict) else None
    if not isinstance(queue, dict):
        return False
    try:
        waiting = float(queue.get("waiting") or 0)
    except (TypeError, ValueError):
        waiting = 0
    if queue.get("active") is not False or queue.get("paused") is not False or waiting <= 0:
        return False

    state["start_attempted"] = True
    if persist_before_start is not None:
        persist_before_start()
    try:
        start_queue(token)
    except Exception as exc:
        # Флаг уже выставлен: потерянный HTTP-ответ мог означать успешный POST,
        # поэтому второй start опаснее, чем продолжение authoritative task polling.
        print_debug(f"VoiceGenCore: one-shot запуск очереди не подтверждён: {exc}")
        return False
    print_user(
        "VoiceGenCore: очередь провайдера была остановлена; отправлен один запрос запуска.",
        "WARNING",
    )
    return True


def _bounded_cancel_http(method, url, token, timeout):
    """Hard wall-clock bound, including a peer that trickles its HTTP body."""
    result = queue.Queue(maxsize=1)
    def request():
        try:
            result.put((True, requests.request(method, url, headers=_headers(token), timeout=(min(5, timeout), min(15, timeout)))))
        except Exception as exc:
            result.put((False, exc))
    # The caller persists cancel intent first. A timed-out request can finish,
    # but a retry never sends another cancel; no billable create is used here.
    threading.Thread(target=request, daemon=True).start()
    try:
        ok, value = result.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError('VoiceGenCore: истёк срок подтверждения отмены') from exc
    if not ok:
        raise value
    return value


def cancel_task(token, task_id, timeout=15):
    """Bounded best-effort cancellation after an explicit user request only."""
    if not task_id:
        return False
    try:
        url = f"{API_BASE}/client/tasks/{task_id}/cancel"
        r = _bounded_cancel_http('POST', url, token, timeout)
        r.raise_for_status()
        print_debug(f"VoiceGenCore task {task_id}: запрос отмены пользователя принят")
        return True
    except Exception as exc:
        print_debug(f"Не удалось отменить VoiceGenCore task {task_id}: {exc}")
        return False


def _account_info(me):
    """Достаёт поля аккаунта из прямого или обёрнутого ответа /client/me."""
    if not isinstance(me, dict):
        return {}
    account_fields = {
        "has_active_subscription", "free_remaining", "max_threads",
        "is_active", "is_blocked", "current_plan",
    }
    pending = [me]
    seen = set()
    while pending:
        current = pending.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        if account_fields.intersection(current):
            return current
        for key in ("data", "client", "account", "user"):
            nested = current.get(key)
            if isinstance(nested, dict):
                pending.append(nested)
    return me


def _explicit_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "active"):
            return True
        if normalized in ("false", "0", "no", "inactive"):
            return False
    return None


def _number_field(info, names):
    for name in names:
        if name not in info:
            continue
        try:
            return float(info[name])
        except (TypeError, ValueError):
            continue
    return None


def _vgc_access_error(me, required_characters):
    """Возвращает понятную причину запрета или None, если /client/me даёт доступ."""
    info = _account_info(me)
    if not info:
        return "VoiceGenCore: /client/me не вернул данные аккаунта; запуск остановлен."

    if _explicit_bool(info.get("is_blocked")) is True or _explicit_bool(info.get("blocked")) is True:
        return "VoiceGenCore: аккаунт заблокирован провайдером."
    if "is_active" in info and _explicit_bool(info.get("is_active")) is False:
        return "VoiceGenCore: аккаунт провайдера неактивен."
    max_threads = _number_field(info, ("max_threads", "threads_limit", "max_concurrent_threads"))
    if max_threads is not None and max_threads <= 0:
        return "VoiceGenCore: провайдер не разрешил ни одного потока для этого аккаунта."

    subscribed = _explicit_bool(info.get("has_active_subscription"))
    free_remaining = _number_field(
        info,
        ("free_remaining", "free_characters_remaining", "free_symbols_remaining"),
    )
    if subscribed is True:
        return None
    if free_remaining is not None and free_remaining > 0:
        if required_characters > free_remaining:
            return (
                "VoiceGenCore: подписка неактивна, бесплатного лимита недостаточно: "
                f"нужно {required_characters} символов, доступно {int(free_remaining)}."
            )
        return None
    if subscribed is False:
        return "VoiceGenCore: подписка неактивна и бесплатный лимит исчерпан."
    return (
        "VoiceGenCore: /client/me не подтвердил активную подписку или доступный "
        "бесплатный лимит; запуск остановлен."
    )


def _clamp_thread_count(requested, me):
    requested = max(1, int(requested))
    info = _account_info(me)
    maximum = _number_field(info, ("max_threads", "threads_limit", "max_concurrent_threads"))
    if maximum is None:
        return requested, None
    maximum = max(1, int(maximum))
    return min(requested, maximum), maximum


def _audio_is_ready(path):
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _verify_audio_completeness(output_dir, expected):
    """Fail-closed гейт: исключение проверки никогда не превращается в SUCCESS."""
    try:
        produced = sum(
            1 for idx in range(1, expected + 1)
            if _audio_is_ready(os.path.join(output_dir, f"{idx}.mp3"))
        )
    except Exception as exc:
        print_error_user_friendly(f"VoiceGenCore: не удалось проверить полноту озвучки: {exc}")
        return False
    if produced < expected:
        print_error_user_friendly(
            f"⚠️ Озвучка неполная: озвучено {produced} абзацев из {expected}. "
            "Доозвучь проект, рендер остановлен."
        )
        return False
    return True


try:
    from portable_paths import apply_audio_speed
except Exception:
    def apply_audio_speed(_p, _s, strict=False):
        if abs(float(_s) - 1.0) >= .01:
            raise JournalError('VoiceGenCore: модуль изменения скорости недоступен; исходный remote ID сохранён')
        return True
TTS_SPEED = 1.0  # темп речи (atempo); ставится из --speed в main()


class VGCCancelled(JournalError):
    pass


def _cancel_requested():
    path = os.environ.get("VS_QUEUE_CANCEL_FILE")
    return bool(path and os.path.isfile(path))


def _pause(seconds):
    deadline = time.monotonic() + max(0, seconds)
    while time.monotonic() < deadline:
        if _cancel_requested():
            raise VGCCancelled("пользователь запросил отмену")
        time.sleep(min(.2, max(0, deadline - time.monotonic())))


def download_audio(token, task_id, output_path):
    """Bounded download to a private temporary MP3, validated before publish."""
    deadline = time.monotonic() + max(1, float(os.environ.get('VGC_DOWNLOAD_TIMEOUT_SECONDS', '900')))
    suffix = hashlib.sha256(str(task_id).encode()).hexdigest()[:12]
    temporary = output_path + '.part-' + suffix + '.mp3'
    url = f"{API_BASE}/client/tasks/{task_id}/download"
    try:
        with requests.get(url, headers=_headers(token), timeout=(15, 30), stream=True) as response:
            response.raise_for_status()
            expected = response.headers.get('Content-Length') if not response.headers.get('Content-Encoding') else None
            size = 0
            with open(temporary, 'wb') as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if _cancel_requested():
                        raise VGCCancelled('пользователь запросил отмену')
                    if time.monotonic() >= deadline:
                        raise TimeoutError('VoiceGenCore: истёк срок скачивания; remote task ID сохранён')
                    if chunk:
                        handle.write(chunk)
                        size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if not size or (expected and int(expected) != size):
                raise ValueError('VoiceGenCore: неполное скачивание аудио')
        apply_audio_speed(temporary, TTS_SPEED, strict=True)
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type:format=duration',
                                '-of', 'json', temporary], capture_output=True, text=True, timeout=20)
        metadata = json.loads(probe.stdout) if probe.returncode == 0 else {}
        if (not any(stream.get('codec_type') == 'audio' for stream in metadata.get('streams', []))
                or float(metadata.get('format', {}).get('duration') or 0) <= 0):
            raise ValueError('VoiceGenCore: результат не является корректным аудио')
        if _cancel_requested():
            raise VGCCancelled('пользователь запросил отмену')
        with open(temporary, 'rb') as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
        fd = os.open(str(Path(output_path).parent), os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _cancel_remote_once(journal, token):
    record = journal.record
    if not record or not record.get('task_id'):
        return
    if not record.get('cancel_attempted'):
        journal.state('cancel_requested', cancel_attempted=True)
        confirmed = cancel_task(token, record['task_id'])
        journal.state('cancel_requested', cancel_response='accepted' if confirmed else 'unknown')
    try:
        response = _bounded_cancel_http('GET', f"{API_BASE}/client/tasks/{record['task_id']}", token, 5)
        response.raise_for_status()
        data = response.json()
        status = str((data.get('task') or data).get('status', '')).lower()
        if status in {'cancelled', 'canceled', 'failed', 'error', 'done', 'completed'}:
            journal.state('cancelled', remote_status=status)
    except Exception:
        pass
    print_error_user_friendly('VoiceGenCore: отмена пользователя сохранена; remote task ID сохранён, повторной генерации нет')


def _process_text_durable(journal, token, text, voice_id, voice_engine, stability, filename, thread_count, idx):
    record = journal.load()
    if _cancel_requested():
        _cancel_remote_once(journal, token)
        return False
    if journal.completed_matches():
        return True
    if record and record.get('state') in {'intent', 'uncertain'}:
        raise JournalError('VoiceGenCore: исход создания неизвестен; намерение сохранено. Сверьте задачу у провайдера, новый POST заблокирован')
    if record and record.get('state') == 'cancel_requested':
        raise JournalError('VoiceGenCore: задача отменена; автоматическое повторное создание остановлено')
    task_id = record.get('task_id') if record and record.get('state') not in {'failed', 'cancelled'} else None
    if task_id:
        # A missing/corrupt completed file needs the original remote result
        # again; restore its credential pin before making any network request.
        journal.state('pending')
    invocation_deadline = time.monotonic() + MAX_WAIT_TIME_MINUTES * 60
    recovery = {'start_attempted': bool(record and record.get('queue_start_attempted')),
                'last_probe_at': None, 'provider_accounts_empty': False}
    last_heartbeat = 0
    previous_state = None
    for attempt in range(MAX_RETRIES):
        try:
            if _cancel_requested():
                raise VGCCancelled('пользователь запросил отмену')
            if not task_id:
                journal.intent(MAX_CREATE_ATTEMPTS)
                try:
                    task_id = create_task(token, text, voice_id, voice_engine=voice_engine,
                                          stability=stability, filename=filename, thread_count=thread_count, auto_start=True)
                except requests.exceptions.HTTPError as exc:
                    code = exc.response.status_code if exc.response is not None else None
                    definitive = code in {400, 401, 403, 404, 422, 429}
                    journal.state('rejected' if definitive else 'uncertain', create_error_code=code)
                    raise JournalError(f'VoiceGenCore: создание HTTP {code}; ' + ('запрос отклонён, remote ID не создан' if definitive else 'исход неизвестен, новый POST заблокирован')) from exc
                except Exception as exc:
                    journal.state('uncertain', create_error_type=type(exc).__name__)
                    raise JournalError('VoiceGenCore: ответ создания не подтверждён; новый POST заблокирован, требуется сверка у провайдера') from exc
                journal.accepted(task_id)
            while True:
                if _cancel_requested():
                    raise VGCCancelled('пользователь запросил отмену')
                if time.monotonic() >= invocation_deadline:
                    journal.state('pending', last_error='local polling deadline; remote task preserved')
                    print_error_user_friendly(f'__VGC_REMOTE_PENDING__|{task_id}|истёк локальный срок ожидания; продолжение использует тот же ID и исходный ключ')
                    return False
                status, progress, data = check_task_status(token, task_id)
                if status in {'done', 'completed'}:
                    journal.state('ready', remote_status=status)
                    break
                if status in {'failed', 'error', 'cancelled'}:
                    journal.state('cancelled' if status == 'cancelled' else 'failed', remote_status=status)
                    print_error_user_friendly(f'VoiceGenCore: remote task {task_id} завершилась со статусом {status}; ID сохранён')
                    return False
                journal.state('active', remote_status=status, remote_progress=progress)
                def persist_start():
                    journal.state('active', queue_start_attempted=True)
                _maybe_start_stopped_queue(token, task_id, status, progress, data, recovery, persist_before_start=persist_start)
                now = time.monotonic()
                state = (status, progress, _task_status_message(data))
                if state != previous_state or now-last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    detail = _task_status_message(data)
                    print_user(f'Озвучка: {status} ({progress if progress is not None else "?"}%) — {detail}', 'PROCESS')
                    previous_state, last_heartbeat = state, now
                _pause(min(POLL_INTERVAL, max(0, invocation_deadline-time.monotonic())))
            download_audio(token, task_id, str(journal.output))
            journal.complete()
            return True
        except VGCCancelled:
            _cancel_remote_once(journal, token)
            return False
        except JournalError:
            raise
        except Exception as exc:
            # Once accepted, all retry paths keep the same task_id. Timeouts
            # never cancel paid work and never turn into a fresh POST.
            if journal.record:
                journal.state('pending', last_error_type=type(exc).__name__)
            if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None and exc.response.status_code in {401,403}:
                print_error_user_friendly('VoiceGenCore: исходный ключ больше не имеет доступа; remote ID сохранён')
                return False
            if attempt + 1 == MAX_RETRIES:
                print_error_user_friendly(f'__VGC_REMOTE_PENDING__|{task_id}|опрос/скачивание не завершены; ID и ключ закреплены для продолжения')
                return False
            try:
                _pause(min(RETRY_DELAY_SECONDS*(attempt+1), max(0, invocation_deadline-time.monotonic())))
            except VGCCancelled:
                _cancel_remote_once(journal, token)
                return False
    return False


def process_text(token, text, voice_id, voice_engine, stability,
                 output_dir, filename, thread_count, idx=0):
    try:
        journal = VGCJournal(output_dir, filename, token, text, voice_id, voice_engine, stability, TTS_SPEED)
        with journal.lock():
            return _process_text_durable(journal, token, text, voice_id, voice_engine, stability, filename, thread_count, idx)
    except CredentialRequired as exc:
        print_error_user_friendly('__VGC_CREDENTIAL_REQUIRED__|' + exc.fingerprint)
        print_error_user_friendly(str(exc))
        return False
    except (JournalError, OSError) as exc:
        print_error_user_friendly(str(exc).replace(token, '[redacted]'))
        return False


def cancel_pending_project(project_dir, token, budget_seconds=30):
    """Cancel/reconcile only saved IDs; bounded and independent of source.txt."""
    deadline = time.monotonic() + min(30, max(.1, float(budget_seconds)))
    unsettled = 0
    try:
        root = validate_pin(project_dir, token)
        for path in sorted(root.glob('request-*.json')):
            record = read_json(path)
            if record.get('state') in VGCJournal.TERMINAL:
                continue
            if time.monotonic() >= deadline:
                unsettled += 1
                continue
            if not record.get('task_id'):
                # Never guess which billable task an uncertain create made.
                unsettled += 1
                continue
            journal = VGCJournal.from_saved(project_dir, path, token)
            with journal.lock():
                record = journal.load()
                if not record.get('cancel_attempted'):
                    journal.state('cancel_requested', cancel_attempted=True)
                    confirmed = cancel_task(token, record['task_id'], timeout=max(.01, min(15, deadline-time.monotonic())))
                    journal.state('cancel_requested', cancel_response='accepted' if confirmed else 'unknown')
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    unsettled += 1
                    continue
                try:
                    response = _bounded_cancel_http('GET', f"{API_BASE}/client/tasks/{record['task_id']}", token, max(.01, min(5, remaining)))
                    response.raise_for_status()
                    data = response.json()
                    status = str((data.get('task') or data).get('status', '')).lower()
                    if status in {'cancelled', 'canceled', 'failed', 'error', 'done', 'completed'}:
                        journal.state('cancelled', remote_status=status)
                    else:
                        unsettled += 1
                except Exception:
                    unsettled += 1
        pinpath = root / 'credential_pin.json'
        if pinpath.exists():
            with file_lock(root / 'pin.lock'):
                records = [read_json(path) for path in root.glob('request-*.json')]
                pin = read_json(pinpath)
                if records and all(r.get('state') in VGCJournal.TERMINAL for r in records):
                    pin['state'] = 'released'
                    atomic_json(pinpath, pin)
                elif pin.get('state') != 'released':
                    unsettled = max(1, unsettled)
        if unsettled:
            print_error_user_friendly(f'VoiceGenCore: отмена ещё не подтверждена для {unsettled} запросов; журнал и ID сохранены')
            return 2
        print_user('VoiceGenCore: сохранённые удалённые запросы завершены или отменены', 'SUCCESS')
        return 0
    except CredentialRequired as exc:
        print_error_user_friendly('__VGC_CREDENTIAL_REQUIRED__|' + exc.fingerprint)
        return 2
    except (JournalError, OSError) as exc:
        print_error_user_friendly(str(exc).replace(token, '[redacted]'))
        return 2


def main():
    parser = argparse.ArgumentParser(description="Worker озвучки через VoiceGenCore API.")
    parser.add_argument("--source_file", help="Текстовый файл (абзацы = строки)")
    parser.add_argument("--project_dir", required=True, help="Папка проекта")
    parser.add_argument("--cancel_pending", action="store_true", help="Отменить только сохранённые remote ID без нового создания задач")
    parser.add_argument("--use_cache", action="store_true", help="Пропускать готовые файлы")
    parser.add_argument("--voice_id", default="", help="ID из каталога VGC (32 hex) или elevenlabs_voice_id")
    parser.add_argument("--voice_engine", default=DEFAULT_VOICE_ENGINE, help="Движок (elevenLabsV3)")
    parser.add_argument("--stability", default=str(DEFAULT_STABILITY), help="Stability 0-1")
    parser.add_argument("--thread_count", default=str(DEFAULT_THREAD_COUNT), help="Потоков обработки (1-50)")
    parser.add_argument("--speed", default=os.environ.get("TTS_SPEED", "1.0"), help="Темп речи (atempo): 1.0=норма, <1 медленнее, >1 быстрее")
    parser.add_argument("--full_text", action="store_true",
                        help="Весь текст ОДНОЙ задачей → один mp3 (VGC сам режет на чанки на сервере). "
                             "Кратно быстрее поабзацной озвучки (1 задача вместо сотен).")
    args = parser.parse_args()
    global TTS_SPEED
    try:
        TTS_SPEED = float(args.speed)
    except (TypeError, ValueError):
        TTS_SPEED = 1.0

    print_user("Озвучка VoiceGenCore API...", "PROCESS")

    token = current_vgc_token()
    if not token:
        print_error_user_friendly("Ключ VoiceGenCore не установлен! Добавьте в Настройки → API ключи.")
        return 1

    if args.cancel_pending:
        return cancel_pending_project(args.project_dir, token)
    if not args.source_file:
        print_error_user_friendly('Не указан --source_file для озвучки')
        return 1

    print_debug("Токен установлен")
    try:
        journal_root = validate_pin(args.project_dir, token)
        resume_journal = (journal_root / 'credential_pin.json').exists()
    except CredentialRequired as exc:
        print_error_user_friendly('__VGC_CREDENTIAL_REQUIRED__|' + exc.fingerprint)
        return 1
    except JournalError as exc:
        print_error_user_friendly(str(exc))
        return 1

    # Проверка ключа — с повторами при обрывах связи (см. check_auth_with_retry)
    try:
        me = check_auth_with_retry(token)
        print_debug("Авторизация VoiceGenCore OK")
    except VGCCancelled:
        print_user("VoiceGenCore: отмена во время ожидания ответа провайдера.", "WARNING")
        return 1
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        if code in (401, 403):
            print_error_user_friendly(f"VoiceGenCore: ключ отвергнут (HTTP {code}). Проверьте ключ.")
        else:
            print_error_user_friendly(f"VoiceGenCore: ошибка авторизации (HTTP {code})")
        return 1
    except Exception as e:
        print_error_user_friendly(f"VoiceGenCore: не удалось подключиться: {e}")
        return 1

    # Чтение текста
    try:
        with open(args.source_file, 'r', encoding='utf-8', errors='replace') as f:
            paragraphs = [p.strip() for p in f.read().splitlines() if p.strip()]
    except FileNotFoundError:
        print_error_user_friendly(f"Файл не найден: {args.source_file}")
        return 1

    if not paragraphs:
        print_error_user_friendly("Файл для озвучки пуст.")
        return 1

    if args.full_text:
        # Весь скрипт → одна задача → один 1.mp3. VGC сам режет на чанки (chunk_size ~1200) на
        # СВОЕЙ стороне, поэтому не нужно слать сотни абзацев по одному. Резко быстрее.
        paragraphs = ["\n".join(paragraphs)]
        print_user("Режим «весь текст»: одна задача VGC вместо поабзацной озвучки.", "INFO")

    output_dir = os.path.join(args.project_dir, AUDIO_DIR_NAME)
    os.makedirs(output_dir, exist_ok=True)
    print_debug(f"Выходная папка: {output_dir}")

    if args.full_text:
        # full_text = один 1.mp3. Убираем старую ПОАБЗАЦНУЮ озвучку (2.mp3+ от прежних прогонов):
        # иначе рендер склеит целое (1.mp3) + абзацы (2..N.mp3) = задвоение, а кэш примет старый
        # 1.mp3 (=абзац 1) за готовый full_text. Если только 1.mp3 и кэш вкл — оставляем (валидный кэш).
        try:
            existing = [f for f in os.listdir(output_dir) if f.lower().endswith(".mp3")]
        except OSError:
            existing = []
        stale = [f for f in existing if f != "1.mp3"]
        if (stale or not args.use_cache) and not resume_journal:
            for f in existing:
                try:
                    os.remove(os.path.join(output_dir, f))
                except Exception:
                    pass
            if stale:
                print_user(f"Убрал старую поабзацную озвучку ({len(existing)} mp3) — full_text пишет один файл.", "INFO")

    voice_id = args.voice_id.strip()
    if not voice_id:
        print_error_user_friendly("Не указан Voice ID! Укажите --voice_id.")
        return 1

    voice_engine = args.voice_engine.strip() or DEFAULT_VOICE_ENGINE
    try:
        stability = float(args.stability)
    except (ValueError, TypeError):
        stability = DEFAULT_STABILITY
    try:
        thread_count = int(args.thread_count)
    except (ValueError, TypeError):
        thread_count = DEFAULT_THREAD_COUNT
    requested_thread_count = thread_count
    thread_count, max_threads = _clamp_thread_count(thread_count, me)
    if thread_count != requested_thread_count:
        print_user(
            f"VoiceGenCore: запрошено потоков {requested_thread_count}, доступно {max_threads}; "
            f"использую {thread_count}.",
            "WARNING",
        )

    # Фильтрация по кэшу
    tasks = []
    for idx, paragraph_text in enumerate(paragraphs, 1):
        filename = f"{idx}.mp3"
        output_path = os.path.join(output_dir, filename)
        if args.use_cache and _audio_is_ready(output_path) and not resume_journal:
            print_debug(f"Пропуск абзаца {idx} (кэш)")
            continue
        tasks.append((idx, paragraph_text, filename))

    # Accepted tasks have already spent quota; do not block their download
    # with a second preflight charge for the original text length.
    required_characters = 0
    try:
        for idx, paragraph_text, filename in tasks:
            journal = VGCJournal(output_dir, filename, token, paragraph_text, voice_id, voice_engine, stability, TTS_SPEED)
            record = journal.load()
            if not record or record.get('state') in {'rejected', 'failed', 'cancelled'}:
                required_characters += len(paragraph_text)
    except JournalError as exc:
        print_error_user_friendly(str(exc))
        return 1
    access_error = _vgc_access_error(me, required_characters) if required_characters else None
    if access_error:
        print_error_user_friendly(access_error)
        return 1

    if not tasks:
        if not _verify_audio_completeness(output_dir, len(paragraphs)):
            return 1
        print_user("Все абзацы уже озвучены (кэш).", "INFO")
        return 0

    print_user(f"Обработка {len(tasks)} абзацев через VoiceGenCore...", "PROCESS")

    completed = 0
    failures = 0
    for idx, paragraph_text, filename in tasks:
        result = process_text(
            token, paragraph_text, voice_id, voice_engine,
            stability, output_dir, filename, thread_count, idx=idx
        )
        completed += 1
        if result is not True:
            failures += 1
        if completed % 3 == 0 or completed == len(tasks):
            print_progress(completed, len(tasks), "абзацев")

    if failures > 0:
        print_error_user_friendly(f"Не удалось обработать {failures} абзацев из {len(tasks)}")
        return 1

    # Гейт полноты: сколько mp3 реально лежит в папке против числа абзацев исходного текста.
    # Ловит случаи, когда часть абзацев так и не озвучена (обрыв прогона, пропуск, порча кэша).
    if not _verify_audio_completeness(output_dir, len(paragraphs)):
        return 1

    print_user("Озвучка VoiceGenCore завершена!", "SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
