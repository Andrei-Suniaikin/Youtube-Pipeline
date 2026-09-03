#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Voicer API Worker (Mat3u) — озвучка текста через https://voicer.mat3u.com/api/v1
Использует Bearer-токен, поддерживает voice_id, model_id, автопаузы.
"""
import os
import sys
import time
import json
import requests
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

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

# --- Voicer API ---
API_BASE = "https://voicer.mat3u.com/api/v1"


def _normalize_voicer_token(raw):
    """Убирает пробелы/переносы; если вставили целиком «Bearer …», оставляет только токен."""
    if raw is None:
        return ""
    t = str(raw).strip()
    if not t:
        return ""
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


def current_voicer_token():
    """Всегда из os.environ — при in-process запуске ключ может обновиться после первого импорта модуля."""
    return _normalize_voicer_token(os.environ.get("VOICER_API_TOKEN", ""))


MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5
MAX_WAIT_TIME_MINUTES = 10
HEARTBEAT_INTERVAL_SECONDS = 30
AUDIO_DIR_NAME = "_FINAL_AUDIO_VOICER"
# Потоков (абзацев параллельно) внутри одного прогона. Настраивается через env VOICER_PARALLEL
# (сервер выставляет его из _concurrency в video_studio_api_keys.json). Лимит API Voicer — 5 задач/ключ.
try:
    PARALLEL_REQUESTS = max(1, int(os.environ.get("VOICER_PARALLEL", "2") or "2"))
except (TypeError, ValueError):
    PARALLEL_REQUESTS = 2

# Дефолты по документации
DEFAULT_VOICE_ID = "AB9XsbSA4eLG12t2myjN"
DEFAULT_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_SPLIT_TYPE = "smart"
# API Voicer 2.1: smart | paragraphs | max_length (значение "sentences" из UI недопустимо → маппинг)
VALID_SPLIT_TYPES = frozenset({"smart", "paragraphs", "max_length"})
SPLIT_TYPE_ALIASES = {"sentences": "smart"}


def normalize_split_type(split_type):
    if not split_type:
        return DEFAULT_SPLIT_TYPE
    s = str(split_type).strip().lower()
    if s in SPLIT_TYPE_ALIASES:
        s = SPLIT_TYPE_ALIASES[s]
    if s in VALID_SPLIT_TYPES:
        return s
    return DEFAULT_SPLIT_TYPE


def get_headers():
    token = current_voicer_token()
    if not token:
        raise ValueError(_('voicer_api_key_error'))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def create_task(text, voice_id=None, model_id=None, split_type=None,
                auto_pause_enabled=False, auto_pause_duration=0.5, auto_pause_frequency=1):
    url = f"{API_BASE}/voice/synthesize"
    payload = {
        "text": text,
        "voice_id": voice_id or DEFAULT_VOICE_ID,
        "model_id": model_id or DEFAULT_MODEL_ID,
        "split_type": normalize_split_type(split_type),
    }
    if auto_pause_enabled:
        payload["auto_pause_enabled"] = True
        payload["auto_pause_duration"] = float(auto_pause_duration)
        payload["auto_pause_frequency"] = int(auto_pause_frequency)
    response = requests.post(url, json=payload, headers=get_headers(), timeout=60)
    response.raise_for_status()
    data = response.json()
    task_id = data.get("task_id")
    if not task_id:
        raise Exception("Ответ API не содержит task_id")
    est = data.get("estimated_time")
    try:
        estimated_time = float(est) if est is not None else None
    except (TypeError, ValueError):
        estimated_time = None
    return task_id, estimated_time


class VoicerTaskTerminalError(Exception):
    """Провайдер сообщил финальный неуспешный статус задачи."""


def _wait_seconds(estimated_time_seconds=None):
    max_seconds = MAX_WAIT_TIME_MINUTES * 60
    if estimated_time_seconds is not None and estimated_time_seconds > 0:
        # Запас относительно оценки API, чтобы длинные задачи не обрывались по таймауту
        max_seconds = max(max_seconds, estimated_time_seconds * 2.5 + 120)
    return max_seconds


def _sleep_with_deadline(seconds, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(seconds, remaining))
    return time.monotonic() < deadline


def wait_for_completed(task_id, estimated_time_seconds=None, deadline=None):
    start = time.monotonic()
    if deadline is None:
        deadline = start + _wait_seconds(estimated_time_seconds)
    url = f"{API_BASE}/voice/status/{task_id}"
    transient_errors = 0
    last_heartbeat_at = None
    last_heartbeat_status = None
    while time.monotonic() < deadline:
        if not _sleep_with_deadline(RETRY_DELAY_SECONDS, deadline):
            break
        try:
            r = requests.get(url, headers=get_headers(), timeout=30)
            r.raise_for_status()
            transient_errors = 0
            try:
                body = r.json()
            except ValueError as exc:
                raise VoicerTaskTerminalError(
                    "Voicer вернул невалидный JSON статуса задачи"
                ) from exc
            if not isinstance(body, dict):
                raise VoicerTaskTerminalError(
                    f"Неожиданный ответ статуса: {type(body).__name__}"
                )
            status = body.get("status") or body.get("task_status")
            if isinstance(status, str):
                status = status.strip().lower()
            if status == "completed":
                return True
            if status == "censored":
                blocked = body.get("blocked_chunks") or []
                parts = []
                for ch in blocked:
                    if isinstance(ch, dict):
                        parts.append(f"[{ch.get('index', '?')}] {ch.get('text', '')[:200]}")
                detail = "; ".join(parts) if parts else "(нет описания чанков)"
                raise VoicerTaskTerminalError(
                    "Задача остановлена цензурой ElevenLabs (status=censored). "
                    "Исправьте текст заблокированных фрагментов и вызовите POST /voice/retry/{task_id}. "
                    f"blocked_chunks: {detail}"
                )
            if status in ("failed", "error", "cancelled"):
                err = (body.get("error_message") or body.get("error") or
                       body.get("details") or body.get("reason") or
                       body.get("message") or json.dumps(body, ensure_ascii=False)[-300:])
                raise VoicerTaskTerminalError(
                    f"Voicer task {task_id} failed (status={status}): {err}"
                )
            print_debug(f"Статус задачи {task_id}: {status}...")
            now = time.monotonic()
            elapsed = int(now - start)
            if (
                status != last_heartbeat_status
                or last_heartbeat_at is None
                or now - last_heartbeat_at >= HEARTBEAT_INTERVAL_SECONDS
            ):
                print_user(f"Озвучка: {status} ({elapsed}с)", "PROCESS")
                last_heartbeat_status = status
                last_heartbeat_at = now
        except requests.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if code in (401, 403, 404) or (code is not None and 400 <= code < 500 and code != 429):
                raise
            if code == 429 or (code is not None and code >= 500):
                transient_errors += 1
                print_debug(
                    f"Временная HTTP-ошибка статуса {task_id}: {code} "
                    f"({transient_errors}/{MAX_RETRIES})"
                )
                if transient_errors >= MAX_RETRIES:
                    raise
                continue
            raise
        except requests.exceptions.RequestException as e:
            transient_errors += 1
            print_debug(f"Ошибка проверки статуса {task_id}: {e}")
            if transient_errors >= MAX_RETRIES:
                raise
    raise TimeoutError(f"Превышено время ожидания для задачи {task_id}")


try:
    from portable_paths import apply_audio_speed
except Exception:
    def apply_audio_speed(_p, _s):
        return
TTS_SPEED = 1.0  # темп речи (atempo); ставится из --speed в main()


def download_audio(task_id, output_path):
    url = f"{API_BASE}/voice/download/{task_id}"
    response = requests.get(url, headers=get_headers(), timeout=120, stream=True)
    response.raise_for_status()
    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    apply_audio_speed(output_path, TTS_SPEED)
    print_debug(f"Аудио сохранено: {output_path}")


def _http_error_snippet(exc):
    try:
        r = exc.response
        if r is not None and r.text:
            return (r.text[:400] + "…") if len(r.text) > 400 else r.text
    except Exception:
        pass
    return ""


def _audio_is_ready(path):
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _verify_audio_completeness(output_dir, expected):
    try:
        produced = sum(
            1 for idx in range(1, expected + 1)
            if _audio_is_ready(os.path.join(output_dir, f"{idx}.mp3"))
        )
    except Exception as exc:
        print_error_user_friendly(f"Voicer: не удалось проверить полноту озвучки: {exc}")
        return False
    if produced < expected:
        print_error_user_friendly(
            f"⚠️ Озвучка неполная: озвучено {produced} абзацев из {expected}. "
            "Доозвучь проект, рендер остановлен."
        )
        return False
    return True


def process_paragraph(data):
    idx, paragraph_text, output_mp3_path, voice_id, model_id, split_type, auto_pause_enabled, auto_pause_duration, auto_pause_frequency = data
    print_debug(f"[{idx}] Обработка абзаца...")

    task_id = None
    estimated_time = None
    # POST может успеть создать платную задачу даже при сетевом таймауте/5xx. Автоматически
    # повторяем create только после явного 429 (сервер запрос отверг); иначе fail-closed.
    for attempt in range(MAX_RETRIES):
        try:
            task_id, estimated_time = create_task(
                paragraph_text,
                voice_id=voice_id,
                model_id=model_id,
                split_type=split_type,
                auto_pause_enabled=auto_pause_enabled,
                auto_pause_duration=auto_pause_duration,
                auto_pause_frequency=auto_pause_frequency,
            )
            if not task_id:
                raise Exception("Не получен task_id")
            break
        except requests.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            snippet = _http_error_snippet(exc)
            if code == 429 and attempt < MAX_RETRIES - 1:
                print_user("Превышен лимит запросов Voicer (429), пауза и повтор...", "WARNING")
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            if code in (401, 403):
                print_error_user_friendly(
                    f"Voicer API: ключ или доступ отклонён (HTTP {code}). Проверьте ключ и лимит."
                )
            elif code in (400, 404, 422):
                print_error_user_friendly(
                    f"Voicer API: создать задачу не удалось (HTTP {code}). {snippet or str(exc)}"
                )
            else:
                print_error_user_friendly(
                    "Voicer API: ответ на создание задачи неоднозначен; повторный create отключён, "
                    "чтобы не списать квоту дважды. Повторите запуск позже."
                )
            return False
        except requests.exceptions.RequestException as exc:
            print_debug(f"Сетевая ошибка create Voicer для абзаца {idx}: {exc}")
            print_error_user_friendly(
                "Voicer API: связь оборвалась при создании задачи; автоматический повтор отключён, "
                "чтобы не создать платный дубль. Повторите запуск позже."
            )
            return False
        except Exception as e:
            print_debug(f"Ошибка create Voicer для абзаца {idx}: {e}")
            print_error_user_friendly(f"Voicer API: не удалось создать задачу для абзаца {idx}: {e}")
            return False

    if not task_id:
        print_error_user_friendly("Voicer API: задача не создана после повторов 429.")
        return False

    print_debug(f"[{idx}] Task ID: {task_id}. Ожидание...")
    deadline = time.monotonic() + _wait_seconds(estimated_time)
    try:
        wait_for_completed(task_id, estimated_time_seconds=estimated_time, deadline=deadline)
    except VoicerTaskTerminalError as exc:
        print_error_user_friendly(f"Voicer: {str(exc)[:300]}")
        return False
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        print_error_user_friendly(
            f"Voicer: проверка задачи {task_id} остановлена (HTTP {code}). "
            f"{_http_error_snippet(exc) or str(exc)}"
        )
        return False
    except requests.exceptions.RequestException as exc:
        print_error_user_friendly(
            f"Voicer: статус задачи {task_id} недоступен после {MAX_RETRIES} сетевых повторов: {exc}"
        )
        return False
    except TimeoutError:
        print_error_user_friendly(
            f"Voicer: задача {task_id} не завершилась за отведённое время; повторный create не выполнялся."
        )
        return False
    except Exception as exc:
        print_error_user_friendly(f"Voicer: не удалось проверить задачу {task_id}: {exc}")
        return False

    # Download ретраится bounded, но всегда для того же task_id.
    for attempt in range(MAX_RETRIES):
        try:
            download_audio(task_id, output_mp3_path)
            print_debug(f"Абзац {idx} готов")
            return True
        except requests.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            snippet = _http_error_snippet(exc)
            if code in (401, 403) or (code is not None and 400 <= code < 500 and code not in (404, 429)):
                print_error_user_friendly(
                    f"Voicer: скачать задачу {task_id} не удалось (HTTP {code}). {snippet or str(exc)}"
                )
                return False
            if attempt < MAX_RETRIES - 1 and (code in (404, 429) or (code is not None and code >= 500)):
                print_user(f"Voicer: файл задачи {task_id} ещё недоступен, повтор...", "WARNING")
                if not _sleep_with_deadline(RETRY_DELAY_SECONDS * (attempt + 1), deadline):
                    break
                continue
            print_error_user_friendly(
                f"Voicer: не удалось скачать задачу {task_id} (HTTP {code}). {snippet or str(exc)}"
            )
            return False
        except requests.exceptions.RequestException as exc:
            if attempt < MAX_RETRIES - 1:
                print_user(f"Voicer: сеть при скачивании задачи {task_id}, повтор...", "WARNING")
                if not _sleep_with_deadline(RETRY_DELAY_SECONDS * (attempt + 1), deadline):
                    break
                continue
            print_error_user_friendly(
                f"Voicer: не удалось скачать задачу {task_id} после {MAX_RETRIES} сетевых повторов: {exc}"
            )
            return False
        except Exception as exc:
            print_error_user_friendly(f"Voicer: не удалось сохранить файл задачи {task_id}: {exc}")
            return False
    print_error_user_friendly(
        f"Voicer: файл задачи {task_id} не был получен до общего дедлайна; новый task_id не создавался."
    )
    return False


def main():
    parser = argparse.ArgumentParser(description="Worker озвучки через Voicer API (Mat3u).")
    parser.add_argument("--source_file", required=True)
    parser.add_argument("--project_dir", required=True)
    parser.add_argument("--use_cache", action="store_true")
    parser.add_argument("--voice_id", default="", help="ID голоса Voicer/ElevenLabs")
    parser.add_argument("--model_id", default="", help="ID модели (eleven_multilingual_v2 и т.д.)")
    parser.add_argument("--split_type", default="", help="smart, sentences, paragraphs, max_length")
    parser.add_argument("--auto_pause_enabled", action="store_true")
    parser.add_argument("--auto_pause_duration", default="0.5")
    parser.add_argument("--auto_pause_frequency", default="1")
    parser.add_argument("--speed", default=os.environ.get("TTS_SPEED", "1.0"), help="Темп речи (atempo): 1.0=норма, <1 медленнее, >1 быстрее")
    parser.add_argument("--full_text", action="store_true",
                        help="Озвучить весь текст ОДНОЙ задачей → один mp3 (как десктопная кнопка «весь текст»). "
                             "Voicer сам режет на чанки и склеивает.")
    args = parser.parse_args()
    global TTS_SPEED
    try:
        TTS_SPEED = float(args.speed)
    except (TypeError, ValueError):
        TTS_SPEED = 1.0

    print_user("Озвучка Voicer API...", "PROCESS")

    tok = current_voicer_token()
    if not tok:
        print_error_user_friendly(_('voicer_api_key_error'))
        return 1

    print_debug(f"Токен установлен: {tok[:4]}...")

    try:
        with open(args.source_file, 'r', encoding='utf-8', errors='replace') as f:
            paragraphs = [p.strip() for p in f.read().splitlines() if p.strip()]
    except FileNotFoundError:
        print_error_user_friendly(f"{_('file_not_found')}: {args.source_file}")
        return 1

    if not paragraphs:
        print_error_user_friendly("Файл для озвучки пуст.")
        return 1

    if args.full_text and paragraphs:
        # Схлопываем весь текст в один абзац → одна задача → один mp3 (1.mp3).
        # Границы отдельных абзацев не нужны: Voicer режет на чанки сам (split_type), а рендер
        # синхронит по whisper'у поверх склеенного трека, не по границам файлов.
        joined = " ".join(" ".join(paragraphs).split())
        paragraphs = [joined] if joined else []

    output_dir = os.path.join(args.project_dir, AUDIO_DIR_NAME)
    os.makedirs(output_dir, exist_ok=True)
    print_debug(f"Выходная папка: {output_dir}")

    if args.full_text:
        # Чистим хвост прежнего по-абзацного прогона. Если рядом лежат 2.mp3, 3.mp3… — проект
        # раньше озвучивали по абзацам; при склейке рендер подхватил бы их поверх нового единого
        # 1.mp3. При наличии таких файлов сносим и 1.mp3 (он от первого абзаца, не «весь текст»),
        # чтобы кэш не отдал неверный кусок; иначе (был только 1.mp3 от full_text) — оставляем кэш.
        import glob as _glob
        _existing = _glob.glob(os.path.join(output_dir, "*.mp3"))
        _had_multi = any(os.path.basename(_f) != "1.mp3" for _f in _existing)
        for _f in _existing:
            if _had_multi or os.path.basename(_f) != "1.mp3":
                try:
                    os.remove(_f)
                except OSError:
                    pass
        if _had_multi:
            print_debug("full_text: снесён хвост по-абзацного прогона (2.mp3+ и 1.mp3)")

    voice_id = args.voice_id.strip() or None
    model_id = args.model_id.strip() or None
    split_type = args.split_type.strip() or None
    try:
        ap_dur = float(args.auto_pause_duration)
    except (ValueError, TypeError):
        ap_dur = 0.5
    try:
        ap_freq = int(args.auto_pause_frequency)
    except (ValueError, TypeError):
        ap_freq = 1

    tasks = []
    for idx, paragraph_text in enumerate(paragraphs, 1):
        filename = f"{idx}.mp3"
        output_mp3_path = os.path.join(output_dir, filename)
        if args.use_cache and _audio_is_ready(output_mp3_path):
            print_debug(f"Пропуск абзаца {idx} (кэш)")
            continue
        tasks.append((
            idx, paragraph_text, output_mp3_path,
            voice_id, model_id, split_type,
            bool(args.auto_pause_enabled), ap_dur, ap_freq
        ))

    if not tasks:
        if not _verify_audio_completeness(output_dir, len(paragraphs)):
            return 1
        print_user(_('tts_no_new_paragraphs'), "INFO")
        return 0

    print_user(f"Обработка {len(tasks)} абзацев...", "PROCESS")
    print_debug(f"Потоков: {PARALLEL_REQUESTS}")

    completed = 0
    failures = 0
    with ThreadPoolExecutor(max_workers=PARALLEL_REQUESTS) as executor:
        for result in executor.map(process_paragraph, tasks):
            completed += 1
            if result is not True:
                failures += 1
            if completed % 5 == 0:
                print_progress(completed, len(tasks), "абзацев")

    if failures > 0:
        print_error_user_friendly(f"Не удалось обработать {failures} абзацев из {len(tasks)}")
        return 1

    # Гейт полноты: сколько mp3 реально лежит в папке против числа абзацев исходного текста.
    # Ловит случаи, когда часть абзацев так и не озвучена (обрыв прогона, пропуск, порча кэша).
    if not _verify_audio_completeness(output_dir, len(paragraphs)):
        return 1

    print_user("Озвучивание Voicer завершено", "SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
