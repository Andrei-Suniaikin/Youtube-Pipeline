# -*- coding: utf-8 -*-
"""
Rewrite Server — FastAPI-сервер для выполнения очереди задач на VPS.
Запуск: uvicorn server:app --host 0.0.0.0 --port 8899
"""

import os
import json
import re
import time
import io
import zipfile
from pathlib import Path
from typing import List, Optional
from dataclasses import asdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

# Базовые пути (переопределяются через env)
BASE_DIR = Path(os.environ.get("REWRITE_SERVER_DIR", os.path.dirname(os.path.abspath(__file__))))
OUTPUT_DIR = Path(os.environ.get("REWRITE_OUTPUT_DIR", BASE_DIR / "outputs"))
UPLOAD_DIR = Path(os.environ.get("REWRITE_UPLOAD_DIR", BASE_DIR / "uploads"))
PRESETS_DIR = Path(os.environ.get("REWRITE_PRESETS_DIR", BASE_DIR / "presets"))
STATIC_DIR = Path(os.environ.get("REWRITE_STATIC_DIR", BASE_DIR / "static"))
DB_PATH = os.environ.get("REWRITE_DB_PATH", str(BASE_DIR / "job_queue.db"))
# PATCH-2026-06-25-secfix (M1): god-mode 'desktop' по X-API-Key удалён, API_SECRET больше не используется.

# Создаём директории
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PRESETS_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# Импорт ядра (без tkinter)
from job_queue import Job, JobQueue, JobWorker, JobType, JobStatus
from rewrite_core import (
    generate_and_postprocess_single_text,
    rewrite_single_language,
    PresetManager,
    Preset,
    STOP_FLAG,
    SAFE_TEXT_SUPPLEMENT,
    make_output_filename,
    LANGUAGES,
    get_keys_config,
    save_keys_config,
    ADMIN_USER_ID,
    set_current_user_id,
    clear_current_user_id,
)
from auth import LicenseManager
from concurrent.futures import ThreadPoolExecutor, as_completed
import queue as std_queue
import threading

# PresetManager с путём для сервера
PRESETS_FILE = PRESETS_DIR / "generation.json"
PRESET_MANAGER = PresetManager(str(PRESETS_FILE))
queue = JobQueue(DB_PATH)
license_mgr = LicenseManager()

# ---------- Job stop events (per-job cancellation) ----------
_job_stop_events: dict = {}
_job_stop_events_lock = threading.Lock()


def _run_task_with_resume(attempts, backoff, log_q, stop_event, user_id, fn, *args, **kwargs):
    """PATCH-2026-06-12-task-auto-resume: авто-ретрай таски при success=False.
    Переинвок fn — resume в rewrite_core подхватывает ТОЛЬКО упавшие чанки из
    PROGRESS_MANAGER (готовые не перегенерируются -> качество и токены не тратятся).
    Файл по-прежнему сохраняется лишь когда ВСЕ чанки валидны (дыр не пропускаем)."""
    res = (None, False)
    for _att in range(max(1, attempts)):
        res = _run_with_job_stop(stop_event, user_id, fn, *args, **kwargs)
        try:
            ok = bool(res[1])
        except Exception:
            ok = False
        if ok or STOP_FLAG.is_stopped():
            return res
        if _att < attempts - 1:
            try:
                log_q.put(f"\U0001F501 \u0430\u0432\u0442\u043E-\u0440\u0435\u0442\u0440\u0430\u0439 \u0442\u0430\u0441\u043A\u0438 {_att + 2}/{attempts}: \u0434\u043E\u0437\u0430\u043B\u0438\u0432 \u0443\u043F\u0430\u0432\u0448\u0438\u0445 \u0447\u0430\u043D\u043A\u043E\u0432 (resume)")
            except Exception:
                pass
            time.sleep(backoff)
    return res


def _run_with_job_stop(stop_event, user_id, fn, *args, **kwargs):
    """Обёртка: устанавливает thread-local stop event + user_id ctx для воркера ThreadPoolExecutor.

    user_id ctx гарантирует, что глубокие утилиты в rewrite_core (get_postprocess_openai_key,
    debug_log, и т.п. без явного user_id) читают ключи правильного партнёра."""
    STOP_FLAG.set_thread_stop_event(stop_event)
    set_current_user_id(user_id or ADMIN_USER_ID)
    try:
        return fn(*args, **kwargs)
    finally:
        STOP_FLAG.clear_thread_stop_event()
        clear_current_user_id()


def _drain_queue(q):
    """Вычитать все сообщения из очереди без блокировки."""
    lines = []
    try:
        while True:
            lines.append(q.get_nowait())
    except std_queue.Empty:
        pass
    return lines


def _setup_api_key_config(job: Job):
    """Подставить per-job API ключ в per-user конфиг для постпроцесса (Pass A/B/C/D)."""
    if "claude" not in (job.model or "").lower() and (job.api_key or "").strip():
        uid = job.user_id or ADMIN_USER_ID
        cfg = get_keys_config(uid)
        save_keys_config(
            anthropic_api_key=cfg.get("anthropic_api_key", ""),
            openai_api_key=(job.api_key or "").strip(),
            oauth_tokens=cfg.get("oauth_tokens", []),
            user_id=uid,
        )


def _finalize_job_status(job: Job, total: int, success_count: int):
    """Выставить финальный статус задачи по результатам выполнения."""
    if STOP_FLAG.is_stopped():
        job.status = JobStatus.CANCELLED.value
        job.error = "Остановлено пользователем"
    elif total > 0 and success_count < total:
        job.status = JobStatus.PARTIAL.value
        job.error = f"{total - success_count} из {total} задач не выполнено"


def _get_effective_api_key(job: Job):
    """Определить эффективный API ключ для задачи.
    2026-06-04: для GPT-ветки ENV OPENAI_API_KEY доступен ТОЛЬКО админу — иначе пусто.
    Не-админ без своего ключа должен был получить 422 на create-этапе (см. api.py / server.py)."""
    if job.api_key:
        return job.api_key
    if "claude" in job.model.lower():
        return os.environ.get("ANTHROPIC_API_KEY", "")
    return rewrite_core.env_openai_key_for(job.user_id or "")


def _execute_job_common(job: Job, job_fn):
    """Общая обёртка выполнения задачи: stop event, лог-дрейн, очистка.
    job_fn(job, log_queue, stop_event) содержит бизнес-логику."""
    print(f"[{job.job_type.upper()} JOB] id={job.id} model='{job.model}' use_sub={job.use_subscription} api_key={'***'+job.api_key[-4:] if job.api_key else 'EMPTY'}")
    STOP_FLAG.reset()
    stop_event = threading.Event()
    with _job_stop_events_lock:
        _job_stop_events[job.id] = stop_event
    STOP_FLAG.set_thread_stop_event(stop_event)
    set_current_user_id(job.user_id or ADMIN_USER_ID)
    log_queue = std_queue.Queue()
    stop_drain = threading.Event()

    def drain_log():
        while not stop_drain.is_set():
            lines = _drain_queue(log_queue)
            if lines:
                queue.append_log(job.id, "\n".join(lines) + "\n")
            stop_drain.wait(0.5)

    drain_thread = threading.Thread(target=drain_log, daemon=True)
    drain_thread.start()
    try:
        _setup_api_key_config(job)
        job_fn(job, log_queue, stop_event)
    except Exception as e:
        job.error = str(e)
        job.status = JobStatus.FAILED.value
    finally:
        stop_drain.set()
        drain_thread.join(timeout=3)
        remaining = _drain_queue(log_queue)
        if remaining:
            queue.append_log(job.id, "\n".join(remaining) + "\n")
        STOP_FLAG.clear_thread_stop_event()
        clear_current_user_id()
        with _job_stop_events_lock:
            _job_stop_events.pop(job.id, None)
        queue.update_job(job)


def _gen_filename_base(title: str, topic: str = "", preview: str = "", job_id: str = "") -> str:
    """PATCH-2026-06-28-filename: читаемое имя файла генерации = сгенерённое НАЗВАНИЕ + короткий id.
    Фолбэк title→topic→preview→'script'. Чистит ТОЛЬКО запрещённые в Windows/Mac символы
    (< > : " / \\ | ? * и управляющие), схлопывает пробелы, режет до ~90 симв по границе слова.
    Пробелы и юникод (кириллица/диакритика) СОХРАНЯЮТСЯ — читаемо. Короткий [id] защищает от перезаписи."""
    name = (title or topic or preview or "script").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)   # запрещённые ФС-символы → пробел
    name = re.sub(r"\s+", " ", name).strip(" .")          # схлопнуть пробелы, убрать концевые точки/пробелы
    if len(name) > 90:
        name = name[:90].rsplit(" ", 1)[0].rstrip(" .,;:—-")  # обрезать по последнему слову
    name = name or "script"
    sid = (job_id or "")[:8]
    return f"{name} [{sid}]" if sid else name


def _execute_generation_job(job: Job):
    """Generation v2 worker (Round 2, 2026-05-23): chunk-loop + JSON output + postprocess pipeline.

    Old flow (3-step bait funnel + N×M tasks×languages cross product) removed.
    New flow: single topic × single language → chunked LLM calls returning strict JSON
    {preview, title, script, log_update} → assemble → save script .txt + meta .json.
    2026-06-03: multi-chunk (num_chunks > 1) для subscription provider теперь идёт через
    ClaudeSDKClient в ОДНОМ разговоре (continue-turns) — модель помнит весь предыдущий
    текст сама, не нужен previous_tail; cache reuse на turn 2+ дешевле. Single-shot
    (num_chunks == 1) и не-subscription провайдеры остаются на старом one-shot пути.
    Rotation log (anti-repeat) убран — conversation-mode даёт это бесплатно.
    Postprocess (Pass A/B/C/D) ОТКЛЮЧЁН для generation (Path B v2 design, 2026-05-23).
    """
    def _run(job, log_queue, stop_event):
        from rewrite_core import (
            build_chunk_prompt, parse_chunk_response,
            extract_field_from_content,
            calculate_generation_chunks, call_api_universal,
            call_claude_conversation,
            build_funnel_prompt, parse_funnel_response,  # PATCH-2026-06-27-funnelstep (был не импортирован → NameError)
            _char_length_line,  # PATCH-2026-06-27-chars
        )

        # === Parse v2 payload из gen_tasks ===
        try:
            payload = json.loads(job.gen_tasks or "{}")
        except Exception:
            job.error = "gen_tasks: invalid JSON"
            job.status = JobStatus.FAILED.value
            return
        if not isinstance(payload, dict) or payload.get("v") != 2:
            job.error = "gen_tasks: legacy format не поддерживается в Round 2 (v=2 required)"
            job.status = JobStatus.FAILED.value
            return

        topic = payload.get("topic") or ""
        preset_id = payload.get("preset_id") or job.preset_name
        language_req = payload.get("language")
        target_words_req = payload.get("target_length_words")
        max_log_entries = int(payload.get("max_log_entries") or 5)
        chunk_override = payload.get("chunk_size_words")  # None если не задан
        try:
            chunk_override = int(chunk_override) if chunk_override else None
        except (TypeError, ValueError):
            chunk_override = None

        preset = _preset_manager_for(job.user_id or ADMIN_USER_ID).get_preset(preset_id)  # B1: пресет владельца задачи
        if not preset:
            job.error = f"preset not found: {preset_id}"
            job.status = JobStatus.FAILED.value
            return

        # === Resolve language ===
        language = (language_req or preset.language or extract_field_from_content(preset.content, "language") or "English").strip()
        # === Resolve target_words ===
        target_words = target_words_req or preset.target_length_words or 0
        # === Chunk decision (per-model defaults + опц. override) ===
        model_for_chunking = job.model or ""
        if not target_words:
            num_chunks, words_per_chunk = 1, 0
        else:
            num_chunks, words_per_chunk = calculate_generation_chunks(
                target_words, language, model=model_for_chunking, chunk_size_override=chunk_override,
            )

        # Rotation log убран 2026-06-03 (см. rewrite_core.build_chunk_prompt комментарий).
        recent_phrases: List[str] = []

        # === Auth resolve ===
        oauth_tokens_list = json.loads(job.oauth_tokens) if job.oauth_tokens else []
        model = job.model or "claude-sonnet-4-6"
        model_is_claude = "claude" in model.lower()
        if model_is_claude:
            provider = "subscription" if job.use_subscription else "claude"
        else:
            provider = "openai"
        api_key_effective = _get_effective_api_key(job)

        _chunk_src = f"override={chunk_override}" if chunk_override else "auto"
        log_queue.put(
            f"📐 {preset.name}: {num_chunks} chunk(s) × ~{words_per_chunk or 'auto'} words, "
            f"model={model}, language={language}, chunk_size={_chunk_src}"
        )

        # === Chunk loop ===
        chunks_output: List[str] = []
        preview = title = ""
        all_log_updates: List[str] = []
        previous_tail = ""

        host_name_hint = extract_field_from_content(preset.content, "host_name") or ""
        channel_name_hint = extract_field_from_content(preset.content, "channel_name") or ""

        # === PATCH-2026-06-27-funnelstep: отдельный ВИДИМЫЙ шаг воронки ПЕРЕД генерацией ===
        # Claude проектирует превью/название/хук/план (план = бюджет слов по битам) → текст пишется ПО плану,
        # длина держится планом за один проход (без дорогих recovery-повторов).
        funnel_hook = ""
        funnel_plan: List[str] = []
        if target_words:  # воронка осмысленна только когда задана цель длины
            try:
                log_queue.put("📐 Проектирую воронку (превью → название → хук → план-бюджет)…")
                _fmsgs = build_funnel_prompt(
                    preset_content=preset.content, topic=topic, language=language,
                    target_words=target_words, num_chunks=num_chunks, words_per_chunk=words_per_chunk or 0,
                    host_name=host_name_hint, channel_name=channel_name_hint,
                )
                _fok, _fraw = call_api_universal(
                    _fmsgs, api_key_effective, model, log_queue, "gen-funnel",
                    provider=provider, oauth_token=job.oauth_token or "",
                )
                _fp = parse_funnel_response(_fraw) if _fok else None
                # PATCH-2026-06-27-thinking: если ответ не JSON (thinking/ack «I'll design…» вместо воронки) — ОДИН ретрай.
                if not (_fp and (_fp.get("hook") or _fp.get("plan"))):
                    log_queue.put(f"↻ воронка: ответ не JSON ({(str(_fraw)[:48]).strip()!r}…) — ретрай со строгим хинтом")
                    _fmsgs2 = build_funnel_prompt(
                        preset_content=preset.content, topic=topic, language=language,
                        target_words=target_words, num_chunks=num_chunks, words_per_chunk=words_per_chunk or 0,
                        host_name=host_name_hint, channel_name=channel_name_hint,
                        retry_hint=("REMINDER: output ONLY the JSON object and start with '{' as the very first character. "
                                    "Do NOT write 'I'll', 'Let me', 'Here is', or any sentence/plan before the JSON. "
                                    "Do NOT think out loud. Emit the JSON now."),
                    )
                    _fok2, _fraw2 = call_api_universal(
                        _fmsgs2, api_key_effective, model, log_queue, "gen-funnel-retry",
                        provider=provider, oauth_token=job.oauth_token or "",
                    )
                    _fp2 = parse_funnel_response(_fraw2) if _fok2 else None
                    if _fp2 and (_fp2.get("hook") or _fp2.get("plan")):
                        _fp = _fp2
                if _fp and (_fp.get("hook") or _fp.get("plan")):
                    funnel_hook = _fp["hook"]
                    funnel_plan = _fp["plan"]
                    if _fp.get("preview"):
                        preview = _fp["preview"]
                    if _fp.get("title"):
                        title = _fp["title"]
                    job.progress_detail = f"воронка готова: preview={preview!r} title={title!r}"
                    queue.update_job(job)
                    log_queue.put(
                        f"✅ Воронка: preview={_fp['preview']!r} title={_fp['title']!r} "
                        f"hook={(_fp['hook'][:60])!r} план={len(funnel_plan)} частей"
                    )
                    if funnel_plan and len(funnel_plan) != num_chunks:
                        log_queue.put(
                            f"ℹ️ план {len(funnel_plan)} частей ≠ {num_chunks} чанков — раздам что есть, лишнее по индексу"
                        )
                else:
                    log_queue.put("⚠️ Воронка не распарсилась — генерация в обычном режиме (inline-воронка + recovery)")
            except Exception as _fe:
                log_queue.put(f"⚠️ Шаг воронки упал ({type(_fe).__name__}: {_fe}) — генерация в обычном режиме")

        def _plan_for(_idx):  # план части _idx (пусто если воронки нет / индекс вне диапазона)
            return funnel_plan[_idx] if (funnel_plan and 0 <= _idx < len(funnel_plan)) else ""

        # === Multi-chunk conversation mode (one ClaudeSDKClient, continue-turns) ===
        # Применяется только когда num_chunks > 1 И provider==subscription.
        # Преимущества: модель видит весь предыдущий текст (континуити, нет повторов),
        # cache reuse (system+turn1 закэшируются, на turn 2+ платим только за delta).
        # Single-shot (num_chunks==1) остаётся на старом one-shot пути ниже.
        _multi_done = False
        if num_chunks > 1 and provider == "subscription":
            log_queue.put(
                f"💬 Conversation mode: {num_chunks} turns в ОДНОМ разговоре "
                f"(модель помнит весь контекст сама)"
            )

            # System prompt — тот же что в build_chunk_prompt
            _system_prompt = (
                "You are a YouTube script writer. Apply the channel preset strictly. "
                "Output ONLY a valid JSON object — no markdown wrapper, no prefix or suffix text, no comments."
            )

            # Turn 0: full preset + topic + chunk position (как build_chunk_prompt для chunk 0)
            _msgs0 = build_chunk_prompt(
                preset_content=preset.content,
                topic=topic,
                chunk_idx=0,
                num_chunks=num_chunks,
                target_words_this_chunk=words_per_chunk or None,
                previous_tail="",
                recent_log_phrases=recent_phrases,
                language=language,
                host_name=host_name_hint or None,
                channel_name=channel_name_hint or None,
                plan_for_chunk=_plan_for(0),   # PATCH-2026-06-27-funnelstep
                hook=funnel_hook,
            )
            _turn_prompts = [_msgs0[-1].get("content", "")]

            # Turn 1..N-1: короткий continue (БЕЗ повторного preset/topic — модель помнит сама)
            for _k in range(1, num_chunks):
                _is_last_k = (_k == num_chunks - 1)
                _parts = [f"Continue: write chunk {_k + 1} of {num_chunks}."]
                # PATCH-2026-06-27-funnelstep: если есть внешняя воронка — даём план ИМЕННО этой части (бюджет слов);
                # иначе (inline-воронка) — продолжай по плану, спроектированному в chunk 1.
                _plan_k = _plan_for(_k)
                if _plan_k:
                    _parts.append(
                        "PLAN FOR THIS PART (from the funnel step) — follow it EXACTLY and write EACH beat to its "
                        "~word budget («Слов») so the length lands on target:\n" + _plan_k
                    )
                elif funnel_plan:
                    # внешняя воронка есть, но план ЭТОЙ части не пришёл (план короче N чанков) — нейтрально, без ссылки на несуществующий план
                    _parts.append("Continue the SAME arc toward the promised payoff; keep developing at full length with concrete detail.")
                else:
                    _parts.append("Follow the overall PLAN you designed in chunk 1 (keep the promised beats moving toward the payoff).")
                if words_per_chunk:
                    # PATCH-2026-06-28-words: словесный коридор («Слов») + само-перепроверка перед ответом.
                    _parts.append(_char_length_line(words_per_chunk, language))
                if _is_last_k:
                    # PATCH-2026-06-27-funnel (item 5): аутро с именем ведущего и канала.
                    _parts.append("CLOSE the script: a short outro / sign-off that NAMES the host and the channel.")
                else:
                    _parts.append("CONTINUE the narrative — do not repeat content from previous chunks.")
                # PATCH-2026-06-27-funnel (item 5): ровно ОДИН байт на подписку — в центральном чанке.
                if _k == num_chunks // 2:
                    _parts.append(
                        "In THIS chunk, naturally insert ONE short call-to-action (like + subscribe) mentioning "
                        "the channel name — a single in-voice sentence, never jarring. (Only here.)"
                    )
                _continue_keys = '{"script": "...", "log_update": [...]}'
                _parts.append(
                    "\nCRITICAL OUTPUT RULES:\n"
                    "- Output ONLY valid JSON matching the schema below.\n"
                    "- Do NOT wrap JSON in markdown fences.\n"
                    "- Do NOT add any text before or after the JSON object.\n"
                    f"\nOutput STRICTLY this JSON shape:\n{_continue_keys}\n\n"
                    "Rules:\n"
                    f"- TTS-ready: no markdown, numbers/dates/currencies/abbreviations as WORDS in {language}.\n"
                    "- Same voice and style as established in chunk 1.\n"
                    "- `script` must be a single string (no nested objects)."
                )
                _turn_prompts.append("\n".join(_parts))

            log_queue.put(
                f"🔬 conv-mode: turn1_len={len(_turn_prompts[0])} chars, "
                f"continue_len={len(_turn_prompts[1]) if len(_turn_prompts) > 1 else 0} chars"
            )

            _conv_success, _raw_responses, _conv_err = call_claude_conversation(
                turn_prompts=_turn_prompts,
                system_prompt=_system_prompt,
                model=model,
                oauth_token=job.oauth_token or "",
                log_queue=log_queue,
                task_info=f"gen-conv-{num_chunks}chunks",
                # PATCH-2026-06-28-words: recovery ВКЛ ВСЕГДА (раньше при воронке был ВЫКЛ, но план длину не держал → тексты короткие).
                # Меряется в СЛОВАХ (_gen_count_script_words = .split() = «Слов», не «буквенных»).
                target_words_per_turn=(words_per_chunk or 0),
                max_recovery_turns=2,
            )

            # Парсим каждый turn через тот же parse_chunk_response (дубль-emit от SDK
            # уже учтён через raw_decode → берёт ПЕРВЫЙ JSON, игнорирует повтор)
            _parse_failed_at = None
            for _ti, _raw in enumerate(_raw_responses):
                if STOP_FLAG.is_stopped():
                    log_queue.put("⛔ Остановлено пользователем mid-conversation")
                    job.status = JobStatus.CANCELLED.value
                    return
                _parsed = parse_chunk_response(_raw, expect_funnel=(_ti == 0), job_id=job.id, model=model)
                if not _parsed:
                    try:
                        dump_path = Path("/tmp") / f"parse_fail_{job.id[:8]}_conv_turn{_ti + 1}.txt"
                        dump_path.write_text(str(_raw), encoding="utf-8")
                        log_queue.put(f"📝 conv raw dumped: {dump_path} ({len(_raw)} chars)")
                    except Exception:
                        pass
                    _parse_failed_at = _ti + 1
                    break
                if _ti == 0:
                    # PATCH-2026-06-27-funnelstep: не перетирать превью/название из шага воронки.
                    if not preview:
                        preview = _parsed["preview"]
                    if not title:
                        title = _parsed["title"]
                chunks_output.append(_parsed["script"])
                all_log_updates.extend(_parsed["log_update"])
                job.progress_percent = int((_ti + 1) / num_chunks * 80)
                job.progress_detail = f"conv turn {_ti + 1}/{num_chunks} ok ({len(_parsed['script'].split())} слов)"
                queue.update_job(job)
                log_queue.put(
                    f"✅ Conv turn {_ti + 1}/{num_chunks}: {len(_parsed['script'].split())} слов ({len(_parsed['script'])} симв), "
                    f"log_update={len(_parsed['log_update'])}"
                )

            # Решаем что делать с partial — abandon-to-partial стратегия
            _done_count = len(chunks_output)
            if _parse_failed_at is not None:
                _err = f"Conversation: malformed JSON at turn {_parse_failed_at}/{num_chunks}"
                if _done_count > 0:
                    job.error = f"{_err}. Partial: saved {_done_count} chunks. {_conv_err or ''}"
                    job.status = JobStatus.PARTIAL.value
                else:
                    job.error = f"{_err}. {_conv_err or ''}"
                    job.status = JobStatus.FAILED.value
                    return
            elif not _conv_success:
                # conv call вернул False (timeout/error mid-conversation)
                if _done_count > 0:
                    job.error = f"Conversation incomplete: {_done_count}/{num_chunks} turns. {_conv_err}"
                    job.status = JobStatus.PARTIAL.value
                else:
                    job.error = _conv_err or f"Conversation failed before any turn completed"
                    job.status = JobStatus.FAILED.value
                    return

            _multi_done = True  # → старый for-loop сделает break

        for chunk_idx in range(num_chunks):
            if _multi_done:
                break  # conversation mode уже всё сделал — skip single-shot loop
            if STOP_FLAG.is_stopped():
                log_queue.put("⛔ Остановлено пользователем mid-chunk")
                job.status = JobStatus.CANCELLED.value
                return

            messages = build_chunk_prompt(
                preset_content=preset.content,
                topic=topic,
                chunk_idx=chunk_idx,
                num_chunks=num_chunks,
                target_words_this_chunk=words_per_chunk or None,
                previous_tail=previous_tail,
                recent_log_phrases=recent_phrases,
                language=language,
                host_name=host_name_hint or None,
                channel_name=channel_name_hint or None,
                plan_for_chunk=_plan_for(chunk_idx),   # PATCH-2026-06-27-funnelstep
                hook=funnel_hook,
            )

            # 🔬 DIAG (Round 2 debug): фиксируем точные длины prompt'а до LLM-вызова
            _diag_total = sum(len(m.get("content", "")) for m in messages)
            _diag_sys = len(messages[0].get("content", "")) if messages else 0
            _diag_usr = len(messages[-1].get("content", "")) if messages else 0
            _diag_has_tail = "Previous chunk ended" in (messages[-1].get("content", "") if messages else "")
            _diag_has_preset = "Channel preset" in (messages[-1].get("content", "") if messages else "")
            log_queue.put(
                f"🔬 chunk {chunk_idx + 1}/{num_chunks} prompt: total={_diag_total} sys={_diag_sys} "
                f"usr={_diag_usr} has_preset={_diag_has_preset} has_prev_tail={_diag_has_tail}"
            )

            task_info = f"gen chunk {chunk_idx + 1}/{num_chunks}"

            # Retry-loop поверх subscription timeout (claude CLI иногда виснет на больших prompts).
            SUBSCRIPTION_RETRY_MAX = 2
            SUBSCRIPTION_RETRY_BACKOFF_SEC = 30
            success, raw = False, ""
            for _sub_attempt in range(SUBSCRIPTION_RETRY_MAX + 1):
                success, raw = call_api_universal(
                    messages, api_key_effective, model, log_queue, task_info,
                    provider=provider, oauth_token=job.oauth_token or "",
                )
                _is_sub_timeout = (
                    not success
                    and isinstance(raw, str)
                    and raw.startswith("SUBSCRIPTION_TIMEOUT")
                )
                if success or not _is_sub_timeout:
                    break
                if _sub_attempt < SUBSCRIPTION_RETRY_MAX:
                    log_queue.put(
                        f"⏳ chunk {chunk_idx + 1}: subscription timeout — wait "
                        f"{SUBSCRIPTION_RETRY_BACKOFF_SEC}s, retry "
                        f"{_sub_attempt + 2}/{SUBSCRIPTION_RETRY_MAX + 1}"
                    )
                    time.sleep(SUBSCRIPTION_RETRY_BACKOFF_SEC)

            # 🔬 DIAG: фиксируем что вернул LLM
            log_queue.put(
                f"🔬 chunk {chunk_idx + 1} response: success={success} raw_len={len(raw) if raw else 0} "
                f"head={(str(raw)[:120]).replace(chr(10),' ')!r}"
            )
            if not success:
                if isinstance(raw, str) and raw.startswith("SUBSCRIPTION_TIMEOUT"):
                    job.error = (
                        f"Chunk {chunk_idx + 1}: Claude Code CLI stream stalled после "
                        f"{SUBSCRIPTION_RETRY_MAX + 1} attempts. Известный upstream bug — "
                        f"anthropics/claude-code#25979 (regression в claude-code 2.1.42+). "
                        f"Что попробовать: повторить чуть позже, другой OAuth-токен (rotation), "
                        f"Haiku вместо Sonnet/Opus, или downgrade CLI до 2.1.40. "
                        f"Forensics: /tmp/cli_hang_*"
                    )
                else:
                    job.error = f"LLM error on chunk {chunk_idx + 1}: {str(raw)[:300]}"
                job.status = JobStatus.FAILED.value
                return

            parsed = parse_chunk_response(raw, expect_funnel=(chunk_idx == 0), job_id=job.id, model=model)
            if not parsed:
                # Dump raw в файл для будущей диагностики (raw обрезается в логе до 200 chars).
                try:
                    dump_path = Path("/tmp") / f"parse_fail_{job.id[:8]}_chunk{chunk_idx + 1}_a.txt"
                    dump_path.write_text(str(raw), encoding="utf-8")
                    log_queue.put(f"📝 raw dumped: {dump_path} ({len(raw)} chars)")
                except Exception:
                    pass

                # Rebuild messages через build_chunk_prompt с retry_hint.
                # Старая логика (append к messages) не работала: claude-code-sdk
                # flattens список — берёт ТОЛЬКО последний user message, теряя preset+topic.
                retry_messages = build_chunk_prompt(
                    preset_content=preset.content,
                    topic=topic,
                    chunk_idx=chunk_idx,
                    num_chunks=num_chunks,
                    target_words_this_chunk=words_per_chunk or None,
                    previous_tail=previous_tail,
                    recent_log_phrases=recent_phrases,
                    language=language,
                    host_name=host_name_hint or None,
                    channel_name=channel_name_hint or None,
                    plan_for_chunk=_plan_for(chunk_idx),   # PATCH-2026-06-27-funnelstep
                    hook=funnel_hook,
                    retry_hint=(
                        "Previous response was not valid JSON or was truncated. "
                        "Output ONLY a single valid JSON object — no markdown wrapper, "
                        "no leading/trailing text, no comments, no duplicated objects. "
                        "Start with { and end with }. Escape all internal quotes inside "
                        "string values with backslash. Keep `script` as ONE string, "
                        "stay within target word count to avoid truncation."
                    ),
                )
                success2, raw2 = call_api_universal(
                    retry_messages, api_key_effective, model, log_queue,
                    f"{task_info}:json_retry",
                    provider=provider, oauth_token=job.oauth_token or "",
                )
                log_queue.put(
                    f"🔬 chunk {chunk_idx + 1} retry response: success={success2} "
                    f"raw_len={len(raw2) if raw2 else 0} "
                    f"head={(str(raw2)[:120]).replace(chr(10),' ')!r}"
                )
                if not success2:
                    job.error = f"LLM error on chunk {chunk_idx + 1} retry: {str(raw2)[:300]}"
                    job.status = JobStatus.FAILED.value
                    return
                parsed = parse_chunk_response(raw2, expect_funnel=(chunk_idx == 0), job_id=job.id, model=model)
                if not parsed:
                    try:
                        dump_path = Path("/tmp") / f"parse_fail_{job.id[:8]}_chunk{chunk_idx + 1}_b.txt"
                        dump_path.write_text(str(raw2), encoding="utf-8")
                        log_queue.put(f"📝 retry raw dumped: {dump_path} ({len(raw2)} chars)")
                    except Exception:
                        pass
                    job.error = (
                        f"chunk {chunk_idx + 1}: malformed JSON after 1 retry. "
                        f"Response prefix: {str(raw2)[:300]}"
                    )
                    job.status = JobStatus.FAILED.value
                    return

            # PATCH-2026-06-27-length: single-shot recovery — дожать чанк до нижней границы коридора.
            # PATCH-2026-06-28-words: recovery РАБОТАЕТ ВСЕГДА (раньше был ВЫКЛ при воронке, но план длину НЕ держал → тексты короче).
            # Меряем СЛОВА через .split() (= «Слов», НЕ «буквенных»). floor 0.8 = дожим ТОЛЬКО если короче >20% (как рерайт);
            # до 20% недобора — норма, без лишнего повторного запроса (он дорог + иногда падает пустым на 4-8).
            if words_per_chunk and words_per_chunk > 0:
                _lo = int(words_per_chunk * 0.8); _hi = int(words_per_chunk * 1.15)
                _is_outro_chunk = (chunk_idx == num_chunks - 1)  # последний/единственный чанк держит аутро
                _rs = 0
                while _rs < 2:
                    _w = len((parsed.get("script") or "").split())
                    if _w >= _lo:
                        break
                    _rs += 1
                    _deficit = _lo - _w
                    _tail = (parsed.get("script") or "")[-1500:]
                    if _is_outro_chunk:
                        _close = ("If the text already ended with a sign-off, treat that as PREMATURE — keep developing, and "
                                  "only at the VERY END write the final short sign-off naming the host and the channel.")
                    else:
                        _close = "Do NOT write any closing/outro/sign-off yet."
                    _rec_msgs = [
                        messages[0],
                        {"role": "user", "content": (
                            f"You are continuing a script you already wrote. It is currently {_w} words but MUST reach "
                            f"at least {_lo} words (band {_lo}-{_hi}). Here is how it currently ENDS:\n\n\"\"\"{_tail}\"\"\"\n\n"
                            f"Continue from EXACTLY where it ends, seamlessly. Add about {_deficit} more words of real "
                            f"substance (depth, concrete detail, an extra scene/development). Do NOT repeat earlier text, "
                            f"do NOT restart. {_close} "
                            f"Output ONLY JSON: {{\"script\": \"<the additional continuation text only>\"}}."
                        )},
                    ]
                    _ok_r, _raw_r = call_api_universal(
                        _rec_msgs, api_key_effective, model, log_queue,
                        f"{task_info}:len_recovery{_rs}",
                        provider=provider, oauth_token=job.oauth_token or "",
                    )
                    if not _ok_r:
                        log_queue.put(f"⚠️ chunk {chunk_idx + 1} length recovery {_rs}: call failed, stop")
                        break
                    _pr = parse_chunk_response(_raw_r, expect_funnel=False, job_id=job.id, model=model)
                    if not _pr or not (_pr.get("script") or "").strip():
                        log_queue.put(f"⚠️ chunk {chunk_idx + 1} length recovery {_rs}: parse/empty, stop")
                        break
                    parsed["script"] = (parsed.get("script") or "").rstrip() + "\n\n" + _pr["script"].lstrip()
                    log_queue.put(
                        f"↪ chunk {chunk_idx + 1} length recovery {_rs}: {_w}→{len(parsed['script'].split())} words "
                        f"(band {_lo}-{_hi})"
                    )

            if chunk_idx == 0:
                # PATCH-2026-06-27-funnelstep: не перетирать превью/название из шага воронки.
                if not preview:
                    preview = parsed["preview"]
                if not title:
                    title = parsed["title"]
            chunks_output.append(parsed["script"])
            all_log_updates.extend(parsed["log_update"])
            previous_tail = parsed["script"][-500:] if parsed["script"] else ""

            job.progress_percent = int((chunk_idx + 1) / num_chunks * 80)  # 80% — chunks, 20% — postprocess
            job.progress_detail = f"chunk {chunk_idx + 1}/{num_chunks} ok ({len(parsed['script'].split())} слов)"
            queue.update_job(job)
            log_queue.put(
                f"✅ Chunk {chunk_idx + 1}/{num_chunks}: {len(parsed['script'].split())} слов ({len(parsed['script'])} симв), "
                f"log_update={len(parsed['log_update'])}"
            )

        # === Assemble raw script ===
        raw_script = "\n\n".join(chunks_output).strip()

        # === Postprocess: DISABLED for generation (Path B v2 design, 2026-05-23) ===
        # На сильных main-моделях (Sonnet 4.6 / Opus 4.7) output уже native-quality;
        # слабый postprocess (gpt-4.1-nano) делал synonym swaps + изредка factual errors
        # (см. memory project_overview.md: "Generation Path B v2", live-diff 2026-05-23).
        # Постпроцесс остаётся для rewrite path — там main source = transcript, нужна шлифовка.
        final_script = raw_script
        log_queue.put("📐 Postprocess SKIPPED for generation (Path B v2 design)")

        # === Save outputs ===
        output_dir = str(Path(job.output_dir or OUTPUT_DIR))
        os.makedirs(output_dir, exist_ok=True)
        # PATCH-2026-06-28-filename: читаемое имя = сгенерённое НАЗВАНИЕ [короткий-id] (вместо slug-пресета_тема_хэш).
        base = _gen_filename_base(title, topic, preview, job.id)
        script_path = Path(output_dir) / f"{base}.txt"
        try:
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(final_script)
        except Exception as e:
            log_queue.put(f"⚠️ Failed to save outputs: {e}")

        # Rotation log запись убрана 2026-06-03 (см. build_chunk_prompt комментарий).
        # Файлы rotation_logs/ на диске не удаляются (audit), просто не дописываем.

        # === Update preset last_used_at ===
        try:
            preset.last_used_at = time.time()
            _preset_manager_for(job.user_id or ADMIN_USER_ID).save_preset(preset)  # B1
        except Exception:
            pass

        # === Finalize job ===
        job.output_files = json.dumps([script_path.name], ensure_ascii=False)
        job.progress_percent = 100
        job.progress_detail = f"done: preview='{preview}' title='{title}'"
        if job.status == JobStatus.RUNNING.value:
            job.status = JobStatus.COMPLETED.value
        log_queue.put(
            f"🎬 Done: preview={preview!r} title={title!r} script={len(final_script.split())} слов ({len(final_script)} симв) "
            f"(postprocess=skipped)"
        )

    _execute_job_common(job, _run)


def _execute_rewrite_job(job: Job):
    """Выполняет задачу рерайта."""
    def _run(job, log_queue, stop_event):
        files = json.loads(job.rewrite_files)
        languages = json.loads(job.rewrite_languages)
        oauth_tokens_list = json.loads(job.oauth_tokens) if job.oauth_tokens else []
        output_dir = str(Path(job.output_dir or OUTPUT_DIR))
        os.makedirs(output_dir, exist_ok=True)
        resolved_files = []
        user_id = getattr(job, "user_id", "")
        for fp in files:
            if os.path.isabs(fp) and os.path.exists(fp):
                resolved_files.append(fp)
            else:
                basename = os.path.basename(fp)
                user_up = UPLOAD_DIR / user_id / basename if user_id else None
                global_up = UPLOAD_DIR / basename
                if user_up and user_up.exists():
                    resolved_files.append(str(user_up))
                elif global_up.exists():
                    resolved_files.append(str(global_up))
                else:
                    resolved_files.append(fp)
        is_safe_text = getattr(job, "gen_safe_text", False)
        tasks = []
        for file_path in resolved_files:
            for lang in languages:
                cp = lang.get("custom_prompt", "")
                if is_safe_text:
                    cp = (cp + "\n\n" + SAFE_TEXT_SUPPLEMENT) if cp else SAFE_TEXT_SUPPLEMENT
                tasks.append((
                    file_path,
                    lang.get("display", ""),
                    lang.get("code", lang.get("display", "")),
                    lang.get("channel", ""),
                    lang.get("host", ""),
                    cp,
                ))
        total = len(tasks)
        completed = 0
        success_count = 0
        api_key_effective = _get_effective_api_key(job)
        with ThreadPoolExecutor(max_workers=job.max_threads) as executor:
            futures = {}
            for task_idx, (file_path, lang_display, lang_code, channel, host, custom_prompt) in enumerate(tasks):
                if STOP_FLAG.is_stopped():
                    break
                start_idx = (task_idx % len(oauth_tokens_list)) if oauth_tokens_list else 0
                oauth_token = oauth_tokens_list[start_idx] if oauth_tokens_list else ""
                future = executor.submit(
                    _run_task_with_resume, 3, 20, log_queue,
                    stop_event, job.user_id or ADMIN_USER_ID,
                    rewrite_single_language,
                    file_path, lang_display, lang_code,
                    job.rewrite_chunk_size, job.model,
                    api_key_effective,
                    output_dir, log_queue,
                    channel, host, job.rewrite_address_style or "", custom_prompt,
                    job.use_subscription, oauth_token,
                    oauth_tokens_list, start_idx,
                    bool(getattr(job, "rewrite_preserve_paragraphs", False)),
                )
                futures[future] = (file_path, lang_display, lang_code)
            for future in as_completed(futures):
                if STOP_FLAG.is_stopped():
                    break
                try:
                    _, success = future.result()
                    completed += 1
                    if success:
                        success_count += 1
                    fp, ld, lc = futures.get(future, ("", "", ""))
                    job.progress_percent = int(completed / total * 100)
                    job.progress_detail = f"{completed}/{total} — {os.path.basename(fp)} ({ld})"
                    queue.update_job(job)
                except Exception as e:
                    log_queue.put(f"❌ Ошибка: {e}")
                    completed += 1
        _finalize_job_status(job, total, success_count)
        output_files = []
        for file_path in resolved_files:
            base_name = os.path.splitext(os.path.basename(file_path))[0][:50]
            for lang in languages:
                lc = lang.get("code", lang.get("display", ""))
                fname = make_output_filename(base_name, lc)
                if (Path(output_dir) / fname).exists():
                    output_files.append(fname)
        job.output_files = json.dumps(output_files, ensure_ascii=False)

        # Защита от ложного COMPLETED (backlog #1):
        # _finalize_job_status решает по success_count из ThreadPoolExecutor,
        # а реальная правда — что сохранилось на диске в output_files.
        # Возможен gap: rewrite_single_language вернул True, но файл не дошёл
        # до диска (race / exception при финальной записи). В этом случае
        # success_count == total, _finalize_job_status не понизил статус,
        # worker поставит COMPLETED — клиент через API получит 404 при скачивании.
        if not STOP_FLAG.is_stopped() and total > 0:
            saved = len(output_files)
            if saved == 0:
                # FAILED override любого предыдущего статуса (включая PARTIAL от
                # _finalize_job_status): 0 файлов на диске = клиенту нечего качать,
                # это hard fail независимо от того что насчитал success_count.
                job.status = JobStatus.FAILED.value
                job.error = (job.error or "") + f" | no output files saved (0/{total})"
            elif saved < total and job.status == JobStatus.RUNNING.value:
                # success_count == total, но на диске меньше — корень бага #1.
                # Понижаем только если статус ещё RUNNING (не перетираем CANCELLED/PARTIAL).
                job.status = JobStatus.PARTIAL.value
                job.error = (job.error or "") + f" | saved {saved}/{total} files on disk"
            # saved == total — статус не трогаем (worker поставит COMPLETED штатно)

    _execute_job_common(job, _run)


worker = JobWorker(queue, generation_executor=_execute_generation_job, rewrite_executor=_execute_rewrite_job, max_concurrent_jobs=int(os.environ.get("MAX_CONCURRENT_JOBS", "15")))  # PATCH-2026-06-18: 5->15, env-управляемый

FILE_MAX_AGE_DAYS = int(os.environ.get("REWRITE_FILE_MAX_AGE_DAYS", "7"))
_cleanup_stop = threading.Event()


def _cleanup_old_files():
    """Фоновый поток: удаляет файлы и задачи старше FILE_MAX_AGE_DAYS."""
    while not _cleanup_stop.wait(3600):
        cutoff = time.time() - FILE_MAX_AGE_DAYS * 86400
        for root_dir in (OUTPUT_DIR, UPLOAD_DIR):
            if not root_dir.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(str(root_dir), topdown=False):
                for fname in filenames:
                    fpath = os.path.join(dirpath, fname)
                    try:
                        if os.path.getmtime(fpath) < cutoff:
                            os.remove(fpath)
                    except OSError:
                        pass
                try:
                    if dirpath != str(root_dir) and not os.listdir(dirpath):
                        os.rmdir(dirpath)
                except OSError:
                    pass
        try:
            for job in queue.get_all_jobs():
                if job.finished_at and job.finished_at < cutoff and job.status in ("completed", "failed", "cancelled", "partial"):
                    queue.delete_job(job.id)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Migration 001: api_keys table + zombie cleanup (idempotent, safe on every restart)
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE_DIR / "migrations"))
        from migrations.api_v1_001 import migrate_001_api_v1  # type: ignore
        created, zombies, msg = migrate_001_api_v1(
            sessions_db_path=os.environ.get("SESSION_DB", str(BASE_DIR / "sessions.db")),
            jobs_db_path=DB_PATH,
        )
        print(f"[startup] {msg}")
    except Exception as _e:
        print(f"[startup] migration 001 failed: {_e}")
    # Migration user_keys_001: per-user keys storage (2026-05-24) — idempotent
    try:
        from migrations.user_keys_001 import migrate_user_keys  # type: ignore
        _t, _ins, _msg_uk = migrate_user_keys(
            sessions_db_path=os.environ.get("SESSION_DB", str(BASE_DIR / "sessions.db")),
        )
        print(f"[startup] {_msg_uk}")
    except Exception as _e:
        print(f"[startup] migration user_keys_001 failed: {_e}")
    # Migration presets_v2 (Round 1 of Generation refactor, 2026-05-23) — idempotent
    try:
        from migrations.presets_v2_001 import migrate_presets_v2  # type: ignore
        _pres = migrate_presets_v2(str(PRESETS_FILE))
        print(f"[startup] {_pres['message']}")
        if _pres.get("backup"):
            print(f"[startup]   backup: {_pres['backup']}")
    except Exception as _e:
        print(f"[startup] migration presets_v2 failed: {_e}")
    # Migration B1 (2026-06-25-secfix): пресеты per-tenant. Глобальный generation.json
    # переносим в presets/<ADMIN_USER_ID>/ (все существующие пресеты принадлежат админу). Идемпотентно.
    try:
        _admin_pf = PRESETS_DIR / ADMIN_USER_ID / "generation.json"
        if PRESETS_FILE.exists() and not _admin_pf.exists():
            _admin_pf.parent.mkdir(parents=True, exist_ok=True)
            import shutil as _shutil
            _shutil.move(str(PRESETS_FILE), str(_admin_pf))
            print(f"[startup] B1 presets migrated: global -> {_admin_pf}")
        elif PRESETS_FILE.exists() and _admin_pf.exists():
            print("[startup] B1 presets: и глобальный, и admin-файл существуют — глобальный оставлен (нужен ручной merge)")
    except Exception as _e:
        print(f"[startup] B1 presets migration failed: {_e}")
    # Configure api-auth (sessions_db path for /api/v1/* dependency)
    try:
        import api_auth as _api_auth
        _api_auth.configure(
            os.environ.get("SESSION_DB", str(BASE_DIR / "sessions.db")),
            license_mgr=license_mgr,   # для session-fallback в /api/v1/* (UI)
        )
    except Exception as _e:
        print(f"[startup] api_auth configure failed: {_e}")
    worker.start()
    cleanup_thread = threading.Thread(target=_cleanup_old_files, daemon=True)
    cleanup_thread.start()
    yield
    _cleanup_stop.set()
    worker.stop()


app = FastAPI(
    title="Rewrite Studio API",
    description=(
        "Programmatic access to multi-language rewrite jobs.\n\n"
        "- Session-based UI lives at `/`.\n"
        "- API v1 lives under `/api/v1/*` and uses api-key auth (Bearer or X-API-Key).\n"
        "- See README_API.md and /docs for details."
    ),
    version="1.0.0",
    openapi_tags=[
        {"name": "API v1", "description": "Programmatic REST API (api-key auth)"},
    ],
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ============== AUTH MIDDLEWARE ==============

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    public_paths = ("/", "/index.html", "/auth/login", "/docs", "/openapi.json", "/redoc", "/static")
    if any(path == p or path.startswith(p + "/") for p in public_paths):
        return await call_next(request)

    # /api/v1/* — собственный api-key auth через Depends(get_api_user) внутри router'а.
    # Middleware пропускает; внутри router'а возврат 401 если ключ невалиден.
    if path.startswith("/api/v1/") or path == "/api/v1":
        return await call_next(request)

    # Авторизация веб-интерфейса: только X-Session-Id (лицензия).
    # PATCH-2026-06-25-secfix (M1): god-mode 'desktop' по X-API-Key удалён —
    # он давал доступ ко ВСЕМ тенантам по одному статичному секрету.
    session_id = request.headers.get("X-Session-Id", "") or request.query_params.get("sid", "")

    if session_id:
        session_info = license_mgr.validate(session_id)
        if not session_info:
            return JSONResponse(status_code=401, content={"error": "Сессия истекла. Войдите заново."})
        request.state.user_id = license_mgr.get_user_id(session_id)
        request.state.user_name = session_info["user_name"]
        request.state.license_key = session_info["license_key"]
        request.state.auth_mode = "session"
    else:
        return JSONResponse(status_code=401, content={"error": "Требуется авторизация"})

    return await call_next(request)


# ============== AUTH ENDPOINTS ==============

@app.post("/auth/login")
async def auth_login(request: Request):
    body = await request.json()
    license_key = body.get("license_key", "").strip()
    if not license_key:
        return JSONResponse(status_code=400, content={"error": "Введите ключ"})

    ip = request.client.host if request.client else "unknown"
    success, session_id_or_error, user_name = license_mgr.login(license_key, ip)

    if success:
        # user_id (MD5(license_key)[:12]) нужен фронту для admin-gate (см. ADMIN_USER_ID в UI).
        user_id = license_mgr.get_user_id(session_id_or_error) or ""
        return {"session_id": session_id_or_error, "user_name": user_name, "user_id": user_id}
    else:
        return JSONResponse(status_code=403, content={"error": session_id_or_error})


@app.post("/auth/ping")
async def auth_ping(request: Request):
    session_id = request.headers.get("X-Session-Id", "")
    if session_id and license_mgr.ping(session_id):
        # user_id отдаём, чтобы фронт мог восстановить gate при resume-сессии без relogin.
        return {"status": "ok", "user_id": license_mgr.get_user_id(session_id) or ""}
    return JSONResponse(status_code=401, content={"error": "Сессия истекла"})


@app.post("/auth/logout")
async def auth_logout(request: Request):
    session_id = request.headers.get("X-Session-Id", "")
    if session_id:
        license_mgr.logout(session_id)
    return {"status": "ok"}


# ============== MODELS ==============

class GenerationJobRequest(BaseModel):
    """Round 2 (2026-05-23): single-topic flow + backward compat для старых клиентов.

    Новый flow требует preset_id + topic (+ optional language/model/target_length_words).
    Старый flow с {tasks, languages, preset_name, ...} принимается, если len(tasks)==1;
    при len(tasks) > 1 → 422 (multi-task removed).
    """
    # === Новый flow ===
    preset_id: Optional[str] = None
    topic: Optional[str] = Field(None, max_length=50000)
    language: Optional[str] = None
    target_length_words: Optional[int] = Field(None, ge=300, le=100000)
    max_log_entries: int = Field(5, ge=0, le=20)
    chunk_size_words: Optional[int] = Field(
        None, ge=500, le=100000,
        description="Max слов на один LLM-вызов (override). Если не задано — auto по модели.",
    )
    topics: Optional[List[str]] = Field(
        None,
        description="Batch (2026-06-20): список тем в один POST → N джобов. Если задан — topic игнорируется.",
    )
    items: Optional[List[dict]] = Field(
        None,
        description="Batch с языком на строку (2026-06-20): [{topic, language?}, ...]. Приоритетнее topics/topic; "
                    "пустой language в строке → берётся общий language/пресет.",
    )

    # === Backward compat (legacy fields, ignored if новые есть) ===
    preset_name: Optional[str] = None
    tasks: Optional[list] = None  # len>1 → 422
    languages: Optional[list] = None  # ignored after first
    safe_text: Optional[bool] = None  # ignored; больше не инжектится supplement
    max_threads: Optional[int] = None  # ignored — single-shot flow
    parallel: Optional[bool] = None  # ignored

    # === Modal/auth (как было) ===
    model: str = "claude-sonnet-4-6"
    use_subscription: bool = Field(
        False,
        description="DEPRECATED: auto-derived from model + oauth_tokens, body value ignored.",
    )
    oauth_token: str = ""
    oauth_tokens: list = []
    anthropic_api_key: str = Field(
        "",
        description="DEPRECATED: Claude requests now go through OAuth tokens only. Body value ignored.",
    )
    openai_api_key: str = ""
    parallel: bool = False
    safe_text: bool = False


class RewriteJobRequest(BaseModel):
    files: List[str]
    languages: list
    model: str = "claude-3-5-haiku-20241022"
    max_threads: int = 1
    chunk_size: int = 8000
    address_style: str = ""
    host_name: str = ""        # job-level branding (UI may also pass per-language; см. ниже)
    channel_name: str = ""
    use_subscription: bool = Field(
        False,
        description="DEPRECATED: auto-derived from model + oauth_tokens, body value ignored.",
    )
    oauth_tokens: list = []
    anthropic_api_key: str = Field(
        "",
        description="DEPRECATED: Claude requests now go through OAuth tokens only. Body value ignored.",
    )
    openai_api_key: str = ""
    parallel: bool = False
    safe_text: bool = False
    preserve_paragraphs: bool = True

    @field_validator("languages")
    @classmethod
    def _validate_languages(cls, v):
        """Reuse api.resolve_lang_input для согласованности с /api/v1/jobs.
        Принимает list of dict ({code, display, ...}) или list of str (для очень старых клиентов).
        Нормализует .code к canonical lang_name.
        """
        from api import resolve_lang_input
        if not isinstance(v, list) or not v:
            raise ValueError("languages must be a non-empty list")
        out = []
        for item in v:
            if isinstance(item, str):
                raw = item
                d = {"code": raw, "display": raw}
            elif isinstance(item, dict):
                raw = item.get("code") or item.get("display") or ""
                d = dict(item)
            else:
                raise ValueError(f"invalid language entry: {item!r}")
            canon, candidates = resolve_lang_input(raw)
            if canon:
                d["code"] = canon
                if not d.get("display"):
                    d["display"] = canon
            elif candidates:
                raise ValueError(
                    f"ambiguous short code '{raw}': matches {candidates}. Use canonical name to disambiguate."
                )
            else:
                raise ValueError(
                    f"unsupported language: '{raw}'. Use canonical name (e.g. 'English'), "
                    f"Russian display ('Английский'), or short code (e.g. 'en')."
                )
            out.append(d)
        return out


# ============== HELPER ==============

def _get_user_id(request: Request) -> str:
    # PATCH-2026-06-25-secfix: fail-closed. Аутентифицированный запрос ВСЕГДА имеет
    # request.state.user_id (middleware иначе вернул бы 401). Отсутствие = баг → 401.
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return uid


def _get_user_output_dir(request: Request) -> Path:
    user_id = _get_user_id(request)
    user_out = OUTPUT_DIR / user_id
    user_out.mkdir(parents=True, exist_ok=True)
    return user_out


def _preset_manager_for(user_id: str) -> PresetManager:
    """Per-tenant PresetManager (B1 PATCH-2026-06-25-secfix): пресеты скоупятся по user_id
    (как outputs/keys). Раньше был один глобальный стор — любой тенант читал/перезаписывал/стирал чужие."""
    d = PRESETS_DIR / (user_id or ADMIN_USER_ID)
    d.mkdir(parents=True, exist_ok=True)
    return PresetManager(str(d / "generation.json"))


def _job_belongs_to_user(job: Job, user_id: str) -> bool:
    # PATCH-2026-06-25-secfix (M1): убран обход 'desktop'. Только владелец задачи.
    return getattr(job, "user_id", "") == user_id


def _job_display_meta(j: Job) -> dict:
    """Читаемое имя задачи + бейджи из JSON-блобов джобы. Без изменения схемы БД.
    PATCH-2026-06-20-joblist-names. Генерация -> topic/preset; рерайт -> имя исходного файла."""
    name = ""
    langs = ""
    nfiles = 0
    try:
        if j.job_type == JobType.GENERATION.value:
            payload = json.loads(j.gen_tasks or "{}")
            if isinstance(payload, list):  # legacy v1: список задач
                payload = payload[0] if payload else {}
            topic = (payload.get("topic") or "").strip() if isinstance(payload, dict) else ""
            first_line = topic.split("\n", 1)[0].strip()  # только заголовок, без "ИДЕЯ:/ХУКИ:" простыни
            name = first_line[:80] or (j.preset_name or "Генерация")
            lang = payload.get("language") if isinstance(payload, dict) else None
            langs = lang or ""
            nfiles = 1
        else:
            files = json.loads(j.rewrite_files or "[]")
            if files:
                base = os.path.basename(str(files[0]))
                name = base + (f" +{len(files) - 1}" if len(files) > 1 else "")
            else:
                name = "Рерайт"
            langs_list = json.loads(j.rewrite_languages or "[]")
            langs = ", ".join(
                (l.get("code") or l.get("display") or "")
                for l in langs_list if isinstance(l, dict)
            )
            nfiles = len(files)
    except Exception:
        name = name or (j.preset_name or j.job_type or "задача")
    return {"name": name, "langs": langs, "nfiles": nfiles}


# ============== FRONTEND ==============

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Rewrite Studio</h1><p>index.html не найден в static/</p>")


# ============== DATA ENDPOINTS ==============

@app.get("/languages")
def get_languages():
    return [{"display": display, "code": code} for display, code in LANGUAGES.items()]


@app.get("/models")
def get_models(request: Request):
    # Claude IDs — alias-format (pinned snapshot без даты-дрейфа).
    # Источник: https://platform.claude.com/docs/en/docs/about-claude/models/overview (2026-05-22)
    base = [
        {"value": "claude-opus-4-7",   "label": "Claude Opus 4.7"},
        {"value": "claude-opus-4-8",   "label": "Claude Opus 4.8"},
        {"value": "claude-fable-5",    "label": "Claude Fable 5"},
        {"value": "claude-opus-4-6",   "label": "Claude Opus 4.6"},
        {"value": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
        {"value": "claude-haiku-4-5",  "label": "Claude Haiku 4.5"},
        {"value": "gpt-4.1-nano",      "label": "GPT-4.1 Nano"},
        {"value": "gpt-4.1-mini",      "label": "GPT-4.1 Mini"},
        {"value": "gpt-4o-mini",       "label": "GPT-4o Mini"},
        {"value": "gpt-4o",            "label": "GPT-4o"},
    ]
    cfg = get_keys_config(_get_user_id(request))
    custom = cfg.get("custom_models", [])
    for m in custom:
        if m and m.strip():
            base.append({"value": m.strip(), "label": m.strip()})
    return base


@app.get("/settings/keys")
def get_settings_keys(request: Request):
    uid = _get_user_id(request)
    cfg = get_keys_config(uid)
    is_admin = (uid == ADMIN_USER_ID)
    resp = {
        "anthropic_api_key": cfg.get("anthropic_api_key", ""),
        "openai_api_key": cfg.get("openai_api_key", ""),
        "oauth_tokens": cfg.get("oauth_tokens", []),
        "is_admin": is_admin,
    }
    if is_admin:
        resp.update({
            "gpt_service_tier": cfg.get("gpt_service_tier", "default"),
            "gpt_use_cache": cfg.get("gpt_use_cache", True),
            "postprocess_check_model": cfg.get("postprocess_check_model", "gpt-5-mini"),
            "custom_models": cfg.get("custom_models", []),
            "debug_mode": cfg.get("debug_mode", False),
        })
    return resp


@app.post("/settings/keys")
async def save_settings_keys(request: Request):
    body = await request.json()
    uid = _get_user_id(request)
    kwargs = {
        "anthropic_api_key": body.get("anthropic_api_key", ""),
        "openai_api_key": body.get("openai_api_key", ""),
        "oauth_tokens": body.get("oauth_tokens", []),
        "user_id": uid,
    }
    # Admin-only поля: non-admin silently drop, даже если пришли в body.
    if uid == ADMIN_USER_ID:
        for k in ("gpt_service_tier", "gpt_use_cache", "postprocess_check_model",
                  "custom_models", "debug_mode"):
            if k in body:
                kwargs[k] = body[k]
    save_keys_config(**kwargs)
    return {"status": "ok"}


# Preset v2 helpers (Round 1 of Generation refactor, 2026-05-23). Старые URL по {name}
# поддерживаются как backward compat (PRESET_MANAGER.get_preset резолвит id+name).


def _preset_summary(p: "Preset") -> dict:
    """Short summary без content (для list endpoint)."""
    return {
        "id": p.id,
        "name": p.name,
        "target_length_words": p.target_length_words,
        "language": p.language,  # language task
        "created_at": p.created_at,
        "last_used_at": p.last_used_at,
    }


@app.get("/presets")
def list_presets_v2(request: Request):
    """List CURRENT TENANT's presets (B1: per-user стор)."""
    pm = _preset_manager_for(_get_user_id(request))
    return {p.name: _preset_summary(p) for p in pm.list_presets()}


@app.post("/presets/sync")
def sync_presets(presets: dict, request: Request):
    """Полная замена пресетов ТЕКУЩЕГО тенанта (B1: пишет только в его файл, не глобально).
    Принимает или {<name>: ...}, или {presets: {...}}."""
    user_id = _get_user_id(request)
    data = {"presets": presets} if "presets" not in presets else presets
    user_file = PRESETS_DIR / user_id / "generation.json"
    user_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(user_file) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(user_file))  # атомарно
    _preset_manager_for(user_id).load()
    return {"status": "ok"}


@app.get("/presets/{identifier}")
def get_preset_endpoint(identifier: str, request: Request):
    """Получить preset ТЕКУЩЕГО тенанта по id (canonical) или name (backward compat)."""
    pm = _preset_manager_for(_get_user_id(request))
    p = pm.get_preset(identifier)
    if not p:
        raise HTTPException(status_code=404, detail="preset not found")
    return p.to_dict()


@app.post("/presets/{identifier}")
async def create_or_update_preset(identifier: str, request: Request):
    """Create / update preset по id или name.

    Body может содержать:
      - новая schema: {name, target_length_words?, content}
      - старая schema (BACKWARD COMPAT): {prompt_template, custom_instructions, total_words, content_type, ...}
        → автоматически склеиваем в content (как в migration v2).
    """
    body = await request.json()
    pm = _preset_manager_for(_get_user_id(request))  # B1: per-tenant стор
    name = str(body.get("name") or identifier).strip() or identifier

    # Преобразование старого формата в новый (та же логика что в миграции)
    if "content" in body:
        content = str(body.get("content") or "")
    else:
        parts = []
        ct = str(body.get("content_type") or "").strip()
        if ct:
            parts.append(f"Type: {ct}")
        pt = str(body.get("prompt_template") or "").strip()
        if pt:
            parts.append(pt)
        ci = str(body.get("custom_instructions") or "").strip()
        if ci:
            parts.append(ci)
        content = "\n\n".join(parts) if parts else "(empty)"

    twl = body.get("target_length_words") or body.get("total_words")
    try:
        twl = int(twl) if twl is not None else None
    except Exception:
        twl = None

    # Resolve id: если такой preset уже есть (по id или name) — переиспользуем его id
    existing = pm.get_preset(identifier)
    if existing:
        pid = existing.id
        created_at = existing.created_at or time.time()
    else:
        # Generate new id via slugify (та же ф-я что в migration)
        try:
            from migrations.presets_v2_001 import _slugify
            pid = _slugify(name)
            # collision check
            existing_ids = {p.id for p in pm.list_presets()}
            base = pid
            sfx = 1
            while pid in existing_ids:
                sfx += 1
                pid = f"{base}-{sfx}"
        except Exception:
            import secrets as _secrets
            pid = _secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        created_at = time.time()

    # language task: из тела (future UI) → иначе сохраняем существующий (чтобы UI-edit не сбрасывал дефолт)
    lang_default = (str(body.get("language")).strip() or None) if body.get("language") else (existing.language if existing else None)

    p = Preset(
        id=pid,
        name=name,
        target_length_words=twl,
        language=lang_default,
        content=content,
        created_at=created_at,
        last_used_at=existing.last_used_at if existing else None,
    )
    pm.save_preset(p)
    return p.to_dict()


@app.delete("/presets/{identifier}")
def delete_preset_endpoint(identifier: str, request: Request):
    """Удалить preset ТЕКУЩЕГО тенанта по id (или name) (B1)."""
    pm = _preset_manager_for(_get_user_id(request))
    if not pm.delete_preset(identifier):
        raise HTTPException(status_code=404, detail="preset not found")
    return {"status": "deleted"}


# ============== JOB ENDPOINTS ==============

@app.post("/jobs/generation")
def create_generation_job(req: GenerationJobRequest, request: Request):
    """Round 2 generation handler (2026-05-23): single-topic flow + backward compat."""
    user_id = _get_user_id(request)
    user_output = _get_user_output_dir(request)

    # === Resolve preset (id новый, name legacy) ===
    preset_identifier = req.preset_id or req.preset_name or ""
    if not preset_identifier:
        raise HTTPException(status_code=422, detail="preset_id (или preset_name для legacy) required")
    preset_obj = _preset_manager_for(user_id).get_preset(preset_identifier)  # B1: пресет тенанта
    if not preset_obj:
        raise HTTPException(status_code=404, detail=f"preset not found: {preset_identifier}")

    # === Resolve темы (+опц. язык на строку): items[] | topics[] | single topic | legacy tasks ===
    # PATCH-2026-06-20-gen-batch: один POST -> N джобов; язык можно задать на каждую тему (items).
    pairs = []  # [(topic, per_item_language_or_empty), ...]
    if req.items:
        for it in req.items:
            if not isinstance(it, dict):
                continue
            t = str(it.get("topic", "")).strip()
            lng = str(it.get("language") or "").strip()
            if t:
                pairs.append((t, lng))
    elif req.topics:
        pairs = [(t.strip(), "") for t in req.topics if t and t.strip()]
    else:
        topic = (req.topic or "").strip()
        if not topic and req.tasks:
            if len(req.tasks) > 1:
                raise HTTPException(
                    status_code=422,
                    detail="multi-task generation removed in Round 2. Send one topic per call.",
                )
            first = req.tasks[0]
            if isinstance(first, dict):
                topic = str(first.get("title", "")).strip()
            elif isinstance(first, str):
                topic = first.strip()
        pairs = [(topic, "")] if topic else []
    pairs = [(t, lng) for (t, lng) in pairs if len(t) >= 10]
    if not pairs:
        raise HTTPException(status_code=422, detail="topic required (min 10 chars)")

    # === Resolve language (legacy languages[0] fallback) ===
    language = (req.language or "").strip()
    if not language and req.languages:
        first_lang = req.languages[0]
        if isinstance(first_lang, dict):
            language = str(first_lang.get("name") or first_lang.get("code") or first_lang.get("display") or "")
        elif isinstance(first_lang, str):
            language = first_lang
    # final fallback после worker'а — берёт из preset.content (через extract_field_from_content) или English

    # === Auth resolve ===
    oauth_tokens_list = req.oauth_tokens or []
    model_is_claude = "claude" in (req.model or "").lower()
    if model_is_claude:
        if not oauth_tokens_list and not req.oauth_token:
            raise HTTPException(
                status_code=422,
                detail="Claude models require OAuth tokens via /settings → OAuth токены Pro. "
                       "API key для Claude больше не поддерживается."
            )
        api_key = ""
    else:
        # 2026-06-04 GPT key resolve: body → partner's saved → env (only admin) → 422.
        api_key = req.openai_api_key
        if not api_key:
            api_key = (rewrite_core.get_keys_config(user_id).get("openai_api_key") or "")
        if not api_key:
            api_key = rewrite_core.env_openai_key_for(user_id)
        if not api_key:
            raise HTTPException(
                status_code=422,
                detail="OpenAI API key required: pass openai_api_key or save it in /settings."
            )
    effective_use_subscription = model_is_claude and bool(oauth_tokens_list or req.oauth_token)

    # === Default target_length_words: если ни в request, ни в preset нет — 8000 ===
    # (raньше worker делал num_chunks=1 на пустом target → single огромный chunk,
    #  что и провоцировало stream-stall на Opus. Sane default снижает риск.)
    target_words_final = req.target_length_words
    if not target_words_final:
        preset_target = getattr(preset_obj, "target_length_words", None)
        if not preset_target:
            target_words_final = 8000
            print(f"[GEN_JOB] target_length_words default 8000 applied (req=None, preset=None) for preset={preset_obj.id}")

    # === Pack payload + создаём по джобу на каждую тему (batch) ===
    # PATCH-2026-06-20-gen-batch: общие поля (preset/auth/language/target) резолвлены выше ОДИН раз;
    # в цикл обёрнут ТОЛЬКО pack+Job+add_job. Каждый job — тот же проверенный single-topic v2-dict.
    job_ids = []
    failed = []
    for topic, item_lang in pairs:
        eff_lang = item_lang or language  # язык строки приоритетнее общего language/пресета
        try:
            payload = {
                "v": 2,
                "topic": topic,
                "preset_id": preset_obj.id,
                "preset_name": preset_obj.name,
                "language": eff_lang or None,
                "target_length_words": target_words_final,
                "max_log_entries": req.max_log_entries,
                "chunk_size_words": req.chunk_size_words,
            }
            job = Job(
                job_type=JobType.GENERATION.value,
                model=req.model,
                api_key=api_key or "",
                use_subscription=effective_use_subscription,
                oauth_token=req.oauth_token or (oauth_tokens_list[0] if oauth_tokens_list else ""),
                oauth_tokens=json.dumps(oauth_tokens_list, ensure_ascii=False),
                max_threads=1,  # single-shot
                output_dir=str(user_output),
                gen_tasks=json.dumps(payload, ensure_ascii=False),  # v2 JSON dict
                preset_name=preset_obj.name,
                gen_languages=json.dumps([{"name": eff_lang}] if eff_lang else [], ensure_ascii=False),
                gen_safe_text=False,
                parallel=True,  # gen → parallel-launcher (capped by JobWorker.max_concurrent_jobs);
                                 # реальные claude subprocess'ы лимитируются global _claude_sema (15 на 12GB box).
                user_id=user_id,
            )
            job_ids.append(queue.add_job(job))
        except Exception as e:
            failed.append({"topic": topic[:60], "error": str(e)})
    if not job_ids:
        raise HTTPException(status_code=500, detail=f"no jobs created (failed={failed})")
    # job_id (первый) — обратная совместимость со старым клиентом; job_ids/count — новый batch-ответ.
    return {"job_id": job_ids[0], "job_ids": job_ids, "count": len(job_ids), "failed": failed, "status": "pending"}


@app.post("/jobs/rewrite")
def create_rewrite_job(req: RewriteJobRequest, request: Request):
    user_id = _get_user_id(request)
    user_output = _get_user_output_dir(request)
    model_is_claude = "claude" in (req.model or "").lower()
    oauth_tokens_list = req.oauth_tokens or []
    # Task C: Claude теперь только через OAuth. body.anthropic_api_key + ENV ANTHROPIC_API_KEY игнорируются.
    if model_is_claude:
        if not oauth_tokens_list:
            raise HTTPException(
                status_code=422,
                detail="Claude models require OAuth tokens via /settings → OAuth токены Pro. "
                       "API key для Claude больше не поддерживается."
            )
        api_key = ""  # OAuth path не использует api_key
    else:
        # 2026-06-04 GPT key resolve: body → partner's saved → env (only admin) → 422.
        api_key = req.openai_api_key
        if not api_key:
            api_key = (rewrite_core.get_keys_config(user_id).get("openai_api_key") or "")
        if not api_key:
            api_key = rewrite_core.env_openai_key_for(user_id)
        if not api_key:
            raise HTTPException(
                status_code=422,
                detail="OpenAI API key required: pass openai_api_key or save it in /settings."
            )
    # use_subscription auto-derive (DEPRECATED body field): для Claude всегда True если есть oauth.
    effective_use_subscription = model_is_claude and bool(oauth_tokens_list)
    # Task 3: для всех кроме админа (Володя) — принудительно preserve_paragraphs=True (для GPT-пути).
    # 2026-06-03: для Claude (subscription/api) — всегда False (естественный реролл; chunk-loop
    # с сохранением абзацев 1:1 заточен под слабую gpt-4.1-nano, для Sonnet/Opus вредит).
    # ADMIN_USER_ID keep in sync with api.py / index.html.
    ADMIN_USER_ID = "292d249f2de0"
    if model_is_claude:
        effective_preserve_paragraphs = False
    else:
        effective_preserve_paragraphs = True if user_id != ADMIN_USER_ID else req.preserve_paragraphs
    # Прокидка job-level host/channel в каждую запись languages, НЕ перетирая
    # уже заданные per-language значения (UI исторически передаёт их в lang dict).
    langs_with_branding = []
    for lang in req.languages:
        d = dict(lang) if isinstance(lang, dict) else {"code": str(lang), "display": str(lang)}
        if not d.get("host"):
            d["host"] = req.host_name
        if not d.get("channel"):
            d["channel"] = req.channel_name
        langs_with_branding.append(d)
    job = Job(
        job_type=JobType.REWRITE.value,
        model=req.model,
        api_key=api_key or "",
        max_threads=req.max_threads,
        output_dir=str(user_output),
        rewrite_files=json.dumps(req.files, ensure_ascii=False),
        rewrite_languages=json.dumps(langs_with_branding, ensure_ascii=False),
        rewrite_chunk_size=req.chunk_size,
        rewrite_address_style=req.address_style,
        rewrite_preserve_paragraphs=effective_preserve_paragraphs,
        use_subscription=effective_use_subscription,
        oauth_tokens=json.dumps(req.oauth_tokens or [], ensure_ascii=False),
        gen_safe_text=req.safe_text,
        parallel=req.parallel,
        user_id=user_id,
    )
    job_id = queue.add_job(job)
    return {"job_id": job_id, "status": "pending"}


@app.get("/jobs")
def list_jobs(request: Request):
    user_id = _get_user_id(request)
    user_jobs = queue.get_jobs_by_user(user_id)
    return [
        {
            "id": j.id,
            "type": j.job_type,
            "status": j.status,
            "progress": j.progress_percent,
            "detail": j.progress_detail,
            "log": "",  # PATCH-2026-06-18-joblist-nolog: лог не тащим в список (3МБ->КБ); берётся через /jobs/{id}
            "parallel": getattr(j, "parallel", False),
            "created_at": j.created_at,
            **_job_display_meta(j),  # PATCH-2026-06-20-joblist-names: name, langs, nfiles
        }
        for j in user_jobs
    ]


@app.get("/jobs/download-zip-bulk")
def download_jobs_zip_bulk(request: Request):
    """Скачать файлы нескольких задач одним ZIP. ids через запятую (?ids=a,b,c).
    PATCH-2026-06-20-bulk-download. ВАЖНО: зарегистрирован ДО /jobs/{job_id},
    иначе тот перехватит путь (job_id='download-zip-bulk')."""
    user_id = _get_user_id(request)
    ids = request.query_params.get("ids", "")
    id_list = [s for s in ids.split(",") if s]
    if not id_list:
        raise HTTPException(status_code=400, detail="no job ids")
    buf = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for job_id in id_list:
            job = queue.get_job(job_id)
            if not job or not _job_belongs_to_user(job, user_id):
                continue  # чужие/несуществующие — молча пропускаем (не палим чужие задачи)
            output_dir = Path(job.output_dir or OUTPUT_DIR)
            files = json.loads(job.output_files or "[]")
            for fn in files:
                path = output_dir / fn
                if path.exists():
                    zf.write(path, f"job-{job_id}/{fn}")  # namespace per job — нет коллизий имён
                    added += 1
    if added == 0:
        raise HTTPException(status_code=404, detail="no files to download")
    buf.seek(0)
    filename = f"jobs-{len(id_list)}-files.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    # B3 PATCH-2026-06-25-secfix: НЕ отдавать секреты. Вырезаем ключи/токены из ответа
    # (UI-контракт сохранён — остальные поля как раньше).
    d = asdict(job)
    for _k in ("api_key", "oauth_token", "oauth_tokens"):
        d.pop(_k, None)
    return d


@app.get("/jobs/{job_id}/files")
def get_job_files(job_id: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    return json.loads(job.output_files or "[]")


@app.get("/jobs/{job_id}/files/{filename}")
def download_file(job_id: str, filename: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    output_dir = Path(job.output_dir or OUTPUT_DIR)
    path = output_dir / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(path), filename=filename)


@app.get("/jobs/{job_id}/download-zip")
def download_job_zip(job_id: str, request: Request):
    """Скачать все файлы задачи одним ZIP-архивом."""
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    output_dir = Path(job.output_dir or OUTPUT_DIR)
    files = json.loads(job.output_files or "[]")
    if not files:
        raise HTTPException(status_code=404, detail="no files to download")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fn in files:
            path = output_dir / fn
            if path.exists():
                zf.write(path, fn)
    buf.seek(0)
    filename = f"job-{job_id}-files.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    if job.status == JobStatus.PENDING.value:
        queue.cancel_job(job_id)
    elif job.status == JobStatus.RUNNING.value:
        with _job_stop_events_lock:
            evt = _job_stop_events.get(job_id)
        if evt:
            evt.set()
        else:
            # M3 PATCH-2026-06-25-secfix: НЕ дёргаем глобальный STOP_FLAG — он обрывал ВСЕ
            # выполняющиеся задачи всех тенантов. Если per-job event ещё не зарегистрирован
            # (узкая гонка RUNNING<->регистрация) — no-op; событие регистрируется через миг,
            # пользователь может повторить «Стоп». Глобальный stop остаётся только для admin shutdown.
            pass
    return {"status": "cancelled"}


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, request: Request):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    if not queue.retry_job(job_id):
        raise HTTPException(status_code=400, detail="retry only for failed/cancelled/partial jobs")
    return {"status": "pending"}


@app.post("/jobs/{job_id}/retry-full")
def retry_job_full(job_id: str, request: Request):
    """
    Полная перегенерация: сбрасывает весь прогресс этой задачи (все чанки, готовые
    результаты, выходные файлы) и запускает с нуля.
    Отличается от обычного /retry который резюмит только failed/incomplete чанки.
    """
    from rewrite_core import PROGRESS_MANAGER
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")

    # Сбрасываем прогресс для каждой пары (file, language)
    cleared = 0
    if job.job_type == JobType.REWRITE.value:
        try:
            files = json.loads(job.rewrite_files or "[]")
            languages = json.loads(job.rewrite_languages or "[]")
        except Exception:
            files, languages = [], []
        for f in files:
            file_path = f if os.path.isabs(f) else str(_get_user_output_dir(request).parent.parent / "uploads" / user_id / os.path.basename(f))
            for lang in languages:
                lang_name = lang.get("code") if isinstance(lang, dict) else str(lang)
                if PROGRESS_MANAGER.delete_task(os.path.basename(f), lang_name, user_id=user_id):  # B5: tenant-scoped
                    cleared += 1
                # Также удаляем готовый .txt чтобы не вводил в заблуждение до перегенерации
                base_name = os.path.splitext(os.path.basename(f))[0][:50]
                from rewrite_core import make_output_filename
                out_path = Path(job.output_dir) / make_output_filename(base_name, lang_name)
                try:
                    if out_path.exists():
                        out_path.unlink()
                except Exception:
                    pass

    # Сбрасываем output_files чтобы UI не показывал старые
    job.output_files = "[]"
    queue.update_job(job)

    if not queue.retry_job(job_id):
        raise HTTPException(status_code=400, detail="retry only for failed/cancelled/partial jobs")
    return {"status": "pending", "cleared_tasks": cleared}


@app.post("/jobs/{job_id}/parallel")
def mark_job_parallel(job_id: str, request: Request, parallel: bool = True):
    user_id = _get_user_id(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_belongs_to_user(job, user_id):
        raise HTTPException(status_code=403, detail="access denied")
    if not queue.mark_job_parallel(job_id, parallel):
        raise HTTPException(status_code=400, detail="parallel only for pending jobs")
    return {"status": "ok"}


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


MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 МБ


@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    user_id = _get_user_id(request)
    user_upload = UPLOAD_DIR / user_id
    user_upload.mkdir(parents=True, exist_ok=True)
    # B4 PATCH-2026-06-25-secfix: санитайз имени из multipart (роут-регекс не защищает —
    # имя берётся из тела). basename срезает пути, плюс защита от ../ и абсолютных, + containment.
    safe = os.path.basename(file.filename or "uploaded").replace("/", "_").replace("\\", "_")[:200]
    if not safe or safe in (".", ".."):
        safe = "uploaded"
    user_root = user_upload.resolve()
    path = (user_root / safe).resolve()
    if not (path == user_root / safe and str(path).startswith(str(user_root) + os.sep)):
        raise HTTPException(status_code=400, detail="invalid filename")
    total_size = 0
    try:
        with open(path, "wb") as f:
            while True:
                chunk = await file.read(8192)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail=f"Файл слишком большой (макс. {MAX_UPLOAD_SIZE // 1024 // 1024} МБ)")
                f.write(chunk)
    except HTTPException:
        if path.exists():
            os.remove(path)
        raise
    return {"filename": safe, "path": str(path)}  # B4: возвращаем САНИРОВАННОЕ имя (фронт ссылается на него)


# ============== API v1 (programmatic, api-key auth) ==============

from api import build_api_router  # noqa: E402

api_router = build_api_router(
    queue=queue,
    upload_dir=UPLOAD_DIR,
    output_dir=OUTPUT_DIR,
    job_stop_events=_job_stop_events,
    job_stop_events_lock=_job_stop_events_lock,
)
app.include_router(api_router)
# Channel Studio (Path B) откачен 2026-05-23 — заменяется модернизацией Generation tab.


# ============== UI helper routes for api-key management (session auth) ==============

# Field уже импортирован выше (см. line ~22)
from api_auth import create_api_key, list_api_keys, revoke_api_key  # noqa: E402


class _ApiKeyCreateRequest(BaseModel):
    name: str


@app.post("/api/keys/create", tags=["UI helpers"], summary="Create api-key (session auth)")
def ui_api_keys_create(req: _ApiKeyCreateRequest, request: Request):
    """UI-only endpoint: создать api-key для текущей сессии. Plain key возвращается ОДИН раз."""
    user_id = _get_user_id(request)
    license_key = getattr(request.state, "license_key", "") or ""
    sessions_db = os.environ.get("SESSION_DB", str(BASE_DIR / "sessions.db"))
    return create_api_key(sessions_db, user_id, license_key, req.name)


@app.get("/api/keys/list", tags=["UI helpers"], summary="List api-keys (session auth)")
def ui_api_keys_list(request: Request):
    user_id = _get_user_id(request)
    sessions_db = os.environ.get("SESSION_DB", str(BASE_DIR / "sessions.db"))
    return {"items": list_api_keys(sessions_db, user_id)}


@app.post("/api/keys/{key_id}/revoke", tags=["UI helpers"], summary="Revoke api-key (session auth)")
def ui_api_keys_revoke(key_id: str, request: Request):
    user_id = _get_user_id(request)
    sessions_db = os.environ.get("SESSION_DB", str(BASE_DIR / "sessions.db"))
    ok = revoke_api_key(sessions_db, key_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="api key not found or already revoked")
    return {"key_id": key_id, "status": "revoked"}


# Статика (CSS, JS, картинки)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8899)
