# -*- coding: utf-8 -*-
"""Публичный лог задачи не должен палить внутреннюю логику пайплайна.

Главный тест — `test_no_template_leaks_internals`: он сам вытаскивает из
server.py и rewrite_core.py **все** шаблоны, которые уходят в log_queue, и
проверяет, что после фильтра наружу не выходит ничего, кроме заранее
разрешённых формулировок прогресса. Новый лог, добавленный в пайплайн завтра,
попадёт в этот тест автоматически.
"""

import ast
import json
import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from log_public import PublicLogFilter, public_log_line  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = ("server.py", "rewrite_core.py")

# Ровно то, что клиенту видеть можно. Всё остальное — утечка.
ALLOWED = re.compile(
    r"^\[\d{2}:\d{2}:\d{2}\] (?:"
    r"Задача принята"
    r"|Подготовка(?: — \d+ частей)?"
    r"|Проверка качества"
    r"|Обработка…"
    r"|Повторная попытка"
    r"|Ожидание — (?:лимит провайдера \(E-LIMIT\)|повторная попытка)"
    r"|Остановлено"
    r"|Готово(?: — файл сохранён| частично)?"
    r"|Часть работы готова"
    r"|Часть \d+/\d+"
    r"|Продолжение — \d+/\d+"
    r"|Ошибка: [^(]+\(E-[A-Z]+\)"
    r"|.{1,40}: (?:\d+/\d+|старт — \d+ частей|продолжение — \d+/\d+"
    r"|готово|готово частично|не удалось завершить \(E-PART\))"
    r")$"
)

# Признаки того, что наружу утекла механика пайплайна.
FORBIDDEN = (
    "pass a", "pass b", "pass c", "pass d", "hash=", "prompt_len", "tokens",
    "token", "cache", "traceback", "/opt/", "/users/", "claude-", "gpt-",
    "opus", "sonnet", "codex", "oauth", "stderr", "sdk", "cli", "json",
    "temperature", "chunk_size", "system prompt", "postprocess", "judge",
    "editor", "cleaner", "воронк", "retry", "recovery", "dumped", "preview=",
    "title=", "model=", "provider=", "usage", "flex", "backoff", "split",
    "narrative", "нарратив", "брендинг", "абзац", "симв", "chars", "words",
)


def _render(node):
    """AST-узел строки/f-строки → конкретная строка. Подстановки → '7'."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                out.append(str(value.value))
            else:
                out.append("7")
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.IfExp):
        return _render(node.body)
    return None


def _is_log_call(node):
    """(имя_функции, узел_сообщения) для log_msg / log_queue.put / debug_log."""
    func = node.func
    if isinstance(func, ast.Name) and func.id in ("log_msg", "debug_log"):
        return func.id, node.args[1] if len(node.args) > 1 else None
    if isinstance(func, ast.Attribute) and func.attr == "put":
        base = func.value
        name = getattr(base, "id", "") or getattr(base, "attr", "")
        if "log" in str(name).lower() or "queue" in str(name).lower():
            return "put", node.args[0] if node.args else None
    return None, None


def collect_templates():
    """Все строковые шаблоны логов из боевых модулей."""
    templates = []
    for filename in SOURCES:
        path = os.path.join(BASE, filename)
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kind, message = _is_log_call(node)
            if message is None:
                continue
            rendered = _render(message)
            if not rendered:
                continue
            if kind == "debug_log":
                rendered = f"[DEBUG] {rendered}"
            templates.append((filename, node.lineno, rendered))
    return templates


TEMPLATES = collect_templates()


def test_templates_actually_collected():
    """Страховка: если сбор сломается, остальные тесты станут пустыми."""
    assert len(TEMPLATES) >= 150, f"собрано всего {len(TEMPLATES)} шаблонов"


def test_no_template_leaks_internals():
    """Ни один реальный шаблон лога не выносит наружу внутреннюю кухню."""
    leaks = []
    for filename, lineno, raw in TEMPLATES:
        for line in PublicLogFilter().feed([raw]):
            low = line.lower()
            bad = [word for word in FORBIDDEN if word in low]
            if bad or not ALLOWED.match(line):
                leaks.append(f"{filename}:{lineno}\n  raw={raw!r}\n  out={line!r}\n  hit={bad}")
    assert not leaks, "лог палит внутренности:\n" + "\n".join(leaks)


def test_filter_is_not_trivially_silent():
    """Фильтр не должен «защищать» простым молчанием на всё подряд."""
    passed = [raw for _f, _l, raw in TEMPLATES if PublicLogFilter().feed([raw])]
    assert len(passed) >= 15, f"наружу проходит всего {len(passed)} шаблонов"


# ============== прогресс доходит и читается ==============

def _feed(lines):
    flt = PublicLogFilter()
    out = []
    for line in lines:
        out.extend(flt.feed([line]))
    return [re.sub(r"^\[\d{2}:\d{2}:\d{2}\] ", "", x) for x in out]


def test_rewrite_progress_flow():
    assert _feed([
        "[14:02:15] 🌍 Английский: начинаю (12 частей)",
        "[14:02:16] 🔧 Английский: model='claude-opus-4-6', provider='claude', ",
        "[14:03:40]    ✅ Английский: часть 1/12",
        "[14:05:02]    ✅ Английский: часть 2/12",
        "[14:05:03]   🔑 System prompt: hash=ab12cd, длина=8123 символов",
        "[14:19:55] ✅ Английский: готово → rewrite_en.txt",
    ]) == [
        "Английский: старт — 12 частей",
        "Английский: 1/12",
        "Английский: 2/12",
        "Английский: готово",
    ]


def test_generation_progress_flow():
    assert _feed([
        "📐 Птицы-ген: 8 chunk(s) × ~900 words, ",
        "📐 Проектирую воронку (превью → название → хук → план-бюджет)…",
        "💬 Conversation mode (claude): 8 turns в ОДНОМ разговоре ",
        "✅ Conv turn 1/8: 912 слов (5821 симв), ",
        "✅ Conv turn 2/8: 880 слов (5610 симв), ",
        "🧹 cleaner: убрано 3 меты",
        "🎬 Done: preview='...' title='...' script=7300 слов",
    ]) == [
        "Подготовка — 8 частей",
        "Подготовка",
        "Часть 1/8",
        "Часть 2/8",
        "Проверка качества",
        "Готово — файл сохранён",
    ]


def test_resume_and_partial():
    assert _feed([
        "🌍 Немецкий: начинаю (5 частей)",
        "🔄 Немецкий: продолжаю с части 3/5",
        "⚠️ Немецкий: готово с 2 ошибками",
    ]) == [
        "Немецкий: старт — 5 частей",
        "Немецкий: продолжение — 3/5",
        "Немецкий: готово частично",
    ]


# ============== ошибки: код вместо подробностей ==============

@pytest.mark.parametrize("raw,expected", [
    ("    ⏳ Rate limit [rewrite-en], жду 30с...", "Ожидание — лимит провайдера (E-LIMIT)"),
    ("  🔄 Серверный Claude slot 2/3 на лимите — следующий", "Ожидание — лимит провайдера (E-LIMIT)"),
    ("  ⏱️  Subscription timeout: opus stalled 900s", "Ожидание — повторная попытка"),
    ("    ⏳ Timeout, повтор 2/3 через 20с...", "Ожидание — повторная попытка"),
    ("  ❌ Conversation connect failed: connection refused", "Ошибка: сеть недоступна (E-NET)"),
    ("  ❌ Не отвечает: request timeout after 900s", "Ошибка: истекло время ожидания (E-TIMEOUT)"),
    ("❌ Не сохранён финальный скрипт: empty — статус failed", "Ошибка: результат не сохранён (E-SAVE)"),
    ("  ❌ Subscription error: TaskGroup exit-1 /opt/rewrite/x.py", "Ошибка: сбой обработки (E-INTERNAL)"),
    ("❌ Ошибка: KeyError('script')", "Ошибка: сбой обработки (E-INTERNAL)"),
    ("⛔ Остановлено пользователем mid-chunk", "Остановлено"),
])
def test_error_codes_hide_details(raw, expected):
    assert public_log_line(raw) == expected


def test_failed_language_reports_part_error():
    out = _feed([
        "🌍 Испанский: начинаю (9 частей)",
        "⚠️ Испанский: 3 чанков провалились — файл НЕ сохранён (не отдаём брак).",
    ])
    assert out[-1] == "Испанский: не удалось завершить (E-PART)"


# ============== внутренняя кухня молчит ==============

@pytest.mark.parametrize("raw", [
    "  🔑 System prompt: hash=9f2a, длина=8123 символов (~2030 токенов)",
    "  💾 CACHE CREATED: 4102 tokens",
    "  ⚡ CACHE HIT: 4102 tokens (saved $0.0123)",
    "  🔬 chunk 2/8 prompt: total=9123 sys=8123 ",
    "   ⚠️ Pass A: не удалось распарсить JSON",
    "   Pass B: 12000 → 11400 chars (95%)",
    "  📊 Input: 8123 tokens",
    "  🔑 Использую Pro подписку (модель: claude-opus-4-6)",
    "  🔬 Hang forensics saved: /opt/rewrite/forensics (pids: 12,13)",
    "📝 raw dumped: /opt/rewrite/outputs/raw_3.json (5821 chars)",
    "[DEBUG] system prompt: you are a rewriter...",
    "   ⚠️ Chunk 3: paragraphs mismatch (7/9), retry 1/3",
    "  ⚠️ Модель claude-opus-4-6 недоступен, fallback на claude-sonnet-4-6",
    "⚠️ v7 упал (TimeoutError: stalled) — обычный путь",
    "  ⚠️ disconnect: peer closed connection",
    "   ⚠️ Length retry дал 7/9 абзацев — отклоняю, оставляю прежний",
])
def test_internal_lines_are_dropped(raw):
    assert public_log_line(raw) is None


def test_unknown_language_position_is_not_echoed():
    """Если формат лога поедет, в позицию языка не должна попасть техстрока."""
    assert public_log_line("✅ Chunk 3: часть 4/12") == "Часть 4/12"
    assert public_log_line("✅ /opt/rewrite/x.txt: готово") == "Часть работы готова"


# ============== поведение фильтра ==============

def test_duplicates_collapse_but_heartbeat_repeats():
    flt = PublicLogFilter(heartbeat_sec=120)
    t0 = datetime(2026, 8, 1, 14, 0, 0)
    assert flt.feed(["🧹 cleaner: ok"], now=t0)
    assert flt.feed(["✂️ editor: ok"], now=t0) == []          # тот же «Проверка качества»
    assert flt.feed([], now=t0 + timedelta(seconds=60)) == []  # рано
    first = flt.feed([], now=t0 + timedelta(seconds=121))
    second = flt.feed([], now=t0 + timedelta(seconds=242))
    assert first and second, "heartbeat должен повторяться, а не глохнуть после первого"
    assert first[0].endswith("Обработка…")


def test_close_states():
    assert PublicLogFilter().close("completed")[0].endswith("Готово — файл сохранён")
    assert PublicLogFilter().close("cancelled")[0].endswith("Остановлено")
    assert PublicLogFilter().close("failed")[0].endswith("(E-FAIL)")
    assert PublicLogFilter().close("running") == []


def test_start_line():
    assert PublicLogFilter().start()[0].endswith("Задача принята")


# ============== сквозная проверка врезки в server.py ==============

# Отдельный процесс: server.py читает пути из env на импорте, а suite уже
# импортирует его с боевыми путями (tests/test_byok_*.py).
_E2E = textwrap.dedent(
    """
    import json, sys
    import server
    from job_queue import Job, JobType, JobStatus

    server._validate_model_admission = lambda model, uid: (True, False, ["tok"])
    server.license_mgr.is_user_active = lambda uid: True

    RAW = [
        "[14:02:15] 🌍 Английский: начинаю (3 частей)",
        "[14:02:16]   🔑 System prompt: hash=9f2a1c, длина=8123 символов",
        "[14:02:17]   🔑 Использую Pro подписку (модель: claude-opus-4-6)",
        "[14:03:40]    ✅ Английский: часть 1/3",
        "[14:03:41]   💾 CACHE HIT: 4102 tokens (saved $0.0123)",
        "[14:04:02]    ⚠️ Chunk 2: paragraphs mismatch (7/9), retry 1/3",
        "[14:04:40]    ✅ Английский: часть 2/3",
        "[14:06:10]    ✅ Английский: часть 3/3",
        "[14:06:20] 🔎 Английский: постпроцесс (Pass A+B → merge → Pass C)...",
        "[14:06:50] ✅ Английский: готово → rewrite_en.txt",
    ]

    job_id = server.queue.add_job(Job(
        job_type=JobType.REWRITE.value, user_id="u1", provider="claude",
        model="claude-sonnet-4-6", rewrite_files=json.dumps(["a.txt"]),
    ))
    job = server.queue.claim_next("claude")

    def job_fn(job, log_queue, stop_event):
        for line in RAW:
            log_queue.put(line)
        job.status = JobStatus.COMPLETED.value

    server._execute_job_common(job, job_fn)

    stored = server.queue.get_job(job_id).log
    raw_path = server._raw_log_path(job)
    print(json.dumps({
        "stored": stored,
        "raw": raw_path.read_text(encoding="utf-8") if raw_path.exists() else "",
    }))
    """
)


def _run_e2e(tmp_path, public_enabled: str):
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": BASE,
        "HOME": str(tmp_path),
        "REWRITE_SERVER_DIR": str(tmp_path),
        "REWRITE_STATIC_DIR": os.path.join(BASE, "static"),
        "REWRITE_DB_PATH": str(tmp_path / "jobs.db"),
        "SESSION_DB": str(tmp_path / "sessions.db"),
        "REWRITE_JOB_LOG_DIR": str(tmp_path / "job_logs"),
        "REWRITE_ALLOWED_CLAUDE_MODELS": "claude-sonnet-4-6",
        "REWRITE_SHARED_KEYS_ENABLED": "0",
        "REWRITE_LOG_PUBLIC": public_enabled,
    })
    done = subprocess.run(
        [sys.executable, "-c", _E2E], cwd=BASE, env=env,
        text=True, capture_output=True, timeout=120,
    )
    assert done.returncode == 0, f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_job_log_split_end_to_end(tmp_path):
    """В БД — прогресс, на диске — вся техника. Полный путь через server.py."""
    result = _run_e2e(tmp_path, "1")
    stored, raw = result["stored"], result["raw"]

    assert "Задача принята" in stored
    assert "Английский: 1/3" in stored and "Английский: 3/3" in stored
    assert "Английский: готово" in stored
    for secret in ("hash=", "CACHE HIT", "Pass ", "opus", "mismatch", "retry"):
        assert secret.lower() not in stored.lower(), f"утечка в БД: {secret}"

    # то же самое целиком лежит на сервере
    assert "hash=9f2a1c" in raw and "CACHE HIT" in raw and "Pass A+B" in raw
    assert len(raw.splitlines()) == 10


def test_kill_switch_restores_full_log(tmp_path):
    """REWRITE_LOG_PUBLIC=0 — мгновенный откат к прежнему поведению."""
    result = _run_e2e(tmp_path, "0")
    assert "hash=9f2a1c" in result["stored"], "тумблер не вернул полный лог"
    assert result["raw"] == "", "при выключенном фильтре файл не пишем"
