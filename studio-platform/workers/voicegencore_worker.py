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


def _maybe_start_stopped_queue(token, task_id, status, progress, data, state):
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


def cancel_task(token, task_id):
    """Отмена задачи у провайдера. Зовётся ТОЛЬКО по явному решению человека.

    По локальному таймауту не зовётся намеренно: заказ уже оплачен, и наш собственный
    таймер — не доказательство того, что работа не идёт (см. `_fail_timeout`).
    """
    if not task_id:
        return False
    try:
        url = f"{API_BASE}/client/tasks/{task_id}/cancel"
        r = requests.post(url, headers=_headers(token), timeout=15)
        r.raise_for_status()
        print_debug(f"VoiceGenCore task {task_id} отменена после таймаута")
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
    def apply_audio_speed(_p, _s):
        return
TTS_SPEED = 1.0  # темп речи (atempo); ставится из --speed в main()


def download_audio(token, task_id, output_path):
    """GET /client/tasks/{task_id}/download — скачать mp3."""
    url = f"{API_BASE}/client/tasks/{task_id}/download"
    r = requests.get(url, headers=_headers(token), timeout=120, stream=True)
    r.raise_for_status()
    with open(output_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    apply_audio_speed(output_path, TTS_SPEED)
    print_debug(f"Аудио сохранено: {output_path}")


def process_text(token, text, voice_id, voice_engine, stability,
                 output_dir, filename, thread_count, idx=0):
    """Полный цикл: create → poll → download."""
    output_path = os.path.join(output_dir, filename)

    task_id = None
    task_started_at = None
    task_deadline = None
    create_attempts = 0
    last_heartbeat_at = None
    last_heartbeat_state = None
    queue_recovery_state = {
        "start_attempted": False,
        "last_probe_at": None,
        "provider_accounts_empty": False,
    }

    def _deadline_expired():
        return task_deadline is not None and time.monotonic() >= task_deadline

    def _fail_timeout():
        # 19.09.2026: локальный срок ожидания истёк — но задача у провайдера ОПЛАЧЕНА и
        # может быть почти готова. Раньше здесь стояла отмена: свой таймер выбрасывал
        # чужую оплаченную работу, и продолжить её было уже нечем. Это ровно та грабля,
        # из-за которой 03.09 в бою подняли таймауты картинок в десять раз.
        # Теперь заказ остаётся жить, а его номер печатается машинным маркером —
        # по нему продолжают опрос тем же ID и тем же ключом (маркер и формат взяты из
        # боевого воркера, срез live_2026-09-18).
        print_error_user_friendly(
            f"__VGC_REMOTE_PENDING__|{task_id}|истёк локальный срок ожидания; "
            f"заказ у провайдера не отменён, продолжение использует тот же ID")
        print_error_user_friendly(
            f"VoiceGenCore: задача {task_id} не уложилась в {MAX_WAIT_TIME_MINUTES} мин "
            f"локального ожидания. Заказ у провайдера остался жив и оплачен — его можно "
            f"продолжить по тому же номеру, отменять его мы не стали."
        )
        return False

    def _retry_sleep(seconds):
        if task_deadline is None:
            time.sleep(seconds)
            return True
        remaining = task_deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(seconds, remaining))
        return not _deadline_expired()

    for attempt in range(MAX_RETRIES):
        try:
            if _deadline_expired():
                return _fail_timeout()

            # 1) Create — не более MAX_CREATE_ATTEMPTS раз. Если задача уже создана (task_id есть),
            #    poll/download просто ретраятся по тому же task_id (без дублей).
            if not task_id:
                if create_attempts >= MAX_CREATE_ATTEMPTS:
                    print_error_user_friendly(f"VoiceGenCore: не удалось создать задачу за {MAX_CREATE_ATTEMPTS} попыток (перегрузка сервера?)")
                    return False
                create_attempts += 1
                task_id = create_task(
                    token, text, voice_id,
                    voice_engine=voice_engine,
                    stability=stability,
                    filename=filename,
                    thread_count=thread_count,
                    auto_start=True,
                )
                task_started_at = time.monotonic()
                task_deadline = task_started_at + MAX_WAIT_TIME_MINUTES * 60
                print_debug(f"[{idx}] Task ID: {task_id}. Ожидание...")

            # 2) Poll
            while not _deadline_expired():
                remaining = task_deadline - time.monotonic()
                time.sleep(min(POLL_INTERVAL, max(0, remaining)))
                if _deadline_expired():
                    return _fail_timeout()
                status, progress, data = check_task_status(token, task_id)
                if status == "done":
                    break
                if status in ("error", "failed", "cancelled"):
                    task_obj = data.get("task") or data
                    err = (task_obj.get("error_message") or data.get("error")
                           or data.get("message") or json.dumps(data, ensure_ascii=False)[:300])
                    raise Exception(f"Task {task_id} failed (status={status}): {err}")
                _maybe_start_stopped_queue(
                    token, task_id, status, progress, data, queue_recovery_state
                )
                now = time.monotonic()
                elapsed = int(now - task_started_at)
                pct = f" ({progress}%)" if progress is not None else ""
                status_message = _task_status_message(data)
                heartbeat_state = (status, progress, status_message)
                if (
                    heartbeat_state != last_heartbeat_state
                    or last_heartbeat_at is None
                    or now - last_heartbeat_at >= HEARTBEAT_INTERVAL_SECONDS
                ):
                    print_debug(f"[{idx}] Статус: {status}{pct} ({elapsed}с)...")
                    if (
                        status == "queued"
                        and (
                            _waiting_for_provider_accounts(data)
                            or queue_recovery_state.get("provider_accounts_empty")
                        )
                    ):
                        print_user(
                            "Озвучка: queued (0%) — VoiceGenCore ожидает свободные "
                            f"аккаунты провайдера ({elapsed}с)",
                            "PROCESS",
                        )
                    else:
                        print_user(f"Озвучка: {status}{pct} ({elapsed}с)", "PROCESS")
                    last_heartbeat_state = heartbeat_state
                    last_heartbeat_at = now
            else:
                return _fail_timeout()

            # 3) Download
            download_audio(token, task_id, output_path)
            print_debug(f"[{idx}] Абзац готов: {filename}")
            return True

        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            snippet = _http_error_snippet(e)
            print_debug(f"[{idx}] Попытка {attempt+1}/{MAX_RETRIES} — HTTP {code}: {e}" +
                        (f" | {snippet}" if snippet else ""))
            if code == 401:
                print_error_user_friendly(
                    "VoiceGenCore API: 401 Unauthorized — ключ не принят. "
                    "Проверьте ключ VoiceGenCore в настройках."
                )
                return False
            if code == 403:
                print_error_user_friendly(
                    "VoiceGenCore API: 403 — нет доступа или исчерпан лимит."
                )
                return False
            if code == 429:
                if attempt < MAX_RETRIES - 1:
                    print_user("Превышен лимит запросов VoiceGenCore (429), пауза...", "WARNING")
                    if not _retry_sleep(RETRY_DELAY_SECONDS * (attempt + 2)):
                        return _fail_timeout()
                    continue
                print_error_user_friendly("VoiceGenCore: 429 Too Many Requests.")
                return False
            if code is not None and code >= 500:
                if attempt < MAX_RETRIES - 1:
                    print_user("Сервер VoiceGenCore недоступен, повтор...", "WARNING")
                    if not _retry_sleep(RETRY_DELAY_SECONDS * (attempt + 1)):
                        return _fail_timeout()
                    continue
            # 404 "Result file is not available yet" — НЕ фатально: статус стал done,
            # но файл результата ещё материализуется на сервере (гонка). Ретраим download.
            if code == 404 and "not available yet" in (snippet or "").lower():
                if attempt < MAX_RETRIES - 1:
                    print_user("VoiceGenCore: результат ещё готовится на сервере, ждём...", "WARNING")
                    if not _retry_sleep(RETRY_DELAY_SECONDS * (attempt + 2)):
                        return _fail_timeout()
                    continue
                print_error_user_friendly("VoiceGenCore: результат так и не стал доступен (404 not available yet) после всех попыток.")
                return False
            print_error_user_friendly(f"VoiceGenCore HTTP {code}: {snippet or str(e)}")
            return False

        except requests.exceptions.RequestException as e:
            print_debug(f"[{idx}] Попытка {attempt+1}/{MAX_RETRIES} — сеть: {e}")
            if attempt < MAX_RETRIES - 1:
                print_user("Проблема с сетью VoiceGenCore, повтор...", "WARNING")
                if not _retry_sleep(RETRY_DELAY_SECONDS * (attempt + 1)):
                    return _fail_timeout()
                continue
            print_error_user_friendly(f"VoiceGenCore: ошибка сети после {MAX_RETRIES} попыток.")
            return False

        except Exception as e:
            print_debug(f"[{idx}] Попытка {attempt+1}/{MAX_RETRIES} — ошибка: {e}")
            if "failed (status=" in str(e):
                print_error_user_friendly(f"VoiceGenCore: {str(e)[:200]}")
                return False
            if attempt < MAX_RETRIES - 1:
                print_user(f"Ошибка ({str(e)[:100]}), повтор...", "WARNING")
                if not _retry_sleep(RETRY_DELAY_SECONDS * (attempt + 1)):
                    return _fail_timeout()
            else:
                print_error_user_friendly(f"VoiceGenCore: не удалось обработать абзац {idx}")
                import traceback
                print_debug(traceback.format_exc())
                return False

    return False


def main():
    parser = argparse.ArgumentParser(description="Worker озвучки через VoiceGenCore API.")
    parser.add_argument("--source_file", required=True, help="Текстовый файл (абзацы = строки)")
    parser.add_argument("--project_dir", required=True, help="Папка проекта")
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

    print_debug(f"Токен установлен: {token[:4]}...")

    # Проверка ключа
    try:
        me = check_auth(token)
        print_debug(f"Авторизация VoiceGenCore OK: {json.dumps(me, ensure_ascii=False)[:200]}")
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

    access_error = _vgc_access_error(me, sum(len(p) for p in paragraphs))
    if access_error:
        print_error_user_friendly(access_error)
        return 1

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
        if stale or not args.use_cache:
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
        if args.use_cache and _audio_is_ready(output_path):
            print_debug(f"Пропуск абзаца {idx} (кэш)")
            continue
        tasks.append((idx, paragraph_text, filename))

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
