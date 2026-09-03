#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prompt_llm.py — ЧЕМ писать промпты (сцены, субъект, story-bible, смягчение).

Три канала, выбор — в кабинете (env VS_PROMPT_PROVIDER):

    openai (дефолт) → api.openai.com/v1/chat/completions, ключ OPENAI_API_KEY, модель из кода воркера
    codex           → подписка ChatGPT через Codex CLI,  модель gpt-5.4-mini  (VS_CODEX_MODEL)
    claude          → подписка Claude через claude-code-sdk + OAuth-токен, модель claude-sonnet-5 (VS_CLAUDE_MODEL)

Порт из ~/Documents/rewrite_web_site/rewrite_core.py (call_codex_subscription /
call_claude_subscription) — тем же способом там в проде ходят обе подписки.

ВАЖНО: логика написания промптов НЕ меняется. Модуль получает готовый OpenAI-payload
(messages / temperature / max_tokens / response_format) и возвращает ответ в
OpenAI-совместимой форме, поэтому вызывающий код разбирает его ровно так же, как
ответ api.openai.com. Меняется только транспорт и модель.

Параллельность: НЕ ограничивается — её задают пулы воркера (сцены 20, батчи 100),
как и на OpenAI-пути. VS_CODEX_MAX_PARALLEL / VS_CLAUDE_MAX_PARALLEL (>0) — аварийные
ручки, если провайдер начнёт душить.
"""

from __future__ import annotations

import base64
import contextlib
import json as _json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Linux production always has fcntl
    _fcntl = None

try:
    import requests as _requests
except Exception:                                    # requests есть везде, где есть воркер
    _requests = None

PROVIDER_OPENAI = "openai"
PROVIDER_CODEX = "codex"
PROVIDER_CLAUDE = "claude"
PROVIDER_ALT = "altclaude"     # сторонний Anthropic-прокси, OpenAI-совместимый

# Codex с ChatGPT-аккаунтом НЕ принимает gpt-4.1-* ("model is not supported when using Codex
# with a ChatGPT account", live-проверено 28.07) — только модели подписки. gpt-5.4-mini выбран
# как ближайший аналог текущего API-дефолта gpt-4.1-mini; список живых: gpt-5.4-mini, gpt-5.4,
# gpt-5.5, gpt-5.6-terra/sol/luna.
DEFAULT_CODEX_MODEL = "gpt-5.4-mini"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5"

_MINIMAL_PROVIDER_ENV_KEYS = (
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
)
_CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "multi_agent",
    "apps",
    "remote_plugin",
    "browser_use",
    "computer_use",
    "image_generation",
    "goals",
    "shell_snapshot",
)
_CODEX_TEXT_ONLY_CONFIG = (
    "--strict-config",
    "-c", 'approval_policy="never"',
    "-c", 'sandbox_mode="read-only"',
    "-c", 'shell_environment_policy.inherit="none"',
    "-c", "shell_environment_policy.include_only=[]",
    "-c", 'web_search="disabled"',
)
_CODEX_AUTH_LOCKS: Dict[str, threading.RLock] = {}
_CODEX_AUTH_LOCKS_GUARD = threading.Lock()

_PROVIDER_ALIASES = {
    "": PROVIDER_OPENAI, "openai": PROVIDER_OPENAI, "gpt": PROVIDER_OPENAI,
    "gpt_api": PROVIDER_OPENAI, "gptapi": PROVIDER_OPENAI, "api": PROVIDER_OPENAI,
    "codex": PROVIDER_CODEX, "gpt_token": PROVIDER_CODEX, "gpttoken": PROVIDER_CODEX,
    "chatgpt": PROVIDER_CODEX, "chatgpt_subscription": PROVIDER_CODEX, "codex_subscription": PROVIDER_CODEX,
    "claude": PROVIDER_CLAUDE, "claude_token": PROVIDER_CLAUDE, "claudetoken": PROVIDER_CLAUDE,
    "anthropic": PROVIDER_CLAUDE, "subscription": PROVIDER_CLAUDE,
    # Альтернативный Claude API — сторонний OpenAI-совместимый прокси (свой base_url + ключ).
    # Отдельный канал, а НЕ подмена openai: openai-ключ нужен ещё и картинкам (GPT_IMAGE2).
    "altclaude": PROVIDER_ALT, "alt_claude": PROVIDER_ALT, "alt": PROVIDER_ALT,
    "claude_api": PROVIDER_ALT, "claudeapi": PROVIDER_ALT,
    "alt_token": PROVIDER_ALT, "alttoken": PROVIDER_ALT,
}

try:
    import anyio as _anyio
    from claude_code_sdk import (
        ClaudeCodeOptions as _ClaudeCodeOptions,
        query as _claude_query,
    )
    from claude_code_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport as _ClaudeSubprocessCLITransport,
    )
    _CLAUDE_ISOLATED_SDK_AVAILABLE = True
    _CLAUDE_SDK_IMPORT_ERROR = ""
except Exception as _claude_import_exc:  # optional desktop dependency
    _anyio = None
    _ClaudeCodeOptions = None
    _claude_query = None
    _ClaudeSubprocessCLITransport = object
    _CLAUDE_ISOLATED_SDK_AVAILABLE = False
    _CLAUDE_SDK_IMPORT_ERROR = str(_claude_import_exc)


# ── настройки из окружения ────────────────────────────────────────────────────

def provider() -> str:
    """Активный канал написания промптов. Неизвестное значение → openai (как было)."""
    raw = (os.environ.get("VS_PROMPT_PROVIDER") or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _PROVIDER_ALIASES.get(raw, PROVIDER_OPENAI)


def is_subscription() -> bool:
    return provider() in (PROVIDER_CODEX, PROVIDER_CLAUDE)


def codex_model() -> str:
    m = (os.environ.get("VS_CODEX_MODEL") or DEFAULT_CODEX_MODEL).strip()
    return m.split(":", 1)[1] if m.lower().startswith("codex:") else m


def claude_model() -> str:
    return (os.environ.get("VS_CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL).strip()


def claude_token() -> str:
    """OAuth-токен подписки Claude (`claude setup-token`). Чистим от пробелов/переносов/Bearer."""
    tok = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("VS_CLAUDE_OAUTH_TOKEN") or "")
    tok = "".join(tok.split())
    if tok.lower().startswith("bearer"):
        tok = tok[6:]
    return tok



def alt_base() -> str:
    """Базовый адрес альтернативного (OpenAI-совместимого) Anthropic-прокси."""
    return (os.environ.get("VS_ALT_LLM_BASE") or "").strip().rstrip("/")


def alt_key() -> str:
    return "".join((os.environ.get("VS_ALT_LLM_KEY") or "").split())


def alt_model() -> str:
    return (os.environ.get("VS_ALT_LLM_MODEL") or "claude-sonnet-5").strip()


def alt_configured() -> bool:
    return bool(alt_base() and alt_key())


def _sub_timeout(explicit: Optional[float] = None) -> int:
    if explicit:
        try:
            return max(30, int(explicit))
        except (TypeError, ValueError):
            pass
    raw = os.environ.get("VS_SUB_TIMEOUT") or os.environ.get("REWRITE_CODEX_TIMEOUT") or "600"
    try:
        return max(30, int(raw))
    except ValueError:
        return 600


def describe() -> str:
    """Человеческая подпись канала — печатается в лог прогона."""
    p = provider()
    if p == PROVIDER_CODEX:
        return f"подписка ChatGPT (Codex CLI, {codex_model()})"
    if p == PROVIDER_ALT:
        return f"альтернативный Claude API ({alt_model()})"
    if p == PROVIDER_CLAUDE:
        return f"подписка Claude (claude-code-sdk, {claude_model()})"
    return "OpenAI API"


_SEMAPHORES: Dict[str, Any] = {}
_SEMA_LOCK = threading.Lock()


# Потолок одновременных CLI-процессов. Ограничение НЕ от провайдера, а от железа: замер 28.07 на
# 6-ядерном VPS (11.9 GB) — 40 параллельных claude ≈ 10 GB RSS (по ~250 MB на node-процесс, вплотную
# к OOM, а на боксе ещё uvicorn и rewrite), 40 codex ≈ 6.3 GB (~160 MB на процесс, Rust). Отсюда
# дефолты ниже; 0 = снять потолок совсем (тогда параллельность = пул воркера).
_DEFAULT_MAX_PARALLEL = {PROVIDER_CODEX: 40, PROVIDER_CLAUDE: 24}


def _limiter(kind: str):
    """Ограничитель параллельности. Дефолт держит память в берегах; 0 = без ограничения."""
    env_name = "VS_CODEX_MAX_PARALLEL" if kind == PROVIDER_CODEX else "VS_CLAUDE_MAX_PARALLEL"
    try:
        n = int(os.environ.get(env_name) or _DEFAULT_MAX_PARALLEL[kind])
    except ValueError:
        n = _DEFAULT_MAX_PARALLEL[kind]
    if n <= 0:
        return contextlib.nullcontext()
    with _SEMA_LOCK:
        sema = _SEMAPHORES.get(kind)
        if sema is None or getattr(sema, "_vs_n", None) != n:
            sema = threading.Semaphore(n)
            sema._vs_n = n                                    # type: ignore[attr-defined]
            _SEMAPHORES[kind] = sema
    return sema


# ── messages / JSON-режим ─────────────────────────────────────────────────────

def _msg_text(content) -> str:
    if isinstance(content, list):                             # [{"type":"text","text":...}, ...]
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return str(content or "")


def _split_messages(messages: List[Dict]) -> Tuple[str, str]:
    """OpenAI-формат → (system_prompt, user_content), как в rewrite_core.call_claude_subscription."""
    sys_parts, user_parts = [], []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        text = _msg_text(m.get("content"))
        if not text:
            continue
        (sys_parts if str(m.get("role", "user")).lower() == "system" else user_parts).append(text)
    return "\n\n".join(sys_parts), ("\n\n".join(user_parts) or "Please proceed.")


def _json_directive(response_format) -> str:
    """Text fallback for channels without native strict ``response_format``."""
    if not isinstance(response_format, dict):
        return ""
    kind = response_format.get("type")
    head = ("\n\nOUTPUT FORMAT (strict): return ONE valid JSON object and NOTHING else — "
            "no explanations, no prose, no markdown, no ``` fences.")
    if kind == "json_object":
        return head
    if kind == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema") or {}
        if not schema:
            return head
        return (head + " The JSON MUST validate against this JSON Schema — use exactly these keys, "
                "and for enum fields use exactly one of the listed values:\n"
                + _json.dumps(schema, ensure_ascii=False))
    return ""


def _with_directive(messages: List[Dict], directive: str) -> List[Dict]:
    """Дописывает директиву в system-сообщение (создаёт, если system нет). Исходный список не мутируем."""
    if not directive:
        return list(messages or [])
    out = [dict(m) for m in (messages or []) if isinstance(m, dict)]
    for m in reversed(out):
        if str(m.get("role", "")).lower() == "system":
            m["content"] = _msg_text(m.get("content")) + directive
            return out
    return [{"role": "system", "content": directive.strip()}] + out


def _extract_json(text: str) -> str:
    """Первый валидный JSON из ответа модели (снимает ``` fences и болтовню вокруг)."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    try:
        _json.loads(s)
        return s
    except Exception:
        pass
    decoder = _json.JSONDecoder()
    for opener in ("{", "["):
        idx = s.find(opener)
        while idx >= 0:
            try:
                obj, _end = decoder.raw_decode(s[idx:])
                return _json.dumps(obj, ensure_ascii=False)
            except ValueError:
                idx = s.find(opener, idx + 1)
    return s                                                   # пусть парсер воркера ругнётся и сделает ретрай


def _server_isolation_enabled() -> bool:
    return (
        os.environ.get("VS_SERVER_CREDENTIAL_ISOLATION", "").strip().lower()
        in ("1", "true", "yes", "on")
    )


# Изолированные CODEX_HOME/CLAUDE_HOME снимаются TemporaryDirectory только при
# ШТАТНОМ выходе. Сервер vsgen останавливается ~90 с и добивается SIGKILL, так что
# всё, что было в полёте, остаётся в /tmp навсегда: нашли 92 каталога codex на
# 4.4 ГБ и 102 claude, накопленные за шесть дней. Как и с временной папкой рендера,
# лечится не «убирать аккуратнее» (SIGKILL этого не позволит), а вторым владельцем:
# перед созданием нового изолята подметаем чужие, давно осиротевшие.
_ISOLATION_PREFIXES = ("vs-codex-isolated-", "vs-claude-isolated-", "vs-codex-", "vs-claude-")
_ISOLATION_STALE_SEC = int(os.environ.get("VS_ISOLATION_STALE_SEC", "21600") or 21600)  # 6 ч
_isolation_swept = False


def _sweep_stale_isolation() -> None:
    """Убрать осиротевшие изоляты. Один раз на процесс, никогда не бросает.

    Порог в 6 часов заведомо больше любого живого вызова (таймауты — минуты),
    поэтому подмести чужой работающий каталог невозможно. Ошибки глушим: уборка
    мусора не имеет права уронить генерацию промптов.
    """
    global _isolation_swept
    if _isolation_swept:
        return
    _isolation_swept = True
    try:
        tmp = tempfile.gettempdir()
        deadline = time.time() - _ISOLATION_STALE_SEC
        for entry in os.scandir(tmp):
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if not entry.name.startswith(_ISOLATION_PREFIXES):
                    continue
                if entry.stat(follow_symlinks=False).st_mtime > deadline:
                    continue
                shutil.rmtree(entry.path, ignore_errors=True)
            except OSError:
                continue
    except Exception:
        pass


def _provider_child_env(
    provider_name: str,
    *,
    credential: str = "",
    isolated_home: str = "",
    codex_home: str = "",
) -> Dict[str, str]:
    """Минимальное env для agentic CLI: unknown parent/systemd values не наследуются."""
    env = {
        name: os.environ[name]
        for name in _MINIMAL_PROVIDER_ENV_KEYS
        if os.environ.get(name)
    }
    env.setdefault("PATH", os.defpath)
    env.setdefault("LANG", "C.UTF-8")
    if isolated_home:
        env["HOME"] = isolated_home
        env["XDG_CONFIG_HOME"] = os.path.join(isolated_home, ".config")
        env["XDG_CACHE_HOME"] = os.path.join(isolated_home, ".cache")
        env["TMPDIR"] = os.path.join(isolated_home, "tmp")
        for directory in (
            env["XDG_CONFIG_HOME"],
            env["XDG_CACHE_HOME"],
            env["TMPDIR"],
        ):
            os.makedirs(directory, mode=0o700, exist_ok=True)
    if provider_name == PROVIDER_CLAUDE:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = credential
        env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"
    elif provider_name == PROVIDER_CODEX and codex_home:
        env["CODEX_HOME"] = codex_home
    return env


def _redact_provider_text(value: str, secrets: List[str]) -> str:
    clean = str(value or "")
    for secret in sorted(
        {str(item) for item in secrets if len(str(item)) >= 4},
        key=len,
        reverse=True,
    ):
        clean = clean.replace(secret, "[REDACTED]")
        clean = clean.replace(secret[:4] + "...", "[REDACTED]")
        clean = clean.replace(secret[:4] + "…", "[REDACTED]")
    return clean


def _file_digest(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_secret_values(path: str) -> List[str]:
    """String leaves auth.json нужны только для last-chance output redaction."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = _json.load(fh)
    except Exception:
        return []
    found: List[str] = []

    def _walk(item):
        if isinstance(item, dict):
            for child in item.values():
                _walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                _walk(child)
        elif isinstance(item, str) and len(item) >= 8:
            found.append(item)

    _walk(value)
    return found


def _codex_auth_lock(codex_home: str) -> threading.RLock:
    key = os.path.realpath(codex_home)
    with _CODEX_AUTH_LOCKS_GUARD:
        return _CODEX_AUTH_LOCKS.setdefault(key, threading.RLock())


@contextlib.contextmanager
def _codex_auth_lifecycle_lock(codex_home: str):
    """Share the refresh boundary lock with every VPS Codex consumer."""
    thread_lock = _codex_auth_lock(codex_home)
    with thread_lock:
        lock_fd = -1
        try:
            if _fcntl is not None:
                lock_path = os.path.join(codex_home, ".auth.json.lifecycle.lock")
                flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                lock_fd = os.open(lock_path, flags, 0o600)
                os.fchmod(lock_fd, 0o600)
                _fcntl.flock(lock_fd, _fcntl.LOCK_EX)
            yield
        finally:
            if lock_fd >= 0:
                try:
                    _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)


def _codex_refresh_possible(codex_home: str) -> bool:
    """Может ли ЭТОТ вызов задеть rotating refresh token → нужен полный call-lock.

    Зачем вообще вопрос: полный замок на весь вызов CLI сериализует канал начисто —
    замер на боевом сервере 05.08 показал 1 одновременный вызов при настроенных 20,
    то есть 282 сцены писались 61 минуту вместо ~3. При этом ротация — событие
    редкое: access_token жил ещё 235 ч, а auth.json не менялся ни разу за все 282
    вызова подряд. Платить 20-кратной параллельностью за каждый вызов ради события
    раз в сутки смысла нет.

    Поэтому замок берётся только у границы обновления. Возврат обновлённого токена
    и без того защищён: _copy_back_codex_auth делает compare-and-swap под своим
    замком, то есть более старый вызов не может затереть более новый auth.

    Не смогли прочитать срок — считаем, что обновление возможно (перестраховка).
    """
    margin_h = os.environ.get("VS_CODEX_REFRESH_MARGIN_H")
    try:
        margin = float(margin_h) if margin_h else 48.0
    except ValueError:
        margin = 48.0
    if margin <= 0:                      # 0 = вернуть прежнее поведение (замок всегда)
        return True
    try:
        with open(os.path.join(codex_home, "auth.json"), encoding="utf-8") as fh:
            data = _json.load(fh)
        token = ((data.get("tokens") or {}).get("access_token") or "").strip()
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = _json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        if not exp:
            return True
        return (float(exp) - time.time()) < margin * 3600.0
    except Exception:
        return True


def _secure_copy_file(source: str, destination: str) -> None:
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with open(source, "rb") as src, os.fdopen(fd, "wb") as dst:
            fd = -1
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _prepare_codex_isolation(
    isolation_root: str,
    leased_codex_home: str,
) -> Tuple[str, str, Dict[str, str], str, List[str]]:
    home_dir = os.path.join(isolation_root, "home")
    work_dir = os.path.join(isolation_root, "work")
    auth_dir = os.path.join(isolation_root, "codex-auth")
    for directory in (home_dir, work_dir, auth_dir):
        os.makedirs(directory, mode=0o700, exist_ok=True)
    source_auth = os.path.join(leased_codex_home, "auth.json")
    if not leased_codex_home or not os.path.isfile(source_auth):
        raise RuntimeError("CODEX_AUTH: leased CODEX_HOME has no auth.json")
    isolated_auth = os.path.join(auth_dir, "auth.json")
    with _codex_auth_lock(leased_codex_home):
        original_digest = _file_digest(source_auth)
        _secure_copy_file(source_auth, isolated_auth)
    child_env = _provider_child_env(
        PROVIDER_CODEX,
        isolated_home=home_dir,
        codex_home=auth_dir,
    )
    redaction_values = [
        leased_codex_home,
        auth_dir,
        *_json_secret_values(isolated_auth),
    ]
    return work_dir, auth_dir, child_env, original_digest, redaction_values


def _copy_back_codex_auth(
    leased_codex_home: str,
    isolated_codex_home: str,
    original_digest: str,
) -> None:
    """CAS copy-back refresh auth: concurrent older call cannot overwrite newer auth."""
    source_auth = os.path.join(leased_codex_home, "auth.json")
    isolated_auth = os.path.join(isolated_codex_home, "auth.json")
    if not os.path.isfile(isolated_auth):
        return
    refreshed_digest = _file_digest(isolated_auth)
    if refreshed_digest == original_digest:
        return
    lock = _codex_auth_lock(leased_codex_home)
    with lock:
        if not os.path.isfile(source_auth) or _file_digest(source_auth) != original_digest:
            return
        parent = os.path.dirname(source_auth)
        fd, tmp_path = tempfile.mkstemp(prefix=".auth.json.vs-", dir=parent)
        try:
            os.fchmod(fd, 0o600)
            with open(isolated_auth, "rb") as src, os.fdopen(fd, "wb") as dst:
                fd = -1
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(tmp_path, source_auth)
            os.chmod(source_auth, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


def _find_codex_bwrap() -> str:
    direct = shutil.which("bwrap") or shutil.which("bubblewrap")
    if direct:
        return direct
    roots = (
        Path("/usr/lib/node_modules/@openai/codex/node_modules/@openai"),
        Path("/usr/local/lib/node_modules/@openai/codex/node_modules/@openai"),
    )
    for root in roots:
        if not root.is_dir():
            continue
        for candidate in root.glob("codex-linux-*/vendor/*/codex-resources/bwrap"):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return ""


def _wrap_codex_linux_sandbox(
    command: List[str],
    isolation_root: str,
    work_dir: str,
    codex_bin: str,
) -> List[str]:
    """Outer filesystem boundary; host secrets are never mounted."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("CODEX_ISOLATION: secure Codex execution requires Linux")
    bwrap = _find_codex_bwrap()
    if not bwrap:
        raise RuntimeError("CODEX_ISOLATION: bubblewrap is unavailable")
    real_bin = os.path.realpath(codex_bin)
    if not any(
        real_bin == prefix or real_bin.startswith(prefix + os.sep)
        for prefix in ("/usr", "/bin", "/lib", "/lib64")
    ):
        raise RuntimeError("CODEX_ISOLATION: codex binary is outside the minimal runtime")
    false_bin = "/usr/bin/false"
    if not os.path.isfile(false_bin):
        raise RuntimeError("CODEX_ISOLATION: /usr/bin/false is unavailable")
    wrapped = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--dir", "/etc",
        "--dir", "/var",
    ]
    for path in ("/usr", "/lib", "/lib64"):
        if os.path.exists(path):
            wrapped.extend(["--ro-bind", path, path])
    if os.path.islink("/bin"):
        wrapped.extend(["--symlink", os.readlink("/bin"), "/bin"])
    elif os.path.isdir("/bin"):
        wrapped.extend(["--ro-bind", "/bin", "/bin"])
    for path in (
        "/etc/ssl",
        "/etc/ca-certificates",
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/passwd",
        "/etc/group",
        "/etc/ld.so.cache",
    ):
        if os.path.exists(path):
            wrapped.extend(["--ro-bind", path, path])
    wrapped.extend(["--bind", isolation_root, isolation_root, "--chdir", work_dir])
    for shell_path in (
        "/usr/bin/bash",
        "/usr/bin/dash",
        "/usr/bin/sh",
        "/usr/bin/zsh",
    ):
        if os.path.exists(shell_path):
            wrapped.extend(["--ro-bind", false_bin, shell_path])
    return wrapped + command


def _codex_text_only_command(
    codex_bin: str,
    work_dir: str,
    model: str,
    output_path: str,
    output_schema_path: str = "",
) -> List[str]:
    command = [
        codex_bin,
        "exec",
        "--ephemeral",
        *_CODEX_TEXT_ONLY_CONFIG,
    ]
    for feature in _CODEX_DISABLED_FEATURES:
        command.extend(["--disable", feature])
    command += [
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "-C", work_dir,
        "-m", model,
    ]
    if output_schema_path:
        command.extend(["--output-schema", output_schema_path])
    command.extend(["-o", output_path, "-"])
    return command


def _codex_response_schema(response_format) -> Optional[Dict[str, Any]]:
    """Extract the inner OpenAI schema without ever formatting it into an error."""
    if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
        return None
    wrapper = response_format.get("json_schema")
    if not isinstance(wrapper, dict):
        raise ValueError("json_schema response format has no json_schema object")
    schema = wrapper.get("schema")
    if not isinstance(schema, dict):
        raise ValueError("json_schema response format has no schema object")
    return schema


def _write_codex_output_schema(work_dir: str, response_format) -> str:
    """Write Codex' native schema inside the per-call 0700 isolation root.

    The returned file is owned by the surrounding ``TemporaryDirectory``.  It is
    therefore removed on success and every handled error, while the existing stale
    isolation sweeper remains the SIGKILL recovery owner.
    """
    schema = _codex_response_schema(response_format)
    if schema is None:
        return ""
    fd, schema_path = tempfile.mkstemp(
        prefix="response-schema-", suffix=".json", dir=work_dir
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as schema_file:
            fd = -1
            _json.dump(
                schema,
                schema_file,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            schema_file.flush()
            os.fsync(schema_file.fileno())
        return schema_path
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(schema_path)
        except OSError:
            pass
        raise


def _codex_stderr_summary(stderr_text: str) -> str:
    """Return the provider diagnosis, never the echoed request transcript.

    Recent Codex CLI builds include the complete input prompt before the useful
    error.  The server stores only the first 160 characters of our reason, so
    forwarding the raw tail hid a later ``401 Unauthorized`` and prevented the
    broken credential slot from entering its durable cooldown.
    """
    lines = [line.strip() for line in str(stderr_text or "").splitlines()
             if line.strip()]

    def _clean(line: str) -> str:
        line = re.sub(
            r"^\d{4}-\d\d-\d\dT\S+\s+(?:ERROR|WARN|WARNING)\s+[^:]+:\s*",
            "",
            line,
            flags=re.IGNORECASE,
        )
        return re.sub(r"^(?:ERROR|WARN|WARNING):\s*", "", line,
                      flags=re.IGNORECASE).strip()

    auth_status = next((line for line in reversed(lines)
                        if any(marker in line.lower() for marker in (
                            "401 unauthorized", "http error: 401", "not logged in",
                            "authentication failed", "invalid token",
                        ))), "")
    auth_detail = next((line for line in reversed(lines)
                        if any(marker in line.lower() for marker in (
                            "refresh token", "access token could not be refreshed",
                            "please log out and sign in again",
                        ))), "")
    if auth_status or auth_detail:
        detail = _clean(auth_detail or auth_status)
        # Keep an explicit marker at the front: both the worker and the server's
        # credential-cooldown classifier must see it before their log truncation.
        return "401 Unauthorized" + (f": {detail}" if detail else "")

    priority = next((line for line in reversed(lines)
                     if any(marker in line.lower() for marker in (
                         "429", "rate limit", "usage limit", "quota",
                         "limit reached", "not supported", "fatal", "error:",
                     ))), "")
    if priority:
        return _clean(priority)[:1600]
    return "\n".join(lines[-8:])[-1600:]


# ── канал 1: подписка ChatGPT через Codex CLI (порт rewrite_core.call_codex_subscription) ──

def call_codex_subscription(
    messages: List[Dict],
    model: str = "",
    timeout: Optional[float] = None,
    response_format=None,
) -> Tuple[bool, str]:
    """GPT через Codex CLI; shared-server path изолирован outer bwrap."""
    bin_setting = (os.environ.get("VS_CODEX_BIN") or os.environ.get("REWRITE_CODEX_BIN") or "codex")
    codex_bin = shutil.which(bin_setting)
    if not codex_bin and os.path.isfile(bin_setting):
        codex_bin = bin_setting
    if not codex_bin:
        return False, "CODEX_AUTH: codex CLI не найден (поставь codex или задай VS_CODEX_BIN)"

    actual_model = (model or codex_model())
    if actual_model.lower().startswith("codex:"):
        actual_model = actual_model.split(":", 1)[1]
    timeout_sec = _sub_timeout(timeout)

    prompt_parts = [
        "Act only as a text-generation engine. Do not inspect files, call tools, or modify anything. "
        "Follow the messages below and return only the requested final content."
    ]
    for message in messages or []:
        role = str(message.get("role", "user")).upper()
        prompt_parts.append(f"\n<{role}>\n{_msg_text(message.get('content'))}\n</{role}>")
    prompt = "\n".join(prompt_parts)

    def _execute(command, child_env, work_dir, output_path, stderr_path):
        timed_out = False
        with open(os.devnull, "w") as devnull, open(
            stderr_path, "w", encoding="utf-8"
        ) as stderr_file:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=devnull,
                stderr=stderr_file,
                text=True,
                env=child_env,
                cwd=work_dir,
            )
            try:
                proc.communicate(prompt, timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                timed_out = True
            return_code = proc.returncode
        final_text = ""
        if os.path.exists(output_path):
            with open(output_path, "r", encoding="utf-8", errors="replace") as fh:
                final_text = fh.read().strip()
        with open(stderr_path, "r", encoding="utf-8", errors="replace") as fh:
            stderr_text = fh.read()[-4000:].strip()
        return timed_out, return_code, final_text, stderr_text

    with _limiter(PROVIDER_CODEX):
        redaction_values: List[str] = []
        try:
            codex_home = (
                os.environ.get("VS_CODEX_HOME")
                or os.environ.get("REWRITE_CODEX_HOME")
                or ""
            ).strip()
            server_isolated = _server_isolation_enabled()
            prefix = "vs-codex-isolated-" if server_isolated else "vs-codex-"
            _sweep_stale_isolation()
            with contextlib.ExitStack() as isolated_stack:
                if server_isolated and _codex_refresh_possible(codex_home):
                    # Один leased CODEX_HOME обслуживает до 20 scene threads.
                    # Полный call-lock не даёт двум CLI переиспользовать один
                    # rotating refresh token одновременно — но нужен он ТОЛЬКО у
                    # границы обновления токена (см. _codex_refresh_possible).
                    # Вне этого окна вызовы идут параллельно, а возврат токена
                    # остаётся под CAS в _copy_back_codex_auth.
                    isolated_stack.enter_context(
                        _codex_auth_lifecycle_lock(codex_home)
                    )
                isolation_root = isolated_stack.enter_context(
                    tempfile.TemporaryDirectory(prefix=prefix)
                )
                if server_isolated:
                    (
                        work_dir,
                        isolated_codex_home,
                        child_env,
                        original_auth_digest,
                        redaction_values,
                    ) = _prepare_codex_isolation(isolation_root, codex_home)
                else:
                    work_dir = os.path.join(isolation_root, "work")
                    home_dir = os.path.join(isolation_root, "home")
                    os.makedirs(work_dir, mode=0o700)
                    os.makedirs(home_dir, mode=0o700)
                    if codex_home and not os.path.isfile(os.path.join(codex_home, "auth.json")):
                        return False, "CODEX_AUTH: каталог CODEX_HOME не авторизован"
                    child_env = _provider_child_env(
                        PROVIDER_CODEX,
                        isolated_home=home_dir,
                        codex_home=codex_home,
                    )
                    if not codex_home:
                        # Legacy/local compatibility. Shared-server path never reaches this.
                        child_env["HOME"] = os.path.expanduser("~")
                    isolated_codex_home = ""
                    original_auth_digest = ""
                    if codex_home:
                        redaction_values.append(codex_home)

                output_path = os.path.join(work_dir, "final.txt")
                stderr_path = os.path.join(work_dir, "stderr.log")
                output_schema_path = _write_codex_output_schema(
                    work_dir, response_format
                )
                if output_schema_path:
                    # Codex may echo argv on an early CLI failure.  The schema
                    # contents are never in argv/prompt; hide even its temp path.
                    redaction_values.append(output_schema_path)
                cmd = _codex_text_only_command(
                    codex_bin,
                    work_dir,
                    actual_model,
                    output_path,
                    output_schema_path,
                )
                if server_isolated:
                    cmd = _wrap_codex_linux_sandbox(
                        cmd, isolation_root, work_dir, codex_bin
                    )
                result = _execute(
                    cmd, child_env, work_dir, output_path, stderr_path
                )
                if server_isolated:
                    redaction_values.extend(
                        _json_secret_values(
                            os.path.join(isolated_codex_home, "auth.json")
                        )
                    )
                    try:
                        _copy_back_codex_auth(
                            codex_home,
                            isolated_codex_home,
                            original_auth_digest,
                        )
                    except Exception as exc:
                        clean = _redact_provider_text(str(exc), redaction_values)
                        return False, f"CODEX_AUTH: auth refresh не сохранён: {clean}"

                timed_out, return_code, final_text, stderr_text = result
                final_text = _redact_provider_text(final_text, redaction_values)
                stderr_text = _redact_provider_text(stderr_text, redaction_values)
                if timed_out:
                    return False, f"CODEX_TIMEOUT: нет ответа за {timeout_sec}с"
                if return_code == 0 and final_text:
                    return True, final_text
                stderr_summary = _codex_stderr_summary(stderr_text)
                low = stderr_summary.lower()
                # Classify the extracted provider diagnosis, never the raw stderr:
                # recent Codex builds may echo the complete script (for example
                # "401 BCE") before a completely unrelated schema/CLI error.
                if low.startswith("401 unauthorized"):
                    error_prefix = "CODEX_AUTH"
                elif any(x in low for x in ("usage limit", "rate limit", "quota", "limit reached", "429")):
                    error_prefix = "CODEX_LIMIT"
                else:
                    error_prefix = "CODEX_ERROR"
                return False, (
                    f"{error_prefix}: "
                    f"{stderr_summary or f'codex завершился с кодом {return_code}'}"
                )
        except Exception as exc:
            clean = _redact_provider_text(str(exc), redaction_values)
            return False, f"CODEX_ERROR: {type(exc).__name__}: {clean}"


# ── канал 2: подписка Claude через claude-code-sdk (порт rewrite_core.call_claude_subscription) ──

_CLAUDE_STREAM_SILENCE_SEC = int(os.environ.get("VS_CLAUDE_STREAM_SILENCE", "300") or 300)

_CLAUDE_BLOCKED_TOOLS = [
    "Agent",
    "AskUserQuestion",
    "Bash",
    "BashOutput",
    "Edit",
    "EnterPlanMode",
    "ExitPlanMode",
    "Glob",
    "Grep",
    "KillShell",
    "MultiEdit",
    "NotebookEdit",
    "Read",
    "Skill",
    "SlashCommand",
    "Task",
    "TaskOutput",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Write",
]
# 4 хватает почти всегда, но под параллелью 40 один вызов из 40 упёрся в error_max_turns на num_turns=5
# (модель дольше «думала» инструментами) → берём запас: лишние ходы не тратятся, если ответ пришёл сразу.
_CLAUDE_MAX_TURNS = int(os.environ.get("VS_CLAUDE_MAX_TURNS", "8") or 8)


async def _deny_claude_tool(_tool_name, _tool_input, _context):
    from claude_code_sdk import PermissionResultDeny

    return PermissionResultDeny(
        message="Tools are disabled for the VideoStudio text-generation service.",
        interrupt=True,
    )


if _CLAUDE_ISOLATED_SDK_AVAILABLE:
    class _SanitizedClaudeTransport(_ClaudeSubprocessCLITransport):
        """SDK transport which never merges os.environ into the Claude process."""

        def __init__(self, prompt, options, process_env: Dict[str, str]):
            super().__init__(prompt=prompt, options=options)
            self._vs_process_env = dict(process_env)

        async def connect(self) -> None:
            if self._process:
                return
            from subprocess import PIPE
            from anyio.streams.text import TextReceiveStream, TextSendStream

            cmd = self._build_command()
            process_env = dict(self._vs_process_env)
            process_env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"
            if self._cwd:
                process_env["PWD"] = self._cwd
            stderr_dest = (
                self._options.debug_stderr
                if "debug-to-stderr" in self._options.extra_args
                and self._options.debug_stderr
                else None
            )
            self._process = await _anyio.open_process(
                cmd,
                stdin=PIPE,
                stdout=PIPE,
                stderr=stderr_dest,
                cwd=self._cwd,
                env=process_env,
                user=self._options.user,
            )
            if self._process.stdout:
                self._stdout_stream = TextReceiveStream(self._process.stdout)
            if self._is_streaming and self._process.stdin:
                self._stdin_stream = TextSendStream(self._process.stdin)
            elif not self._is_streaming and self._process.stdin:
                await self._process.stdin.aclose()
            self._ready = True


def _claude_isolated_options(
    token: str,
    *,
    system_prompt: str,
    model: str,
):
    _sweep_stale_isolation()
    isolation_root = tempfile.mkdtemp(prefix="vs-claude-isolated-")
    os.chmod(isolation_root, 0o700)
    home_dir = os.path.join(isolation_root, "home")
    work_dir = os.path.join(isolation_root, "work")
    os.makedirs(home_dir, mode=0o700)
    os.makedirs(work_dir, mode=0o700)
    child_env = _provider_child_env(
        PROVIDER_CLAUDE,
        credential=token,
        isolated_home=home_dir,
    )
    options = _ClaudeCodeOptions(
        system_prompt=system_prompt or None,
        model=model,
        max_turns=_CLAUDE_MAX_TURNS,
        allowed_tools=[],
        disallowed_tools=list(_CLAUDE_BLOCKED_TOOLS),
        env=child_env,
        cwd=work_dir,
        mcp_servers={},
        permission_mode="dontAsk",
        can_use_tool=_deny_claude_tool,
        include_partial_messages=True,
        extra_args={
            "tools": "",
            "strict-mcp-config": None,
            "disable-slash-commands": None,
            "no-chrome": None,
            "setting-sources": "",
            "no-session-persistence": None,
        },
    )
    return options, child_env, isolation_root


# «unhandled errors in a taskgroup» — самая частая форма безликого отказа: anyio отдаёт
# наружу только обёртку ExceptionGroup, внутри которой у sub-exception тоже нет текста.
# Без этого маркера _claude_diagnose() не запускался, причина оставалась нераспознанной,
# и лимит подписки выглядел как «ошибка без причины»: в логах 08–10.08.2026 канал гасился
# по 13 раз подряд, а настоящее «out of extra usage · resets Aug 14, 10am» никто не видел.
_FACELESS_MARKERS = ("command failed with exit code", "check stderr output",
                     "unhandled errors in a taskgroup")
_diag_done = False
_diag_lock = threading.Lock()


def _is_faceless(low: str) -> bool:
    """Ошибка без причины: SDK отдал только код выхода, классифицировать нечего."""
    if any(x in low for x in _FACELESS_MARKERS):
        return not any(x in low for x in ("usage limit", "rate limit", "quota",
                                          "limit reached", "hit your limit",
                                          "not logged in", "unauthorized", "401"))
    return False


def _claude_diagnose(token: str) -> str:
    """Настоящая причина отказа подписки — ОДИН раз за процесс.

    `claude -p` печатает «You've hit your limit · resets …» / «Not logged in» в stdout
    и выходит с кодом 0, тогда как SDK на том же токене падает безликим exit 1.
    Один дешёвый вызов на процесс: дальше причина уже известна и канал помечен мёртвым.
    """
    global _diag_done
    with _diag_lock:
        if _diag_done:
            return ""
        _diag_done = True
    env = dict(os.environ)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(var, None)
    try:
        # stdin=DEVNULL: с унаследованным открытым stdin `claude -p` блокируется на его
        # чтении и уходит в таймаут вместо ответа (замер 11.08.2026: 5 с против 45 с).
        # Диагностика — единственный источник настоящей причины отказа, молчать ей нельзя.
        r = subprocess.run(["claude", "-p", "ping", "--model", claude_model()],
                           capture_output=True, text=True, timeout=90, env=env,
                           stdin=subprocess.DEVNULL)
    except Exception as e:                                  # noqa: BLE001 — диагностика не должна падать
        return f"диагностика не удалась ({type(e).__name__})"
    out = ((r.stdout or "") + " " + (r.stderr or "")).strip()
    out = " ".join(out.split())[:200]
    return _redact_provider_text(out, [token]) if out else ""



def _flatten_exception(exc: BaseException, depth: int = 0) -> str:
    """Тип и текст исключения ВМЕСТЕ с содержимым ExceptionGroup.

    `str(ExceptionGroup)` даёт только «unhandled errors in a TaskGroup (N sub-exceptions)»,
    без причины. Разворачиваем рекурсивно, чтобы и в лог, и в классификатор ниже попадал
    настоящий текст провайдера («usage limit», «not logged in», «CLI exited with code …»).
    """
    name = type(exc).__name__
    text = str(exc).strip()
    parts = [f"{name}: {text}" if text else name]
    subs = getattr(exc, "exceptions", None)
    if subs and depth < 4:
        for sub in list(subs)[:5]:
            try:
                parts.append(_flatten_exception(sub, depth + 1))
            except Exception:                                   # noqa: BLE001 — диагностика не должна падать
                parts.append(type(sub).__name__)
    return " | ".join(p for p in parts if p)


def _find_timeout_error(exc: BaseException) -> Optional[BaseException]:
    """anyio заворачивает ошибки в ExceptionGroup — достаём TimeoutError изнутри."""
    if isinstance(exc, TimeoutError):
        return exc
    for sub in getattr(exc, "exceptions", None) or []:
        found = _find_timeout_error(sub)
        if found is not None:
            return found
    return None


def call_claude_subscription(messages: List[Dict], model: str = "", oauth_token: str = "",
                             timeout: Optional[float] = None) -> Tuple[bool, str]:
    """Claude через Pro/Max подписку: claude-code-sdk + OAuth-токен, БЕЗ API-кредитов."""
    if not _CLAUDE_ISOLATED_SDK_AVAILABLE:
        return False, (
            "CLAUDE_SDK: secure claude-code-sdk transport недоступен "
            "(pip install claude-code-sdk anyio) — "
            + _CLAUDE_SDK_IMPORT_ERROR
        )

    token = "".join((oauth_token or "").split()) or claude_token()
    if token.lower().startswith("bearer"):
        token = token[6:]
    if not token:
        return False, ("CLAUDE_AUTH: OAuth-токен подписки не задан. Получи `claude setup-token` "
                       "и впиши его в кабинете (Ключи → Чем писать промпты).")

    system_prompt, user_content = _split_messages(messages)
    hard_timeout = _sub_timeout(timeout)
    options, child_env, isolation_root = _claude_isolated_options(
        token,
        system_prompt=system_prompt,
        model=(model or claude_model()),
    )

    chunks: List[str] = []
    meta: Dict[str, Any] = {}                                 # диагностика из ResultMessage при пустом ответе

    async def _run():
        from dataclasses import replace

        last_data = [time.monotonic()]
        iterate_done = _anyio.Event()
        got_assistant = [False]
        transport_options = replace(
            options,
            permission_prompt_tool_name="stdio",
        )

        async def _prompt_stream():
            yield {
                "type": "user",
                "message": {"role": "user", "content": user_content},
                "parent_tool_use_id": None,
                "session_id": "videostudio-isolated",
            }

        transport = _SanitizedClaudeTransport(
            prompt=_prompt_stream(),
            options=transport_options,
            process_env=child_env,
        )

        async def _watchdog():
            while not iterate_done.is_set():
                with _anyio.move_on_after(15):
                    await iterate_done.wait()
                    return
                silence = time.monotonic() - last_data[0]
                if silence > _CLAUDE_STREAM_SILENCE_SEC:
                    raise TimeoutError(f"нет данных от Claude CLI {int(silence)}с (stream stall)")

        async def _iterate():
            try:
                async for message in _claude_query(
                    prompt=_prompt_stream(),
                    options=options,
                    transport=transport,
                ):
                    last_data[0] = time.monotonic()
                    kind = type(message).__name__
                    if kind == "AssistantMessage":
                        content = getattr(message, "content", None)
                        if isinstance(content, str):
                            chunks.append(content)
                            got_assistant[0] = True
                        elif isinstance(content, list):
                            for block in content:
                                text = getattr(block, "text", None)
                                if isinstance(text, str) and text:
                                    chunks.append(text)
                                    got_assistant[0] = True
                    elif kind == "ResultMessage":
                        # причина пустого ответа (лимит/refusal/max_turns) живёт здесь — сохраняем для лога
                        for f in ("subtype", "is_error", "num_turns", "stop_reason",
                                  "usage", "total_cost_usd", "session_id", "duration_ms"):
                            v = getattr(message, f, None)
                            if v is not None:
                                meta[f] = v
                        result = getattr(message, "result", None)   # fallback: AssistantMessage не пришёл
                        if not got_assistant[0] and isinstance(result, str) and result:
                            chunks.append(result)
            finally:
                iterate_done.set()

        with _anyio.fail_after(hard_timeout):
            async with _anyio.create_task_group() as tg:
                tg.start_soon(_watchdog)
                await _iterate()

    try:
        with _limiter(PROVIDER_CLAUDE):
            try:
                _anyio.run(_run)
            except BaseException as exc:                        # включая ExceptionGroup от anyio
                timeout_err = _find_timeout_error(exc)
                if timeout_err is not None:
                    clean = _redact_provider_text(str(timeout_err), [token])
                    return False, f"CLAUDE_TIMEOUT: {clean}"
                if not isinstance(exc, Exception):
                    raise
                # У anyio наружу торчит ТОЛЬКО «ExceptionGroup: unhandled errors in a
                # TaskGroup (1 sub-exception)» — настоящая причина лежит внутри группы.
                # Из-за этого в логах ночи 05→06.08 копилось 957 безликих CLAUDE_ERROR,
                # а классификация ниже (лимит/авторизация) не срабатывала и канал молча
                # ретраился по 20 раз вместо переключения. Разворачиваем группу.
                msg = _redact_provider_text(
                    _flatten_exception(exc) or type(exc).__name__,
                    [token],
                )
                low = msg.lower()
                if _is_faceless(low):
                    # SDK проглотил причину: наружу торчит голое «exit code 1».
                    # `claude -p` печатает её открытым текстом (и выходит с rc=0).
                    hint = _claude_diagnose(token)
                    if hint:
                        msg = msg + " | причина: " + hint
                        low = msg.lower()
                if any(x in low for x in ("usage limit", "rate limit", "quota", "limit reached", "hit your limit")):
                    return False, "CLAUDE_LIMIT: " + msg
                if any(x in low for x in ("not logged in", "unauthorized", "authentication", "401", "invalid token",
                                          "oauth")):
                    return False, "CLAUDE_AUTH: " + msg
                return False, f"CLAUDE_ERROR: {type(exc).__name__}: {msg}"

        text = _redact_provider_text("".join(chunks).strip(), [token])
        if not text:
            info = ", ".join(f"{k}={v}" for k, v in meta.items()) or "без деталей"
            info = _redact_provider_text(info, [token])
            low_info = info.lower()
            if "limit" in low_info or "usage" in low_info:
                return False, "CLAUDE_LIMIT: подписка упёрлась в лимит (" + info + ")"
            return False, "CLAUDE_ERROR: пустой ответ подписки (" + info + ")"
        _note_usage(meta)          # расход этого вызова уедет в usage ответа
        return True, text
    finally:
        shutil.rmtree(isolation_root, ignore_errors=True)


# ── OpenAI-совместимый ответ ──────────────────────────────────────────────────

_LIMIT_MARKERS = ("_limit:", "usage limit", "rate limit", "quota", "limit reached",
                  "hit your limit", "too many requests", "429")
_AUTH_MARKERS = ("_auth:", "_sdk:", "not logged in", "unauthorized", "authentication",
                 "invalid api key")


class LLMResponse:
    """requests.Response-совместимая обёртка: воркер разбирает ответ подписки тем же кодом."""

    def __init__(self, status_code: int, data: Optional[Dict] = None, text: str = ""):
        self.status_code = int(status_code)
        self._data = data
        self.text = text or (_json.dumps(data, ensure_ascii=False) if data is not None else "")
        self.headers: Dict[str, str] = {"content-type": "application/json"}

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8", "replace")

    def json(self) -> Dict:
        if self._data is None:
            raise ValueError("нет JSON в ответе: " + self.text[:200])
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            msg = f"HTTP {self.status_code}: {self.text[:400]}"
            if _requests is not None:
                raise _requests.exceptions.HTTPError(msg, response=self)   # ловится retry-логикой воркера
            raise RuntimeError(msg)


# Расход последнего вызова В ЭТОМ потоке: CLI отдаёт его в ResultMessage, но раньше он
# нигде не сохранялся, и вопрос «куда уходит подписка» упирался в догадки. Теперь цифры
# доезжают до воркера в поле usage ответа (форма как у OpenAI).
_LAST_USAGE: Dict[int, Dict] = {}


def _note_usage(meta: Dict) -> None:
    u = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
    if not u and meta.get("total_cost_usd") is None:
        return
    _LAST_USAGE[threading.get_ident()] = {
        "input_tokens": u.get("input_tokens"),
        "output_tokens": u.get("output_tokens"),
        "cache_read_input_tokens": u.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": u.get("cache_creation_input_tokens"),
        "cost_usd": meta.get("total_cost_usd"),
        "session_id": meta.get("session_id"),
        "duration_ms": meta.get("duration_ms"),
    }


def _take_usage() -> Dict:
    return _LAST_USAGE.pop(threading.get_ident(), {}) or {}


def _openai_shaped(content: str, model: str) -> Dict:
    return {
        "id": "prompt_llm-subscription",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": _take_usage(),
    }


# ── Фолбэк между каналами ─────────────────────────────────────────────────────
# Канал умер (лимит подписки / нет авторизации) → уходим на следующий и НЕ ломимся в мёртвый
# на каждой из сотен сцен: помечаем его нерабочим на _DEAD_TTL секунд. Порядок по умолчанию —
# codex → claude → openai (начинаем всегда с канала, выбранного в кабинете).
_FALLBACK_ORDER = [PROVIDER_CODEX, PROVIDER_CLAUDE, PROVIDER_ALT, PROVIDER_OPENAI]
_DEAD_TTL = int(os.environ.get("VS_CHANNEL_DEAD_TTL", "600") or 600)
_dead: Dict[str, float] = {}
_dead_lock = threading.Lock()
_announced: Dict[str, bool] = {}


def fallback_enabled() -> bool:
    return (os.environ.get("VS_PROMPT_FALLBACK", "1") or "1").strip().lower() not in ("0", "false", "no", "off")


def _say(msg: str) -> None:
    """Одноразовое сообщение в лог задачи (stdout воркера) — без спама на каждой сцене."""
    with _dead_lock:
        if _announced.get(msg):
            return
        _announced[msg] = True
    print(msg, flush=True)


_dead_reason: Dict[str, str] = {}


def _short_why(why: str) -> str:
    """Причина отказа человеку, а не обёртка SDK.

    Диагностика дописывает настоящий текст провайдера хвостом («… | причина: You've
    hit your limit · resets Aug 10»), а голова строки — бесполезный ExceptionGroup.
    В лог и в кабинет выносим хвост.
    """
    why = (why or '').strip()
    marker = 'причина: '
    if marker in why:
        why = why.split(marker, 1)[1].strip()
    for pref in ('CLAUDE_LIMIT:', 'CLAUDE_AUTH:', 'CLAUDE_ERROR:', 'CODEX_LIMIT:',
                 'CODEX_AUTH:', 'CODEX_ERROR:'):
        if why.startswith(pref):
            why = why[len(pref):].strip()
    return ' '.join(why.split())[:160]


def _mark_dead(chan: str, why: str) -> None:
    with _dead_lock:
        _dead[chan] = time.monotonic() + _DEAD_TTL
        _dead_reason[chan] = _short_why(why)
    _say(f"⚠️ Канал промптов «{chan}» недоступен ({_short_why(why)}) — переключаюсь на следующий")


_FACELESS_LIMIT = int(os.environ.get("VS_CHANNEL_FACELESS_STRIKES", "3") or 3)
_strikes: Dict[str, int] = {}


def _strike(chan: str, why: str) -> bool:
    """Безликая ошибка канала подряд N раз → считаем канал нерабочим.

    Причину знать не обязательно: важно перестать долбиться в канал, который
    систематически не отвечает. Одиночный транзиент канал не убивает.
    """
    with _dead_lock:
        n = _strikes.get(chan, 0) + 1
        _strikes[chan] = n
    if n >= _FACELESS_LIMIT:
        _mark_dead(chan, why or f"{n} безликих ошибок подряд")
        return True
    return False


def _strike_reset(chan: str) -> None:
    with _dead_lock:
        _strikes.pop(chan, None)


def _is_dead(chan: str) -> bool:
    with _dead_lock:
        until = _dead.get(chan, 0)
        if until and until > time.monotonic():
            return True
        if until:
            _dead.pop(chan, None)          # TTL истёк — дадим каналу второй шанс
    return False


def _channel_ready(chan: str, openai_call, alt_call=None) -> bool:
    if chan == PROVIDER_ALT:
        return alt_call is not None        # база/ключ/модель живут в воркере, как и у openai
    if chan == PROVIDER_OPENAI:
        return openai_call is not None     # ключ и заголовки есть только у воркера
    if chan == PROVIDER_CODEX:
        bin_setting = (os.environ.get("VS_CODEX_BIN") or os.environ.get("REWRITE_CODEX_BIN") or "codex")
        return bool(shutil.which(bin_setting) or os.path.isfile(bin_setting))
    if not claude_token():
        return False
    try:
        import claude_code_sdk  # noqa: F401
        import anyio            # noqa: F401
    except ImportError:
        return False
    return True


def _call_channel(chan: str, messages: List[Dict], response_format, timeout, openai_call,
                  alt_call=None):
    """Один канал → (LLMResponse | requests.Response)."""
    if chan == PROVIDER_ALT:
        return alt_call()                  # OpenAI-совместимый прокси: ответ уже в нужной форме
    if chan == PROVIDER_OPENAI:
        return openai_call()               # тот же путь, что и всегда: сессия и ключ воркера
    if chan == PROVIDER_CODEX:
        model = codex_model()
        # json_schema is enforced by Codex natively.  Do not duplicate the
        # potentially large schema in the prompt; that wastes input tokens and
        # can make the CLI echo it into diagnostics.  json_object stays on the
        # legacy text directive because there is no schema file to pass.
        if not (
            isinstance(response_format, dict)
            and response_format.get("type") == "json_schema"
        ):
            messages = _with_directive(
                messages, _json_directive(response_format)
            )
        ok, out = call_codex_subscription(
            messages,
            model,
            timeout=timeout,
            response_format=response_format,
        )
    else:
        model = claude_model()
        messages = _with_directive(
            messages, _json_directive(response_format)
        )
        ok, out = call_claude_subscription(messages, model, timeout=timeout)
    if ok:
        return LLMResponse(200, _openai_shaped(_extract_json(out) if response_format else out, model))
    low = (out or "").lower()
    if any(m in low for m in _LIMIT_MARKERS):
        code = 429
    elif any(m in low for m in _AUTH_MARKERS):
        code = 401
    else:
        code = 502
    return LLMResponse(code, None, text=out or "неизвестная ошибка подписки")


def post(payload: Dict, timeout: Optional[float] = None, openai_call=None,
         alt_call=None) -> LLMResponse:
    """Отправить OpenAI-payload выбранным каналом; если канал не работает — следующим по цепочке.

    Коды: 200 ок · 429 лимит (воркер подождёт и повторит) · 401 нет/битая авторизация
    (воркер сделает loud-fail, чтобы не рисовать по мусорным промптам) · 502 прочее (ретрай).
    openai_call — замыкание воркера на обычный OpenAI-запрос (последнее звено цепочки).
    """
    response_format = payload.get("response_format")
    # Channel-specific formatting happens in _call_channel.  Codex receives a
    # native --output-schema, Claude keeps the textual directive, while the
    # OpenAI-compatible closures continue to own their untouched payload.
    messages = list(payload.get("messages") or [])

    primary = provider()
    chain = [primary] if not fallback_enabled() else \
        [primary] + [c for c in _FALLBACK_ORDER if c != primary]

    last = None
    transient_last = None
    tried, skipped, dead_now = [], [], []
    for chan in chain:
        if _is_dead(chan):
            # В provider-locked задаче внешний scheduler владеет durable backoff.
            # Не превращаем локальный timeout/429, который только что поставил
            # in-process dead marker, в синтетический 401 на следующей попытке.
            # Для fallback-chain marker по-прежнему исключает больной канал.
            if len(chain) > 1:
                skipped.append(f"{chan}: помечен нерабочим (ошибка в последние {_DEAD_TTL}с)")
                continue
        if not _channel_ready(chan, openai_call, alt_call):
            skipped.append({
                PROVIDER_OPENAI: "openai: нет ключа OpenAI",
                PROVIDER_CODEX: "codex: не найден codex CLI",
                PROVIDER_CLAUDE: "claude: нет OAuth-токена или claude-code-sdk",
                PROVIDER_ALT: "altclaude: не задан адрес/ключ альтернативного API",
            }[chan])
            continue
        tried.append(chan)
        resp = _call_channel(chan, messages, response_format, timeout, openai_call, alt_call)
        if getattr(resp, "status_code", 0) == 200:
            _strike_reset(chan)
            if chan != primary:
                _say(f"↩️ Промпты пишет запасной канал «{chan}» (основной «{primary}» недоступен)")
            return resp
        last = resp
        if resp.status_code == 401:               # авторизация терминальна → на следующий
            _mark_dead(chan, getattr(resp, "text", "") or str(resp.status_code))
            dead_now.append(chan)
        elif resp.status_code == 429:
            # Лимит/троттл сохраняет свой transient HTTP-класс. Marker нужен серверу
            # для парковки credential, но не имеет права переписать ответ в fake 401.
            transient_last = resp
            _mark_dead(chan, getattr(resp, "text", "") or str(resp.status_code))
            dead_now.append(chan)
        else:
            transient_last = resp
            # 502 без причины: канал систематически не отвечает. Раньше это не
            # помечало его мёртвым, и сотни сцен долбились в него по 20 попыток.
            if _strike(chan, getattr(resp, "text", "") or "ошибка без причины"):
                dead_now.append(chan)
            elif len(chain) > 1:
                _say(f"↪️ Канал «{chan}» ответил ошибкой — пробую следующий")
    if last is not None:
        # Transport/timeout/429 остаются transient даже если strike/dead marker
        # сработал на этой же попытке. Воркера вернёт EX_TEMPFAIL=75, а bounded
        # durable retry выполнит сервер — без подмены ошибки авторизацией.
        if transient_last is not None:
            return transient_last
        # Все опробованные каналы признаны нерабочими → это не «сетевая заминка»,
        # а «писать промпты нечем». Отдаём 401, чтобы воркер упал громко и сразу,
        # а не крутил по 20 ретраев на каждой из сотен частей.
        if tried and len(dead_now) == len(tried):
            why = "; ".join(f"{c}: {_dead_reason.get(c) or 'не отвечает'}" for c in tried)
            return LLMResponse(401, None,
                               text=("нечем писать промпты — все каналы недоступны. " + why
                                     + ". Проверь подписки в кабинете: Ключи → Чем писать промпты."))
        return last
    return LLMResponse(401, None,
                       text=("нечем писать промпты — ни один канал не доступен. "
                             + ("; ".join(skipped) if skipped else "каналы не настроены")
                             + ". Впиши ключ OpenAI, OAuth-токен Claude в кабинете (Ключи → Чем писать "
                               "промпты) или проверь авторизацию Codex на сервере."))


def preflight() -> Tuple[bool, str]:
    """Готов ли выбранный канал (вызывается до расхода кредитов на картинки)."""
    prov = provider()
    if prov == PROVIDER_ALT:
        if not alt_configured():
            return False, ("Выбран альтернативный Claude API, но не задан адрес или ключ "
                           "(Ключи → Чем писать промпты).")
        return True, ""
    if prov == PROVIDER_OPENAI:
        return True, ""
    if prov == PROVIDER_CODEX:
        bin_setting = (os.environ.get("VS_CODEX_BIN") or os.environ.get("REWRITE_CODEX_BIN") or "codex")
        if not (shutil.which(bin_setting) or os.path.isfile(bin_setting)):
            return False, ("Выбрана подписка ChatGPT, но codex CLI не найден на сервере "
                           "(поставь codex или задай VS_CODEX_BIN).")
        home = (os.environ.get("VS_CODEX_HOME") or os.environ.get("REWRITE_CODEX_HOME") or "").strip()
        if home and not os.path.isfile(os.path.join(home, "auth.json")):
            return False, f"Выбрана подписка ChatGPT, но в {home} нет auth.json (codex не авторизован)."
        return True, ""
    try:
        import claude_code_sdk  # noqa: F401
        import anyio            # noqa: F401
    except ImportError:
        return False, ("Выбрана подписка Claude, но claude-code-sdk не установлен "
                       "(pip install claude-code-sdk anyio).")
    if not claude_token():
        return False, ("Выбрана подписка Claude, но OAuth-токен не задан. Получи его командой "
                       "`claude setup-token` и впиши в кабинете: Ключи → Чем писать промпты.")
    return True, ""
