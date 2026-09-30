# -*- coding: utf-8 -*-
"""Вызов языковых моделей по подписке: Claude CLI и Codex CLI.

Сознательно НЕ импортируем rewrite_core (9749 строк): оба CLI вызываются
напрямую и дают тот же доступ по подписке, без сцепки с чужим кодом.
Если чужой сервис поменяется — здесь ничего не сломается.

Работает только на VPS: там лежат токен подписки Claude и дом Codex.

Альт-ключ (MANGA_ALT_API_KEY) — сторонний прокси, совместимый с Anthropic и OpenAI,
тот же, что у рерайта. Задан — claude CLI идёт на MANGA_ALT_ANTHROPIC_BASE_URL с
ANTHROPIC_API_KEY (OAuth-токен из окружения ребёнка убран, пул подписок не нужен),
codex CLI — на MANGA_ALT_OPENAI_BASE_URL флагами -c без CODEX_HOME. Одновременных
вызовов через альт в процессе — не больше MANGA_ALT_CONCURRENCY. Не задан — всё как было.

Лёгкий режим (MANGA_CLAUDE_LEAN=on): claude CLI без служебного промпта, настроек,
MCP и сохранения сессии — вход вызова без ~23 тыс. токенов служебной нагрузки.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Optional

import claude_pool as CP
import load_gate as LG
import codex_pool as CXP

CODEX_HOME = os.environ.get("CODEX_HOME") or (
    "/root/.codex-server" if os.path.isdir("/root/.codex-server") else os.path.expanduser("~/.codex"))
DEFAULT_TIMEOUT = 600
# Product jobs must not inherit the desktop agent's personal reasoning setting.
TEXT_EFFORT = os.environ.get("MANGA_TEXT_REASONING_EFFORT", "medium")
if TEXT_EFFORT not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
    raise ValueError("Неверный MANGA_TEXT_REASONING_EFFORT")


def _setting(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _pairs(raw: str) -> dict:
    out = {}
    for item in raw.split(","):
        k, _, v = item.partition("=")
        if k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def alt_key() -> str:
    """Альт-ключ из окружения службы. Читается на каждом вызове; наружу — только через env ребёнка."""
    return _setting("MANGA_ALT_API_KEY")


def lean_on() -> bool:
    return _setting("MANGA_CLAUDE_LEAN").lower() in {"1", "on", "true", "yes", "да"}


try:
    ALT_CONCURRENCY = max(1, min(16, int(_setting("MANGA_ALT_CONCURRENCY", "5"))))
except ValueError:
    raise ValueError("Неверный MANGA_ALT_CONCURRENCY: нужно целое число 1–16") from None
_ALT_SLOTS = threading.BoundedSemaphore(ALT_CONCURRENCY)
# Модели, которых нет у прокси, → разрешённые (claude-sonnet-5, claude-opus-5; gpt-5.6-*).
ALT_MODEL_MAP = _pairs(_setting("MANGA_ALT_MODEL_MAP",
                                "sonnet=claude-sonnet-5,opus=claude-opus-5,haiku=claude-sonnet-5,"
                                "claude-sonnet-4-6=claude-sonnet-5,claude-haiku-4-5=claude-sonnet-5"))
# = default_reasoning_level боевого Codex CLI для этих моделей (как у рерайта, 17.09.2026)
ALT_CODEX_EFFORT = {"gpt-5.6-sol": "low", "gpt-5.6-terra": "medium", "gpt-5.6-luna": "medium"}
LEAN_SYSTEM_PROMPT = ("You are a careful writing assistant. Follow the user's instructions exactly "
                      "and reply with the requested output only, without preamble or commentary.")
_alt_said = False


def _say_alt_once():
    global _alt_said
    if not _alt_said:
        _alt_said = True
        CP.log.warning("Claude/Codex: текст пишет альт-ключ (прокси %s), пул подписок и проба лимитов "
                       "не используются; одновременных вызовов до %d", _alt_base_anthropic(), ALT_CONCURRENCY)


def _alt_base_anthropic() -> str:
    return _setting("MANGA_ALT_ANTHROPIC_BASE_URL", "https://triklz27.ru")


ALT_WAIT_LOG_EVERY = 60  # с: как часто писать в журнал, что вызов стоит в очереди к альту


@contextmanager
def _alt_slot():
    """Общий потолок одновременных вызовов через альт-ключ (claude + codex). Ждём без
    таймаута, но задача, которую забрали у исполнителя, ждать перестаёт."""
    scope = CP.current_scope()
    waited, said = time.time(), time.time()
    while not _ALT_SLOTS.acquire(timeout=0.25):
        if scope is not None and scope.check is not None:
            scope.check()
        if time.time() - said >= ALT_WAIT_LOG_EVERY:
            said = time.time()
            CP.log.warning("ждёт места у альт-ключа %d с (заняты все %d мест MANGA_ALT_CONCURRENCY)",
                           int(said - waited), ALT_CONCURRENCY)
    try:
        yield
    finally:
        _ALT_SLOTS.release()


def _alt_rejected(model: str, code) -> "TokenError":
    return TokenError(f"{model}: альт-ключ не принят прокси (HTTP {code}). Проверьте MANGA_ALT_API_KEY "
                      "в web.env или уберите его, чтобы текст снова шёл через подписку")


class LLMError(RuntimeError):
    pass


class AltBusyError(LLMError):
    """Прокси альт-ключа ответил 429 или 5xx — временно. call() повторяет его с паузой
    сам (до ALT_BUSY_RETRIES раз), даже когда вызван с retries=0 (так зовёт call_json)."""


# Сколько раз студия сама переспрашивает занятый прокси и с какой паузой: MANGA_ALT_BUSY_RETRIES.
# 19.09 ночью при 6 одновременных вызовах прокси отдавал 429 сотнями, двух повторов по 3–6 с
# не хватало, и кусок текста терялся — ролик падал целиком.
try:
    ALT_BUSY_RETRIES = max(0, min(20, int(_setting("MANGA_ALT_BUSY_RETRIES", "2"))))
except ValueError:
    raise ValueError("Неверный MANGA_ALT_BUSY_RETRIES: нужно целое число 0–20") from None
try:
    ALT_BUSY_PAUSE = max(1, min(120, int(_setting("MANGA_ALT_BUSY_PAUSE", "3"))))
except ValueError:
    raise ValueError("Неверный MANGA_ALT_BUSY_PAUSE: нужно целое число 1–120 (секунд на первый повтор)") from None
# Сколько раз claude CLI сам переспрашивает прокси. По умолчанию у CLI 10 с растущей паузой:
# на отвергнутом ключе это 177 с до ответа (замер 18.09, CLI 2.1.40), с 2 — 2 с.
ALT_CLI_RETRIES = "2"
_API_ERR = re.compile(r"API Error:\s*(\d{3})\b")
_AUTH_WORDS = re.compile(r"authentication_error|invalid_api_key|invalid x-api-key|Failed to authenticate", re.I)


def _alt_code(res: dict, text: str):
    """HTTP-код ответа прокси. CLI 2.1.267 кладёт его в api_error_status, CLI 2.1.40 —
    только в текст: 'Failed to authenticate. API Error: 401 {...invalid_api_key...}'."""
    code = res.get("api_error_status")
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    m = _API_ERR.search(text or "")
    if m:
        return int(m.group(1))
    return 401 if _AUTH_WORDS.search(text or "") else None


class LimitError(LLMError):
    """Подписка Claude упёрлась в лимит. Повтор через секунды не поможет — call() его не повторяет."""


class TokenError(LLMError):
    """Ни один токен пула не принят API (401/403). call() его не повторяет."""


class CutError(LLMError):
    """Ответ упёрся в предел вывода CLI (размышления + текст; у claude-sonnet-5 в CLI 2.1.40 —
    32000 токенов, CLAUDE_CODE_MAX_OUTPUT_TOKENS выше не поднимает). CLI тогда сам просит модель
    «продолжить», а в поле result кладёт только последнее сообщение — хвост ответа. Живой случай
    28.09: из сцены с хуком пропали фразы 1–30, остаток прошёл допуск по объёму. Такой ответ не
    принимаем: call() повторяет вызов, call_json() — тоже, тем же промптом."""


def _cuts(stdout: str) -> int:
    """Сколько раз ответ упёрся в предел вывода. stream-json CLI помечает такое сообщение
    полем error=max_output_tokens (замер CLI 2.1.40 и 2.1.251, 28.09)."""
    n = 0
    for line in (stdout or "").splitlines():
        if "max_output_tokens" not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") == "assistant" and d.get("error") == "max_output_tokens":
            n += 1
    return n


@dataclass
class Answer:
    text: str
    seconds: float
    model: str
    attempts: int = 1
    usage: Optional[dict] = None


def _run(cmd, stdin_text, timeout, env=None, cwd=None):
    return subprocess.run(
        cmd, input=stdin_text, capture_output=True, text=True,
        timeout=timeout, env=env or {**os.environ}, cwd=cwd,
    )


def call_claude(prompt: str, model: str = "sonnet",
                timeout: int = DEFAULT_TIMEOUT, tools=None, cwd=None) -> Answer:
    """Ответ модели — текст (поле result итоговой строки stream-json CLI); ответ, упёршийся в предел
    вывода CLI, не принимается (CutError). Расход пишется в журнал
    проекта (claude_pool.record). С пулом подписок (MANGA_CLAUDE_TOKENS) токен
    выбирается по запасу окон; «лимит» от CLI — берётся другая подписка или шаг
    ждёт сброса окна. tools/cwd — для шага «Обложка» (ветка scale50/cover-step).
    MANGA_ALT_FOR_CLAUDE=off — claude идёт по подписке, даже когда альт-ключ задан (альт — только codex)."""
    key = alt_key() if _setting("MANGA_ALT_FOR_CLAUDE", "on").lower() not in {"off", "0", "no", "false"} else ""
    lean = lean_on()
    if key:
        _say_alt_once()
        model = ALT_MODEL_MAP.get(model, model)
    route = "alt" if key else "subscription"
    # stream-json, а не json: в json-выводе нет следа обрыва по пределу вывода (см. CutError).
    cmd = ["claude", "-p", "--model", model, "--output-format", "stream-json", "--verbose"]
    if lean:
        # Промпт идёт через stdin, позиционного аргумента нет — переменные флаги ничего не съедят.
        cmd += ["--system-prompt", LEAN_SYSTEM_PROMPT, "--setting-sources", "",
                "--strict-mcp-config", "--no-session-persistence",
                # --tools принимает только имена инструментов («Read»); правило с путём
                # («Read(//папка/**)») остаётся в --allowedTools ниже. С путём в --tools CLI
                # не давал обложке открыть картинки в лёгком режиме (разбор 21.09).
                "--tools", ",".join(sorted({str(t).split("(", 1)[0] for t in tools})) if tools else ""]
    if tools:
        cmd += ["--allowedTools", *[str(t) for t in tools]]

    def row(res, seconds, sub_name="", ok=True):
        r = CP.usage_row(res, model, seconds, sub_name, ok=ok)
        r.update(route=route, lean=lean)
        return r

    pool = None if key else CP.POOL
    tries = 1 + (2 * len(pool.subs) if pool else 0)
    scope = CP.current_scope()
    for attempt in range(1, tries + 1):
        if scope is not None and scope.check is not None:
            scope.check()  # задачу забрали — следующий вызов уже не наш, не покупаем его
        try:
            sub = pool.acquire() if pool else None
        except CP.TokensRejected as e:
            raise TokenError(CP.scrub(f"claude/{model}: {e}")) from None
        if key:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "MANGA_CLAUDE_TOKENS",
                                "MANGA_ALT_API_KEY")}
            env.update(ANTHROPIC_BASE_URL=_alt_base_anthropic(), ANTHROPIC_API_KEY=key,
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
            env.setdefault("CLAUDE_CODE_MAX_RETRIES", ALT_CLI_RETRIES)
        else:
            # Альт-ключ ребёнку по подписке не нужен (MANGA_ALT_FOR_CLAUDE=off) — не передаём.
            env = None
            if sub or "MANGA_ALT_API_KEY" in os.environ:
                env = {k: v for k, v in os.environ.items() if k != "MANGA_ALT_API_KEY"}
            if sub:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = sub.token
        t0 = time.time()
        limited, rejected = False, 0
        try:
            try:
                if key:
                    with _alt_slot():
                        t0 = time.time()
                        p = _run(cmd, prompt, timeout, env, cwd=cwd)
                else:
                    p = _run(cmd, prompt, timeout, env, cwd=cwd)
            except subprocess.TimeoutExpired:
                CP.record(row(None, time.time() - t0, sub.name if sub else "", ok=False))
                raise
            seconds = time.time() - t0
            res = CP.parse_result(p.stdout)
            if res is None:
                out = (p.stdout or "").strip()
                if not out:
                    CP.record(row(None, seconds, sub.name if sub else "", ok=False))
                    raise LLMError(CP.scrub(f"claude/{model}: пустой ответ. stderr: {(p.stderr or '')[:300]}"))
                if out.startswith('{"type":'):
                    # stream-json без итоговой строки: CLI оборвался посреди ответа — это не ответ.
                    CP.record(row(None, seconds, sub.name if sub else "", ok=False))
                    raise LLMError(CP.scrub(f"claude/{model}: CLI оборвался без итоговой строки. "
                                            f"stderr: {(p.stderr or '')[:300]}"))
                # Старый CLI без JSON: ответ берём, расход честно помечаем неизвестным.
                limited = bool(pool) and CP.looks_limited(out) and len(out) < 400
                CP.record(row(None, seconds, sub.name if sub else "", ok=not limited))
                if not limited:
                    CP.log.warning("claude/%s: CLI ответил не JSON — расход этого вызова неизвестен", model)
                    return Answer(out, seconds, f"claude:{model}")
            else:
                text = str(res.get("result") or "").strip()
                failed = bool(res.get("is_error")) or res.get("subtype", "success") != "success"
                r = row(res, seconds, sub.name if sub else "", ok=not failed and bool(text))
                stop = res.get("stop_reason")
                if stop == "max_tokens":
                    r["stop"] = stop  # ответ оборван по длине — видно в журнале
                    CP.log.warning("claude/%s: ответ оборван по длине (stop_reason=max_tokens, выход %s ток.)",
                                   model, r.get("output"))
                # Обрыв виден двумя способами: сообщение error=max_output_tokens (CLI просил «продолжить»)
                # или итог stop_reason=max_tokens без такого сообщения (живой прогон 28.09: 3596 знаков
                # раскадровки, выход 0 ток.; в журналах 20.09 — 29 раз). Оба — не ответ.
                cuts = _cuts(p.stdout) + (stop == "max_tokens")
                if cuts and not (failed and CP.cli_failure(res, text)):
                    # Лимит или отказ токена после обрыва идут прежним путём (другая подписка / ожидание).
                    r.update(ok=False, cut=cuts)
                    CP.record(r)
                    CP.log.warning("claude/%s: ответ %d раз упёрся в предел вывода CLI — в result только хвост, "
                                   "не принимаю (выход %s ток.)", model, cuts, r.get("output"))
                    raise CutError(CP.scrub(f"claude/{model}: ответ упёрся в предел вывода CLI ({cuts} раз), "
                                            "CLI отдал только хвост — ответ не принят"))
                CP.record(r)
                if not failed and text:
                    return Answer(text, seconds, f"claude:{model}", usage=row(res, seconds))
                why_failed = CP.cli_failure(res, text) if failed else ""
                if key and failed:
                    code = _alt_code(res, text)
                    if why_failed == "rejected" or code in (401, 403):
                        raise _alt_rejected(f"claude/{model}", code)
                    detail = CP.scrub(f"{(text or (p.stderr or ''))[:300]}").replace(key, "***")
                    if code == 429 or (isinstance(code, int) and code >= 500):
                        # 429 и 5xx у прокси — временно: call() повторит с паузой.
                        raise AltBusyError(f"claude/{model}: прокси альт-ключа ответил HTTP {code}: {detail}")
                    CP.log.warning("claude/%s: обрыв или ошибка ответа через альт-ключ — повтор", model)
                    raise LLMError(f"claude/{model}: обрыв или ошибка ответа через альт-ключ: {detail}")
                limited = why_failed == "limit"
                if why_failed == "rejected" and pool:
                    # Токен отозвали между проверками: не долбить его — взять другую подписку.
                    rejected = res["api_error_status"]
                elif not limited:
                    why = text[:300] or f"пустой ответ. stderr: {(p.stderr or '')[:300]}"
                    raise LLMError(CP.scrub(f"claude/{model}: {why}"))
        finally:
            if sub is not None:
                pool.release(sub, limited=limited, rejected=rejected)
        if rejected:
            CP.log.warning("claude/%s: подписка «%s» не принята (HTTP %s), беру другую (попытка %d)",
                           model, sub.name, rejected, attempt)
            continue
        if not pool:
            raise LimitError(f"claude/{model}: лимит подписки Claude исчерпан. Чтобы шаг ждал сброса окна, "
                             "а не падал, задайте MANGA_CLAUDE_TOKENS (можно тем же одним токеном)")
        CP.log.warning("claude/%s: подписка «%s» упёрлась в лимит, беру другую (попытка %d)",
                       model, sub.name, attempt)
    raise LimitError(f"claude/{model}: подписки Claude упираются в лимит или не приняты после {tries} попыток")


# codex exec печатает в stderr эхо промпта (блок «user\n<промпт>»), а ошибки API — строками
# «ERROR: unexpected status 401 Unauthorized: Invalid API key., url: …» (замер codex 0.154,
# 18.09). Код ищем только в строках ERROR после вырезанного эха: «Кадр 403» или «Forbidden
# Technique» в тексте манхвы не должны становиться «ключ не принят».
_CODEX_AUTH = re.compile(r"status:?\s*(401|403)\b|invalid[ _]api[ _]key|incorrect api key", re.I)
_CODEX_BUSY = re.compile(r"status:?\s*(429|5\d\d)\b|too many requests|rate limit|service unavailable|"
                         r"bad gateway|overloaded", re.I)


def _codex_error_lines(stderr: str, prompt: str) -> str:
    text = stderr
    body = (prompt or "").strip()
    if body:
        text = text.replace(body, "")
    return "\n".join(line for line in text.splitlines()
                     if re.match(r"\s*(ERROR|error)\b\s*:", line))


CODEX_LOCK_SAY_AFTER = 5.0      # секунд ожидания замка входа до строки на шаге


@contextmanager
def _codex_auth_lock(home_path: str, name: str = ""):
    """Замок обновления входа Codex (тот же файл, что у рерайта). Берётся на весь вызов
    только когда CLI может обновить auth.json (CXP.refresh_possible), иначе вызовы идут
    параллельно. Получив замок, проверяем ещё раз: если вход уже обновил предыдущий вызов
    (last_refresh свежий), замок сразу отпускаем — остальные вызовы идут параллельно.
    Ждёт без таймаута, но задача, которую забрали, ждать перестаёт; ожидание дольше
    нескольких секунд видно строкой шага."""
    if not CXP.refresh_possible(home_path):
        yield
        return
    import fcntl
    fd = os.open(os.path.join(home_path, CXP.LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    held = False
    try:
        scope = CP.current_scope()
        started, said = time.monotonic(), False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if scope is not None and scope.check is not None:
                    scope.check()
                if not said and time.monotonic() - started >= CODEX_LOCK_SAY_AFTER:
                    said = True
                    msg = (f"Ждёт замка входа Codex «{name or os.path.basename(home_path)}»: "
                           "другой вызов сейчас может обновлять вход, вызовы этой учётки идут по одному")
                    CP.log.warning(msg)
                    if scope is not None and scope.on_wait is not None:
                        scope.on_wait(scope.step, msg)
                time.sleep(0.25)
        if said and scope is not None and scope.on_wait is not None:
            scope.on_wait(scope.step, f"Замок входа Codex получен, шаг продолжает работу")
        if not CXP.refresh_possible(home_path):
            fcntl.flock(fd, fcntl.LOCK_UN)       # вход уже обновлён — дальше параллельно
            held = False
        yield
    finally:
        if held:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


REMOTE_MARK = "===МАНГА-ОТВЕТ==="


# Общие каналы ssh: у демона на той машине есть предел одновременных рукопожатий
# (MaxStartups, по умолчанию 10) — при 40 вызовах он рвёт соединения, и вызов возвращает
# пустой ответ (20.09: «kex_exchange_identification: Connection closed by remote host»).
# Поэтому все вызовы идут через несколько ПОСТОЯННЫХ каналов: одно рукопожатие на канал,
# внутри — до MaxSessions вызовов. Число каналов — MANGA_SSH_CHANNELS.
try:
    SSH_CHANNELS = max(1, min(32, int(_setting("MANGA_SSH_CHANNELS", "6"))))
except ValueError:
    raise ValueError("Неверный MANGA_SSH_CHANNELS: нужно целое число 1–32") from None
_ssh_turn = itertools.count()


def _ssh_socket(home) -> str:
    """Какой канал взять этому вызову: по кругу, чтобы сессии делились поровну."""
    n = next(_ssh_turn) % SSH_CHANNELS
    return f"/tmp/manga-ssh-{home.name}-{n}"


def _ssh_base(home, socket: str = "") -> list:
    """ssh до машины, где живёт эта учётка Codex."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
           "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
           "-o", "ServerAliveCountMax=3",
           "-o", "ControlMaster=auto", "-o", f"ControlPath={socket or ('/tmp/manga-ssh-' + home.name + '-0')}",
           "-o", "ControlPersist=300"]
    if getattr(home, "port", 0):
        cmd += ["-p", str(home.port)]
    if getattr(home, "key", ""):
        cmd += ["-i", home.key]
    return cmd


def _scp(home, src: str, dst: str):
    cmd = ["scp", "-q", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    if getattr(home, "port", 0):
        cmd += ["-P", str(home.port)]
    if getattr(home, "key", ""):
        cmd += ["-i", home.key]
    cmd += [src, f"{home.ssh}:{dst}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def codex_exec_pooled(prompt: str, model: str, effort: str, images=(), timeout: int = DEFAULT_TIMEOUT,
                      wait_reset: bool = True):
    """Один ответ codex exec через пул учёток MANGA_CODEX_HOMES → (текст, код выхода, ошибка).

    Учётка — с наибольшим запасом; «лимит» — берётся другая (или шаг ждёт сброса), «вход
    слетел» — учётка откладывается, берётся другая; ни у одной нет входа — TokenError с
    командой перелогина. Прочие сбои (модель недоступна, обрыв) — пустой текст и код выхода:
    решает вызывающий (у судьи — запасная модель). Каждый вызов — строка в журнале проекта."""
    pool = CXP.POOL
    images = [str(p) for p in images or ()]
    tries = 1 + 2 * len(pool.homes)
    scope = CP.current_scope()
    for attempt in range(1, tries + 1):
        if scope is not None and scope.check is not None:
            scope.check()
        try:
            home = pool.acquire(wait_reset=wait_reset)
        except CXP.LoginsGone as e:
            raise TokenError(pool.scrub(str(e))) from None
        except CXP.AllBlocked as e:
            # wait_reset=False: вызывающему не ждать сброса окна (до суток) — у него есть запасной путь.
            raise LimitError(pool.scrub(str(e))) from None
        # 27.09.2026, аудит F06: слот учётки (и место на машине) держал только try/finally ниже,
        # а он начинался ПОСЛЕ ожидания процессора, mkstemp и scp картинок. Отмена в ожидании
        # (JobGone) или «картинка не доехала» выходили мимо finally — слот так и оставался занят:
        # на заглушках 5 сбоев подряд → занято 4/4, следующий вызов этой учётки ждал вечно
        # (при сбое scp утекало и место на машине). Теперь всё после захвата — в одном try,
        # gated помнит, взято ли место на машине, out_path — создан ли временный файл.
        limited = rejected = suspect = gone = False
        detail = ""
        gate, gated, out_path = None, False, None
        t0 = time.time()
        try:
            gate = LG.gate_for(home.name, _ssh_base(home) if home.ssh else None, home.cores)
            if gate is not None:
                # Гибкий предел: пускаем вызов, пока машина занята меньше MANGA_LOAD_TARGET.
                waited = gate.acquire(check=scope.check if scope is not None else None,
                                      stop=scope.stop if scope is not None else None,
                                      log=CP.log.warning)
                gated = True     # место на машине взято — вернуть его обязан finally
                if waited > 30:
                    CP.log.warning("вызов ждал места %.0f с: машина была занята", waited)
            fd, out_path = tempfile.mkstemp(suffix=".txt")
            os.close(fd)
            remote_files = []
            if home.ssh:
                # Учётка живёт на другой машине: вызов идёт туда по ssh, ответ возвращается
                # тем же соединением после метки. Картинки (паспорт по листу) едут scp.
                stamp = uuid.uuid4().hex
                rout = f"/tmp/manga_codex_{stamp}.txt"
                rimgs = []
                for n, img in enumerate(images):
                    rp = f"/tmp/manga_img_{stamp}_{n}{os.path.splitext(img)[1] or '.png'}"
                    code, why = _scp(home, img, rp)
                    if code != 0:
                        raise LLMError(f"codex/{model}: картинка не доехала на машину учётки: {why[-200:]}")
                    rimgs.append(rp)
                remote_files = rimgs + [rout]
                inner = [home.bin, "exec"]
                for rp in rimgs:
                    inner += ["-i", rp]
                inner += ["-m", model, "-c", f'model_reasoning_effort="{effort}"', "--json",
                          "--skip-git-repo-check", "--ephemeral", "--color", "never", "-o", rout, "-"]
                extra_env = " ".join(f"{k}={shlex.quote(v)}" for k, v in (getattr(home, "env", {}) or {}).items())
                script = (f"{extra_env} CODEX_HOME={shlex.quote(home.path)} "
                          + " ".join(shlex.quote(x) for x in inner)
                          + f"; rc=$?; echo {REMOTE_MARK}; cat {shlex.quote(rout)} 2>/dev/null; "
                          + "rm -f " + " ".join(shlex.quote(x) for x in remote_files) + "; exit $rc")
                cmd = _ssh_base(home, _ssh_socket(home)) + [home.ssh, script]
                env = {**os.environ}
            else:
                cmd = ["codex", "exec"]
                for p in images:
                    cmd += ["-i", p]
                cmd += ["-m", model, "-c", f'model_reasoning_effort="{effort}"', "--json",
                        "--skip-git-repo-check", "--ephemeral", "--color", "never", "-o", out_path, "-"]
                env = {**os.environ, "CODEX_HOME": home.path}
            try:
                # Замок обновления входа — только для местной учётки: у машины по ssh
                # свой файл пропуска, её CLI обновляет его сам.
                with (nullcontext() if home.ssh else _codex_auth_lock(home.path, home.name)):
                    t0 = time.time()
                    p = _run(cmd, prompt, timeout, env)
            except subprocess.TimeoutExpired:
                CP.record(CXP.usage_row(None, model, time.time() - t0, home.name, False, len(images)))
                raise
            seconds = time.time() - t0
            events_text = p.stdout or ""
            if home.ssh:
                events_text, _, answer = (p.stdout or "").partition(REMOTE_MARK)
                out = answer.strip()
            else:
                try:
                    out = open(out_path, encoding="utf-8").read().strip()
                except OSError:
                    out = ""
            ev = CXP.parse_events(events_text)
            if ev["limits"]:
                pool.note_windows(home, CXP.windows_from_usage(ev["limits"], time.time()))
            CP.record(CXP.usage_row(ev["usage"], model, seconds, home.name, bool(out), len(images)))
            if out:
                return out, 0, ""
            texts = ev["errors"] + [_codex_error_lines(p.stderr or "", prompt)]
            why = CXP.failure([t for t in texts if t])
            detail = "\n".join(t for t in texts if t) or (p.stderr or "")
            if why == "rejected":
                rejected = True
            elif why == "suspect":
                suspect = True
            elif why == "limit":
                limited = True
            else:
                return "", p.returncode or 1, pool.scrub(detail)[-300:]
        finally:
            if out_path:
                try: os.unlink(out_path)
                except OSError: pass
            if gated:
                gate.release()
            gone = pool.release(home, limited=limited, rejected=rejected, suspect=suspect)
        if suspect and not gone:
            # 401/403 от CLI, но wham/usage вход принимает — обычный сбой вызова, как в b2fcab9:
            # его подхватывают повторы llm.call (у судьи — запасная модель).
            return "", p.returncode or 1, pool.scrub(detail)[-300:]
        if rejected or suspect:
            CP.log.warning("codex/%s: %s, беру другую учётку (попытка %d)", model, CXP.relogin_line(home), attempt)
        else:
            CP.log.warning("codex/%s: учётка «%s» упёрлась в лимит, беру другую (попытка %d)",
                           model, home.name, attempt)
    raise LimitError(f"codex/{model}: учётки Codex упираются в лимит или не принимают вход после {tries} попыток")


def call_codex(prompt: str, model: str = "gpt-5.6-terra",
               timeout: int = DEFAULT_TIMEOUT, wait_reset: bool = True) -> Answer:
    t0 = time.time()
    if CXP.POOL is not None and not alt_key():
        out, code, err = codex_exec_pooled(prompt, model, TEXT_EFFORT, (), timeout, wait_reset=wait_reset)
        if not out:
            raise LLMError(f"codex/{model}: пустой ответ. {err}")
        return Answer(out, time.time() - t0, f"codex:{model}")
    fd, out_path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    key = alt_key()
    if key:
        _say_alt_once()
        # Как у рерайта: свой провайдер флагами -c, ключ — из env ребёнка, без CODEX_HOME и auth.json.
        env = {k: v for k, v in os.environ.items() if k not in ("CODEX_HOME", "OPENAI_API_KEY")}
        env["MANGA_ALT_API_KEY"] = key
        base = _setting("MANGA_ALT_OPENAI_BASE_URL", "https://triklz27.ru/v1")
        # MANGA_ALT_CODEX_EFFORT — усилие для всех моделей через альт (low/medium/high); не задано — как у рерайта.
        effort = _setting("MANGA_ALT_CODEX_EFFORT").lower() or ALT_CODEX_EFFORT.get(model, TEXT_EFFORT)
        if effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError(f"Неверный MANGA_ALT_CODEX_EFFORT={effort!r}: нужно low, medium, high или xhigh")
        cmd = ["codex", "exec", "-m", model, "--ignore-user-config",
               "-c", 'model_provider="manga_alt"',
               "-c", 'model_providers.manga_alt.name="manga_alt"',
               "-c", f'model_providers.manga_alt.base_url="{base}"',
               "-c", 'model_providers.manga_alt.env_key="MANGA_ALT_API_KEY"',
               "-c", 'model_providers.manga_alt.wire_api="responses"',
               "-c", f'model_reasoning_effort="{effort}"']
    else:
        env = {**os.environ, "CODEX_HOME": CODEX_HOME}
        cmd = ["codex", "exec", "-m", model, "-c", f'model_reasoning_effort="{TEXT_EFFORT}"']
    cmd += ["--skip-git-repo-check", "--ephemeral", "--color", "never", "-o", out_path, "-"]
    account = "alt" if key else CXP._default_name(CODEX_HOME)
    try:
        try:
            if key:
                with _alt_slot():
                    t0 = time.time()
                    p = _run(cmd, prompt, timeout, env)
            else:
                p = _run(cmd, prompt, timeout, env)
        except subprocess.TimeoutExpired:
            CP.record(_route(CXP.usage_row(None, model, time.time() - t0, account, False), key))
            raise
        try:
            out = open(out_path, encoding="utf-8").read().strip()
        except OSError:
            out = ""
        # Без пула команда та же, что в выпуске 4 (без --json): расход — «нет данных».
        CP.record(_route(CXP.usage_row(None, model, time.time() - t0, account, bool(out)), key))
    finally:
        try: os.unlink(out_path)
        except OSError: pass
    if not out:
        err = CP.scrub(p.stderr or "")
        if key:
            err = err.replace(key, "***")
            errors = _codex_error_lines(err, prompt)
            m = _CODEX_AUTH.search(errors)
            if m:
                raise _alt_rejected(f"codex/{model}", m.group(1) or "401/403")
            if _CODEX_BUSY.search(errors):
                raise AltBusyError(f"codex/{model}: прокси альт-ключа занят или сбоит. {errors[-300:]}")
        raise LLMError(f"codex/{model}: пустой ответ. stderr: {err[-300:] if key else err[:300]}")
    return Answer(out, time.time() - t0, f"codex:{model}")


def _route(row: dict, key: str) -> dict:
    if key:
        row["route"] = "alt"
    return row


def call(prompt: str, model: str = "claude:sonnet", timeout: int = DEFAULT_TIMEOUT,
         retries: int = 2, tools=None, cwd=None, wait_reset: bool = True) -> Answer:
    """model — 'claude:sonnet' | 'codex:gpt-5.6-terra' и т.п.
    tools — разрешённые инструменты CLI (пока умеет только claude; codex их не берёт)."""
    kind, _, name = model.partition(":")
    fn = {"claude": call_claude, "codex": call_codex}.get(kind)
    if fn is None:
        raise LLMError(f"неизвестный поставщик {kind!r} в {model!r}")
    extra = {"tools": tools, "cwd": cwd} if (tools and kind == "claude") else {}
    if kind == "codex" and not wait_reset:
        extra["wait_reset"] = False   # 28.09.2026: у вызывающего есть запасной путь — не ждать сброса окна
    last = None
    attempt, busy = 0, 0
    while attempt < retries + 1:
        attempt += 1
        try:
            a = fn(prompt, name or ("sonnet" if kind == "claude" else "gpt-5.6-terra"), timeout, **extra)
            a.attempts = attempt + busy
            return a
        except AltBusyError as e:
            last = e
            if busy < ALT_BUSY_RETRIES:
                busy += 1
                attempt -= 1  # занятость прокси не съедает обычные попытки
                pause = ALT_BUSY_PAUSE * busy
                CP.log.warning("%s — повтор через %d с (%d/%d)", e, pause, busy, ALT_BUSY_RETRIES)
                time.sleep(pause)
                continue
        except subprocess.TimeoutExpired:
            last = LLMError(f"{model}: таймаут {timeout}s (попытка {attempt})")
        except (LimitError, TokenError):
            raise  # повтор через 3–6 с попал бы в то же исчерпанное окно / тот же отказ токена
        except LLMError as e:
            last = e
        if attempt <= retries:
            time.sleep(3 * attempt)
    raise last


_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.S)


def strip_fence(text: str) -> str:
    """Снимает ограждение ```...```, если модель его добавила вопреки просьбе."""
    m = _FENCE.match(text.strip())
    return m.group(1).strip() if m else text.strip()


def call_json(prompt: str, model: str = "claude:sonnet", timeout: int = DEFAULT_TIMEOUT,
              retries: int = 2, tools=None, cwd=None):
    """Просит JSON и разбирает его. Схему НЕ валидирует — это дело вызывающего,
    чтобы правило «полноту проверяет код, а не модель» не размывалось."""
    import json
    last_err = None
    for attempt in range(1, retries + 2):
        # Инструменты — только шагу «Обложка»: остальные вызовы идут прежней формой, её же
        # ждут подмены call (предел одновременных вызовов Claude, тесты).
        try:
            a = call(prompt, model, timeout, retries=0, **({"tools": tools, "cwd": cwd} if tools else {}))
        except CutError as e:
            last_err = e  # ответ оборван по пределу вывода — тот же промпт ещё раз, приписка про JSON не к месту
            continue
        raw = strip_fence(a.text)
        # модель иногда пишет пояснение до/после — вырезаем крайние скобки
        i, j = raw.find("{"), raw.rfind("}")
        if i >= 0 and j > i:
            raw = raw[i:j + 1]
        try:
            return json.loads(raw), a
        except json.JSONDecodeError as e:
            last_err = e
            prompt = prompt + f"\n\nПРЕДЫДУЩИЙ ОТВЕТ НЕ РАЗОБРАЛСЯ КАК JSON ({e}). Верни ТОЛЬКО валидный JSON."
    raise LLMError(f"{model}: JSON не разобрался после {retries+1} попыток: {last_err}")
