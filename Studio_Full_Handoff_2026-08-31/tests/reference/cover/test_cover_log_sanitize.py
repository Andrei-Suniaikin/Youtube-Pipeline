# -*- coding: utf-8 -*-
"""Пользовательский лог задачи: только прогресс, без внутренней кухни.

`job.log` уходит клиенту в `GET /jobs/{id}`. Тесты фиксируют, что подробности
пайплайна (провайдеры, промпты, шаги, ретраи, пути, traceback) наружу не
попадают, а прогресс при этом читается.
"""

import os
import subprocess
import sys
from pathlib import Path

from log_sanitize import JobLogSanitizer, expected_cover_count, humanize_error


# Реальные форматы сообщений cover_core (с префиксами обёрток task_log).
CORE_MESSAGES = [
    "\n==================================================",
    "🌱 Seed-mode: папка='W7-01-A', языков=3, вариантов=4",
    "✏️ Title-mode: base_title='10 МЕСТ'",
    "🚀 Параллельный режим: 8 потоков, 3 URL × 4 языков = 12 задач",
    "[parallel] 8 workers for 12 tasks (max_threads=8)",
    "▶ К генерации: 12 задач в пуле из 8 потоков",
    "📌 Ссылка 1/3: https://youtube.com/watch?v=abc",
    "🔍 Анализ обложки...",
    "🎨 Style-transfer (img2img): обложка + референс → flow (Nano Banana), перевод текста на «Русский»",
    "⚡ Простая генерация (img2img): превью → grok, перевод текста на «Польский»",
    "✍️ Адаптация текста на Русский...",
    "📝 Промпт: A dramatic WW2 scene, cinematic lighting, bold title on the left",
    "✅ Проверка результата (попытка 1/3)...",
    "   ✅ Проверка пройдена",
    "   ❌ Проблема: текст обрезан справа",
    "[veo] попытка 2/3: HTTP 500: {\"error\":\"internal\"}",
    "[gpt-image-2] попытка 1/3: пустой ответ",
    "   ✅ Сохраняю вариант без текста (попытка 2)",
    "Traceback (most recent call last):\n  File \"cover_core.py\", line 1781",
]

# Ничего из этого не должно попасть в лог пользователя.
FORBIDDEN = (
    "Nano Banana", "Fast Gen", "gpt-image", "[veo]", "grok", "flow",
    "Промпт", "Seed-mode", "Title-mode", "workers", "потоков",
    "/root/", "outputs/", "Traceback", "youtube.com", "Проверка",
    "Адаптация", "Анализ", "no accounts",
)


def _feed_all(sanitizer, messages):
    out = []
    for msg in messages:
        out.extend(sanitizer.feed(msg))
    return out


def test_internal_pipeline_messages_are_dropped():
    sanitizer = JobLogSanitizer(total=12, verbose=False)
    assert _feed_all(sanitizer, CORE_MESSAGES) == []


def test_progress_lines_are_readable():
    sanitizer = JobLogSanitizer(total=12, verbose=False)
    assert sanitizer.start() == ["▶ Старт: 12 обложек"]
    assert JobLogSanitizer(total=1, verbose=False).start() == ["▶ Старт: 1 обложка"]
    assert JobLogSanitizer(total=3, verbose=False).start() == ["▶ Старт: 3 обложки"]
    assert JobLogSanitizer(total=21, verbose=False).start() == ["▶ Старт: 21 обложка"]
    lines = _feed_all(sanitizer, [
        "[Русский] 💾 Сохранено: /root/cover_tool/outputs/uid/W7/ru_cover.jpg",
        "[Польский v2] 💾 Сохранено (без текста): /root/cover_tool/outputs/uid/W7/pl_cover.jpg",
        "⏭ Уже есть: W7/en_cover.jpg",
        "⏭ [4/12] Уже есть: W7/de_cover.jpg",
    ])
    assert lines == [
        "✓ 1/12 · ru_cover.jpg",
        "✓ 2/12 · pl_cover.jpg (без текста)",
        "⏭ 3/12 · уже готово",
        "⏭ 4/12 · уже готово",
    ]
    # Путь до файла не утекает — только имя, которое и так видно в галерее.
    assert all("/root/" not in line for line in lines)


def test_retry_and_reference_warnings_are_anonymous():
    sanitizer = JobLogSanitizer(total=2, verbose=False)
    lines = _feed_all(sanitizer, [
        "   ⚠ Попытка 2/3: Fast Gen не отдал картинку — HTTP 500 upstream",
        "   ⚠ Попытка 3/3: пустой результат от API, повтор...",
        "[Русский] ⚠ Реф-файл ref_01.jpg пропущен (не существует/>5MB/не картинка)",
        "   ⚠ Референс не найден/не читается (/root/cover_tool/outputs/uid/ref.jpg) — генерирую без него",
    ])
    assert lines == [
        "⚠ повтор попытки 2/3",
        "⚠ повтор попытки 3/3",
        "⚠ референс не приложен",
        "⚠ референс не приложен",
    ]
    assert all(not any(bad in line for bad in FORBIDDEN) for line in lines)


def test_cancel_is_reported_once():
    sanitizer = JobLogSanitizer(total=4, verbose=False)
    first = sanitizer.feed("⏹ Задание отменено. Отменяю незапущенные генерации...")
    second = sanitizer.feed("⏹ Задание отменено")
    assert first == ["⏹ Отменено"]
    assert second == []          # ядро пишет отмену из каждого потока


def test_errors_are_humanized_and_deduplicated():
    sanitizer = JobLogSanitizer(total=4, verbose=False)
    first = sanitizer.feed("❌ [Русский]: Generation failed: no accounts available")
    dup = sanitizer.feed("❌ [Польский]: Generation failed: no accounts available")
    assert first == ["✗ Сервис перегружен или лимит исчерпан — повторите позже"]
    assert dup == []             # дубли схлопываются, счёт уходит в сводку
    assert sanitizer.failed == 2
    finish = sanitizer.finish(ok=2)
    assert finish == [
        "■ Завершено: 2 из 4",
        "✗ Не удалось: 2 — Сервис перегружен или лимит исчерпан — повторите позже",
    ]


def test_intermediate_check_failures_are_not_shown_as_errors():
    """«❌ Проблема: …» — шаг проверки надписи, а не результат задачи."""
    sanitizer = JobLogSanitizer(total=1, verbose=False)
    assert sanitizer.feed("   ❌ Проблема: текст обрезан справа") == []
    assert sanitizer.feed(
        "   ❌ Не удалось сгенерировать даже без текста за 3 попыток — сохраняю последний вариант с текстом"
    ) == []
    assert sanitizer.failed == 0


def test_humanize_error_covers_every_branch():
    cases = {
        "Job cancelled by user": "Задача отменена",
        "Request blocked by safety policy": "Запрос отклонён фильтром контента — измените текст или референс",
        "Серверный ключ не настроен: COVER_FAST_GEN_KEY (или COVER_FAST_GEN_KEYS)":
            "Нет доступа к сервису генерации — сообщите администратору",
        "HTTP 401 Unauthorized": "Нет доступа к сервису генерации — сообщите администратору",
        "429 Too Many Requests": "Сервис перегружен или лимит исчерпан — повторите позже",
        "No accounts available": "Сервис перегружен или лимит исчерпан — повторите позже",
        "Read timed out after 300s": "Сервис не ответил вовремя — повторите задачу",
        "Не удалось скачать превью": "Не удалось получить исходное превью по ссылке",
        "Fast Gen не отдал картинку": "Сервис не вернул изображение — повторите задачу",
        "[Errno 28] No space left on device": "Ошибка сохранения файла на сервере",
        "Connection reset by peer": "Сервис генерации временно недоступен — повторите позже",
        "": "Не удалось выполнить задачу — попробуйте повторить",
        "totally unexpected internal state": "Не удалось выполнить задачу — попробуйте повторить",
    }
    for raw, expected in cases.items():
        assert humanize_error(raw) == expected, raw
    # Исходный текст исключения наружу не подставляется никогда.
    assert "COVER_FAST_GEN_KEY" not in humanize_error(
        "Серверный ключ не настроен: COVER_FAST_GEN_KEY"
    )


def test_verbose_mode_is_passthrough():
    sanitizer = JobLogSanitizer(total=3, verbose=True)
    assert sanitizer.start() == []
    assert sanitizer.feed("📝 Промпт: secret") == ["📝 Промпт: secret"]
    assert sanitizer.finish(ok=3) == []


def test_expected_cover_count():
    assert expected_cover_count("seed", [], ["ru", "pl"], None, {"ru": "s", "pl": "s"}, 4) == 8
    assert expected_cover_count("title", [], ["ru", "pl"], {"ru": "t", "pl": "t"}, None, 3) == 2
    assert expected_cover_count("url", ["u1", "u2"], ["ru", "pl"], None, None, 3) == 4
    assert expected_cover_count("url", [], [], None, None, 0) == 1


# --- проводка в cover_server ------------------------------------------------

_WORKER_SCRIPT = r'''
import json
import os
import cover_server
from job_queue_cover import Job, JobStatus


class _Lease:
    openai_key = "k"
    generation_key = "k"

    def release(self):
        pass


class _Result:
    def __init__(self, ok):
        self.success = ok
        self.error = "" if ok else "Generation failed: no accounts available"


cover_server.acquire_server_keys_for_job = lambda job: _Lease()


def fake_process_batch(*args, **kwargs):
    log_fn = kwargs["log_fn"]
    progress_cb = kwargs["progress_cb"]
    log_fn("🌱 Seed-mode: папка='W7-01-A', языков=2, вариантов=1")
    log_fn("[parallel] 8 workers for 2 tasks (max_threads=8)")
    log_fn("🎨 Style-transfer (img2img): обложка + референс → flow (Nano Banana), перевод на «Русский»")
    log_fn("📝 Промпт: A dramatic WW2 scene, cinematic lighting")
    log_fn("[Русский] 💾 Сохранено: /root/cover_tool/outputs/uid/W7/ru_cover.jpg")
    progress_cb(1, 2)
    log_fn("   ⚠ Попытка 2/3: Fast Gen не отдал картинку — HTTP 500")
    log_fn("❌ [Польский]: Generation failed: no accounts available")
    return [_Result(True), _Result(False)]


def failing_process_batch(*args, **kwargs):
    raise RuntimeError("Серверный ключ не настроен: COVER_FAST_GEN_KEY (или COVER_FAST_GEN_KEYS)")


def make_job(job_id):
    return Job(
        id=job_id,
        status=JobStatus.RUNNING.value,
        mode="seed",
        urls="[]",
        languages=json.dumps(["Русский", "Польский"]),
        seeds_per_lang=json.dumps({"Русский": "seed a", "Польский": "seed b"}),
        n_variants=1,
        user_id="uid-log-test",
    )


cover_server.process_batch = fake_process_batch
ok_job = make_job("log-ok")
cover_server._execute_cover_job(ok_job)

cover_server.process_batch = failing_process_batch
bad_job = make_job("log-fail")
cover_server._execute_cover_job(bad_job)

print(json.dumps({
    "ok_log": ok_job.log,
    "ok_status": ok_job.status,
    "fail_log": bad_job.log,
    "fail_error": bad_job.error,
    "fail_status": bad_job.status,
}, ensure_ascii=False))
'''


def _run_worker(tmp_path, extra_env=None):
    project = Path(__file__).parents[1]
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(project),
        "COVER_SERVER_DIR": str(tmp_path),
        "COVER_DB_PATH": str(tmp_path / "jobs.db"),
        "COVER_SESSION_DB": str(tmp_path / "sessions.db"),
        "COVER_STATIC_DIR": str(project / "static"),
        "COVER_OPENAI_KEY": "test-openai",
        "COVER_FAST_GEN_KEY": "test-fastgen",
        "COVER_VEO_KEY": "test-veo",
        "COVER_LOG_JOURNAL": "0",
    })
    env.update(extra_env or {})
    result = subprocess.run(
        [sys.executable, "-c", _WORKER_SCRIPT],
        cwd=project, env=env, text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    import json as _json
    return _json.loads(result.stdout.strip().splitlines()[-1])


def test_worker_log_shows_progress_only(tmp_path):
    data = _run_worker(tmp_path)
    log = data["ok_log"]
    assert log.splitlines() == [
        "▶ Старт: 2 обложки",
        "✓ 1/2 · ru_cover.jpg",
        "⚠ повтор попытки 2/3",
        "✗ Сервис перегружен или лимит исчерпан — повторите позже",
        "■ Завершено: 1 из 2",
        "✗ Не удалось: 1 — Сервис перегружен или лимит исчерпан — повторите позже",
    ]
    for bad in FORBIDDEN:
        assert bad not in log, bad


def test_worker_failure_reports_reason_without_traceback(tmp_path):
    data = _run_worker(tmp_path)
    assert data["fail_status"] == "failed"
    assert data["fail_error"] == "Нет доступа к сервису генерации — сообщите администратору"
    assert "Traceback" not in data["fail_log"]
    assert "COVER_FAST_GEN_KEY" not in data["fail_log"]
    assert data["fail_log"].strip().endswith(
        "✗ Нет доступа к сервису генерации — сообщите администратору"
    )


def test_verbose_env_restores_detailed_log(tmp_path):
    data = _run_worker(tmp_path, {"COVER_LOG_VERBOSE": "1"})
    assert "📝 Промпт: A dramatic WW2 scene" in data["ok_log"]
    assert "Traceback" in data["fail_log"]
    assert "COVER_FAST_GEN_KEY" in data["fail_error"]
