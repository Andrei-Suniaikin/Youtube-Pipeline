# -*- coding: utf-8 -*-
"""
Rewrite Core - бизнес-логика генерации и рерайта.
Без tkinter. Используется GUI и сервером.
"""

import os
import sys
import json
import re
import math
import threading
import queue
import time
import subprocess
import shutil
import stat
import tempfile
import hashlib
import sqlite3
sys.path.insert(0, os.environ.get("VIDEO_STUDIO_SHARED_DIR", "/opt/video-studio-shared"))
import credential_budget as _host_budget
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable, List, Dict, Tuple, Optional, Set, Union, Callable
from dataclasses import dataclass, asdict
from pydantic import BaseModel, Field
from enum import Enum
import random
import unicodedata
import urllib.request
import urllib.error
from contextlib import closing, contextmanager, nullcontext
from functools import wraps

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows fallback for local tooling
    _fcntl = None

try:
    import anthropic
    import httpx
    ANTHROPIC_AVAILABLE = True
except ImportError:
    anthropic = None
    httpx = None
    ANTHROPIC_AVAILABLE = False
try:
    import anyio
    from claude_code_sdk import query, ClaudeCodeOptions
    CLAUDE_CODE_SDK_AVAILABLE = True
except ImportError:
    CLAUDE_CODE_SDK_AVAILABLE = False
try:
    import docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False
try:
    import pypdf
    PYPDF_AVAILABLE = True
except ImportError:
    try:
        import PyPDF2 as pypdf
        PYPDF_AVAILABLE = True
    except ImportError:
        PYPDF_AVAILABLE = False
try:
    from odf import text, teletype
    from odf.opendocument import load as load_odt
    ODT_AVAILABLE = True
except ImportError:
    ODT_AVAILABLE = False
try:
    from striprtf.striprtf import rtf_to_text
    RTF_AVAILABLE = True
except ImportError:
    RTF_AVAILABLE = False

# ============== НАСТРОЙКИ ==============

# API Keys
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def _parse_server_secret_list(raw: str) -> List[str]:
    """Parse a JSON array or comma/newline-separated server-only secret list."""
    value = (raw or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("malformed JSON server credential list") from exc
        if not isinstance(parsed, list):
            raise ValueError("server credential JSON must be an array")
        if any(not isinstance(item, str) for item in parsed):
            raise ValueError("server credential JSON items must be strings")
        candidates = parsed
    else:
        candidates = re.split(r"[\r\n,]+", value)
    result: List[str] = []
    seen = set()
    for candidate in candidates:
        secret = str(candidate or "").strip()
        if secret and secret not in seen:
            seen.add(secret)
            result.append(secret)
    return result


def get_server_openai_keys() -> List[str]:
    """Unique shared OpenAI credentials from server environment only.

    ``OPENAI_API_KEYS`` accepts a JSON array or CSV/newline list. The historical
    scalar ``OPENAI_API_KEY`` remains compatible and is appended if distinct.
    """
    values = _parse_server_secret_list(os.environ.get("OPENAI_API_KEYS", ""))
    scalar = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if scalar and scalar not in values:
        values.append(scalar)
    return values


def get_server_openai_key() -> str:
    """Scalar compatibility accessor; callers should lease from the pool."""
    values = get_server_openai_keys()
    return values[0] if values else ""


def _normalize_oauth_tokens(values: Iterable[Any]) -> List[str]:
    """Strip whitespace/``Bearer`` prefix and dedupe OAuth tokens."""
    normalized: List[str] = []
    for token in values:
        token = "".join(str(token or "").split())
        if token.lower().startswith("bearer"):
            token = token[6:]
        if token and token not in normalized:
            normalized.append(token)
    return normalized


def get_server_claude_oauth_tokens() -> List[str]:
    """Unique server-owned Claude OAuth credentials (shared pool).

    Per-user DB values are deliberately not consulted here: this accessor is
    the *server* pool. BYOK-2026-07-30: the tenant's own tokens are resolved
    separately by :func:`get_user_claude_oauth_tokens` and take priority — see
    :func:`get_effective_claude_oauth_tokens`.
    """
    raw = (
        os.environ.get("REWRITE_CLAUDE_OAUTH_TOKENS")
        or os.environ.get("CLAUDE_CODE_OAUTH_TOKENS")
        or ""
    )
    values = _parse_server_secret_list(raw)
    scalar = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if scalar and scalar not in values:
        values.append(scalar)
    # 22.09.2026: и те, что администратор добавил на экране «Ключи».
    return _normalize_oauth_tokens(values + _managed_subscriptions("claude"))


def get_server_anthropic_keys() -> List[str]:
    """Unique server-owned Anthropic API keys from the environment only.

    BYOK-2026-07-30 (задача A): Anthropic API-ключ — полноценный вид credential
    наравне с OpenAI-ключом и Claude OAuth-токеном. Серверное значение остаётся
    владельческим: раздаётся только владельцам серверной подписки
    (:func:`server_credential_owner`), см. :func:`get_effective_anthropic_keys`.
    """
    values = _parse_server_secret_list(os.environ.get("ANTHROPIC_API_KEYS", ""))
    scalar = (os.environ.get("ANTHROPIC_API_KEY") or ANTHROPIC_API_KEY or "").strip()
    if scalar and scalar not in values:
        values.append(scalar)
    return values


# ============== BYOK: свой ключ юзера приоритетнее серверного ==============
# BYOK-2026-07-30. Требование владельца: «пусть принудительно не будет, а будет
# настройка у всех, чтобы была возможность свой вписать».
#   1) есть свой ключ провайдера у юзера → работаем на ЕГО ключе;
#   2) своего нет → серверный (общий) пул, как раньше;
#   3) возможность вписать свой есть у ВСЕХ, не только у админа.
# Серверные значения при этом наружу не отдаются никогда, а свой ключ юзера
# живёт в памяти процесса на время вызова: в job JSON / job-БД / HTTP-ответы он
# не попадает, а из логов и результатов провайдера вычищается тем же
# redact_server_credentials (user-пулы учтены в _server_secret_values).


def server_credential_owner(user_id: str = "") -> bool:
    """True когда ``user_id`` вправе работать на СЕРВЕРНОЙ подписке владельца.

    BYOK-2026-07-30 (задача B). Серверная подписка владельца (его codex-home,
    его Anthropic-ключ) — личный credential, а не общий ресурс сервиса:
    доставаться другому тенанту она не должна. Владельцем считается админ-тенант
    и, опционально, явно перечисленные в ``REWRITE_SERVER_SUBSCRIPTION_USER_IDS``
    аккаунты владельца. Всем остальным остаётся BYOK — свой credential.
    """
    uid = _resolve_user_id(user_id)
    if uid in ADMIN_USER_IDS:
        return True
    allowed = _allowed_values("REWRITE_SERVER_SUBSCRIPTION_USER_IDS", "")
    # 04.09.2026: список поимённый, и новый партнёр в него не попадал — доступ к
    # серверной подписке молча не выдавался, пока кто-то не вспомнит дописать id.
    # "*" = «всем аккаунтам сервиса», решение владельца; пустой список по-прежнему
    # означает «только админ», поведение без звёздочки не меняется.
    return "*" in allowed or uid in allowed


def own_credentials_only(user_id: str = "") -> bool:
    """Per-owner fail-closed policy; old accounts retain server fallback."""
    uid = _resolve_user_id(user_id)
    forced = _allowed_values("REWRITE_OWN_ONLY_USER_IDS", "")
    if "*" in forced or uid in forced:
        return True
    return get_keys_config(uid, strict=True).get("credential_mode") == "own_only"


def get_user_openai_keys(user_id: str = "") -> List[str]:
    """Own OpenAI credentials of this tenant; never server ENV values.

    FIX-2026-07-30 (P1 №8): сбой чтения хранилища больше не превращается в
    «своего ключа нет» — если BYOK у тенанта был, поднимаем
    :class:`KeysConfigUnavailable` вместо тихого отката на серверный ключ.
    """
    try:
        raw = (get_keys_config(user_id, strict=True) or {}).get("openai_api_key") or ""
    except KeysConfigUnavailable as exc:
        _byok_fallback_or_raise(user_id, "openai", exc)
        return []
    result: List[str] = []
    for candidate in re.split(r"[\r\n,]+", str(raw)):
        secret = candidate.strip()
        if secret and secret not in result:
            result.append(secret)
    # Свой ключ регистрируется в редакторе секретов — вычищается из логов и
    # результатов провайдера ровно так же, как серверный.
    _register_server_secret_tree(result)
    _remember_byok(_resolve_user_id(user_id), "openai", bool(result))
    return result


def get_user_claude_oauth_tokens(user_id: str = "") -> List[str]:
    """Own Claude OAuth tokens of this tenant; never server ENV values."""
    try:
        cfg = get_keys_config(user_id, strict=True) or {}
    except KeysConfigUnavailable as exc:
        _byok_fallback_or_raise(user_id, "claude", exc)
        return []
    raw = cfg.get("oauth_tokens") or []
    if isinstance(raw, str):
        raw = [raw]
    tokens = _normalize_oauth_tokens(raw)
    _register_server_secret_tree(tokens)
    _remember_byok(_resolve_user_id(user_id), "claude", bool(tokens))
    return tokens


def get_user_anthropic_keys(user_id: str = "") -> List[str]:
    """Own Anthropic API keys of this tenant; never server ENV values."""
    try:
        raw = (
            get_keys_config(user_id, strict=True) or {}
        ).get("anthropic_api_key") or ""
    except KeysConfigUnavailable as exc:
        _byok_fallback_or_raise(user_id, "anthropic", exc)
        return []
    result: List[str] = []
    for candidate in re.split(r"[\r\n,]+", str(raw)):
        secret = candidate.strip()
        if secret and secret not in result:
            result.append(secret)
    _register_server_secret_tree(result)
    _remember_byok(_resolve_user_id(user_id), "anthropic", bool(result))
    return result


def get_effective_anthropic_keys(user_id: str = "") -> List[str]:
    """BYOK-first Anthropic keys. Серверный ключ — только владельцу.

    BYOK-2026-07-30 (задачи A+B): свой ключ тенанта приоритетнее; серверный
    (ENV) Anthropic-ключ — владельческий credential и не выдаётся другим
    тенантам (историческое поведение :func:`env_anthropic_key_for` сохранено,
    но теперь его нельзя обойти через ``keys_cfg`` fallback в воркере).
    """
    own = get_user_anthropic_keys(user_id)
    if own:
        return own
    if not own_credentials_only(user_id) and server_credential_owner(user_id):
        return get_server_anthropic_keys()
    return []



# ── раздача ключей по кругу ───────────────────────────────────────────────────
# Ключей у провайдера может быть несколько (вписываются через запятую или с новой
# строки). Раньше список читался, но вызов всегда брал ПЕРВЫЙ — второй ключ не давал
# ничего: ни ёмкости, ни запаса при исчерпании лимита. Раздаём по очереди, чтобы
# нагрузка и расход делились поровну.
_KEY_RR_LOCK = threading.Lock()
_KEY_RR_POS: Dict[str, int] = {}


def _pick_round_robin(values: List[str], family: str) -> str:
    """Следующий ключ семейства по кругу. Пустой список — пустая строка."""
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    with _KEY_RR_LOCK:
        pos = _KEY_RR_POS.get(family, -1) + 1
        if pos >= len(values):
            pos = 0
        _KEY_RR_POS[family] = pos
    return values[pos]


def get_effective_anthropic_key(user_id: str = "") -> str:
    """Очередной ключ Anthropic из набора тенанта (по кругу)."""
    return _pick_round_robin(get_effective_anthropic_keys(user_id), f"anthropic:{user_id}")


def get_effective_openai_keys(user_id: str = "") -> List[str]:
    """BYOK-first: own tenant keys, otherwise the shared server pool."""
    own = get_user_openai_keys(user_id)
    return own or ([] if own_credentials_only(user_id) else get_server_openai_keys())


def get_effective_openai_key(user_id: str = "") -> str:
    """Очередной ключ OpenAI из набора тенанта (по кругу)."""
    return _pick_round_robin(get_effective_openai_keys(user_id), f"openai:{user_id}")


def get_effective_claude_oauth_tokens(user_id: str = "") -> List[str]:
    """BYOK-first: own tenant tokens, otherwise the shared server pool."""
    own = get_user_claude_oauth_tokens(user_id)
    return own or ([] if own_credentials_only(user_id) else get_server_claude_oauth_tokens())


def get_claude_admission_credential(user_id: str = "") -> Optional[List[str]]:
    """Какой credential выигрывает для claude-модели. Один порядок на все места.

    FIX-2026-07-30 (P1 №2): раньше admission сначала звал
    :func:`get_effective_claude_oauth_tokens` — а он при отсутствии СВОИХ токенов
    отдаёт СЕРВЕРНЫЙ пул. Из-за этого свой Anthropic API-ключ тенанта проигрывал
    серверной подписке владельца: задача уходила на чужую подписку, хотя у юзера
    был свой оплаченный ключ. Порядок теперь честный «своё важнее серверного»:

        свой OAuth → свой Anthropic-ключ → серверный OAuth → серверный Anthropic.

    Возвращает:
      * непустой список — работаем подпиской (OAuth), это его токены;
      * ``[]``          — OAuth не выигрывает, идём прямым Anthropic API-ключом;
      * ``None``        — credential'а нет вовсе (admission обязан отказать).
    """
    own_oauth = get_user_claude_oauth_tokens(user_id)
    if own_oauth:
        return own_oauth
    if get_user_anthropic_keys(user_id):
        return []
    if own_credentials_only(user_id):
        return None
    server_oauth = get_server_claude_oauth_tokens()
    if server_oauth:
        return server_oauth
    if server_credential_owner(user_id) and get_server_anthropic_keys():
        return []
    return None


def credential_source(provider: str, user_id: str = "") -> str:
    """``user`` / ``server`` / ``none`` — for honest logs and admission errors."""
    provider = (provider or "").strip().lower()
    if provider == "openai":
        if get_user_openai_keys(user_id):
            return "user"
        return "server" if not own_credentials_only(user_id) and get_server_openai_keys() else "none"
    if provider == "claude":
        if get_user_claude_oauth_tokens(user_id):
            return "user"
        # FIX-2026-07-30 (P1 №2): свой Anthropic-ключ проверяется ДО серверного
        # OAuth-пула — иначе источник назывался "server", хотя реально работает
        # свой ключ тенанта (и лог/кабинет врали про подписку владельца).
        if get_user_anthropic_keys(user_id):
            return "user"
        if not own_credentials_only(user_id) and get_server_claude_oauth_tokens():
            return "server"
        # Своего/серверного OAuth нет — Claude ещё доступен по API-ключу.
        return credential_source("anthropic", user_id)
    if provider == "anthropic":
        if get_user_anthropic_keys(user_id):
            return "user"
        if not own_credentials_only(user_id) and server_credential_owner(user_id) and get_server_anthropic_keys():
            return "server"
        return "none"
    if provider == "codex":
        # BYOK-2026-07-30: своя подписка тенанта (его auth.json) приоритетна;
        # серверная подписка владельца — только владельцу (задача B).
        if get_user_codex_auth(user_id):
            return "user"
        if not own_credentials_only(user_id) and server_credential_owner(user_id) and get_server_codex_homes():
            return "server"
        return "none"
    return "none"


def has_provider_credential(provider: str, user_id: str = "") -> bool:
    """True when the job can run: either the tenant's own key or a server one."""
    return credential_source(provider, user_id) != "none"


def get_server_codex_homes() -> List[str]:
    """Unique server-owned CODEX_HOME directories with persistent auth."""
    values = _parse_server_secret_list(os.environ.get("REWRITE_CODEX_HOMES", ""))
    scalar = (os.environ.get("REWRITE_CODEX_HOME") or "").strip()
    if scalar and scalar not in values:
        values.append(scalar)
    # 22.09.2026: и те, что администратор добавил на экране «Ключи».
    for home in _managed_subscriptions("codex"):
        if home not in values:
            values.append(home)
    if not values:
        default_home = os.path.join(os.path.expanduser("~"), ".codex")
        if os.path.isfile(os.path.join(default_home, "auth.json")):
            values.append(default_home)
    return values


def validate_codex_auth_document(payload: Any) -> None:
    """Reject Codex subscription credentials that cannot refresh themselves."""
    if not isinstance(payload, dict):
        raise ValueError("auth.json must be a JSON object")
    if payload.get("auth_mode") != "chatgpt":
        raise ValueError("auth.json must use ChatGPT subscription login")
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError("auth.json must contain a tokens object")
    for field in ("access_token", "refresh_token"):
        if not isinstance(tokens.get(field), str) or not tokens[field].strip():
            raise ValueError(f"auth.json must contain tokens.{field}")


# ---------- BYOK-2026-07-30: своя подписка Codex у тенанта ----------
# Codex авторизуется файлом ``auth.json`` внутри CODEX_HOME, поэтому «свой
# credential» здесь — это содержимое ЕГО auth.json (тенант вставляет его в
# настройках). Значение живёт в sessions.db.user_keys.codex_auth, наружу не
# отдаётся, в job/HTTP/лог не попадает и материализуется в приватный
# per-user CODEX_HOME (0700 / auth.json 0600) только на время вызова CLI.


_CODEX_SECRET_FIELD_NAMES = {
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "openai_api_key",
    "client_secret",
    "secret",
    "token",
}


def _codex_secret_strings(payload: str) -> List[str]:
    """Заведомо секретные значения из ``auth.json`` (а не всё дерево подряд).

    FIX-2026-07-30 (P3 №5): раньше в глобальный ``_REGISTERED_SERVER_SECRETS``
    уезжало ВСЁ дерево auth.json — timestamps, account_id, e-mail, любая строка
    от 8 символов. Реестр общий на процесс, растёт от каждого тенанта, и
    redact_server_credentials гоняет по нему ``str.replace`` для каждой строки
    лога. Регистрируем только сам payload и поля-секреты.
    """
    payload = str(payload or "")
    found: List[str] = [payload] if len(payload) >= 8 else []
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return found

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                is_secret_name = str(key).strip().lower() in _CODEX_SECRET_FIELD_NAMES
                if isinstance(value, str):
                    if is_secret_name and len(value) >= 8:
                        found.append(value)
                else:
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(parsed)
    return found


def get_user_codex_auth(user_id: str = "", register_secrets: bool = False) -> str:
    """Own Codex ``auth.json`` payload of this tenant ("" когда не задан).

    FIX-2026-07-30 (P3 №5): регистрация в редакторе секретов включается ТОЛЬКО
    там, где значение реально уходит наружу процесса (материализация
    CODEX_HOME). Read-only пути (``credential_source``, ``/settings/keys``,
    ``/models``) больше не засоряют глобальный реестр на каждом запросе.
    """
    try:
        raw = (get_keys_config(user_id, strict=True) or {}).get("codex_auth") or ""
    except KeysConfigUnavailable as exc:
        # FIX-2026-07-30 (P1 №8): «не прочитал» ≠ «своей подписки нет».
        _byok_fallback_or_raise(user_id, "codex", exc)
        return ""
    value = str(raw).strip()
    if value and register_secrets:
        _register_server_secret_tree(_codex_secret_strings(value))
    _remember_byok(_resolve_user_id(user_id), "codex", bool(value))
    return value


def _codex_user_homes_root() -> str:
    """Постоянный приватный корень под per-user CODEX_HOME.

    FIX-2026-07-30 (P2 №3 + №4): по умолчанию корень лежал в общем
    ``tempfile.gettempdir()``. Отсюда две беды: (а) чистка ``/tmp`` уносила
    обновлённый самим Codex токен — тенант снова разлогинен; (б) общий каталог
    с предсказуемым именем в multi-user системе — чужие права и подмена
    симлинком. Теперь корень по умолчанию рядом с ``sessions.db`` (каталог
    данных приложения), а переопределяется только серверным ENV.
    """
    configured = (os.environ.get("REWRITE_CODEX_USER_HOMES_DIR") or "").strip()
    if configured:
        return configured
    return os.path.join(
        os.path.dirname(os.path.abspath(SESSIONS_DB_PATH)), ".codex-user-homes"
    )


def _ensure_private_dir(path: str) -> None:
    """Создать/проверить приватный каталог: реальный каталог, наш, права 0700.

    FIX-2026-07-30 (P2 №4): ``os.makedirs(exist_ok=True)`` молча принимал
    существующий симлинк/чужой каталог. Проверка через ``lstat`` (без
    разыменования) отсекает подмену.
    """
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"codex home path is not a real directory: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError(f"codex home path belongs to another user: {path}")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)


def _write_private_file(path: str, payload: str) -> None:
    """Атомарная запись 0600 без следования симлинкам (O_NOFOLLOW|O_EXCL)."""
    tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass
    descriptor = os.open(tmp_path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.replace(tmp_path, path)


def _read_private_file(path: str) -> str:
    """Прочитать файл, не следуя симлинку; отсутствие/симлинк → ""."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return ""
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        return handle.read()


# Кто владелец материализованного CODEX_HOME: путь → user_id. Нужен, чтобы
# обновлённый Codex'ом auth.json вернулся в БД тенанта (FIX-2026-07-30 P2 №3).
_CODEX_HOME_OWNERS: Dict[str, str] = {}
_CODEX_HOME_OWNERS_LOCK = threading.Lock()


def _codex_home_owner(home: str) -> str:
    if not home:
        return ""
    with _CODEX_HOME_OWNERS_LOCK:
        return _CODEX_HOME_OWNERS.get(os.path.realpath(home), "")


def _codex_payloads(raw: str) -> List[str]:
    if not raw:
        return []
    parsed = json.loads(raw)
    docs = parsed if isinstance(parsed, list) else [parsed]
    if len(docs) > 100:
        raise ValueError("at most 100 own Codex accounts are supported")
    result, seen = [], set()
    for doc in docs:
        validate_codex_auth_document(doc)
        if isinstance(parsed, list) and not ((doc.get("tokens") or {}).get("account_id") or doc.get("account_id")):
            raise ValueError("each Codex account in a pool requires account_id for stable refresh")
        payload = raw if not isinstance(parsed, list) else json.dumps(doc, ensure_ascii=False, sort_keys=True)
        identity = _codex_account_identity(payload)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(payload)
    return result


def _codex_account_identity(payload: str) -> str:
    doc = json.loads(payload)
    tokens = doc.get("tokens") or {}
    identity = tokens.get("account_id") or doc.get("account_id")
    # Older single-auth documents did not require account_id. Their refresh
    # token is a private fallback identity, never exposed in responses/logs.
    identity = str(identity or tokens.get("refresh_token") or "")
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


_CODEX_HOME_AUTH_IDENTITIES: Dict[str, str] = {}


def get_user_codex_homes(user_id: str = "") -> List[str]:
    raw = get_user_codex_auth(user_id, register_secrets=True)
    if not raw:
        return []
    payloads = _codex_payloads(raw)
    uid = _resolve_user_id(user_id)
    digest = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]
    root = _codex_user_homes_root()
    homes = []
    legacy_object = isinstance(json.loads(raw), dict)
    for payload in payloads:
        identity = _codex_account_identity(payload)
        # Preserve the existing home for a legacy object and resume tokens.
        home = os.path.join(root, digest if legacy_object else digest + "-" + identity)
        auth_path = os.path.join(home, "auth.json")
        stamp_path = os.path.join(home, ".rewrite_source_sha256")
        source_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        try:
            parent = os.path.dirname(os.path.abspath(root))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, mode=0o700, exist_ok=True)
            _ensure_private_dir(root)
            _ensure_private_dir(home)
            current = _read_private_file(stamp_path).strip()
            if current != source_hash or not os.path.isfile(auth_path):
                _write_private_file(auth_path, payload)
                _write_private_file(stamp_path, source_hash)
        except OSError as exc:
            raise RuntimeError(f"cannot materialize own Codex auth: {exc}") from exc
        with _CODEX_HOME_OWNERS_LOCK:
            canonical = os.path.realpath(home)
            _CODEX_HOME_OWNERS[canonical] = uid
            _CODEX_HOME_AUTH_IDENTITIES[canonical] = identity
        homes.append(home)
    return homes



# ── Общий семафор лимита подписки ─────────────────────────────────────────────
# Подписка одна на все сервисы машины (rewrite-server + vsgen). Кто заметил
# исчерпанное окно, тот записал время сброса в /var/lib/claude_limits/state.json,
# а видят все — иначе один ждёт, второй в это время долбит тот же токен.
# Модуль намеренно необязательный: нет его — работаем как раньше, только вслепую.
# 22.09.2026: подписки для текста, которые администратор добавляет на экране «Ключи»
# (без SSH и перезапуска). Модуль лежит рядом; нет его — пул живёт одним ENV, как раньше.
try:
    import subscriptions as _text_subs
except ImportError:                                 # noqa: BLE001
    _text_subs = None


def _managed_subscriptions(provider: str) -> List[str]:
    if _text_subs is None:
        return []
    try:
        return (_text_subs.codex_homes() if provider == "codex"
                else _text_subs.claude_tokens())
    except Exception:                               # noqa: BLE001
        return []


def _note_subscription(provider: str, value: str, outcome: str,
                       until: float = 0.0, detail: str = "") -> None:
    """Чем кончился вызов подпиской — в её состояние на экране «Ключи» и в тревоги."""
    if _text_subs is not None and value:
        _text_subs.note(provider, value, outcome, until=until, detail=detail)


_CODEX_REL_HOURS = re.compile(r"(\d+)\s*(?:h\b|hr\b|hours?\b|час)", re.I)
_CODEX_REL_MINUTES = re.compile(r"(\d+)\s*(?:m\b|min\b|minutes?\b|мин)", re.I)


def _codex_limit_reset_from_text(text) -> float:
    """«try again in 2h 15m», «resets in 45 minutes» → unix-время сброса; 0 — не нашлось."""
    t = str(text or "")
    if not re.search(r"\b(?:in|after|через)\b", t, re.I):
        return 0.0
    h = _CODEX_REL_HOURS.search(t)
    m = _CODEX_REL_MINUTES.search(t)
    secs = (int(h.group(1)) * 3600 if h else 0) + (int(m.group(1)) * 60 if m else 0)
    return time.time() + secs if secs > 0 else 0.0


def _codex_limit_to_cooldown(pool, codex_home: str, result):
    """CODEX_LIMIT → дом отдыхает до сброса, ответ получает префикс SUBSCRIPTION_LIMIT.

    03.10.2026, девять выпусков «рерайт не справился: 0 файлов». Окно подписки дома
    Codex кончилось, но отказ только записывался в состояние ключа: дом не гасился,
    пул выдавал его снова, кусок падал за шесть секунд, три круга авто-повтора по
    двадцать секунд — и задача с пятью готовыми кусками из шести хоронилась как
    провал, хотя речь шла о паузе до сброса окна. Теперь, как у Claude (mark_limited
    в call_api_universal): этот дом не выдаётся до сброса (живой сосед по пулу
    продолжает раздаваться), а ответ помечается SUBSCRIPTION_LIMIT — по этому
    префиксу remember_limit_hit откладывает джоб до сброса, а не роняет его.
    Время сброса: из ответа, если он его называет; иначе _LIMIT_DEFAULT_WAIT_SEC —
    первый успешный вызов снимет кулдаун раньше (clear_limit).
    """
    ok = bool(result[0])
    text = str(result[-1] or "")
    if ok:
        try:
            pool.clear_limit(codex_home)
        except Exception:                                   # noqa: BLE001
            pass
        return result
    if not text.startswith("CODEX_LIMIT"):
        return result
    reset_ts = (parse_limit_reset(text) or _codex_limit_reset_from_text(text)
                or time.time() + _LIMIT_DEFAULT_WAIT_SEC)
    try:
        pool.mark_limited(codex_home, reset_ts)
    except Exception:                                       # noqa: BLE001
        pass
    marked = f"{SUBSCRIPTION_LIMIT_PREFIX}reset={int(reset_ts)}|{text}"
    return tuple(result[:-1]) + (marked,)


def _note_codex_result(codex_home: str, ok: bool, message: Any) -> None:
    text = str(message or "")
    if ok:
        _note_subscription("codex", codex_home, "ok")
    elif text.startswith("CODEX_AUTH"):
        _note_subscription("codex", codex_home, "auth", detail=text)
    elif text.startswith("CODEX_LIMIT"):
        _note_subscription("codex", codex_home, "limit")


try:
    import claude_limits as _shared_limits
except Exception:                                   # noqa: BLE001
    _shared_limits = None


def _shared_limit_until(value) -> float:
    """До какого времени ключ спит по мнению общего семафора (0 — свободен)."""
    if not _shared_limits or not value:
        return 0.0
    try:
        return float(_shared_limits.limited_until(value) or 0.0)
    except Exception:                               # noqa: BLE001
        return 0.0


def _shared_limit_state(value) -> dict:
    if not _shared_limits or not value:
        return {}
    try:
        return dict(_shared_limits.limited_state(value) or {})
    except Exception:                               # noqa: BLE001
        return {}


def _shared_limit_mark(value, until_ts, kind="limit", who="", why="") -> None:
    if not _shared_limits or not value:
        return
    try:
        _shared_limits.mark_limited(value, until_ts, kind=kind, who=who, why=why)
    except Exception:                               # noqa: BLE001
        pass


def _shared_limit_clear(value, who="") -> None:
    if not _shared_limits or not value:
        return
    try:
        _shared_limits.clear(value, who=who)
    except Exception:                               # noqa: BLE001
        pass



class CredentialPoolExhausted(RuntimeError):
    pass


@dataclass
class _CredentialEntry:
    value: str
    in_use: int = 0
    cooldown_until: float = 0.0     # ключ упёрся в окно подписки и спит до сброса


# One process-wide lock makes duplicate physical credentials obey one limit,
# even when configured by more than one owner. Values remain private to pools.
_PHYSICAL_CREDENTIAL_CONDITION = threading.Condition(threading.RLock())
_PHYSICAL_CREDENTIAL_IN_USE = {}
_PHYSICAL_CREDENTIAL_LIMITS = {}


def _physical_credential_id(provider, value):
    identity = globals().get("_CODEX_HOME_AUTH_IDENTITIES", {}).get(value, value) if provider == "codex" else value
    return provider + ":" + hashlib.sha256(identity.encode()).hexdigest()


class CredentialLease:
    """Opaque lease. Its repr never contains the credential."""

    __slots__ = ("_pool", "_entry", "_physical_id", "_value", "_released", "_host_lease")

    def __init__(self, pool: "ServerCredentialPool", entry: _CredentialEntry, value: str, host_lease=None):
        self._pool = pool
        self._entry = entry
        self._physical_id = _physical_credential_id(pool.provider, value)
        self._value = value
        self._released = False
        self._host_lease = host_lease

    def __repr__(self) -> str:
        return "<CredentialLease redacted>"

    @property
    def value(self) -> str:
        return self._value

    def release(self) -> None:
        self._pool._release(self)

    def __enter__(self) -> str:
        return self._value

    def __exit__(self, *_args) -> bool:
        self.release()
        return False


class ServerCredentialPool:
    """Thread-safe least-loaded/RR pool with an exact per-credential limit."""

    def __init__(
        self,
        provider: str,
        credentials: Optional[List[str]] = None,
        per_credential_limit: int = 1,
    ):
        self.provider = provider
        self._condition = _PHYSICAL_CREDENTIAL_CONDITION
        self._entries: List[_CredentialEntry] = []
        self._retired_entries: List[_CredentialEntry] = []
        self._physical_ids = set()
        self._cursor = -1
        self._per_credential_limit = max(1, int(per_credential_limit))
        self.configure(credentials or [], self._per_credential_limit)

    def configure(
        self, credentials: List[str], per_credential_limit: Optional[int] = None
    ) -> None:
        unique: List[str] = []
        for value in credentials:
            value = str(value or "").strip()
            if value and value not in unique:
                unique.append(value)
        with self._condition:
            limit = (
                self._per_credential_limit
                if per_credential_limit is None
                else max(1, int(per_credential_limit))
            )
            if (
                [entry.value for entry in self._entries] == unique
                and limit == self._per_credential_limit
            ):
                return
            old = {entry.value: entry for entry in self._entries}
            self._retired_entries = [entry for entry in self._retired_entries if entry.in_use]
            self._retired_entries.extend(entry for entry in self._entries if entry.value not in unique and entry.in_use)
            self._entries = [old.get(value) or _CredentialEntry(value=value) for value in unique]
            for identity in self._physical_ids:
                registrations = _PHYSICAL_CREDENTIAL_LIMITS.get(identity, {})
                registrations.pop(id(self), None)
                if not registrations:
                    _PHYSICAL_CREDENTIAL_LIMITS.pop(identity, None)
            self._physical_ids = {_physical_credential_id(self.provider, value) for value in unique}
            for identity in self._physical_ids:
                _PHYSICAL_CREDENTIAL_LIMITS.setdefault(identity, {})[id(self)] = limit
            self._per_credential_limit = limit
            self._cursor = -1
            self._condition.notify_all()

    def acquire(self, timeout: Optional[float] = None) -> CredentialLease:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        host_deadline = time.monotonic() + 2.0
        host_blocked = False
        event = getattr(globals().get("STOP_FLAG", None), "_thread_local", None)
        job_id = getattr(getattr(event, "stop_event", None), "_rewrite_job_id", "")
        with self._condition:
            while True:
                now_ts = time.time()
                if job_id and host_budget_wait_until(job_id):
                    raise CredentialPoolExhausted(HOST_BUDGET_WAIT_PREFIX + "общий лимит ключа занят")
                if globals().get("STOP_FLAG") and STOP_FLAG.is_stopped():
                    raise CredentialPoolExhausted("Остановлено во время ожидания ключа до вызова провайдера")
                eligible = [
                    index
                    for index, entry in enumerate(self._entries)
                    if entry.in_use < self._per_credential_limit
                    and _PHYSICAL_CREDENTIAL_IN_USE.get(_physical_credential_id(self.provider, entry.value), 0)
                        < min(_PHYSICAL_CREDENTIAL_LIMITS.get(_physical_credential_id(self.provider, entry.value), {id(self): self._per_credential_limit}).values())
                    and entry.cooldown_until <= now_ts
                    and _shared_limit_until(entry.value) <= now_ts
                ]
                if eligible:
                    count = len(self._entries)
                    ordered = sorted(eligible, key=lambda i: (self._entries[i].in_use, (i-self._cursor-1)%count))
                    for chosen in ordered:
                        entry = self._entries[chosen]
                        family = {"openai":"openai", "claude":"claude_oauth", "codex":"codex"}.get(self.provider)
                        reserve = short_call_reserve(family, job_id)
                        host = ((_host_budget.try_acquire_leaving(family, entry.value, reserve)
                                 if reserve else _host_budget.try_acquire(family, entry.value, 1))
                                if family else _host_budget.Lease())
                        if host is None:
                            host_blocked = True
                            continue
                        identity = _physical_credential_id(self.provider, entry.value)
                        entry.in_use += 1
                        _PHYSICAL_CREDENTIAL_IN_USE[identity] = _PHYSICAL_CREDENTIAL_IN_USE.get(identity, 0) + 1
                        self._cursor = chosen
                        if job_id:
                            forget_host_budget_streak(job_id)   # ключ дали: серия возвратов прервана
                        return CredentialLease(self, entry, entry.value, host)
                if host_blocked and time.monotonic() >= host_deadline:
                    remember_host_budget_wait(job_id)
                    raise CredentialPoolExhausted(HOST_BUDGET_WAIT_PREFIX + "общий лимит ключа занят; ожидание в очереди до вызова провайдера")
                if not self._entries:
                    raise CredentialPoolExhausted(
                        f"{self.provider} server credential pool is empty"
                    )
                cooling = [u for u in (max(e.cooldown_until, _shared_limit_until(e.value))
                                       for e in self._entries) if u > now_ts]
                if len(cooling) == len(self._entries):
                    # Спят ВСЕ ключи. Ждать здесь нельзя: acquire зовут с timeout=300,
                    # и соседние джобы повисли бы на пять минут, а потом упали с «lease
                    # timeout» — чужие задачи платили бы за наш лимит. Отвечаем сразу и
                    # с временем сброса: решение «ждать или падать» принимает вызывающий.
                    raise CredentialPoolExhausted(
                        f"{SUBSCRIPTION_LIMIT_PREFIX}reset={int(min(cooling))}|"
                        f"{self.provider}: все ключи отдыхают до сброса окна"
                    )
                remaining = (
                    None if deadline is None else max(0.0, deadline - time.monotonic())
                )
                if remaining == 0.0:
                    raise CredentialPoolExhausted(
                        f"{self.provider} credential lease timeout"
                    )
                self._condition.wait(min(0.1, remaining) if remaining is not None else 0.1)

    def _release(self, lease: CredentialLease) -> None:
        with self._condition:
            if lease._released:
                return
            lease._released = True
            if lease._host_lease is not None:
                lease._host_lease.release()
            lease._entry.in_use = max(0, lease._entry.in_use - 1)
            identity = lease._physical_id
            remaining = _PHYSICAL_CREDENTIAL_IN_USE.get(identity, 0) - 1
            if remaining > 0:
                _PHYSICAL_CREDENTIAL_IN_USE[identity] = remaining
            else:
                _PHYSICAL_CREDENTIAL_IN_USE.pop(identity, None)
            self._retired_entries = [entry for entry in self._retired_entries if entry.in_use]
            self._condition.notify_all()

    def mark_limited(self, value: str, until_ts: float) -> None:
        """Ключ упёрся в окно подписки — не выдавать его до until_ts.

        Гасим ИМЕННО ключ, а не пул: живой сосед продолжает раздаваться, и очередь
        не встаёт целиком из-за одного исчерпанного токена.
        """
        with self._condition:
            for entry in self._entries:
                if entry.value == value:
                    entry.cooldown_until = max(entry.cooldown_until, float(until_ts))
            self._condition.notify_all()
        # запись в общий файл — ПОСЛЕ отпускания замка: под ним файловый ввод-вывод
        # держал бы весь пул, а он на горячем пути
        _shared_limit_mark(value, until_ts, kind="limit", who="rewrite",
                           why="окно подписки исчерпано")

    def clear_limit(self, value: str) -> None:
        """Первый успешный вызов снимает кулдаун: окно сбросилось раньше расчётного."""
        with self._condition:
            for entry in self._entries:
                if entry.value == value and entry.cooldown_until:
                    entry.cooldown_until = 0.0
            self._condition.notify_all()
        _shared_limit_clear(value, who="rewrite")

    def cooldown_until(self) -> float:
        """Ближайшее время, когда в пуле снова появится живой ключ (0 — есть сейчас)."""
        with self._condition:
            now_ts = time.time()
            sleeping = [e.cooldown_until for e in self._entries if e.cooldown_until > now_ts]
            return min(sleeping) if len(sleeping) == len(self._entries) and sleeping else 0.0

    def stats(self) -> Dict[str, int]:
        with self._condition:
            return {
                "credentials": len(self._entries),
                "in_use": sum(entry.in_use for entry in self._entries + self._retired_entries),
                "capacity": len(self._entries) * self._per_credential_limit,
                "per_credential_limit": self._per_credential_limit,
            }


_SERVER_CREDENTIAL_POOLS: Dict[str, ServerCredentialPool] = {}
_SERVER_CREDENTIAL_POOLS_LOCK = threading.Lock()
_DEFAULT_CODEX_HOME = "__rewrite_default_codex_home__"


def _credential_limit(provider: str, owner: str = "") -> int:
    """Параллельность на ОДИН credential. У пользовательских пулов — свой лимит.

    FIX-2026-07-30 (P2 №9): серверный дефолт «1 вызов на ключ» рассчитан на пул
    из МНОГИХ ключей владельца. У тенанта со своим единственным ключом та же
    цифра давала ёмкость пула = 1: партнёр получал не «медленнее», а отказы по
    таймауту лизы (300 c). Лимит пользовательских пулов задаётся отдельно и
    только сервером (ENV) — запрос/клиент поднять его не может.
    """
    defaults = {"openai": 1, "claude": 3, "codex": 1, "anthropic": 1}
    names = {
        "anthropic": "REWRITE_ANTHROPIC_PER_KEY_CONCURRENCY",
        "openai": "REWRITE_OPENAI_PER_KEY_CONCURRENCY",
        "claude": "REWRITE_CLAUDE_PER_TOKEN_CONCURRENCY",
        "codex": "REWRITE_CODEX_PER_HOME_CONCURRENCY",
    }
    if owner:
        # Codex у СВОЕГО home остаётся 1: тенантский каталог материализуется под
        # его же auth.json, гонки там не окупаются. Серверный home с 04.09.2026
        # ходит параллельно — замок сужен до окна ротации, запись обратно под CAS
        # (см. _codex_refresh_possible и _sync_codex_auth_back).
        defaults = {"openai": 8, "claude": 3, "codex": 1, "anthropic": 1}
        names = {
            "anthropic": "REWRITE_USER_ANTHROPIC_PER_KEY_CONCURRENCY",
            "openai": "REWRITE_USER_OPENAI_PER_KEY_CONCURRENCY",
            "claude": "REWRITE_USER_CLAUDE_PER_TOKEN_CONCURRENCY",
            "codex": "REWRITE_USER_CODEX_PER_HOME_CONCURRENCY",
        }
    try:
        value = int(os.environ.get(names[provider], str(defaults[provider])))
    except (TypeError, ValueError):
        value = defaults[provider]
    return max(1, min(32, value))


def get_server_credential_pool(
    provider: str, candidates: Optional[List[str]] = None
) -> ServerCredentialPool:
    """Return a process-wide pool sourced exclusively from server settings.

    ``candidates`` is retained only for source compatibility with older
    callers.  It is intentionally ignored: a request/job argument must never
    be able to seed a shared credential pool.
    """
    provider = (provider or "").strip().lower()
    if provider == "openai":
        values = get_server_openai_keys()
    elif provider == "anthropic":
        values = get_server_anthropic_keys()
    elif provider == "claude":
        values = get_server_claude_oauth_tokens()
    elif provider == "codex":
        values = get_server_codex_homes()
    else:
        raise ValueError("unsupported provider")
    return _credential_pool_for(provider, "", values)


def _credential_pool_for(
    provider: str, owner: str, values: List[str]
) -> ServerCredentialPool:
    """Process-wide pool per (provider, owner). ``owner=''`` is the server pool.

    Tenant credentials get their own pool: a partner key must never land in the
    shared pool (cross-tenant leasing) nor share its concurrency accounting.
    """
    key = provider if not owner else f"{provider}\x00user\x00{owner}"
    with _SERVER_CREDENTIAL_POOLS_LOCK:
        pool = _SERVER_CREDENTIAL_POOLS.get(key)
        # FIX-2026-07-30 (P2 №9): у пользовательского пула свой лимит
        # параллельности на credential — серверный «1 на ключ» рассчитан на пул
        # из многих ключей и превращал один свой ключ в ёмкость 1.
        limit = _credential_limit(provider, owner)
        if pool is None:
            pool = ServerCredentialPool(
                provider, values, per_credential_limit=limit
            )
            _SERVER_CREDENTIAL_POOLS[key] = pool
        else:
            pool.configure(values, limit)
        return pool


def get_effective_credential_pool(
    provider: str, user_id: str = ""
) -> ServerCredentialPool:
    """BYOK-2026-07-30: lease from the tenant's own pool when it has one.

    ``candidates`` cannot be injected here either — values are read only from
    the tenant settings store or the server environment.
    """
    provider = (provider or "").strip().lower()
    uid = _resolve_user_id(user_id)
    if provider == "openai":
        own = get_user_openai_keys(uid)
    elif provider == "anthropic":
        own = get_user_anthropic_keys(uid)
    elif provider == "claude":
        own = get_user_claude_oauth_tokens(uid)
    elif provider == "codex":
        # BYOK-2026-07-30: своя подписка тенанта (его собственный auth.json).
        own = get_user_codex_homes(uid)
    else:
        raise ValueError("unsupported provider")
    if own:
        return _credential_pool_for(provider, uid, own)
    # Свой ключ удалён → гасим его пул, чтобы устаревший секрет не жил в памяти.
    with _SERVER_CREDENTIAL_POOLS_LOCK:
        stale = _SERVER_CREDENTIAL_POOLS.get(f"{provider}\x00user\x00{uid}")
    if stale is not None:
        stale.configure([], _credential_limit(provider, uid))
    if own_credentials_only(uid):
        raise CredentialPoolExhausted(f"Own-only mode: no own {provider} credential configured")
    if provider in {"codex", "anthropic"} and not server_credential_owner(uid):
        # Задача B: серверная подписка владельца (его codex-home) чужому тенанту
        # не достаётся. Fail closed с внятным текстом — не тихая работа на
        # кредах владельца.
        raise CredentialPoolExhausted(
            f"{provider} credential is not configured: add your own credential "
            "in settings (server credentials belong to the owner)"
        )
    return get_server_credential_pool(provider)


def credential_job_budget(provider: str, user_id: str = "") -> Tuple[str, int]:
    """Shared machine caps are separate from per-owner credential capacity."""
    uid = _resolve_user_id(user_id)
    source = credential_source(provider, uid)
    owner = uid if source == "user" else ""
    credential_provider = provider
    if provider == "openai":
        count = len(get_user_openai_keys(uid)) if owner else len(get_server_openai_keys())
    elif provider == "claude":
        if owner:
            oauth_count = len(get_user_claude_oauth_tokens(uid))
            count = oauth_count or len(get_user_anthropic_keys(uid))
        else:
            oauth_count = len(get_server_claude_oauth_tokens())
            count = oauth_count or len(get_server_anthropic_keys())
        if not oauth_count:
            credential_provider = "anthropic"
    elif provider == "codex":
        count = len(_codex_payloads(get_user_codex_auth(uid))) if owner else len(get_server_codex_homes())
    else:
        raise ValueError("unsupported provider")
    # Missing credentials get one validation slot, so an old queued job fails
    # visibly instead of waiting forever after the owner removes their key.
    capacity = max(1, count * _credential_limit(credential_provider, owner)) if source != "none" else 1
    return provider + ("\x00user\x00" + uid if owner or source == "none" else "\x00server"), capacity


def server_credential_pool_stats() -> Dict[str, Dict[str, int]]:
    """Counts only. Never returns values, hashes or fingerprints."""
    return {
        provider: get_server_credential_pool(provider).stats()
        for provider in ("openai", "claude", "codex")
    }


def preflight_server_credential_pools() -> Dict[str, Dict[str, int]]:
    """Validate server pools before a destructive shared-key migration."""
    if not get_server_openai_keys():
        raise RuntimeError("OpenAI server credential pool is empty")
    if not get_server_claude_oauth_tokens():
        raise RuntimeError("Claude server credential pool is empty")
    for home in get_server_codex_homes():
        if not os.path.isdir(home):
            raise RuntimeError("Configured CODEX_HOME directory does not exist")
    return server_credential_pool_stats()


_MINIMAL_CHILD_ENV_KEYS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
)
_REGISTERED_SERVER_SECRETS: Set[str] = set()
_REGISTERED_SERVER_SECRETS_LOCK = threading.Lock()
_CLAUDE_DISALLOWED_TOOLS = [
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
    "Task",
    "TaskOutput",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Write",
]


async def _deny_claude_tool(_tool_name, _tool_input, _context):
    from claude_code_sdk import PermissionResultDeny

    return PermissionResultDeny(
        message="Tools are disabled for the Rewrite text-generation service.",
        interrupt=True,
    )


# FIX-2026-09-17-alt-route: кабинеты на серверных ключах пишут через альт-ключ
# (OpenAI- и Anthropic-совместимый прокси) вместо подписок владельца. Меняется только
# транспорт CLI: промпты, куски, повторы и чистка — те же. Владелец и кабинеты со
# своим ключом провайдера идут прежним путём. Без REWRITE_ALT_API_KEY всё как раньше.
ALT_ROUTE_TOKEN = "ALT-PROXY-ROUTE"
ALT_ERROR_PREFIX = "ALT_PROXY:"
_alt_ctx = threading.local()
_ALT_SLOTS = threading.BoundedSemaphore(
    max(1, min(6, int(os.environ.get("REWRITE_ALT_CONCURRENCY", "5") or "5")))
)


def _alt_setting(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _alt_pairs(name: str, default: str) -> Dict[str, str]:
    pairs = {}
    for item in _alt_setting(name, default).split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            if key.strip() and value.strip():
                pairs[key.strip()] = value.strip()
    return pairs


def _alt_key() -> str:
    key = _alt_setting("REWRITE_ALT_API_KEY")
    if key:
        _register_server_secret_tree({"alt_proxy_key": key})
    return key


def alt_route_for(provider: str) -> bool:
    """True — вызов идёт через альт-ключ: кабинет не владельца и ключ провайдера серверный."""
    if not _alt_setting("REWRITE_ALT_API_KEY"):
        return False
    uid = get_current_user_id()
    if not uid or uid == "default" or uid in ADMIN_USER_IDS:
        return False
    return credential_source(provider, uid) == "server"


def _alt_model(model: str) -> str:
    """Модель, которую не пускает альт-ключ, заменяется разрешённой (судья sonnet-4-6 → sonnet-5)."""
    mapping = _alt_pairs(
        "REWRITE_ALT_MODEL_MAP",
        "claude-sonnet-4-6=claude-sonnet-5,claude-haiku-4-5=claude-sonnet-5",
    )
    return mapping.get((model or "").strip(), (model or "").strip())


def _alt_codex_flags(model: str) -> List[str]:
    slug = (model or "").split(":", 1)[-1]
    # = default_reasoning_level боевого Codex CLI для этих моделей (models_cache, 17.09.2026)
    effort = _alt_pairs(
        "REWRITE_ALT_CODEX_EFFORT",
        "gpt-5.6-sol=low,gpt-5.6-terra=medium,gpt-5.6-luna=medium",
    ).get(slug)
    base_url = _alt_setting("REWRITE_ALT_OPENAI_BASE_URL", "https://alt-proxy.example.invalid/v1")
    flags = [
        "-c", 'model_provider="rewrite_alt"',
        "-c", 'model_providers.rewrite_alt.name="rewrite_alt"',
        "-c", f'model_providers.rewrite_alt.base_url="{base_url}"',
        "-c", 'model_providers.rewrite_alt.env_key="REWRITE_ALT_API_KEY"',
        "-c", 'model_providers.rewrite_alt.wire_api="responses"',
    ]
    if effort:
        flags += ["-c", f'model_reasoning_effort="{effort}"']
    return flags


@contextmanager
def _alt_slot(provider: str, model: str):
    """Общий потолок одновременных вызовов через альт-ключ (Codex + Claude вместе).

    Ждём без таймаута, но с проверкой остановки и отмены задачи. Никаких
    HOST_BUDGET_WAIT: задача не откладывается, просто ждёт свободного слота.
    """
    depth = getattr(_alt_ctx, "depth", 0)
    if depth == 0:
        while not _ALT_SLOTS.acquire(timeout=0.25):
            if STOP_FLAG.is_stopped():
                raise RuntimeError("Остановлено пользователем")
            ensure_provider_execution_allowed(provider)
    previous = (getattr(_alt_ctx, "active", False), getattr(_alt_ctx, "model", ""))
    _alt_ctx.depth = depth + 1
    _alt_ctx.active = True
    _alt_ctx.model = model
    try:
        yield
    finally:
        _alt_ctx.depth = depth
        _alt_ctx.active, _alt_ctx.model = previous
        if depth == 0:
            _ALT_SLOTS.release()


def build_provider_child_env(
    provider: str,
    *,
    credential: str = "",
    isolated_home: str = "",
    codex_home: str = "",
) -> Dict[str, str]:
    """Build a deny-by-default subprocess environment for an LLM CLI.

    No parent credential/proxy/application variables are inherited. The only
    provider credential admitted is the lease selected for this exact call.
    """
    env: Dict[str, str] = {}
    for name in _MINIMAL_CHILD_ENV_KEYS:
        if name == "PATH":
            continue
        value = os.environ.get(name)
        if value:
            env[name] = value
    env["PATH"] = (
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        if sys.platform == "darwin"
        else "/usr/local/bin:/usr/bin:/bin"
    )
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
    provider = (provider or "").strip().lower()
    if provider == "claude":
        if credential == ALT_ROUTE_TOKEN:
            # FIX-2026-09-17-alt-route: тот же Claude Code CLI, но на альт-ключе.
            env["ANTHROPIC_BASE_URL"] = _alt_setting(
                "REWRITE_ALT_ANTHROPIC_BASE_URL", "https://alt-proxy.example.invalid"
            )
            env["ANTHROPIC_API_KEY"] = _alt_key()
            env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        else:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = credential
        env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"
    elif provider == "codex" and getattr(_alt_ctx, "active", False):
        # FIX-2026-09-17-alt-route: без CODEX_HOME и auth.json, ключ — из env.
        env["REWRITE_ALT_API_KEY"] = _alt_key()
    elif provider == "codex" and codex_home:
        env["CODEX_HOME"] = codex_home
    return env


def _server_secret_values() -> List[str]:
    values: List[str] = []
    getters = (
        get_server_openai_keys,
        get_server_claude_oauth_tokens,
        get_server_anthropic_keys,
        get_server_codex_homes,
    )
    for getter in getters:
        try:
            values.extend(getter())
        except Exception:
            pass
    for value in (
        ANTHROPIC_API_KEY,
        OPENAI_API_KEY,
        os.environ.get("ANTHROPIC_API_KEY", ""),
        os.environ.get("OPENAI_API_KEY", ""),
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
    ):
        if value:
            values.append(value)
    with _SERVER_CREDENTIAL_POOLS_LOCK:
        for pool in _SERVER_CREDENTIAL_POOLS.values():
            with pool._condition:
                values.extend(entry.value for entry in pool._entries)
    with _REGISTERED_SERVER_SECRETS_LOCK:
        values.extend(_REGISTERED_SERVER_SECRETS)
    return sorted(
        {str(value) for value in values if len(str(value)) >= 8},
        key=len,
        reverse=True,
    )


def redact_server_credentials(value: Any) -> Any:
    """Redact exact server credentials from nested provider/job output."""
    if isinstance(value, str):
        clean = value
        for secret in _server_secret_values():
            clean = clean.replace(secret, "[REDACTED_SERVER_CREDENTIAL]")
        return clean
    if isinstance(value, dict):
        return {
            key: redact_server_credentials(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_server_credentials(child) for child in value]
    if isinstance(value, tuple):
        return tuple(redact_server_credentials(child) for child in value)
    return value


def _register_server_secret_tree(value: Any) -> None:
    found: Set[str] = set()

    def visit(child: Any) -> None:
        if isinstance(child, str) and len(child) >= 8:
            found.add(child)
        elif isinstance(child, dict):
            for nested in child.values():
                visit(nested)
        elif isinstance(child, list):
            for nested in child:
                visit(nested)

    visit(value)
    if found:
        with _REGISTERED_SERVER_SECRETS_LOCK:
            _REGISTERED_SERVER_SECRETS.update(found)


def redact_provider_output(function):
    """Provider boundary: no server credential may leave through result/error."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return redact_server_credentials(function(*args, **kwargs))
        except Exception as exc:
            clean = redact_server_credentials(str(exc))
            if clean != str(exc):
                raise RuntimeError(clean) from None
            raise

    return wrapped


if CLAUDE_CODE_SDK_AVAILABLE:
    from claude_code_sdk import ClaudeSDKClient as _ClaudeSDKClient
    from claude_code_sdk._internal.query import Query as _ClaudeInternalQuery
    from claude_code_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport as _ClaudeSubprocessCLITransport,
    )

    class _SanitizedClaudeTransport(_ClaudeSubprocessCLITransport):
        """SDK transport that never merges the parent process environment."""

        def __init__(
            self,
            prompt,
            options,
            process_env: Dict[str, str],
            isolation_root: str,
        ):
            super().__init__(prompt=prompt, options=options)
            self._rewrite_process_env = dict(process_env)
            self._rewrite_isolation_root = isolation_root

        async def connect(self) -> None:
            if self._process:
                return
            from subprocess import PIPE
            from anyio.streams.text import TextReceiveStream, TextSendStream

            cmd = self._build_command()
            process_env = dict(self._rewrite_process_env)
            process_env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"
            if self._cwd:
                process_env["PWD"] = self._cwd
            stderr_dest = (
                self._options.debug_stderr
                if "debug-to-stderr" in self._options.extra_args
                and self._options.debug_stderr
                else None
            )
            try:
                self._process = await anyio.open_process(
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
            except BaseException:
                shutil.rmtree(self._rewrite_isolation_root, ignore_errors=True)
                raise

        async def close(self) -> None:
            try:
                await super().close()
            finally:
                shutil.rmtree(self._rewrite_isolation_root, ignore_errors=True)


    class _SanitizedClaudeSDKClient(_ClaudeSDKClient):
        def __init__(self, options, process_env, isolation_root):
            super().__init__(options=options)
            self._rewrite_process_env = dict(process_env)
            self._rewrite_isolation_root = isolation_root

        async def connect(self, prompt=None) -> None:
            from collections.abc import AsyncIterable
            from dataclasses import replace

            async def _empty_stream():
                return
                yield {}

            actual_prompt = _empty_stream() if prompt is None else prompt
            configured_options = replace(
                self.options,
                permission_prompt_tool_name="stdio",
            )
            self._transport = _SanitizedClaudeTransport(
                prompt=actual_prompt,
                options=configured_options,
                process_env=self._rewrite_process_env,
                isolation_root=self._rewrite_isolation_root,
            )
            await self._transport.connect()
            self._query = _ClaudeInternalQuery(
                transport=self._transport,
                is_streaming_mode=True,
                can_use_tool=self.options.can_use_tool,
                hooks=None,
                sdk_mcp_servers={},
            )
            await self._query.start()
            await self._query.initialize()
            if (
                prompt is not None
                and isinstance(prompt, AsyncIterable)
                and self._query._tg
            ):
                self._query._tg.start_soon(self._query.stream_input, prompt)


def _claude_isolated_options(
    oauth_token: str,
    **option_values,
):
    isolation_root = tempfile.mkdtemp(prefix="rewrite-claude-isolated-")
    os.chmod(isolation_root, 0o700)
    home_dir = os.path.join(isolation_root, "home")
    work_dir = os.path.join(isolation_root, "work")
    os.makedirs(home_dir, mode=0o700)
    os.makedirs(work_dir, mode=0o700)
    child_env = build_provider_child_env(
        "claude",
        credential=oauth_token,
        isolated_home=home_dir,
    )
    option_values.update(
        {
            "cwd": work_dir,
            "env": child_env,
            "allowed_tools": [],
            "disallowed_tools": list(_CLAUDE_DISALLOWED_TOOLS),
            "mcp_servers": {},
            "permission_mode": "dontAsk",
            "can_use_tool": _deny_claude_tool,
            "extra_args": {
                "tools": "",
                "strict-mcp-config": None,
                "disable-slash-commands": None,
                "no-chrome": None,
                "no-session-persistence": None,
                "setting-sources": "",
            },
        }
    )
    return (
        ClaudeCodeOptions(**option_values),
        child_env,
        isolation_root,
    )


_provider_guard_ctx = threading.local()


def set_provider_execution_guard(guard: Optional[Callable[[str], bool]]) -> None:
    _provider_guard_ctx.guard = guard


def clear_provider_execution_guard() -> None:
    try:
        del _provider_guard_ctx.guard
    except AttributeError:
        pass


def ensure_provider_execution_allowed(provider: str) -> None:
    guard = getattr(_provider_guard_ctx, "guard", None)
    if guard is not None and not guard(provider):
        raise RuntimeError("provider execution cancelled")


def get_allowed_openai_models() -> Set[str]:
    """Server-controlled model allowlist for partner jobs."""
    raw = os.environ.get(
        "REWRITE_ALLOWED_OPENAI_MODELS",
        "gpt-4.1-nano,gpt-4.1-mini,gpt-4o-mini",
    )
    return {item.strip() for item in raw.split(",") if item.strip()}


def _allowed_values(name: str, default: str) -> Set[str]:
    raw = os.environ.get(name, default)
    return {item.strip() for item in raw.split(",") if item.strip()}


def get_allowed_claude_models() -> Set[str]:
    return _allowed_values(
        "REWRITE_ALLOWED_CLAUDE_MODELS",
        "claude-opus-5,claude-opus-4-7,claude-opus-4-8,"
        "claude-sonnet-5,claude-opus-4-6,"
        "claude-sonnet-4-6,claude-haiku-4-5",
    )


def get_allowed_codex_models() -> Set[str]:
    # FIX-2026-09-13-models: по умолчанию только живые модели. gpt-5.4 и gpt-5.4-mini
    # OpenAI сняла (05.09 и ~12.09), gpt-5.5 на этом аккаунте отвечала 404, у luna нет
    # ни одного успешного ответа — потерянная переменная не должна их вернуть.
    return _allowed_values(
        "REWRITE_ALLOWED_CODEX_MODELS",
        "codex:gpt-5.6-sol,codex:gpt-5.6-terra",
    )


def codex_model_unavailable_message(model: str) -> str:
    """Причина для человека: провайдер не даёт эту модель (FIX-2026-09-13-models)."""
    name = (model or "").strip()
    if name.lower().startswith("codex:"):
        name = name.split(":", 1)[1]
    return (
        f"Модель {name} сейчас недоступна у провайдера — "
        "выберите другую модель и запустите заново"
    )


def codex_model_admission_error(model: str) -> str:
    """Модель Codex из сохранённой задачи вне списка разрешённых → текст отказа, иначе "".

    FIX-2026-09-13-models: «Повторить» и авто-возобновление берут модель из задачи,
    а не из формы, поэтому сверяем её заново и отвечаем по-русски. Claude и OpenAI
    не трогаем — у них прежние проверки и тексты.
    """
    model = (model or "").strip()
    if classify_provider(model) != "codex" or model in get_allowed_codex_models():
        return ""
    return codex_model_unavailable_message(model)


# FIX-2026-09-17-server-models: кабинету на серверных ключах — только модели из
# REWRITE_SERVER_KEY_MODELS. Владелец и кабинеты со своим ключом провайдера живут по
# прежним общим спискам. Пустая переменная = ограничения нет (откат без правки кода).
def get_server_key_models() -> Set[str]:
    return _allowed_values("REWRITE_SERVER_KEY_MODELS", "")


def server_key_model_error(model: str, user_id: str = "") -> str:
    """Текст отказа, если модель не из списка для серверных ключей, иначе ""."""
    allowed = get_server_key_models()
    model = (model or "").strip()
    if not allowed or not model or model in allowed:
        return ""
    uid = _resolve_user_id(user_id)
    if uid in ADMIN_USER_IDS:
        return ""
    if credential_source(classify_provider(model), uid) != "server":
        return ""
    names = ", ".join(sorted(item.split(":", 1)[-1] for item in allowed))
    return (
        f"Модель {model.split(':', 1)[-1]} недоступна на серверных ключах. "
        f"Выберите одну из: {names} — или добавьте свой ключ в настройках"
    )


def provider_user_allowed(provider: str, user_id: str) -> bool:
    """Optional server-side allow/deny policy. Empty allowlist means all."""
    prefix = provider.upper()
    allowed = _allowed_values(
        f"REWRITE_{prefix}_ALLOWED_USER_IDS",
        os.environ.get("REWRITE_PROVIDER_ALLOWED_USER_IDS", ""),
    )
    denied = _allowed_values(
        f"REWRITE_{prefix}_DENIED_USER_IDS",
        os.environ.get("REWRITE_PROVIDER_DENIED_USER_IDS", ""),
    )
    return user_id not in denied and (not allowed or user_id in allowed)

# Модели
API_PROVIDER = "claude"  # "openai" или "claude"
DEFAULT_MODEL = "claude-sonnet-4-20250514"  # Sonnet 4 (Sonnet 4.5 может быть недоступен)
# Альтернативы:
# "claude-sonnet-4-20250514" - Sonnet 4
# "claude-3-5-haiku-20241022" - Haiku 3.5 (дешевле)
# "gpt-4o-mini" - для OpenAI
PROGRESS_FILE = os.environ.get("REWRITE_PROGRESS_FILE", os.path.join(os.path.expanduser("~"), "rewrite_progress_v2.json"))
LAST_LANGUAGES_FILE = os.path.join(os.path.expanduser("~"), ".rewrite_last_languages.json")
LAST_COOKIES_FILE = os.path.join(os.path.expanduser("~"), ".rewrite_last_cookies.json")
LAST_OAUTH_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".rewrite_oauth_token.json")
KEYS_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".rewrite_keys_config.json")  # legacy / read-only fallback после миграции на user_keys

# Per-user keys storage (миграция user_keys_001, 2026-05-24).
# ADMIN_USER_ID должен совпадать с server.py / api.py / index.html / migrations/user_keys_001.py.
# ИСПРАВЛЕНО 04.09.2026: было литералом. После слияния личности id админа —
# это tenant_id кабинета, и зашитое значение перестаёт совпадать: админ
# молча теряет права. Cover читал его из окружения давно, здесь так же.
# Правильный конец истории — роль из users.role кабинета, а не отдельный id.
ADMIN_USER_ID = os.environ.get("REWRITE_ADMIN_USER_ID") or "REDACTED_ADMIN_USER_ID"
ADMIN_USER_IDS = {ADMIN_USER_ID}  # 2026-06-04: гейт ENV-ключа сервера

def env_openai_key_for(user_id: str) -> str:
    """BYOK-first OpenAI credential for ``user_id`` (own key → server pool).

    The credential is never returned by an HTTP endpoint and never serialized
    into a job.
    """
    return get_effective_openai_key(user_id)

def env_anthropic_key_for(user_id: str) -> str:
    """Legacy accessor retained for direct in-process calls only.

    Configuration getters and writers never call this function: server-owned
    credentials must not be materialized into ``user_keys`` or HTTP payloads.
    """
    return ANTHROPIC_API_KEY if user_id in ADMIN_USER_IDS else ""
SESSIONS_DB_PATH = os.environ.get("SESSION_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.db"))
MODEL_CAPABILITIES_FILE = os.path.join(os.path.expanduser("~"), ".rewrite_model_capabilities.json")
PRESETS_FILE = os.path.join(os.path.expanduser("~"), ".rewrite_presets.json")
OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "rewrite_results")
DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "video_downloads")
# Генерация текстов с нуля
TEXT_GENERATION_PRESETS_FILE = os.path.join(os.path.expanduser("~"), "text_generation_presets.json")
JOB_QUEUE_DB = os.path.join(os.path.expanduser("~"), "job_queue.db")
QUEUE_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".rewrite_queue_config.json")
GEN_SETTINGS_PRESETS_FILE = os.path.join(os.path.expanduser("~"), ".generation_settings_presets.json")
GEN_OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "generated_texts")

def classify_provider(model: str) -> str:
    """Trusted queue provider derived only from the admitted server model."""
    value = (model or "").strip().lower()
    if value.startswith("codex:"):
        return "codex"
    if "claude" in value:
        return "claude"
    return "openai"


def _detect_provider(model: str, use_subscription: bool = False) -> str:
    """
    Определяет провайдера API по имени модели.
    Возвращает: "codex_subscription", "subscription", "claude" или "openai".
    Префикс codex: означает серверную подписку ChatGPT через Codex CLI.
    """
    if classify_provider(model) == "codex":
        return "codex_subscription"
    if classify_provider(model) == "claude":
        return "subscription" if use_subscription else "claude"
    return "openai"


SAFE_TEXT_SUPPLEMENT = """
### РЕЖИМ БЕЗОПАСНОГО ТЕКСТА (YouTube-safe) ###

Цель: текст должен быть таким же захватывающим и интригующим, но НЕ должен выглядеть как попытка выдать вымысел за реальные документальные факты. YouTube демонетизирует каналы за "недостоверный контент", когда ИИ-текст подаётся как реальная документалистика.

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:

1. НАРРАТИВНАЯ ДИСТАНЦИЯ — подавай историю как пересказ/легенду/реконструкцию, а НЕ как документальный факт:
   - НЕЛЬЗЯ: "В 1982 году генерал Петров приказал расстрелять деревню. Это подтверждают рассекреченные документы."
   - МОЖНО: "Среди жителей тех мест до сих пор передают мрачную историю об одном офицере, который, как рассказывают старожилы, однажды отдал страшный приказ..."
   - Используй фразы: "рассказывают, что...", "по одной из версий...", "легенда гласит...", "существует история о том, как...", "говорят, что в те годы..."
   - НО! Не переусердствуй — достаточно 2-3 таких маркеров на каждые 1000-1500 слов. Текст не должен звучать неуверенно. Он должен звучать как РАССКАЗЧИК У КОСТРА — уверенный, но рассказывающий историю, а не зачитывающий протокол.

2. АНОНИМИЗАЦИЯ — НЕ придумывай конкретные ФИО + звания + даты + номера документов для вымышленных персонажей:
   - НЕЛЬЗЯ: "Майор Сергей Викторович Козлов, дело №4502-Б от 14 марта 1978 года, 3-й отдел КГБ г. Новосибирск"
   - МОЖНО: "Один офицер, чьё имя знали немногие", "человек, которого в тех кругах звали просто Капитан", "в документах он значился под оперативным псевдонимом"
   - Для РЕАЛЬНЫХ общеизвестных исторических личностей (Сталин, Наполеон, Генрих VIII, Елизавета II) — имена можно и нужно использовать, это повышает доверие
   - Для ВЫМЫШЛЕННЫХ персонажей — используй прозвища, архетипы, описательные конструкции вместо фейковых паспортных данных

3. ЗАПРЕЩЁННЫЕ МАРКЕРЫ "ФЕЙКОВОЙ ДОКУМЕНТАЛИСТИКИ":
   - Убери из текста: "рассекреченные документы", "секретный архив", "скрывали от народа", "вся правда о...", "учёные в шоке", "сенсационная находка доказывает"
   - Замени на: "забытая история", "малоизвестный эпизод", "загадка прошлого", "одна из тайн той эпохи"

4. АВТОРСКИЙ СКЕПСИС — добавляй элементы сомнения/размышления рассказчика:
   - "Звучит невероятно? Возможно. Но те, кто был рядом, рассказывают именно так."
   - "Правда это или городская легенда — каждый решает сам."
   - "В этой истории много неясного, и, может быть, именно поэтому она так завораживает."
   - Это НЕ ослабляет текст, а УСИЛИВАЕТ — зритель чувствует честность рассказчика.

5. ФИНАЛ — завершай вопросом к зрителю или моральной рефлексией, а НЕ "разоблачением":
   - НЕЛЬЗЯ: "Теперь вы знаете правду, которую скрывали 50 лет!"
   - МОЖНО: "Что бы ты сделал на его месте? Напиши в комментариях."
   - МОЖНО: "Может быть, эта история — всего лишь легенда. А может, нет. Но она заставляет задуматься."

ЧТО НЕ МЕНЯТЬ:
- Стиль повествования (рубленый, кинематографичный, атмосферный) — сохрани как есть
- Интригу и хуки — они должны остаться сильными
- Брендинг (имя ведущего, название канала) — без изменений
- Длину текста — не сокращай
- Сенсорику (запахи, звуки, ощущения) — это усиливает текст
- Диалоги и прямую речь — это делает текст живым

ВАЖНО: Этот режим — НЕ цензура. Текст должен оставаться таким же захватывающим, мрачным, интригующим. Меняется только ПОДАЧА: из "я зачитываю секретный документ" в "я рассказываю невероятную историю".
"""


def get_queue_config() -> Dict:
    """Загружает настройки очереди и параллельности."""
    default = {"max_concurrent_jobs": 5, "max_analysis_parallelism": 0, "max_parallel": 75}
    try:
        if os.path.exists(QUEUE_CONFIG_FILE):
            with open(QUEUE_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default["max_concurrent_jobs"] = max(1, min(200, int(data.get("max_concurrent_jobs", 5))))
                default["max_analysis_parallelism"] = max(0, min(500, int(data.get("max_analysis_parallelism", 0))))
                default["max_parallel"] = max(1, min(200, int(data.get("max_parallel", 75))))
    except Exception:
        pass
    return default


_user_id_ctx = threading.local()


def set_current_user_id(user_id: str) -> None:
    """Worker устанавливает user_id в thread-local context перед началом job/chunk.
    Нужно, чтобы get_keys_config() без явного user_id (в утилитах глубоко в стеке)
    читал ключи правильного партнёра, а не админа."""
    _user_id_ctx.user_id = user_id or ""


def clear_current_user_id() -> None:
    """Сбрасывает thread-local user_id (вызывать в finally после job/chunk)."""
    try:
        del _user_id_ctx.user_id
    except AttributeError:
        pass


def get_current_user_id() -> str:
    """Возвращает thread-local user_id или '' если не установлен."""
    return getattr(_user_id_ctx, "user_id", "") or ""


def _resolve_user_id(user_id: str) -> str:
    """Пустой / 'default' → thread-local ctx → ADMIN_USER_ID.

    Резолв-цепочка: явный аргумент > thread-local (установленный worker'ом) > admin.
    Это нужно для утилит вроде get_postprocess_openai_key() / debug_log() — у них
    нет signature для user_id, но они должны видеть ключи текущего партнёра."""
    if user_id and user_id != "default":
        return user_id
    ctx = get_current_user_id()
    if ctx and ctx != "default":
        return ctx
    return ADMIN_USER_ID


class KeysConfigUnavailable(RuntimeError):
    """Хранилище настроек не прочиталось. Это НЕ «своего ключа нет».

    FIX-2026-07-30 (P1 №8): раньше ``get_keys_config`` глушил любое исключение и
    отдавал дефолты, а BYOK-аксессоры возвращали пустой список — неотличимо от
    «ключ не задан». При блокировке sqlite под нагрузкой задача тенанта молча
    уезжала на серверный ключ владельца, то есть на его деньги.
    """


# uid → провайдеры, у которых у тенанта ТОЧНО был свой credential (по факту
# удачного чтения). Нужен, чтобы при сбое чтения не откатываться на серверный
# пул там, где BYOK был сконфигурирован.
_BYOK_CONFIGURED: Dict[str, Set[str]] = {}
_BYOK_CONFIGURED_LOCK = threading.Lock()


def _remember_byok(uid: str, provider: str, configured: bool) -> None:
    with _BYOK_CONFIGURED_LOCK:
        known = _BYOK_CONFIGURED.setdefault(uid, set())
        if configured:
            known.add(provider)
        else:
            known.discard(provider)


# FIX-2026-07-30 (P1 №8, добор ревью): признак «у тенанта был свой ключ» обязан
# переживать рестарт uvicorn. _BYOK_CONFIGURED — процессный кэш, который
# наполняли ТОЛЬКО удачные чтения credential'ов, поэтому сразу после рестарта он
# пуст: первый же сбой чтения БД снова уводил BYOK-тенанта на серверный ключ
# владельца (его деньги). Теперь при первом УДАЧНОМ обращении к хранилищу
# делается разовое чтение всей таблицы user_keys — дальше факт BYOK известен
# независимо от того, читали ли мы уже credential именно этого тенанта.
# Ключ состояния — путь к БД: смена SESSIONS_DB_PATH (тесты, миграции) заставляет
# прочитать заново.
_BYOK_PRIME_STATE: Dict[str, str] = {}
_BYOK_PRIME_LOCK = threading.Lock()


def _prime_byok_from_db(db_path: str) -> bool:
    """Разовое чтение user_keys: у кого из тенантов вообще задан свой credential.

    Только ДОБАВЛЯЕТ факты (никогда не снимает) — снятие делает обычный путь
    ``_remember_byok`` по факту удачного чтения. Возвращает True, если чтение
    удалось (тогда повторять не нужно).
    """
    try:
        with closing(sqlite3.connect(db_path, timeout=5.0)) as conn, conn:
            has_codex = _user_keys_has_column(conn, "codex_auth")
            columns = "user_id, anthropic_api_key, openai_api_key, oauth_tokens"
            if has_codex:
                columns += ", codex_auth"
            rows = conn.execute(f"SELECT {columns} FROM user_keys").fetchall()
    except Exception as exc:
        print(f"[keys_config] BYOK prime read failed ({db_path}): {exc}")
        return False
    for row in rows:
        uid = str(row[0] or "").strip()
        if not uid:
            continue
        if str(row[1] or "").strip():
            _remember_byok(uid, "anthropic", True)
        if str(row[2] or "").strip():
            _remember_byok(uid, "openai", True)
        try:
            oauth = json.loads(row[3]) if row[3] else []
        except Exception:
            oauth = []
        if isinstance(oauth, str):
            oauth = [oauth]
        if any(str(t or "").strip() for t in oauth):
            _remember_byok(uid, "claude", True)
        if has_codex and str(row[4] or "").strip():
            _remember_byok(uid, "codex", True)
    return True


def _ensure_byok_primed() -> None:
    """Один успешный проход по user_keys на процесс (и на путь к БД)."""
    db_path = SESSIONS_DB_PATH
    if _BYOK_PRIME_STATE.get(db_path) == "done":
        return
    with _BYOK_PRIME_LOCK:
        if _BYOK_PRIME_STATE.get(db_path) == "done":
            return
        if _prime_byok_from_db(db_path):
            _BYOK_PRIME_STATE[db_path] = "done"


def _byok_was_configured(uid: str, provider: str) -> bool:
    # Последний шанс, если процесс ещё ни разу не читал хранилище удачно.
    _ensure_byok_primed()
    with _BYOK_CONFIGURED_LOCK:
        return provider in _BYOK_CONFIGURED.get(uid, set())


def _byok_fallback_or_raise(user_id: str, provider: str, exc: BaseException):
    """Сбой чтения настроек: молча на серверный ключ — только если BYOK не было."""
    uid = _resolve_user_id(user_id)
    if _byok_was_configured(uid, provider):
        raise KeysConfigUnavailable(
            f"cannot read own {provider} credential of {uid}: {exc}"
        ) from exc
    print(f"[keys_config] read failed for user_id={uid} provider={provider}: {exc}")


def get_keys_config(user_id: str = "", strict: bool = False) -> Dict:
    """Загружает настройки ключей для user_id из sessions.db.user_keys.

    Пустой / 'default' user_id → ADMIN_USER_ID. Vendor credential compatibility
    fields are always empty; only non-secret preferences come from this store.

    ``strict=True`` (пути, читающие credential) поднимает
    :class:`KeysConfigUnavailable` вместо тихой отдачи дефолтов.
    """
    uid = _resolve_user_id(user_id)
    default = {
        "anthropic_api_key": "",
        "openai_api_key": "",
        "oauth_tokens": [],
        # BYOK-2026-07-30: своя подписка Codex тенанта (содержимое auth.json).
        "codex_auth": "",
        "credential_mode": "server_fallback",
        "gpt_service_tier": "default",
        "gpt_use_cache": True,
        "postprocess_check_model": "gpt-5-mini",
        "custom_models": [],
        "debug_mode": False,
        # FIX-2026-07-02: серверное хранение UI-пресетов настроек (раньше только localStorage браузера
        # → терялись между устройствами и при чистке кэша). Объекты {name: {...настройки...}}.
        "rew_presets": {},
        "gen_settings_presets": {},
    }
    store_readable = False
    try:
        with closing(sqlite3.connect(SESSIONS_DB_PATH, timeout=5.0)) as conn, conn:
            row = conn.execute(
                """
                SELECT anthropic_api_key, openai_api_key, oauth_tokens,
                       gpt_service_tier, gpt_use_cache, postprocess_check_model,
                       custom_models, debug_mode, rew_presets, gen_settings_presets
                  FROM user_keys WHERE user_id = ?
                """,
                (uid,),
            ).fetchone()
        if row:
            anthr, openai_k, oauth_json, tier, use_cache, ppmodel, custom_json, dbg, rew_json, gen_json = row
            try:
                oauth = json.loads(oauth_json) if oauth_json else []
            except Exception:
                oauth = []
            if isinstance(oauth, str):
                oauth = [oauth] if oauth else []
            try:
                custom = json.loads(custom_json) if custom_json else []
            except Exception:
                custom = []
            if isinstance(custom, str):
                custom = [custom] if custom else []
            # BYOK-2026-07-30: своё значение тенанта возвращается как есть.
            # Серверные (ENV) креды сюда НЕ подмешиваются — только то, что
            # пользователь вписал сам. Наружу (HTTP) отдаётся лишь признак
            # «задан», см. server.py /settings/keys.
            default["anthropic_api_key"] = (anthr or "").strip()
            default["openai_api_key"] = (openai_k or "").strip()
            default["oauth_tokens"] = [
                str(t).strip() for t in oauth if str(t or "").strip()
            ]
            default["gpt_service_tier"] = tier or "default"
            default["gpt_use_cache"] = bool(use_cache)
            default["postprocess_check_model"] = ppmodel or "gpt-5-mini"
            default["custom_models"] = custom
            default["debug_mode"] = bool(dbg)
            # FIX-2026-07-02: UI-пресеты настроек из БД (объекты {name: {...}}).
            try:
                rp = json.loads(rew_json) if rew_json else {}
            except Exception:
                rp = {}
            default["rew_presets"] = rp if isinstance(rp, dict) else {}
            try:
                gp = json.loads(gen_json) if gen_json else {}
            except Exception:
                gp = {}
            default["gen_settings_presets"] = gp if isinstance(gp, dict) else {}
        store_readable = True
    except Exception as exc:
        # FIX-2026-07-30 (P1 №8): «не смог прочитать» ≠ «ключа нет».
        if strict:
            raise KeysConfigUnavailable(
                f"cannot read user_keys for {uid}: {exc}"
            ) from exc
    # BYOK-2026-07-30: codex_auth читается ОТДЕЛЬНЫМ guarded-запросом, а не в
    # основном SELECT. На БД без этой колонки основной SELECT упал бы целиком, и
    # все остальные BYOK-ключи молча превратились бы в дефолты (=перестали бы
    # работать). Здесь же отсутствие колонки просто даёт "".
    try:
        with closing(sqlite3.connect(SESSIONS_DB_PATH, timeout=5.0)) as conn, conn:
            if _user_keys_has_column(conn, "credential_mode"):
                mode = conn.execute("SELECT credential_mode FROM user_keys WHERE user_id=?", (uid,)).fetchone()
                if mode:
                    default["credential_mode"] = mode[0] or "server_fallback"
            if _user_keys_has_column(conn, "codex_auth"):
                row = conn.execute(
                    "SELECT codex_auth FROM user_keys WHERE user_id = ?", (uid,)
                ).fetchone()
                if row and row[0]:
                    default["codex_auth"] = str(row[0]).strip()
    except Exception as exc:
        if strict:
            raise KeysConfigUnavailable(
                f"cannot read user_keys.codex_auth for {uid}: {exc}"
            ) from exc
    # FIX-2026-07-30 (P1 №8, добор): хранилище только что прочиталось — значит БД
    # здорова, самое время разово запомнить, у кого есть свой ключ. Делать это на
    # пути ошибки поздно: там БД уже недоступна.
    if store_readable:
        try:
            _ensure_byok_primed()
        except Exception:
            pass
    return default


def _user_keys_has_column(conn: sqlite3.Connection, column: str) -> bool:
    try:
        return column in {
            row[1] for row in conn.execute("PRAGMA table_info(user_keys)").fetchall()
        }
    except Exception:
        return False


def _ensure_user_keys_codex_column(conn: sqlite3.Connection) -> bool:
    """Idempotently add ``user_keys.codex_auth``; False когда таблицы ещё нет."""
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_keys'"
        ).fetchone()
    except Exception:
        return False
    if not exists:
        return False
    if _user_keys_has_column(conn, "codex_auth"):
        return True
    try:
        conn.execute(
            "ALTER TABLE user_keys ADD COLUMN codex_auth TEXT NOT NULL DEFAULT ''"
        )
        return True
    except Exception:
        return _user_keys_has_column(conn, "codex_auth")


def _load_model_capabilities() -> Dict[str, Dict[str, bool]]:
    """Загружает кэш неподдерживаемых возможностей моделей (flex, cache, temperature)."""
    try:
        path = os.path.expanduser(MODEL_CAPABILITIES_FILE)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_model_capability(model: str, key: str, value: bool) -> None:
    """Сохраняет в кэш: модель не поддерживает возможность (чтобы не повторять ошибку 100 раз)."""
    try:
        path = os.path.expanduser(MODEL_CAPABILITIES_FILE)
        caps = _load_model_capabilities()
        if model not in caps:
            caps[model] = {}
        caps[model][key] = value
        with open(path, "w", encoding="utf-8") as f:
            json.dump(caps, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# M9 FIX-2026-07-02: сериализует read-modify-write в save_keys_config. Раньше
# get_keys_config → мутация → UPSERT всех колонок шёл без лока → конкурентные писатели
# одного user_id теряли поля друг друга (в т.ч. oauth_tokens). Лок делает
# перечитывание+мерж+запись атомарными между тредами процесса.
_KEYS_WRITE_LOCK = threading.Lock()


def save_keys_config(
    anthropic_api_key: Optional[str] = None,
    openai_api_key: Optional[str] = None,
    oauth_tokens: Optional[List[str]] = None,
    codex_auth: Optional[str] = None,
    credential_mode: Optional[str] = None,
    gpt_service_tier: Optional[str] = None,
    gpt_use_cache: Optional[bool] = None,
    postprocess_check_model: Optional[str] = None,
    custom_models: Optional[List[str]] = None,
    debug_mode: Optional[bool] = None,
    rew_presets: Optional[dict] = None,
    gen_settings_presets: Optional[dict] = None,
    user_id: str = "",
) -> bool:
    """Сохраняет настройки ключей в sessions.db.user_keys для user_id (UPSERT).
    Пустой / 'default' user_id → ADMIN_USER_ID.

    FIX-2026-07-30 (P3 №6): возвращает True только когда ВСЁ переданное реально
    записано. Раньше провал ALTER для ``codex_auth`` (или упавший UPSERT) просто
    проглатывался, а ``POST /settings/keys`` всё равно отвечал ``ok`` — юзер
    считал подписку сохранённой, а её не было.
    """
    uid = _resolve_user_id(user_id)
    if credential_mode is not None and credential_mode not in {"own_only", "server_fallback"}:
        return False
    # M9 FIX-2026-07-02: весь read-modify-write под локом — перечитываем актуальное
    # состояние строки ПОД локом, сливаем только реально переданные поля, затем пишем.
    # Так конкурентные апдейты разных полей одного user_id не затирают друг друга.
    with _KEYS_WRITE_LOCK:
      # FIX-2026-07-30 (P1 №8): read-modify-write обязан читать строго. Иначе
      # сбой чтения давал дефолты, и следующий UPSERT затирал чужие поля
      # (в т.ч. уже сохранённые ключи) пустыми значениями.
      try:
        data = get_keys_config(uid, strict=True)
      except KeysConfigUnavailable as exc:
        print(f"[save_keys_config] aborted for user_id={uid}: {exc}")
        return False
      # FIX-2026-07-02: None-guard ВСЕХ полей → настоящий partial-update. Раньше три ключа
      # писались безусловно, из-за чего апдейт только пресетов затирал бы ключи. Теперь
      # /settings/presets шлёт только пресеты (ключи=None → не трогаются), а /settings/keys
      # шлёт ключи как раньше (непустые/"" → пишутся). M8 _setup/_restore передают ключи явно.
      # BYOK-2026-07-30: свой ключ тенанта снова сохраняется (None = не трогать,
      # "" = очистить). Серверные (ENV) креды в это хранилище не попадают
      # никогда — сюда пишется только то, что пришло аргументом от юзера.
      if anthropic_api_key is not None:
        data["anthropic_api_key"] = (anthropic_api_key or "").strip()
      if openai_api_key is not None:
        data["openai_api_key"] = (openai_api_key or "").strip()
      if oauth_tokens is not None:
        data["oauth_tokens"] = [
            str(t).strip() for t in oauth_tokens if str(t or "").strip()
        ]
      if codex_auth is not None:
        data["codex_auth"] = (codex_auth or "").strip()
      if gpt_service_tier is not None:
        data["gpt_service_tier"] = gpt_service_tier
      if gpt_use_cache is not None:
        data["gpt_use_cache"] = gpt_use_cache
      if postprocess_check_model is not None:
        data["postprocess_check_model"] = postprocess_check_model.strip() or "gpt-5-mini"
      if custom_models is not None:
        data["custom_models"] = [m.strip() for m in custom_models if m and m.strip()]
      if debug_mode is not None:
        data["debug_mode"] = debug_mode
      if rew_presets is not None:
        data["rew_presets"] = rew_presets if isinstance(rew_presets, dict) else {}
      if gen_settings_presets is not None:
        data["gen_settings_presets"] = gen_settings_presets if isinstance(gen_settings_presets, dict) else {}
      try:
        with closing(sqlite3.connect(SESSIONS_DB_PATH, timeout=5.0)) as conn, conn:
            # FIX-2026-07-30 (P0-review добор): колонка проверяется ДО основного
            # UPSERT. Раньше `return False` при провале ALTER стоял ПОСЛЕ него и
            # внутри `with sqlite3.connect(...)`: выход из контекста коммитил
            # транзакцию, то есть остальные ключи реально сохранялись, а
            # POST /settings/keys отдавал 500 «settings were not saved» — юзер
            # думал, что не сохранилось НИЧЕГО. Теперь либо пишем всё, либо не
            # пишем ничего и честно отвечаем «не сохранено».
            if credential_mode is not None and not _user_keys_has_column(conn, "credential_mode"):
                conn.execute("ALTER TABLE user_keys ADD COLUMN credential_mode TEXT NOT NULL DEFAULT 'server_fallback'")
            if codex_auth is not None and not _ensure_user_keys_codex_column(conn):
                print(
                    f"[save_keys_config] codex_auth NOT saved for user_id={uid}: "
                    "user_keys.codex_auth column is missing and cannot be added"
                )
                return False
            conn.execute(
                """
                INSERT INTO user_keys
                  (user_id, anthropic_api_key, openai_api_key, oauth_tokens,
                   gpt_service_tier, gpt_use_cache, postprocess_check_model,
                   custom_models, debug_mode, rew_presets, gen_settings_presets, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  anthropic_api_key = excluded.anthropic_api_key,
                  openai_api_key = excluded.openai_api_key,
                  oauth_tokens = excluded.oauth_tokens,
                  gpt_service_tier = excluded.gpt_service_tier,
                  gpt_use_cache = excluded.gpt_use_cache,
                  postprocess_check_model = excluded.postprocess_check_model,
                  custom_models = excluded.custom_models,
                  debug_mode = excluded.debug_mode,
                  rew_presets = excluded.rew_presets,
                  gen_settings_presets = excluded.gen_settings_presets,
                  updated_at = excluded.updated_at
                """,
                (
                    uid,
                    data["anthropic_api_key"],
                    data["openai_api_key"],
                    json.dumps(data["oauth_tokens"], ensure_ascii=False),
                    data["gpt_service_tier"],
                    1 if data["gpt_use_cache"] else 0,
                    data["postprocess_check_model"],
                    json.dumps(data["custom_models"], ensure_ascii=False),
                    1 if data["debug_mode"] else 0,
                    json.dumps(data.get("rew_presets", {}), ensure_ascii=False),
                    json.dumps(data.get("gen_settings_presets", {}), ensure_ascii=False),
                    time.time(),
                ),
            )
            # BYOK-2026-07-30: codex_auth пишется отдельным UPDATE (колонка уже
            # обеспечена выше), чтобы основной UPSERT остался совместимым с БД
            # без этой колонки — иначе один ALTER-пробел ронял бы сохранение
            # ВСЕХ ключей тенанта.
            if credential_mode is not None:
                conn.execute("UPDATE user_keys SET credential_mode=? WHERE user_id=?", (credential_mode, uid))
            if codex_auth is not None:
                conn.execute(
                    "UPDATE user_keys SET codex_auth = ? WHERE user_id = ?",
                    (data["codex_auth"], uid),
                )
        return True
      except Exception as e:
        print(f"[save_keys_config] failed for user_id={uid}: {e}")
        return False


# Максимум потоков на одну подписку (OAuth)
MAX_THREADS_PER_SUBSCRIPTION = 3

# Консервативные настройки для надёжности
DEFAULT_THREADS = 5  # Не 30!
DEFAULT_CHUNK_SIZE = 8000
MIN_THREADS = 1
MAX_THREADS = 10

# Повторная проверка перевода через GPT-4o-mini: ищет неестественные формулировки и правит под носителя
ENABLE_NATIVE_SPEAKER_CHECK = True
# Порог по длине: исправленный чанк не должен быть короче оригинала более чем на (1 - MIN_FIXED_VS_ORIGINAL_RATIO)
MIN_FIXED_VS_ORIGINAL_RATIO = 0.85  # если исправленный < 85% длины оригинала — правка отклоняется
# Минимальное отношение числа предложений (опционально): не принимать, если предложений сильно меньше
MIN_FIXED_SENTENCES_VS_ORIGINAL_RATIO = 0.80

CYRILLIC_LANGUAGES = {"Russian", "Ukrainian", "Bulgarian", "Belarusian", "Macedonian"}
# Сербский — на латинице (gajica) по нашему выбору, считается Latin для всех целей

# Per-model chunk-size defaults (max words per single LLM call).
# Берётся ~70% от заявленного max_output_tokens модели, чтобы оставить запас
# на JSON wrapper + preview/title/log_update и защититься от quality-degradation
# на максимальной длине output. 1 English word ≈ 1.3 tokens, 1 Russian ≈ 2.5.
# Источник output_tokens limits: Anthropic models/overview docs, 2026-05-23.
#   claude-opus-4-7   = 128k tokens (новый Opus)
#   claude-opus-4-6   = 128k tokens (legacy)
#   claude-sonnet-4-6 = 64k tokens
#   claude-haiku-4-5  = 64k tokens
# GPT — conservative (16k typical output ceiling в Chat Completions).
MODEL_CHUNK_DEFAULTS = {
    # Claude
    # 2026-06-18: РЕАЛИСТИЧНЫЙ размер одного ответа, а не ~70% теоретического
    # max_output. Модель за один ход надёжно пишет ~6-7к слов и "закругляется",
    # поэтому прежние 14-25к/чанк давали недобор (26к target → 2 чанка × ~8.5к = 17к).
    # Теперь чанк ≤ достижимого → words_per_chunk попадает в цель. Бонус: ещё ниже
    # риск stream-stall (#25979). Касается ТОЛЬКО генерации (не рерайта).
    "claude-opus-4-7":    {"latin": 7000, "cyrillic": 5000},
    "claude-opus-4-8":    {"latin": 7000, "cyrillic": 5000},
    "claude-opus-4-6":    {"latin": 7000, "cyrillic": 5000},
    "claude-sonnet-4-6":  {"latin": 6500, "cyrillic": 4500},
    "claude-haiku-4-5":   {"latin": 5000, "cyrillic": 3500},
    # GPT
    "gpt-4.1-nano": {"latin": 8000,  "cyrillic": 4500},
    "gpt-4.1-mini": {"latin": 10000, "cyrillic": 5500},
    "gpt-4o":       {"latin": 8000,  "cyrillic": 4500},
    "gpt-4o-mini":  {"latin": 6000,  "cyrillic": 3500},
    # ChatGPT subscription through Codex CLI. Conservative sizes keep strict JSON
    # responses below the practical per-turn output ceiling and enable continuity.
    "codex:gpt-5.6-terra": {"latin": 3000, "cyrillic": 2200},
    "codex:gpt-5.6-sol":   {"latin": 3000, "cyrillic": 2200},
    "codex:gpt-5.6-luna":  {"latin": 2500, "cyrillic": 1800},
    "codex:gpt-5.5":       {"latin": 3000, "cyrillic": 2200},
    "codex:gpt-5.4":       {"latin": 3000, "cyrillic": 2200},
    "codex:gpt-5.4-mini":  {"latin": 2500, "cyrillic": 1800},
}
# Fallback для неизвестных моделей (custom IDs / новые / старые pinned snapshots)
DEFAULT_CHUNK_WORDS_FALLBACK = {"latin": 5000, "cyrillic": 3500}

# DEPRECATED: legacy плоская таблица. Оставлено для обратной совместимости
# с любыми устаревшими вызовами. Реальный код использует get_chunk_size_words.
GENERATION_CHUNK_WORDS = DEFAULT_CHUNK_WORDS_FALLBACK


def get_chunk_size_words(model: str, lang_name: str) -> int:
    """Возвращает max words per chunk для (model, language).

    Lookup case-insensitive по канонической alias-форме (`claude-opus-4-7` etc).
    Для неизвестных моделей — fallback (5000 latin / 3500 cyrillic).
    """
    script = "cyrillic" if lang_name in CYRILLIC_LANGUAGES else "latin"
    # FIX-2026-06-29: env-override GEN_CHUNK_WORDS — меньше слов/чанк → больше чанков → ПОЛНАЯ длина.
    # Большой чанк → модель «закругляется» и недобирает (CZ-6: 5408/6200 на 1 чанке 7000). Tunable без редеплоя.
    # Раздельно latin/cyrillic: GEN_CHUNK_WORDS (latin) и GEN_CHUNK_WORDS_CYR (cyrillic), оба опциональны.
    _envk = "GEN_CHUNK_WORDS_CYR" if script == "cyrillic" else "GEN_CHUNK_WORDS"
    _env = os.environ.get(_envk)
    if _env:
        try:
            return max(500, int(_env))
        except Exception:
            pass
    model_key = (model or "").strip().lower()
    if model_key in MODEL_CHUNK_DEFAULTS:
        return MODEL_CHUNK_DEFAULTS[model_key][script]
    return DEFAULT_CHUNK_WORDS_FALLBACK[script]


def calculate_generation_chunks(
    total_words: int,
    lang_name: str,
    model: str = "",
    chunk_size_override: Optional[int] = None,
) -> Tuple[int, int]:
    """Рассчитывает количество chunks и слов на chunk для генерации.

    Args:
        total_words: целевая длина всего script.
        lang_name: canonical language name (English/Russian/...).
        model: model alias для per-model default. Если пусто — fallback.
        chunk_size_override: пользовательский override (UI поле). Если задан
            и >= 500 — используется вместо per-model default.

    Returns: (num_chunks, words_per_chunk).
    """
    if chunk_size_override and chunk_size_override >= 500:
        max_words = chunk_size_override
    else:
        max_words = get_chunk_size_words(model, lang_name)
    num_chunks = max(1, math.ceil(total_words / max_words))
    words_per_chunk = total_words // num_chunks
    # FIX-2026-06-29: GEN_LENGTH_FACTOR — модель отдаёт стабильно ~82% запрошенного (замерено на 14 джобах,
    # независимо от chunk_size). Просим в factor раз больше → отдаёт ~100%, БЕЗ повторных запросов (recovery).
    # Калибровка, не догадка: 1/0.82 ≈ 1.22. Env-tunable; 1.0 = выключено.
    _factor = float(os.environ.get("GEN_LENGTH_FACTOR", "1.0"))
    if _factor and _factor != 1.0:
        words_per_chunk = int(words_per_chunk * _factor)
    return num_chunks, words_per_chunk


# ============== Generation v2 helpers (Round 2 of refactor, 2026-05-23) ==============
# chunk-loop с JSON-output + rotation log. Постпроцесс (Pass A/B/C/D) применяется
# отдельной стадией ПОСЛЕ chunk-loop через существующий run_postprocess_pipeline().

ROTATION_LOG_DIR_BASENAME = "rotation_logs"
_GEN_V2_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)

# === Preset content split (variant B, 2026-05-23) ===
# Большие preset.content (~30K chars) при N chunk-вызовах подряд упираются
# в Anthropic rate-limit на крупных prompts. Решение: split на core directives
# (всегда передаём в каждый chunk) + reference data (только chunk 0).
REFERENCE_MARKER_RE = re.compile(r"\n[ \t]*---\s*REFERENCE\s*---[ \t]*\n", re.IGNORECASE)
LARGE_CONTENT_THRESHOLD = 15000
CORE_TRUNCATE_TO = 8000


def split_preset_content(content: str) -> Tuple[str, Optional[str]]:
    """Returns (core_for_all_chunks, reference_for_chunk0_only_or_None).

    Стратегии в порядке приоритета:
    (a) Explicit marker `---REFERENCE---` (юзер сам выбирает где split)
    (b) Length-based fallback при len(content) > LARGE_CONTENT_THRESHOLD
    (c) Иначе — возвращаем (content, None), split не нужен
    """
    if not content:
        return "", None
    m = REFERENCE_MARKER_RE.search(content)
    if m:
        return content[: m.start()].rstrip(), content[m.end():].lstrip()
    if len(content) > LARGE_CONTENT_THRESHOLD:
        return (
            content[:CORE_TRUNCATE_TO].rstrip() + "\n[... reference data only on first chunk ...]",
            content[CORE_TRUNCATE_TO:].strip(),
        )
    return content, None


def _char_length_line(target_words: int, language: str) -> str:
    """PATCH-2026-06-28-words: инструкция длины В СЛОВАХ («Слов», НЕ «слов буквенных»/символах) + само-перепроверка ПЕРЕД ответом."""
    # PATCH-2026-07-18-v6: длина через АБЗАЦЫ — дискретный счёт модель исполняет,
    # непрерывный счёт слов — нет (эмпирика: стабильно ~60% от просимого в словах).
    paras = max(4, round(target_words / 105))
    return (
        f"LENGTH — write EXACTLY {paras} paragraphs in 'script'. Each paragraph = 90-120 words "
        f"(4-6 spoken sentences); a paragraph under 60 words is a fragment — merge or grow it. "
        f"{paras} paragraphs x ~105 words lands on the ~{target_words}-word target automatically, so do NOT "
        f"try to count words — count PARAGRAPHS, that is the only length control. "
        f"SELF-CHECK BEFORE YOU ANSWER — mandatory: count the paragraphs in your 'script' (separated by blank "
        f"lines). Fewer than {paras}? The part is UNFINISHED — add full new paragraphs of real material (a scene, "
        f"a fact, a turn that belongs here) until you reach EXACTLY {paras}. Never pad with filler or repetition. "
        f"Output the JSON only when the paragraph count is exactly {paras}."
    )


def build_funnel_prompt(
    preset_content: str,
    topic: str,
    language: str,
    target_words: int,
    num_chunks: int,
    words_per_chunk: int,
    host_name: str = "",
    channel_name: str = "",
    retry_hint: str = "",   # PATCH-2026-06-27-thinking: жёсткий хинт на ретрае после не-JSON ответа
) -> List[Dict[str, str]]:
    """PATCH-2026-06-27-funnelstep: отдельный шаг — Claude проектирует воронку ПЕРЕД генерацией.
    Возвращает messages для одного вызова, который выдаёт {preview, title, hook, plan}, где
    plan — массив из num_chunks строк, каждая = аутлайн части с БЮДЖЕТОМ слов на каждый бит
    (сумма ≈ words_per_chunk). План = бюджет длины, чтобы текст шёл нужной длины за один проход."""
    parts_n = max(1, int(num_chunks or 1))
    per = int(words_per_chunk) if words_per_chunk else (int(target_words) // parts_n if parts_n else int(target_words))
    # PATCH-2026-06-28-words: бюджет плана в СЛОВАХ («Слов», НЕ символах/буквенных — иначе текст выходит короче).
    # PATCH-2026-07-18-v5: воронка проектирует ретеншн-карту, не только бюджет длины.
    system = (
        "You are a senior YouTube producer and long-form story architect. You design the engagement "
        "FUNNEL (preview -> title -> hook -> a RETENTION MAP: which loop opens/closes where, where "
        "attention is re-hooked, where the title promise pays off) BEFORE any script is written. "
        "The map doubles as a length budget. "
        "Output ONLY a single valid JSON object — first character '{', last '}'. No prose, no markdown fences."
    )
    chan_line = f"Channel name: {channel_name}.\n" if channel_name else ""
    host_line = f"Host / narrator: {host_name}.\n" if host_name else ""
    user = (
        f"CHANNEL PRESET (obey its style, audience, voice):\n{preset_content}\n\n"
        f"{chan_line}{host_line}"
        f"FINAL VIDEO LANGUAGE: {language}.\n"
        f"VIDEO IDEA / TOPIC:\n{topic}\n\n"
        f"Design the engagement FUNNEL for a narration video of about {target_words} words total "
        f"(count «Слов» — tokens separated by spaces, NOT «слов буквенных»/letter-fragments), split into "
        f"{parts_n} part(s) of about {per} words each.\n\n"
        "Output ONLY this JSON:\n"
        "{\n"
        '  "preview": "1-5 words for the thumbnail that grab the target audience (CAPS allowed)",\n'
        '  "title": "<=60 characters, explains the preview, concrete over clickbait, makes them click",\n'
        '  "hook": "1-2 sentences that confirm the preview+title but break off on an OPEN LOOP so the viewer cannot stop watching",\n'
        f'  "plan": ["<part 1 outline>", "... EXACTLY {parts_n} item(s) total ..."]\n'
        "}\n\n"
        "PLAN RULES — each part string is a RETENTION MAP and a LENGTH BUDGET in WORDS («Слов», NOT «слов буквенных» or characters), obey both:\n"
        f"- 'plan' MUST be a JSON array of EXACTLY {parts_n} string(s), one per part.\n"
        "- START each part string with ONE header line: 'LOOPS: open=Lx: promise in 5-8 words | keep-open=ids | "
        "close=Lx at ~P percent of the WHOLE video'. Loops are numbered L1-L5 across the WHOLE video. L1 = the TITLE "
        "promise: it OPENS inside the hook and CLOSES (pays off) inside the 40-70 percent window of the whole "
        "video — NEVER later than 70 percent: a payoff pushed into the last third is the strongest flop pattern. "
        "THE MOMENT L1 closes, the SAME part OPENS a new arc (the price paid, who allowed it, the aftermath "
        "mystery) — a plateau after the payoff is forbidden. Plan smaller loops that pay off stepwise before L1 "
        "(around 20-35 percent) and one strong loop that carries the tail after it (closing around 80-90 percent). If the topic/seed already numbers its own "
        "hooks and loops (PLAN/HOOKS section), MAP THOSE into L1-L5 — do not invent competing ones.\n"
        f"- After the header, break the part into 7-11 BEATS. Write each beat as: 'concrete beat (~N words | NEW: the "
        f"one new fact, name, document, place, object or reversal this beat adds | loop op: OPEN Lx / CLOSE Lx / "
        f"none)'. The N values within one part MUST sum to AT LEAST {per} words. A beat whose NEW field would repeat "
        f"already-planned information is FORBIDDEN — plan fewer, longer scenes rather than repetition beats.\n"
        "- ENTRY (part 1 only): hook (1-2 sentences) -> one-sentence title-card promise (what this video will show) "
        "-> the FIRST CONCRETE SCENE begins within the first 250 words: a person, in a place, at a moment — "
        "background history comes later, in slices. The hook text itself MUST open loop L1 AND plant 2-3 DIFFERENT "
        "secondary loops — a MENU of concrete riddles, each anchored in a name, a number, an object or a date "
        "(an abstract announcement is not a loop) — as unresolved breaks (a fact cut mid-consequence), not as statements — if the hook resolves everything "
        "it announces, rewrite it.\n"
        "- RE-HOOKS: every 600-900 words schedule ONE structural re-hook and name it inside a beat: CHAPTER TURN (a "
        "one-line mini-title promise), or IRONY-FORWARD (one line of fate-knowledge at a section join, e.g. 'it was "
        "his last victory'), or TRIVIA SPIKE (one startling verifiable aside). Rhetorical questions are NOT re-hooks "
        "and must not appear in the plan.\n"
        "- SCALE SHIFTS: alternate beat scale — after 2-3 close-up scene beats, one wide-shot beat (map, army, era), "
        "then back to close-up.\n"
        "- FINALE: exactly ONE finale. L1 closes at 40-70 percent — usually NOT in the last part; the last part "
        "closes the post-payoff arc. The L1 payoff must redeem the TITLE literally, with the SAME entities: if the "
        "title promises a revenge, a secret, N items — the payoff names that revenge / that secret / exactly N "
        "(bait-and-switch is failure). After the final close only NEW aftermath material (fates, consequences), "
        "then a short sign-off. NOTHING after it may recap, re-explain or return to earlier scenes.\n"
        f"- Be SPECIFIC (real beats with real content, never 'introduction'/'conclusion'). Adapt to the {language} "
        f"audience mentality. Numbers and dates as words.\n"
        "- Inside beat descriptions do NOT use double-quote characters — write any quoted phrase with single quotes "
        "or none. Unescaped double quotes inside the JSON strings break parsing.\n"
        "\nCRITICAL — OUTPUT FORMAT (obey exactly):\n"
        "- Your ENTIRE response IS the JSON object and nothing else. The FIRST character you output is '{' and the LAST is '}'.\n"
        "- Do NOT think out loud, do NOT plan in prose, do NOT acknowledge. Write NOTHING before '{' — no 'I'll design...', "
        "no 'Let me...', no 'Here is...', no commentary. Emit the JSON directly.\n"
        "- Do NOT wrap in markdown fences. Do NOT add any text after '}'.\n"
    )
    if retry_hint:
        user = retry_hint.strip() + "\n\n" + user
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _repair_funnel_json(s: str) -> Optional[Dict[str, Any]]:
    """PATCH-2026-06-27-funnelrobust: чинит ОБОРВАННЫЙ JSON воронки — балансирует строки/скобки
    от первой '{' и дозакрывает. Лечит truncation (ответ обрезан на середине плана)."""
    start = s.find("{")
    if start < 0:
        return None
    frag = s[start:]
    stack = []
    in_str = False
    esc = False
    for ch in frag:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append(ch)
            elif ch == "}":
                if stack and stack[-1] == "{":
                    stack.pop()
            elif ch == "]":
                if stack and stack[-1] == "[":
                    stack.pop()
    repaired = frag
    if in_str:
        repaired += '"'  # закрыть оборванную строку
    repaired = re.sub(r",\s*$", "", repaired)  # убрать висящую запятую
    for opener in reversed(stack):
        repaired += "]" if opener == "[" else "}"
    try:
        obj = json.loads(repaired)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _salvage_funnel_fields(s: str) -> Optional[Dict[str, Any]]:
    """PATCH-2026-06-27-funnelrobust: посимвольный salvage полей воронки (когда JSON битый,
    напр. неэкранированная кавычка в польском описании). Тянет preview/title/hook + элементы plan."""
    out: Dict[str, Any] = {}
    for key in ("preview", "title", "hook"):
        m = re.search(r'"' + key + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', s)
        if m:
            try:
                out[key] = json.loads('"' + m.group(1) + '"')
            except Exception:
                out[key] = m.group(1)
    pm = re.search(r'"plan"\s*:\s*\[', s)
    plan: List[str] = []
    if pm:
        rest = s[pm.end():]
        for sm in re.finditer(r'"((?:[^"\\]|\\.)*)"', rest):
            seg = sm.group(1)
            try:
                plan.append(json.loads('"' + seg + '"'))
            except Exception:
                plan.append(seg)
            if re.match(r"\s*\]", rest[sm.end():sm.end() + 6]):
                break
    if plan:
        out["plan"] = plan
    return out if (out.get("hook") or out.get("plan") or out.get("preview")) else None


def parse_funnel_response(raw: str) -> Optional[Dict[str, Any]]:
    """PATCH-2026-06-27-funnelstep/funnelrobust: парсит {preview,title,hook,plan[]} из ответа шага воронки.
    Стратегии (как в parse_chunk_response): strict → raw_decode → truncation-repair → field-salvage."""
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    obj = None
    # 1) strict
    try:
        cand = json.loads(s)
        if isinstance(cand, dict):
            obj = cand
    except Exception:
        pass
    # 2) raw_decode от первой '{' (игнорит хвост/markdown/преамбулу)
    if obj is None:
        start = s.find("{")
        if start >= 0:
            try:
                cand, _ = json.JSONDecoder().raw_decode(s[start:])
                if isinstance(cand, dict):
                    obj = cand
            except Exception:
                pass
    # 3) truncation-repair (оборванный план)
    if obj is None:
        obj = _repair_funnel_json(s)
    # 4) field-salvage (битые кавычки)
    if obj is None:
        obj = _salvage_funnel_fields(s)
    if not isinstance(obj, dict):
        return None
    plan = obj.get("plan")
    if isinstance(plan, list):
        plan = [str(x).strip() for x in plan if x is not None and str(x).strip()]
    elif isinstance(plan, str) and plan.strip():
        plan = [plan.strip()]
    else:
        plan = []
    return {
        "preview": str(obj.get("preview", "") or "").strip(),
        "title": str(obj.get("title", "") or "").strip(),
        "hook": str(obj.get("hook", "") or "").strip(),
        "plan": plan,
    }


def build_chunk_prompt(
    preset_content: str,
    topic: str,
    chunk_idx: int,
    num_chunks: int,
    target_words_this_chunk: Optional[int],
    previous_tail: str,
    recent_log_phrases: List[str],
    language: str,
    host_name: Optional[str] = None,
    channel_name: Optional[str] = None,
    retry_hint: Optional[str] = None,
    plan_for_chunk: str = "",   # PATCH-2026-06-27-funnelstep: внешний план части (бюджет длины)
    hook: str = "",             # внешний хук из шага воронки (для первого чанка)
) -> List[Dict[str, str]]:
    """Сборка messages для одного chunk-вызова в новом generation flow.

    `retry_hint`: если задан — приписывается в конец user message ПЕРЕД повторной
    отправкой Claude. Это нужно потому что claude-code-sdk flattens messages list
    в одну строку (берёт только последний user message), поэтому простой append
    к messages теряет preset+topic контекст.
    """
    system = (
        "You are a YouTube script writer. Apply the channel preset strictly. "
        "Output ONLY a valid JSON object — no markdown wrapper, no prefix or suffix text, no comments."
    )

    # Split на core (всегда) + reference (только chunk 0).
    # Снижает prompt-size на chunks 1+ с ~30K до ~10K при больших preset'ах.
    core_content, reference_content = split_preset_content(preset_content)
    is_first_chunk = (chunk_idx == 0)

    user_parts: List[str] = [f"Channel preset (core directives):\n{core_content}\n"]
    if reference_content:
        if is_first_chunk:
            user_parts.append(
                "Channel preset (reference data — apply this style throughout the script):\n"
                f"{reference_content}\n"
            )
        else:
            user_parts.append(
                "(Reference data was provided in the first chunk — continue applying the same "
                "style and rotation_pool choices established there.)\n"
            )
    user_parts.append(f"Language: {language}")
    if host_name:
        user_parts.append(f"Host name: {host_name}")
    if channel_name:
        user_parts.append(f"Channel name: {channel_name}")
    user_parts.append(f"\nTopic / source material:\n{topic}")
    # Rotation log block убран 2026-06-03 — в conversation-mode модель сама помнит
    # ранее использованные фразы; для single-shot тоже признан мало-полезным.
    # Параметр recent_log_phrases оставлен в сигнатуре (backward compat callers),
    # но игнорируется. log_update в JSON-схеме оставлен (parse терпит пустой).
    _ = recent_log_phrases  # explicit ignore

    is_first = chunk_idx == 0
    is_last = chunk_idx == num_chunks - 1

    if num_chunks == 1:
        position_lines = ["Write the ENTIRE script as one piece."]
        if target_words_this_chunk:
            # PATCH-2026-06-27-chars: длина в СИМВОЛАХ + само-перепроверка перед ответом (один проход, без повторов).
            position_lines.append(_char_length_line(target_words_this_chunk, language))
        # PATCH-2026-06-27-funnel (item 4/5): сильный хук-обрыв + байт на подписку в середине + аутро.
        position_lines.append(
            "Open with a strong HOOK: 1-2 sentences, naturally embedded (NO separate header), that confirm "
            "the title/topic and break off on an OPEN LOOP so the viewer cannot stop watching "
            "(intrigue / mystery gap — never cheap clickbait)."
        )
        position_lines.append(
            "Around the MIDDLE of the script, naturally insert ONE short call-to-action (like + subscribe) "
            "mentioning the channel name — a single in-voice sentence, never jarring."
        )
        position_lines.append("END with a short outro / sign-off that names the host and the channel.")
        # PATCH-2026-06-27-funnel (item 3): inline-воронка — ТОЛЬКО если нет внешнего плана (funnelstep ниже).
        if not plan_for_chunk:
            position_lines.append(
                "FUNNEL — design it BEFORE writing and output it in the SAME JSON: 'preview' (1-5 words for the "
                "thumbnail), 'title' (<=60 chars, makes them click), 'hook' (your exact 1-2-sentence open-loop hook), "
                "'plan' (a short outline of the key beats/turns that keep the viewer to the very end). "
                "Then write the 'script' so it OPENS with your 'hook' and FOLLOWS your 'plan' throughout."
            )
        output_keys = '{"preview": "...", "title": "...", "hook": "...", "plan": "...", "script": "...", "log_update": [...]}'
    else:
        position_lines = [f"This is chunk {chunk_idx + 1} of {num_chunks}."]
        if target_words_this_chunk:
            # PATCH-2026-06-27-chars: длина в СИМВОЛАХ + само-перепроверка перед ответом.
            position_lines.append(_char_length_line(target_words_this_chunk, language))
        if is_first:
            # PATCH-2026-06-27-funnel (item 4): сильный хук-обрыв.
            position_lines.append(
                "OPEN with a strong HOOK: 1-2 sentences, naturally embedded (NO header), confirming the "
                "title/topic and breaking off on an OPEN LOOP so the viewer cannot stop watching "
                "(intrigue / mystery gap — never cheap clickbait)."
            )
            # item 3: inline-воронка в chunk 0 — ТОЛЬКО если нет внешнего плана (funnelstep).
            if not plan_for_chunk:
                position_lines.append(
                    "FUNNEL — design it BEFORE writing and output it in the SAME JSON: 'preview' (1-5 words), "
                    "'title' (<=60 chars), 'hook' (your exact open-loop hook above), 'plan' (key beats/turns that keep "
                    "the viewer to the very END of the WHOLE video). Open this chunk's 'script' with your 'hook' and "
                    "follow your 'plan'; later chunks will continue the SAME plan."
                )
        elif is_last:
            # PATCH-2026-06-27-chars: финальный чанк — символьный коридор + self-check, аутро только ПОСЛЕ.
            if target_words_this_chunk:
                position_lines.append(_char_length_line(target_words_this_chunk, language))
            position_lines.append(
                "This is the FINAL chunk: FIRST fully develop the remaining material and SELF-CHECK you are inside the "
                "character range above; only AFTER you have verified the count, close with a short outro/sign-off that "
                "NAMES the host and the channel. If no host or channel name is given in this prompt, do NOT invent "
                "one — close with a neutral sign-off without any brand, host name or narrator identity. "
                "Do NOT rush, summarize, or wind down early."  # item 5: аутро
            )
        else:
            position_lines.append("CONTINUE the narrative at full length — do NOT wind down or conclude here.")
        # PATCH-2026-06-27-funnel (item 5): ровно ОДИН байт на подписку — в центральном чанке.
        if num_chunks >= 2 and chunk_idx == num_chunks // 2:
            position_lines.append(
                "In THIS chunk, naturally insert ONE short call-to-action (like + subscribe) mentioning the "
                "channel name — a single in-voice sentence, never jarring. (Only here, not in other chunks.) "
                "If no channel name is given in this prompt, SKIP the call-to-action entirely and do NOT invent "
                "a channel or host name."
            )
        if previous_tail and not is_first:
            position_lines.append(f'Previous chunk ended with:\n"""{previous_tail}"""')
        if is_first:
            output_keys = '{"preview": "...", "title": "...", "hook": "...", "plan": "...", "script": "...", "log_update": [...]}'
        else:
            output_keys = '{"script": "...", "log_update": [...]}'

    # PATCH-2026-06-27-funnelstep: внешняя воронка — пишем по плану-бюджету части + готовым хуком (chunk 0).
    if plan_for_chunk:
        # PATCH-2026-07-18-v5: план = ретеншн-карта; вход в первую сцену ≤250 слов.
        position_lines.append(
            "PLAN FOR THIS PART (the retention map from the funnel step) — follow it EXACTLY: write EACH beat to its "
            "~word budget («Слов»), DELIVER each beat's NEW element, execute its loop ops (OPEN = plant once, then "
            "silence; CLOSE = full payoff at the planned position; loops marked keep-open MUST still be open when "
            "this part ends), and place the scheduled re-hooks as one-line structural turns:\n" + plan_for_chunk
        )
        if chunk_idx == 0 and hook:
            position_lines.append(
                "Open the script with THIS exact hook (it was designed for this video; adapt it naturally into "
                + language + ", keep it an open loop): " + hook + " — then ONE sentence of title-card promise, then "
                "enter the FIRST concrete scene within the first 250 words (a person, in a place, at a moment). No "
                "host self-introduction longer than one sentence, no background lecture before a human being appears."
            )
        if chunk_idx == 0:
            output_keys = '{"preview": "...", "title": "...", "script": "...", "log_update": [...]}'

    user_parts.append("\n" + "\n".join(position_lines))
    # PATCH-2026-07-18-v5: правила интереса (6 подтверждённых причин разрыва с конкурентами).
    user_parts.append(
        "\nINTEREST RULES — apply to EVERY paragraph UNLESS the CHANNEL PRESET explicitly "
        "requires a different format, voice or genre — on any conflict the preset wins:\n"
        "1. NEW-INFO RULE: every paragraph must add at least one element the viewer does not yet have — a fact, a "
        "name, a place, a document, a physical object, a sensory period detail, or a reversal. NEVER re-state in new "
        "words what was already told; a paradox or list of clues may be voiced ONCE, never returned to. If verified "
        "facts for a stretch run out, advance with concrete period texture (what that room, street, machine, uniform "
        "of that era looked, sounded, smelled like) — texture moves the scene forward, rephrasing does not.\n"
        "2. SHOW, THEN CUT: carry emotion with ONE physical detail per beat (an object, a measurement, a gesture), "
        "then CUT to the next fact. After a strong scene: ZERO commentary paragraphs. FORBIDDEN narrator moves: "
        "pausing to tell the viewer what to feel ('let us pause over this', 'think how much courage that took', "
        "'imagine this...'), and praising your own material ('no words are needed', 'this says more than any "
        "number'). Maximum 2 such constructions per WHOLE video — zero is better.\n"
        "3. LOOP DISCIPLINE: when a loop opens — plant it once and go SILENT until its payoff. 'Remember this', "
        "'we will come back to this', 'more on that later' are FORBIDDEN (max 2 addressed forward-flags per WHOLE "
        "video). To point at the future use third-person fate-prolepsis at a name's first appearance instead: "
        "'he had eleven months left to live.'\n"
        "4. RE-HOOKS ARE STRUCTURAL: renew attention with the planned chapter turns, irony-forward lines and trivia "
        "spikes — NOT with rhetorical questions. Questions addressed to the viewer are allowed ONLY within the first "
        "150 words of the video; NEVER ask a question and answer it in the same breath.\n"
        "5. SENTENCE RHYTHM: default sentence 12-20 words with the turn INSIDE it ('he had won everything — but the "
        "order never came'); a paragraph should not end with the valence it began with. Sentences of 5 words or "
        "fewer: at most 15 percent of all sentences, reserved for genuine peaks. Aphorism-enders: at most one per 3 "
        "paragraphs. The central metaphor: at most once per 1000 words. TTS-friendly means CLEAR clauses, not "
        "staccato fragments.\n"
        "6. UNCERTAINTY = INTRIGUE, NOT A SEMINAR: where sources disagree or facts are missing, spend ONE sentence "
        "('why — remains a mystery; the files were burned') or a two-line rumor-loop (version one / version two), "
        "then move on. Source-criticism blocks over 50 words are forbidden; a numbers dispute may be voiced ONCE per "
        "video, in one sentence. NEVER voice the preset's or brief's internal fact-checking instructions in the "
        "narration."
    )
    user_parts.append(
        "\nCRITICAL OUTPUT RULES:\n"
        "- Output ONLY valid JSON matching the schema below.\n"
        "- Your ENTIRE response MUST be a single JSON object: the FIRST character is { and the LAST is }. Write NOTHING before { — no preamble, no plan, no acknowledgement sentence (do NOT start with phrases like I will / Let me).\n"
        "- Properly escape every double-quote and line break inside string values so the whole object stays valid JSON. The script must be ONE valid JSON string.\n"
        "- Do NOT ask clarifying questions. Make reasonable assumptions and proceed.\n"
        "- Do NOT wrap JSON in markdown fences (no ```json).\n"
        "- Do NOT add any text before or after the JSON object.\n"
        "- If the topic is ambiguous, pick the most plausible interpretation silently.\n"
        f"\nOutput STRICTLY this JSON shape:\n{output_keys}\n\n"
        "Rules:\n"
        f"- TTS-ready: no markdown, no brackets, numbers/dates/currencies/abbreviations as WORDS in {language}, no remarks like '(applause)'.\n"
        "- Apply preset's structure, voice anchors, anti-LLM bans, forbidden topics if present.\n"
        "- If preset has rotation_pools — pick phrases naturally; report which pool IDs used in log_update.\n"
        "- `script` must be a single string (no nested objects)."
    )

    if retry_hint:
        user_parts.append(f"\n⚠️ RETRY INSTRUCTION: {retry_hint}")

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def _parse_truncated_chunk_json(raw: str) -> Optional[Dict[str, Any]]:
    """Repair-стратегия для обрезанных ответов Claude.

    Когда Claude не дописал JSON (truncation), `"script": "..."` остаётся
    без закрывающей кавычки и `}`. Берём текст от первого `{` до последнего
    содержательного символа, дописываем `"}` и пытаемся распарсить.
    Шаг бэктрекинга 200 chars (компромисс speed/coverage).
    """
    if not raw:
        return None
    start = raw.find("{")
    if start < 0:
        return None
    tail = raw[start:]
    max_back = min(len(tail), 8000)  # repair тратит время, ограничиваем 8K
    for end in range(len(tail), max(0, len(tail) - max_back), -200):
        # repair-кандидат: обрезаем хвост, чистим висячую запятую, закрываем `"}`
        candidate = tail[:end].rstrip().rstrip(",")
        # Эвристика: если последний значимый кусок — незавершённая строка,
        # дописываем `"` и `}`. Иначе только `}`.
        if not candidate.endswith('"') and not candidate.endswith("}"):
            candidate = candidate + '"'
        candidate = candidate + "}"
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and isinstance(obj.get("script"), str) and len(obj["script"]) > 100:
                return obj
        except json.JSONDecodeError:
            continue
    return None


def parse_chunk_response(
    raw: str,
    expect_funnel: bool,
    job_id: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Парсит strict-JSON ответ. Возвращает dict с {preview,title,script,log_update}
    или None если парс упал. expect_funnel=True для первого chunk (preview+title обязательны).

    Стратегии:
      1) Strict json.loads.
      2) raw_decode — берёт ПЕРВЫЙ JSON-объект, игнорирует hvost (дубль `{...}{...}`,
         markdown wrapper).
      3) Truncation-repair (_parse_truncated_chunk_json) — добивает обрезанные `"}`.
      4) Trailing-garbage repair — чистит хвост вида `"]}"}}` (наблюдалось у Haiku 4.5).

    Forensics: если raw не похож на JSON (нет '{' в первых 200 chars), пишем
    /var/log/rewrite/clarifying_responses.log для будущего анализа паттерна.
    """
    if not raw or not raw.strip():
        return None
    parsed: Optional[Dict[str, Any]] = None
    raw_stripped = raw.strip()

    # Strategy 1: direct
    try:
        cand = json.loads(raw_stripped)
        if isinstance(cand, dict):
            parsed = cand
    except json.JSONDecodeError:
        pass

    # Strategy 2: raw_decode (берёт ПЕРВЫЙ JSON-объект, игнорирует hvost)
    if parsed is None:
        start = raw_stripped.find("{")
        if start >= 0:
            try:
                decoder = json.JSONDecoder()
                cand, _end = decoder.raw_decode(raw_stripped[start:])
                if isinstance(cand, dict):
                    parsed = cand
            except json.JSONDecodeError:
                pass

    # Strategy 3: truncation-repair
    if parsed is None:
        parsed = _parse_truncated_chunk_json(raw_stripped)

    # Strategy 4: trailing-garbage repair (Haiku 4.5 issue — мусорный хвост `"]}"}}`)
    if parsed is None:
        try:
            cleaned = re.sub(r'"\]\}"\}+\s*$', '"]}', raw_stripped)
            cleaned = re.sub(r'\}+\s*$', '}', cleaned)  # дублирующиеся закрывающие скобки
            cand = json.loads(cleaned)
            if isinstance(cand, dict):
                parsed = cand
        except (json.JSONDecodeError, re.error):
            pass

    if parsed is None:
        # Strategy 5 (salvage): строгие парсеры упали (битый JSON: преамбула перед "{",
        # кривое экранирование длинного script у opus-4-8). Достаём поля по имени
        # следующего ключа (терпит пробелы в разделителях). Без regex.
        # PATCH-2026-06-27-funnel: hook/plan — boundary-маркеры, чтобы title не захватывал их хвост на битом JSON.
        _GEN_KEYS = ("script", "title", "preview", "hook", "plan", "log_update")

        def _gen_field(name, s):
            i = s.find('"' + name + '"')
            if i == -1:
                return None
            j = s.find(":", i)
            if j == -1:
                return None
            k = s.find('"', j)
            if k == -1:
                return None
            start = k + 1
            end = len(s)
            # M4 FIX-2026-07-02: границу поля ищем НЕ по первому вхождению имени ключа
            # (закавыченная цитата/прямая речь внутри script раньше обрезала контент),
            # а по паттерну реального top-level JSON-ключа: закрывающая кавычка значения
            # + опц. пробелы + запятая + опц. пробелы + "ключ" + опц. пробелы + двоеточие.
            _others = "|".join(re.escape(o) for o in _GEN_KEYS if o != name)
            if _others:
                _bound = re.compile(r'"\s*,\s*"(?:' + _others + r')"\s*:')
                m = _bound.search(s, start)
                if m is not None:
                    end = m.start()  # позиция закрывающей кавычки значения
            # PATCH-2026-07-18-v5.4: multi-emit clamp — шов между объектами в любом виде:
            # "}{", "}\n{", "]}{", "]{" (закрытие log_update-массива) — режем регексом.
            if name == "script":
                _seam_m = re.search(r'"\s*[\]\}]+\s*,?\s*\{\s*"', s[start:end])
                if _seam_m is not None:
                    end = start + _seam_m.start() + 1
            seg = s[start:end].rstrip()
            if seg.endswith(","):
                seg = seg[:-1].rstrip()
            if seg.endswith('"'):
                seg = seg[:-1]
            return seg

        def _gen_unesc(x):
            try:
                return json.loads('"' + x + '"')
            except Exception:
                return x.strip()

        try:
            _sc = _gen_field("script", raw_stripped)
            if _sc is not None:
                _salv = {
                    "preview": _gen_unesc(_gen_field("preview", raw_stripped) or ""),
                    "title": _gen_unesc(_gen_field("title", raw_stripped) or ""),
                    "script": _gen_unesc(_sc),
                    "log_update": [],
                }
                _ok = bool(_salv["script"].strip()) and (
                    (not expect_funnel) or (_salv["preview"].strip() and _salv["title"].strip())
                )
                if _ok:
                    parsed = _salv
        except Exception:
            pass

    if parsed is None:
        # Forensics: если ответ не похож на JSON (модель ушла в clarifying вопросы / текст),
        # сохраняем для будущей диагностики паттерна — без падения worker'а.
        try:
            if "{" not in raw_stripped[:200]:
                from datetime import datetime as _dt
                from pathlib import Path as _P
                _log_dir = _P("/var/log/rewrite")
                _log_dir.mkdir(parents=True, exist_ok=True)
                _log_path = _log_dir / "clarifying_responses.log"
                with open(_log_path, "a", encoding="utf-8") as _lf:
                    _lf.write(
                        f"\n=== {_dt.utcnow().isoformat()}Z "
                        f"job={job_id or 'unknown'} model={model or 'unknown'} ===\n"
                    )
                    _lf.write(raw_stripped[:3000])
                    _lf.write("\n")
        except Exception:
            pass  # forensics best-effort, не ломаем worker
        return None

    required = {"script", "log_update"}
    if expect_funnel:
        required = required | {"preview", "title"}
    # Для repair-кандидата log_update может отсутствовать — допускаем пустой
    if "script" not in parsed:
        return None
    if expect_funnel and not all(k in parsed for k in ("preview", "title")):
        # Если repair восстановил только script но потерял preview/title — fail
        return None

    log_update_raw = parsed.get("log_update", [])
    if not isinstance(log_update_raw, list):
        log_update_raw = []

    return {
        "preview": str(parsed.get("preview", "") or "").strip(),
        "title": str(parsed.get("title", "") or "").strip(),
        "script": _normalize_literal_escapes(str(parsed.get("script", "") or "").strip()),  # FIX-2026-06-29: литеральные \n\n → реальные
        "log_update": [str(x).strip() for x in log_update_raw if x],
    }


def get_rotation_log_path(base_dir: str, user_id: str, preset_id: str) -> str:
    """Sanitized path для rotation log."""
    safe_uid = re.sub(r"[^\w-]", "_", user_id or "default")[:50]
    safe_pid = re.sub(r"[^\w-]", "_", preset_id or "default")[:80]
    return os.path.join(base_dir, ROTATION_LOG_DIR_BASENAME, safe_uid, f"{safe_pid}.log.json")


def _try_parse_content_as_json(content: str) -> Optional[Dict[str, Any]]:
    """Пытается распарсить preset.content как JSON. None если не JSON или не dict."""
    if not content:
        return None
    s = content.strip()
    # Если строка начинается с { — пробуем парс
    if not s.startswith("{"):
        return None
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def extract_field_from_content(content: str, field: str, default: str = "") -> str:
    """Если content — JSON dict, читает поле; иначе default."""
    d = _try_parse_content_as_json(content)
    if d and isinstance(d.get(field), str):
        return d[field]
    return default


# ============== ЯЗЫКИ ==============

LANGUAGES = {
    "Русский": "Russian",
    "Украинский": "Ukrainian",
    "Белорусский": "Belarusian",
    "Польский": "Polish",
    "Чешский": "Czech",
    "Словацкий": "Slovak",
    "Болгарский": "Bulgarian",
    "Сербский": "Serbian",
    "Хорватский": "Croatian",
    "Словенский": "Slovenian",
    "Македонский": "Macedonian",
    "Боснийский": "Bosnian",
    "Английский": "English",
    "Немецкий": "German",
    "Французский": "French",
    "Испанский": "Spanish",
    "Итальянский": "Italian",
    "Каталанский": "Catalan",
    "Португальский": "Portuguese",
    "Нидерландский": "Dutch",
    "Шведский": "Swedish",
    "Норвежский": "Norwegian",
    "Датский": "Danish",
    "Финский": "Finnish",
    "Литовский": "Lithuanian",
    "Латышский": "Latvian",
    "Эстонский": "Estonian",
    "Греческий": "Greek",
    "Румынский": "Romanian",
    "Венгерский": "Hungarian",
    "Турецкий": "Turkish",
    "Казахский": "Kazakh",
    "Узбекский": "Uzbek",
    "Азербайджанский": "Azerbaijani",
    "Армянский": "Armenian",
    "Грузинский": "Georgian",
    "Арабский": "Arabic",
    "Иврит": "Hebrew",
    "Персидский (фарси)": "Persian",
    "Хинди": "Hindi",
    "Китайский (упрощённый)": "Simplified Chinese",
    "Японский": "Japanese",
    "Корейский": "Korean",
    "Вьетнамский": "Vietnamese",
    "Тайский": "Thai",
    "Индонезийский": "Indonesian",
}

LANGUAGES_SORTED = dict(sorted(LANGUAGES.items(), key=lambda x: x[0]))

# ============== СТАТУСЫ ЗАДАЧ ==============

class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"  # Частично завершено

@dataclass
class ChunkProgress:
    index: int
    total: int
    status: str  # pending, completed, failed
    result: str = ""
    error: str = ""
    attempts: int = 0

@dataclass  
class TaskProgress:
    file_path: str
    language: str
    lang_display: str
    status: str
    chunks: List[dict]
    output_file: str = ""
    started_at: str = ""
    updated_at: str = ""
    total_attempts: int = 0
    needs_review: bool = False   # детектор утечки чисел/англ-слов: флаг, НЕ автофикс
    review_note: str = ""

    def to_dict(self):
        return {
            "file_path": self.file_path,
            "language": self.language,
            "lang_display": self.lang_display,
            "status": self.status,
            "chunks": self.chunks,
            "output_file": self.output_file,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "total_attempts": self.total_attempts,
            "needs_review": self.needs_review,
            "review_note": self.review_note
        }
    
    @classmethod
    def from_dict(cls, d):
        return cls(**d)


# ============== АДАПТИВНЫЙ RATE LIMITER ==============

class AdaptiveRateLimiter:
    """
    Умный rate limiter:
    - Экспоненциальный backoff при 429
    - Автоматическое снижение потоков
    - Jitter для предотвращения thundering herd
    """
    def __init__(self):
        self._lock = threading.Lock()
        self.base_delay = 1.0
        self.current_delay = 1.0
        self.max_delay = 300.0  # 5 минут максимум
        self.consecutive_429s = 0
        self.last_429_time = 0
        self.recommended_threads = DEFAULT_THREADS
        
    def on_success(self):
        with self._lock:
            # Успех - уменьшаем задержку
            self.consecutive_429s = 0
            self.current_delay = max(self.base_delay, self.current_delay * 0.8)
            # Постепенно восстанавливаем потоки
            if self.recommended_threads < DEFAULT_THREADS:
                self.recommended_threads = min(DEFAULT_THREADS, self.recommended_threads + 1)
    
    def on_rate_limit(self) -> float:
        with self._lock:
            self.consecutive_429s += 1
            self.last_429_time = time.time()
            
            # Экспоненциальный backoff с jitter
            self.current_delay = min(
                self.max_delay,
                self.base_delay * (2 ** self.consecutive_429s) + random.uniform(0, 5)
            )
            
            # Снижаем потоки после 3 подряд 429
            if self.consecutive_429s >= 3:
                self.recommended_threads = max(MIN_THREADS, self.recommended_threads - 1)
            
            return self.current_delay
    
    def get_delay(self) -> float:
        with self._lock:
            return self.current_delay + random.uniform(0, self.current_delay * 0.1)


# ============== МЕНЕДЖЕР ПРОГРЕССА ==============

class ProgressManager:
    """Сохраняет и загружает прогресс в JSON файл"""
    
    def __init__(self, progress_file: str = PROGRESS_FILE):
        self.progress_file = progress_file
        self._lock = threading.Lock()
        self.tasks: Dict[str, TaskProgress] = {}
        self.cache_stats = {
            "cache_hits": 0,
            "cache_creates": 0,
            "tokens_saved": 0
        }
        self.load()
    
    def _task_key(
        self,
        file_path: str,
        language: str,
        user_id: str = "",
        job_id: str = "",
    ) -> str:
        # Both tenant and job scope are mandatory for runtime callers.  Empty
        # job_id still gets its own delimiter and therefore never reuses legacy
        # ``user::basename::language`` entries.
        return f"{user_id}::{job_id}::{os.path.basename(file_path)}::{language}"
    
    def load(self):
        """Загружает прогресс из файла"""
        try:
            if os.path.exists(self.progress_file):
                with open(self.progress_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for key, task_dict in data.get("tasks", {}).items():
                        self.tasks[key] = TaskProgress.from_dict(task_dict)
        except Exception as e:
            print(f"Ошибка загрузки прогресса: {e}")
            self.tasks = {}
    
    def save(self):
        """Сохраняет прогресс в файл"""
        with self._lock:
            try:
                data = {
                    "updated_at": datetime.now().isoformat(),
                    "tasks": {k: v.to_dict() for k, v in self.tasks.items()}
                }
                # Атомарная запись через временный файл
                temp_file = self.progress_file + ".tmp"
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(temp_file, self.progress_file)
            except Exception as e:
                print(f"Ошибка сохранения прогресса: {e}")
    
    def get_or_create_task(self, file_path: str, language: str, lang_display: str,
                           total_chunks: int, user_id: str = "",
                           job_id: str = "") -> TaskProgress:
        """Получает существующую задачу или создаёт новую"""
        key = self._task_key(file_path, language, user_id, job_id)
        
        with self._lock:
            if key in self.tasks:
                task = self.tasks[key]
                # Проверяем, не изменилось ли количество чанков
                if len(task.chunks) != total_chunks:
                    # Файл изменился - сбрасываем
                    task = None
                elif task.status == TaskStatus.COMPLETED.value:
                    # Уже завершено
                    return task
            else:
                task = None
            
            if task is None:
                # Создаём новую задачу
                chunks = [
                    {"index": i, "status": "pending", "result": "", "error": "", "attempts": 0}
                    for i in range(total_chunks)
                ]
                task = TaskProgress(
                    file_path=file_path,
                    language=language,
                    lang_display=lang_display,
                    status=TaskStatus.PENDING.value,
                    chunks=chunks,
                    started_at=datetime.now().isoformat(),
                    updated_at=datetime.now().isoformat()
                )
                self.tasks[key] = task
            
            return task
    
    def update_chunk(self, file_path: str, language: str, chunk_index: int,
                     status: str, result: str = "", error: str = "",
                     user_id: str = "", job_id: str = ""):
        """Обновляет статус чанка и сохраняет"""
        key = self._task_key(file_path, language, user_id, job_id)
        
        with self._lock:
            if key in self.tasks:
                task = self.tasks[key]
                if chunk_index < len(task.chunks):
                    task.chunks[chunk_index]["status"] = status
                    task.chunks[chunk_index]["result"] = result
                    task.chunks[chunk_index]["error"] = error
                    task.chunks[chunk_index]["attempts"] += 1
                    task.updated_at = datetime.now().isoformat()
                    task.total_attempts += 1
                    
                    # Обновляем статус задачи
                    self._update_task_status(task)
        
        self.save()
    
    def _update_task_status(self, task: TaskProgress):
        """Обновляет общий статус задачи на основе чанков"""
        statuses = [c["status"] for c in task.chunks]
        
        if all(s == "completed" for s in statuses):
            task.status = TaskStatus.COMPLETED.value
        elif any(s == "in_progress" for s in statuses):
            task.status = TaskStatus.IN_PROGRESS.value
        elif any(s == "completed" for s in statuses):
            task.status = TaskStatus.PARTIAL.value
        elif any(s == "failed" for s in statuses):
            task.status = TaskStatus.FAILED.value
        else:
            task.status = TaskStatus.PENDING.value
    
    def set_task_output(self, file_path: str, language: str, output_file: str,
                        user_id: str = "", job_id: str = ""):
        """Устанавливает путь к выходному файлу"""
        key = self._task_key(file_path, language, user_id, job_id)
        with self._lock:
            if key in self.tasks:
                self.tasks[key].output_file = output_file
        self.save()

    def set_needs_review(self, file_path: str, language: str, note: str,
                         user_id: str = "", job_id: str = ""):
        """Помечает задачу как требующую ручной вычитки (НЕ fail, НЕ автофикс).
        Используется детектором утечки цифр/английских слов-чисел."""
        key = self._task_key(file_path, language, user_id, job_id)
        with self._lock:
            if key in self.tasks:
                self.tasks[key].needs_review = True
                self.tasks[key].review_note = note
        self.save()
    
    def get_incomplete_chunks(self, task: TaskProgress) -> List[int]:
        """Возвращает индексы незавершённых чанков"""
        return [
            c["index"] for c in task.chunks 
            if c["status"] != "completed"
        ]
    
    def delete_task(self, file_path: str, language: str, user_id: str = "",
                    job_id: str = "") -> bool:
        """Удаляет конкретную задачу (file+lang) из прогресса. Используется для полной перегенерации."""
        key = self._task_key(file_path, language, user_id, job_id)
        with self._lock:
            if key in self.tasks:
                del self.tasks[key]
                changed = True
            else:
                changed = False
        if changed:
            self.save()
        return changed
    
    def log_cache_usage(self, cache_hit_tokens: int = 0, cache_create_tokens: int = 0):
        """Логирует использование кэша"""
        with self._lock:
            if cache_hit_tokens > 0:
                self.cache_stats["cache_hits"] += 1
                self.cache_stats["tokens_saved"] += int(cache_hit_tokens * 0.9)  # 90% экономия
            if cache_create_tokens > 0:
                self.cache_stats["cache_creates"] += 1
    
# ============== СТОП-ФЛАГ ==============

class StopFlag:
    """Глобальный флаг остановки. Поддерживает thread-local override для остановки только одной задачи в очереди."""
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = False
        self._thread_local = threading.local()

    def set_thread_stop_event(self, event):
        """Установить event для текущего потока. is_stopped() будет проверять event.is_set() вместо глобального флага."""
        self._thread_local.stop_event = event

    def clear_thread_stop_event(self):
        """Снять thread-local override."""
        if hasattr(self._thread_local, "stop_event"):
            del self._thread_local.stop_event

    def stop(self):
        with self._lock:
            self._stop = True

    def is_stopped(self):
        # Сначала проверяем thread-local (для задач очереди — останавливаем только эту задачу)
        if hasattr(self._thread_local, "stop_event") and self._thread_local.stop_event.is_set():
            return True
        with self._lock:
            return self._stop

    def reset(self):
        with self._lock:
            self._stop = False


STOP_FLAG = StopFlag()
RATE_LIMITER = AdaptiveRateLimiter()
PROGRESS_MANAGER = ProgressManager()

# --- FIX-2026-07-30 (P1 №7): лимитер на ВЛАДЕЛЬЦА credential, не один на всех ---
# Раньше был единственный глобальный RATE_LIMITER: любой 429 (в т.ч.
# insufficient_quota от мёртвого BYOK-ключа одного партнёра) экспоненциально
# поднимал задержку ДЛЯ ВСЕХ вызовов ВСЕХ тенантов — один чужой ключ душил
# сервис. Теперь состояние backoff'а живёт отдельно у каждого владельца ключа:
# свой ключ тенанта → его user_id, серверный пул → общий 'server'.
_RATE_LIMITERS: Dict[str, AdaptiveRateLimiter] = {}
_RATE_LIMITERS_LOCK = threading.Lock()


def rate_limiter_for(provider: str, user_id: str = "") -> AdaptiveRateLimiter:
    """Лимитер владельца credential, которым реально пойдёт вызов."""
    provider = (provider or "").strip().lower() or "openai"
    try:
        source = credential_source(provider, user_id)
    except Exception:
        source = "server"
    if source == "user":
        owner = _resolve_user_id(user_id)
    else:
        owner = "server"
    key = f"{provider}\x00{owner}"
    with _RATE_LIMITERS_LOCK:
        limiter = _RATE_LIMITERS.get(key)
        if limiter is None:
            limiter = AdaptiveRateLimiter()
            _RATE_LIMITERS[key] = limiter
        return limiter

# Кэш анализа по файлу: один файл = один анализ для всех языков (вместо 4 дубликатов)
_ANALYSIS_CACHE: Dict[Tuple[str, str, float], Dict] = {}  # ключ: (файл, модель, mtime) — C8 fix
_ANALYSIS_CACHE_LOCK = threading.Lock()
_CORRECTION_LOG_LOCK = threading.Lock()
# Семафоры анализа по лимиту из настроек (0 = без лимита)
_ANALYSIS_SEMAPHORES: Dict[int, threading.Semaphore] = {}
_ANALYSIS_SEMAPHORES_LOCK = threading.Lock()


def _get_analysis_semaphore() -> Optional[threading.Semaphore]:
    """Возвращает семафор для ограничения параллельных анализов (из настроек). 0 = без лимита."""
    cfg = get_queue_config()
    limit = int(cfg.get("max_analysis_parallelism", 0))
    if limit <= 0:
        return None
    with _ANALYSIS_SEMAPHORES_LOCK:
        if limit not in _ANALYSIS_SEMAPHORES:
            _ANALYSIS_SEMAPHORES[limit] = threading.Semaphore(limit)
        return _ANALYSIS_SEMAPHORES[limit]


# ============== ГЕНЕРАЦИЯ ТЕКСТОВ (ПРЕСЕТЫ И ГЕНЕРАТОР) ==============

class Preset(BaseModel):
    """Пресет для генерации текста с нуля (v2 schema, Round 2 — shims удалены 2026-05-23)."""
    id: str
    name: str = Field(..., min_length=1, max_length=80)
    target_length_words: Optional[int] = Field(None, ge=300, le=100000)
    language: Optional[str] = None  # дефолт-язык пресета: воркер берёт его ДО "English" fallback (2026-06-27, language task)
    content: str = Field(..., min_length=1)
    created_at: float = 0.0
    last_used_at: Optional[float] = None

    class Config:
        extra = "ignore"  # старые поля в мигрированном JSON игнорируются без падения

    def to_dict(self) -> Dict[str, Any]:
        # Точный dump новой schema (без shim'ов). Для UI/CRUD.
        return {
            "id": self.id,
            "name": self.name,
            "target_length_words": self.target_length_words,
            "language": self.language,  # language task
            "content": self.content,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Preset":
        """Принимает мигрированный (v2) формат, дополняет defaults если поля отсутствуют."""
        return cls(
            id=str(d.get("id") or d.get("name", "preset")),
            name=str(d.get("name", "")),
            target_length_words=d.get("target_length_words") or d.get("total_words"),
            language=(d.get("language") or None),  # language task
            content=str(d.get("content") or d.get("prompt_template", "")),
            created_at=float(d.get("created_at") or 0.0) if not isinstance(d.get("created_at"), str) else 0.0,
            last_used_at=d.get("last_used_at"),
        )


class PresetManager:
    """Управление пресетами генерации (загрузка/сохранение в JSON v2)."""
    def __init__(self, presets_file: str = TEXT_GENERATION_PRESETS_FILE):
        self.presets_file = presets_file
        self._lock = threading.Lock()
        # ключ — id (slug); name отдельным полем в Preset. Изменено в v2 — было ключом name.
        self.presets: Dict[str, Preset] = {}
        self.load()

    def load(self):
        try:
            if os.path.exists(self.presets_file):
                with open(self.presets_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # В v2 storage остаётся {name → preset_dict} (так миграция мигрировала),
                # но в memory ключ — id. Резолвим оба для backward compat.
                raw = data.get("presets", {}) if isinstance(data, dict) else {}
                fresh: Dict[str, Preset] = {}
                for stored_key, preset_dict in raw.items():
                    if not isinstance(preset_dict, dict):
                        continue
                    p = Preset.from_dict(preset_dict)
                    fresh[p.id] = p
                self.presets = fresh
        except Exception as e:
            print(f"Ошибка загрузки пресетов генерации: {e}")
            self.presets = {}

    def save(self):
        with self._lock:
            try:
                # Сохраняем под именами для удобства чтения файла глазом (как сейчас юзер привык).
                data = {
                    "updated_at": time.time(),
                    "presets": {p.name: p.to_dict() for p in self.presets.values()},
                }
                # M10 FIX-2026-07-02: уникальный tmp вместо общего .tmp — defense-in-depth
                # против interleave при гонке на одном временном файле (кэш инстансов в
                # server.py уже сериализует через self._lock, но уникальный tmp исключает
                # порчу при любом сценарии двух писателей одного файла).
                import tempfile
                _dir = os.path.dirname(self.presets_file) or "."
                _fd, temp_file = tempfile.mkstemp(dir=_dir, suffix=".tmp")
                try:
                    with os.fdopen(_fd, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    os.replace(temp_file, self.presets_file)
                except Exception:
                    try:
                        os.unlink(temp_file)
                    except OSError:
                        pass
                    raise
            except Exception as e:
                print(f"Ошибка сохранения пресетов: {e}")

    def get_preset(self, identifier: str) -> Optional[Preset]:
        """Backward compat: ищет по id (новое) ИЛИ по name (старый UI/curl)."""
        if not identifier:
            return None
        # 1) try id (canonical)
        if identifier in self.presets:
            return self.presets[identifier]
        # 2) fallback: lookup by name
        for p in self.presets.values():
            if p.name == identifier:
                return p
        return None

    def list_presets(self) -> List[Preset]:
        """Список Preset (Round 1 — изменено: возвращает объекты, не имена). Старые вызовы (server.py:692) переделаны."""
        return list(self.presets.values())

    def save_preset(self, preset: Preset):
        """Сохраняет preset (create или update). Ключ — id."""
        if not preset.created_at:
            preset.created_at = time.time()
        self.presets[preset.id] = preset
        self.save()

    def delete_preset(self, identifier: str) -> bool:
        """Удаляет preset по id (или name через get_preset). Returns True если был."""
        p = self.get_preset(identifier)
        if p is None:
            return False
        del self.presets[p.id]
        self.save()
        return True


def clean_chapter_text(text: str) -> str:
    """Очищает текст от маркеров глав (ГЛАВА ПЕРВАЯ и т.д.)."""
    patterns = [
        r"ГЛАВА\s+(ПЕРВАЯ|ВТОРАЯ|ТРЕТЬЯ|ЧЕТВЁРТАЯ|ПЯТАЯ|ШЕСТАЯ|СЕДЬМАЯ|ВОСЬМАЯ|ДЕВЯТАЯ|ДЕСЯТАЯ)",
        r"ГЛАВА\s+\d+",
        r"ЧАСТЬ\s+(ПЕРВАЯ|ВТОРАЯ|ТРЕТЬЯ|ЧЕТВЁРТАЯ|ПЯТАЯ|ШЕСТАЯ|СЕДЬМАЯ|ВОСЬМАЯ|ДЕВЯТАЯ|ДЕСЯТАЯ)",
        r"ЧАСТЬ\s+\d+",
        r"CHAPTER\s+\d+",
        r"PART\s+\d+",
    ]
    cleaned = text
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned)
    cleaned = cleaned.strip()
    # Фикс склеенных предложений: "слово.Следующее" → "слово. Следующее"
    cleaned = re.sub(r"([.!?])([A-ZА-ЯÀ-ÖØ-ÝĀ-ŽŐŰĐŁŃŚŹŻĆĄĘÅÆØÞÐÉÍÓÚÜŐŰŘŤŇĎŠŽČĚÖÜÄẞЁҐЄІЇȘȚĂ])", r"\1 \2", cleaned)
    # Фикс склеенных после запятой: "слово,Следующее"
    cleaned = re.sub(r",([A-ZА-ЯÀ-ÖØ-ÝĀ-ŽŐŰĐŁŃŚŹŻĆĄĘÅÆØÞÐÉÍÓÚÜŐŰŘŤŇĎŠŽČĚÖÜÄẞЁҐЄІЇȘȚĂ])", r", \1", cleaned)
    return cleaned


PRESET_MANAGER = PresetManager()


# ============== УТИЛИТЫ ==============

def log_msg(log_queue: queue.Queue, message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_queue.put(
        redact_server_credentials(f"[{timestamp}] {message}")
    )


def _quota_exhausted(*parts) -> bool:
    """429 про кончившиеся деньги, а не про «слишком часто» (наша правка 20.09.2026).

    OpenAI отдаёт один и тот же код на оба случая, а разница для человека
    противоположная: лимит запросов проходит сам через секунды, исчерпанная квота не
    пройдёт никогда. Смотрим на машинные признаки ответа (`type`/`code`), а не на
    вольный текст: тексты провайдер меняет, коды — нет.
    """
    blob = " ".join(str(p or "") for p in parts).lower()
    return any(mark in blob for mark in (
        "insufficient_quota", "insufficient quota", "billing_hard_limit_reached",
        "exceeded your current quota", "check your plan and billing",
    ))


def debug_log(log_queue: Optional[queue.Queue], prefix: str, data: str):
    """Логирует данные в debug-режиме (если включён в настройках)."""
    if not log_queue or not get_keys_config().get("debug_mode", False):
        return
    timestamp = datetime.now().strftime("%H:%M:%S")
    safe_prefix = redact_server_credentials(str(prefix))
    safe_data = redact_server_credentials(str(data))
    log_queue.put(f"[{timestamp}] [DEBUG] {safe_prefix}")
    for line in safe_data.split("\n"):
        log_queue.put(f"[{timestamp}] [DEBUG]   {line}")


def read_file_as_text(filepath: str) -> str:
    """
    Извлекает текст из файла. Поддерживаются:
    .txt — обычный текст;
    .docx, .doc — Word (нужен python-docx);
    .pdf — PDF (нужен pypdf или PyPDF2);
    .odt — OpenDocument (нужен odfpy);
    .rtf — RTF (нужен striprtf).
    """
    path_lower = filepath.lower()
    ext = os.path.splitext(path_lower)[1]

    if ext == ".txt":
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()

    if ext in (".docx", ".doc"):
        if not DOCX_AVAILABLE:
            raise ImportError(
                "Для чтения .docx/.doc установите: pip install python-docx"
            )
        if ext == ".doc":
            raise ValueError(
                "Формат .doc (старый Word) не поддерживается. Сохраните файл как .docx."
            )
        doc = docx.Document(filepath)
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" ".join(cell.text for cell in row.cells))
        return "\n\n".join(p for p in parts if p).strip()

    if ext == ".pdf":
        if not PYPDF_AVAILABLE:
            raise ImportError(
                "Для чтения PDF установите: pip install pypdf  (или PyPDF2)"
            )
        reader = pypdf.PdfReader(filepath)
        parts = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                parts.append(t)
        return "\n\n".join(parts).strip()

    if ext == ".odt":
        if not ODT_AVAILABLE:
            raise ImportError(
                "Для чтения .odt установите: pip install odfpy"
            )
        doc = load_odt(filepath)
        parts = []
        for elem in doc.getElementsByType(text.P):
            parts.append(teletype.extractText(elem))
        for elem in doc.getElementsByType(text.H):
            parts.append(teletype.extractText(elem))
        return "\n\n".join(p for p in parts if p).strip()

    if ext == ".rtf":
        if not RTF_AVAILABLE:
            raise ImportError(
                "Для чтения RTF установите: pip install striprtf"
            )
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return rtf_to_text(f.read()).strip()

    # Неизвестный формат — пробуем как текст
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        return f.read().strip()


_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def count_paragraphs(text: str) -> int:
    """Считает абзацы по разделителю blank line. Включая пустые между непустыми."""
    if not text:
        return 0
    return len(_PARAGRAPH_SPLIT_RE.split(text))


def normalize_paragraph_separators(text: str, expected_paragraphs: int) -> str:
    """
    Если модель забыла blank line и слила абзацы через одинарный \\n —
    восстанавливает структуру: при count_paragraphs(\\n\\n)==1 пытается
    split по одинарному \\n, и если число строк совпадает с ожидаемым,
    переформатирует обратно через \\n\\n.

    Возвращает нормализованный текст. Если эвристика не сработала — текст
    возвращается как есть (валидация дальше его всё равно отбракует).
    """
    if not text or expected_paragraphs <= 1:
        return text
    if count_paragraphs(text) >= expected_paragraphs:
        return text  # Уже ок
    # Пробуем по одинарному \n
    lines = text.split("\n")
    # Уберём только хвостовые пустые
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) == expected_paragraphs:
        return "\n\n".join(lines)
    # Иногда модель добавляет лишнюю пустую строку между некоторыми, так что
    # сжимаем кратные переносы: \n{2,} → \n\n; затем split по \n\n
    collapsed = re.sub(r"\n{2,}", "\n\n", text)
    if count_paragraphs(collapsed) == expected_paragraphs:
        return collapsed
    return text


def split_text_to_chunks_by_paragraphs(text: str, max_chars: int) -> Tuple[List[str], List[int]]:
    """
    Разбивает текст на чанки по полным абзацам (\\n\\n) — никогда не разрывает абзац.
    Возвращает (chunks, para_counts), где para_counts[i] — число абзацев в i-м чанке.
    Сохраняет пустые абзацы и порядок для последующего 1:1 alignment.
    """
    paragraphs = _PARAGRAPH_SPLIT_RE.split(text)
    chunks: List[str] = []
    para_counts: List[int] = []
    current: List[str] = []
    current_len = 0
    for p in paragraphs:
        added_len = len(p) + (2 if current else 0)
        if current and current_len + added_len > max_chars:
            chunks.append("\n\n".join(current))
            para_counts.append(len(current))
            current = [p]
            current_len = len(p)
        else:
            current.append(p)
            current_len += added_len
    if current:
        chunks.append("\n\n".join(current))
        para_counts.append(len(current))
    return chunks, para_counts


# FIX-2026-09-17-length-retry: повтор по недобору длины куска.
# Замер боя 17.09 (153 срабатывания в job_logs): при слабом недоборе (80–90%) успех был на 1-й попытке
# 73 раза, на 2-й 19, на 3-й 2, на 4-й ни разу — 16 раз все четыре впустую. «Самый длинный» ответ
# принимался без верхней границы: в логах есть рост в 1,4–1,7 раза (87% → 128–141%), а sol на чешской
# теологии дал 87% → 158% — ложный финал на двух третях и пересказ заново. Знаки меряют плохо:
# чешский перевод английского естественно ~87%. Все пороги — через env, без правки кода.
def _env_number(name: str, default: float, cast=float) -> float:
    """Порог из окружения. Мусор («0,85» с запятой, пустая строка) не роняет импорт — берём умолчание."""
    raw = str(os.environ.get(name, "") or "").strip().replace(",", ".")
    if not raw:
        return default
    try:
        return cast(float(raw)) if cast is int else cast(raw)
    except (TypeError, ValueError):
        return default


SHORT_RETRY_RATIO = _env_number("REWRITE_SHORT_RETRY_RATIO", 0.90)
SHORT_RETRY_MILD_FLOOR = _env_number("REWRITE_SHORT_RETRY_MILD_FLOOR", 0.80)
SHORT_RETRY_MILD_MAX = _env_number("REWRITE_SHORT_RETRY_MILD_MAX", 2, int)
SHORT_RETRY_MAX_GROWTH = _env_number("REWRITE_SHORT_RETRY_MAX_GROWTH", 1.35)
# Рост в разах сам по себе не довод: после обрезанного первого ответа (70%) полный перевод (95%)
# законно в 1,36 раза длиннее, а румынский/польский перевод английского идёт на 1,1–1,25.
# Поэтому по росту отклоняем только ответ длиннее этого абсолютного пола (замечание проверки 17.09).
SHORT_RETRY_GROWTH_FLOOR = _env_number("REWRITE_SHORT_RETRY_GROWTH_FLOOR", 1.30)
# Плотное письмо: повтор пропускаем только с этой доли. Ниже итоговая проверка куска (0.30)
# всё равно пометит его браком — лучше дать модели повторить (замечание проверки 17.09).
DENSE_SCRIPT_SKIP_FROM = 0.30
# Ниже этой доли первый ответ — нарратив-преамбула или обрыв: большой рост повтора там законен.
SHORT_RETRY_GROWTH_CHECK_FROM = 0.70


def short_retry_limit(first_ratio: float, default_max: int) -> int:
    """Сколько повторов делать. Слабый недобор — не больше SHORT_RETRY_MILD_MAX."""
    default_max = int(default_max)
    if default_max <= 0:
        return 0  # NARRATION_MAX_RETRIES=0 — выключатель повторов, как было
    if first_ratio >= SHORT_RETRY_MILD_FLOOR:
        return max(1, min(default_max, SHORT_RETRY_MILD_MAX))
    return default_max


def short_retry_candidate_ok(first_ratio: float, retry_ratio: float) -> Tuple[bool, str]:
    """Можно ли принять ответ повтора. Раздутый ответ хуже короткого: там дописанное от модели."""
    if retry_ratio > MAX_CHUNK_LENGTH_RATIO:
        return False, f"раздут до {retry_ratio:.0%} исходника (потолок {MAX_CHUNK_LENGTH_RATIO:.0%})"
    if (first_ratio >= SHORT_RETRY_GROWTH_CHECK_FROM
            and retry_ratio > max(SHORT_RETRY_MAX_GROWTH * first_ratio, SHORT_RETRY_GROWTH_FLOOR)):
        return False, (f"вырос в {retry_ratio / first_ratio:.2f} раза против первого ответа "
                       f"(потолок {SHORT_RETRY_MAX_GROWTH:.2f}) — похоже на дописанное")
    return True, ""


def source_is_donor_cta_only(text: str) -> bool:
    """Recognize a small English promotional tail, never story + a subscribe line.

    Every sentence must match an allowlisted viewer-address form. Unknown forms
    fail closed; a topic teaser is allowed only next to a promotional statement.
    This deliberately does not classify arbitrary source languages or speeches.
    """
    text = re.sub(r"\s+", " ", (text or "").strip()).replace("’", "'")
    if not 100 <= len(text) <= 2500 or re.search(r'[\d"“”«»]', text):
        return False
    sentences = [s.rstrip(".!?") for s in re.split(r"(?<=[.!?])\s+", text)]
    requests = (
        r"(?:hit|click|press) (?:that |the )?like button",
        r"(?:hit |please )?subscribe(?: and turn on notifications)?",
        r"(?:drop|leave) a comment(?: right now)?(?: and tell (?:us|me) where you(?:'re| are) watching from)?",
        r"tell (?:us|me) (?:your location|if someone in your family served|where you(?:'re| are) watching from)",
        r"just let (?:us|me) know you(?:'re| are) here",
    )
    support = (
        r"if this story moved you(?: the way it moved (?:us|me))?, do (?:me|us) a favor",
        r"every (?:single )?like tells youtube to show this story to more people",
        r"we(?:'re| are) rescuing forgotten stories from dusty archives every (?:single )?day",
        r"real (?:people|stories|history|heroism)(?:, real (?:people|stories|history|heroism))?",
        r"are you watching from the (?:united states|united kingdom)(?:, (?:united kingdom|canada|australia))*",
        r"our community stretches across the (?:entire |whole )?world",
        r"you(?:'re| are) not just a viewer",
        r"you(?:'re| are) part of keeping these memories alive",
        r"thank you for watching(?: and thank you for making sure [a-z]+(?: [a-z]+){0,3} doesn't disappear into silence)?",
        r"these (?:men|people) deserve to be remembered, and you(?:'re| are) helping make that(?: happen)?",
    )
    # Topic labels are promotional context, not permission to drop a narrative.
    # The small vocabulary rejects dates, named places/people and unknown facts.
    teaser_words = set("fighter pilots soldiers people heroes stories history war battles weapons pistols "
                       "planes jets oceans archives modified attacked crossed impossible odds and who about".split())
    request_count = 0
    for index, sentence in enumerate(sentences):
        if any(re.fullmatch(pattern, sentence, re.I) for pattern in requests):
            request_count += 1
        elif any(re.fullmatch(pattern, sentence, re.I) for pattern in support):
            continue
        elif (index > 0 and sentence.lower().startswith("stories about ")
              and set(sentence.lower().split()) <= teaser_words
              and re.fullmatch(support[2], sentences[index - 1], re.I)):
            continue
        else:
            return False
    return request_count >= 2


def final_cta_short_output_ok(source: str, output: str, chunk_index: int,
                              total_chunks: int, channel: str, host: str) -> bool:
    """Bounded exception for a branded final CTA, never a plan or empty reply."""
    if chunk_index != total_chunks - 1 or not source_is_donor_cta_only(source):
        return False
    output = (output or "").strip()
    if not 40 <= len(output) <= 600 or len(output) >= len(source) or len(output.split()) < 6:
        return False
    if not channel or not host or channel not in output or host not in output:
        return False
    if re.search(r"\b(?:i need to|let me|i will|i'll|here is|here's|translation|rewrite|prompt|analysis|planning|"
                 r"muszę|potrzebuję|przetłumacz|zrobię|przygotuję|dodam|napiszę|"
                 r"připravím|napíšu|přeložím|přidám|vyzvu|trebuie să|voi traduce|voi rescrie|"
                 r"я должен|мне нужно|перевод|анализ)\b|<\/?(?:think|analysis)>|```", output, re.I):
        return False
    # A direct imperative must begin a sentence/clause. Merely mentioning a
    # future like/subscribe request in a plan is not an address to the viewer.
    return bool(re.search(
        r"(?:^|[.!?]\s+|,\s+)(?:please\s+)?(?:"
        r"like (?:this|the) (?:video|story)|subscribe\b|hit (?:the |that )?like button|"
        r"polub(?:cie)?\b|subskrybuj(?:cie)?\b|"
        r"abonați(?:-vă)?\b|abonează-te\b|apreciați\b|"
        r"odebírej(?:te)?\b|podpoř(?:te)?\b|dejte (?:like|lajk)\b|"
        r"suscríb(?:ete|anse)\b|dale (?:a |un )?like\b|"
        r"inscreva(?:-se)?\b|curta\b|pretplatite(?: se)?\b|"
        r"iratkozz(?:atok)?\b|kedveld\b|підпиш(?:іться|ись)\b|поставте лайк\b|"
        r"εγγραφείτε\b|κάντε (?:like|λάικ)\b|tilaa\b|tykkää\b)", output, re.I))


def chunk_is_dense_script(text: str) -> bool:
    """Иероглифы / кана / хангыль / тайский: сверка длины по знакам с латиницей бессмысленна."""
    visible = len(re.findall(r"\S", text or ""))
    return visible > 0 and len(_DENSE_SCRIPT_RE.findall(text)) > 0.2 * visible


def _accept_retry_if_paragraphs_ok(
    current: str,
    candidate: str,
    preserve_paragraphs: bool,
    expected_paragraphs: int,
    log_queue,
    chunk_idx: int,
    where: str,
) -> str:
    """
    Принять candidate в качестве extracted ТОЛЬКО если paragraph structure совпадает.
    Иначе — оставить current (он уже прошёл paragraph-validation выше по потоку).

    Защищает ratio-retries (короткие/длинные ответы): новый ответ модели часто
    приходит single-line или с Markdown-line-breaks (`  \\n`), что ломает 1:1
    alignment абзацев.
    """
    if not (preserve_paragraphs and expected_paragraphs > 0):
        return candidate
    norm = normalize_paragraph_separators(candidate, expected_paragraphs)
    actual = count_paragraphs(norm)
    if actual == expected_paragraphs:
        return norm
    # FIX-2026-09-13-short-retry: ждём 1 абзац = у куска нет структуры абзацев (субтитры
    # без пустых строк), выравнивать 1:1 нечего. Раньше повтор полной длины отклонялся
    # из-за «46/1 абзацев», и в файл уходил кусок на 46% (88e89ffa81a8, 12.09: 3271 → 1332
    # слова). Склеиваем абзацы повтора в один — счёт 1/1 сходится для всех проверок ниже.
    # Какой ответ лучше по длине, решает вызывающий (narration: самый длинный). Где во
    # входе абзацев больше одного, защита остаётся: несовпадение ломает финальную сверку.
    # Склеиваем только там, где длину уже держит вызывающий (narration — самый длинный ответ,
    # long-retry-success — 90–140%). У long-retry-shorter нижней границы нет: склейка пустила бы
    # в файл повтор на 38% вместо полного куска (замечание проверки 13.09).
    if expected_paragraphs == 1 and where in ("narration-retry-loop", "long-retry-success"):
        merged = _PARAGRAPH_SPLIT_RE.sub("\n", candidate.strip())
        if count_paragraphs(merged) == 1:
            log_msg(
                log_queue,
                f"   📐 Chunk {chunk_idx+1}: {where} даёт {actual}/1 абзацев — во входе абзацев нет, склеиваю в один"
            )
            return merged
    log_msg(
        log_queue,
        f"   ⚠️ Chunk {chunk_idx+1}: {where} даёт {actual}/{expected_paragraphs} абзацев — отклоняю, оставляю прежний"
    )
    return current


# FIX-2026-09-13-short-output: итоговая сверка длины файла рерайта против исходника.
# Меряем СИМВОЛЫ, а не слова (замер боевых файлов 12.09): у финского по словам ниже 0.7
# все 9 файлов, даже полный fi_33333 (0.65 слова при 0.98 символа). По символам ниже 0.7
# только реально сжатые: no 0.46/0.55/0.63/0.66, fi 0.50/0.69. Порог — env, без правки кода.
SHORT_OUTPUT_RATIO = float(os.environ.get("REWRITE_SHORT_OUTPUT_RATIO", "0.70"))
# Иероглифы / кана / хангыль / тайский: полный перевод с английского в разы короче
# по символам — сверка по символам там бессмысленна, молчим.
_DENSE_SCRIPT_RE = re.compile(r"[\u0e00-\u0e7f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def output_shorter_than_source(source_text: str, output_text: str) -> Optional[Tuple[int, int]]:
    """(слов в файле, слов в исходнике), если файл короче SHORT_OUTPUT_RATIO исходника
    по символам. None — длина в норме или сверять нечем."""
    src = (source_text or "").strip()
    out = (output_text or "").strip()
    if not src or not out:
        return None
    for t in (src, out):
        if len(_DENSE_SCRIPT_RE.findall(t)) > 0.2 * len(re.findall(r"\S", t)):
            return None
    if len(out) >= SHORT_OUTPUT_RATIO * len(src):
        return None
    return len(out.split()), len(src.split())


def split_text_to_chunks(text: str, max_chars: int) -> List[str]:
    """Разбивает текст на части по max_chars символов, не обрезая слова."""
    chunks = []
    current_chunk = ""
    
    sentences = re.split(r'(?<=[.!?])\s+', text)
    
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        
        if len(sentence) > max_chars:
            words = sentence.split()
            for word in words:
                if len(current_chunk) + len(word) + 1 <= max_chars:
                    current_chunk += (" " if current_chunk else "") + word
                else:
                    if current_chunk:
                        chunks.append(current_chunk.strip())
                    current_chunk = word
        else:
            if len(current_chunk) + len(sentence) + 1 <= max_chars:
                current_chunk += (" " if current_chunk else "") + sentence
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = sentence
    
    if current_chunk:
        chunks.append(current_chunk.strip())
    
    return chunks


def analyze_text_structure(
    text: str,
    log_queue: queue.Queue,
    use_subscription: bool = False,
    oauth_token: str = "",
    model: str = "claude-3-5-haiku-20241022",
    api_key: str = "",
    oauth_tokens: Optional[List[str]] = None,
    current_token_index: Optional[List[int]] = None,
) -> Dict:
    """
    Анализирует весь текст ПЕРЕД адаптацией.
    Создаёт структурную карту для качественной адаптации.
    Если use_subscription и передан oauth_token — анализ идёт по Pro подписке (та же модель или переданная).
    
    Возвращает:
    - genre: жанр текста
    - key_scenes: критичные сцены с типами
    - preservation_rules: что ОБЯЗАТЕЛЬНО сохранять
    - chunk_roles: роль каждого чанка в общей структуре
    - expected_ratios: ожидаемые соотношения длин для разных типов
    """
    # Для больших текстов берём начало + середину + конец
    sample_size = 30000  # ~7500 токенов
    if len(text) > sample_size:
        chunk_size = sample_size // 3
        sample = text[:chunk_size] + "\n\n[...]\n\n" + text[len(text)//2-chunk_size//2:len(text)//2+chunk_size//2] + "\n\n[...]\n\n" + text[-chunk_size:]
    else:
        sample = text
    
    analysis_prompt = f"""ANALYZE this text for high-quality adaptation planning.

TEXT LENGTH: {len(text)} characters
SAMPLE (beginning + middle + end):
{sample}

Your task: Understand this text deeply to guide perfect adaptation.

Output JSON ONLY:
{{
  "genre": "psychological_horror|thriller|scifi_documentary|narrative|other",
  "writing_style": "detailed_atmospheric|fast_paced|philosophical|technical|conversational",
  "key_characteristics": [
    "slow_burn tension building",
    "detailed environmental descriptions",
    "internal monologues",
    "philosophical reflections",
    "technical procedures"
  ],
  "critical_elements": [
    "Opening hook: detailed facility description",
    "Character X's internal crisis about Y",
    "Scientific procedure Z explained step-by-step",
    "Dialogue revealing key information",
    "Philosophical reflection on theme W"
  ],
  "content_breakdown": {{
    "dialogue_percentage": 30,
    "description_percentage": 40,
    "action_percentage": 15,
    "reflection_percentage": 15
  }},
  "pacing": "slow|medium|fast",
  "preservation_priority": [
    "CRITICAL: All dialogues word-for-word",
    "CRITICAL: All technical details",
    "CRITICAL: All atmosphere-building descriptions",
    "IMPORTANT: Character internal states",
    "IMPORTANT: Time transitions"
  ],
  "youtube_optimization": {{
    "target_retention": "Detailed content for 8+ minute watch time",
    "engagement_elements": ["suspense", "revelations", "character_depth"],
    "style_notes": "Maintain slow burn for binge-watching"
  }}
}}

Be SPECIFIC about critical_elements - not "dialogues exist" but "dialogue where X reveals Y to Z in lines 500-800"."""

    messages = [{"role": "user", "content": analysis_prompt}]
    
    keys_cfg = get_keys_config()
    has_oauth = bool(oauth_token or (oauth_tokens and len(oauth_tokens) > 0))
    provider = _detect_provider(model, use_subscription and has_oauth)
    if provider in ("subscription", "codex_subscription"):
        actual_key = ""
    elif provider == "claude":
        # BYOK-2026-07-30: свой Anthropic-ключ тенанта, иначе серверный — но
        # серверный только владельцу (get_effective_anthropic_key, задача B).
        actual_key = api_key or get_effective_anthropic_key()
    else:
        actual_key = get_effective_openai_key()

    def _do_analysis_api_call():
        if use_subscription and oauth_tokens and current_token_index is not None:
            return call_api_universal_with_token_rotation(
                messages=messages,
                api_key=actual_key,
                model=model,
                log_queue=log_queue,
                task_info="text_analysis",
                provider=provider,
                oauth_tokens=oauth_tokens,
                current_token_index=current_token_index,
            )
        return call_api_universal(
            messages=messages,
            api_key=actual_key,
            model=model,
            log_queue=log_queue,
            task_info="text_analysis",
            provider=provider,
            oauth_token=oauth_token,
        )
    
    sem = _get_analysis_semaphore()
    if sem:
        with sem:
            success, response = _do_analysis_api_call()
    else:
        success, response = _do_analysis_api_call()
    
    if not success:
        err_preview = (response[:200] + "…") if response and len(response) > 200 else (response or "пустой ответ")
        log_msg(log_queue, f"⚠️ Analysis failed, using defaults ({err_preview})")
        return {
            "genre": "unknown",
            "writing_style": "detailed",
            "critical_elements": ["Preserve all content completely"],
            "preservation_priority": ["All content equally important"],
            "content_breakdown": {"dialogue_percentage": 25, "description_percentage": 50, "action_percentage": 15, "reflection_percentage": 10},
            "pacing": "medium"
        }
    
    try:
        # Убираем markdown-обёртку ```json ... ```
        text = response.strip()
        if text.startswith("```"):
            first_newline = text.find("\n")
            if first_newline != -1 and ("json" in text[:first_newline].lower() or "```" in text[:first_newline]):
                text = text[first_newline + 1:]
            if text.endswith("```"):
                text = text[:-3].rstrip()
        # Извлекаем только первый JSON-объект (модель иногда добавляет текст после)
        json_start = text.find('{')
        if json_start == -1:
            raise ValueError("No JSON found")
        depth = 0
        json_end = -1
        for i in range(json_start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    json_end = i + 1
                    break
        if json_end == -1:
            # Ответ обрезан — пробуем починить, добавив закрывающие скобки
            snippet = text[json_start:]
            for add in ["}", "}]}", "}]}]}", "}" * min(depth, 10)]:
                try:
                    repaired = snippet + add
                    analysis = json.loads(repaired)
                    log_msg(log_queue, "   (восстановлен обрезанный JSON)")
                    break
                except json.JSONDecodeError:
                    continue
            else:
                raise ValueError("No complete JSON object found")
        else:
            analysis = json.loads(text[json_start:json_end])
        
        log_msg(log_queue, f"📊 Analysis complete:")
        log_msg(log_queue, f"   Genre: {analysis.get('genre', 'unknown')}")
        log_msg(log_queue, f"   Style: {analysis.get('writing_style', 'unknown')}")
        log_msg(log_queue, f"   Critical elements: {len(analysis.get('critical_elements', []))}")
        
        return analysis
        
    except Exception as e:
        log_msg(log_queue, f"⚠️ Failed to parse analysis: {e}, using defaults")
        return {
            "genre": "unknown",
            "writing_style": "detailed",
            "critical_elements": ["Preserve all content"],
            "preservation_priority": ["All content equally important"],
            "content_breakdown": {"dialogue_percentage": 25, "description_percentage": 50, "action_percentage": 15, "reflection_percentage": 10},
            "pacing": "medium"
        }


def _normalize_literal_escapes(text: str) -> str:
    """FIX-2026-06-29: модель иногда ДВАЖДЫ экранирует переносы (\\n\\n в JSON → литеральные \\n\\n
    остаются в тексте после одного json.loads). Narration-текст (TTS) НЕ содержит легитимных
    backslash-escape → конвертируем литеральные \\n \\r \\t в реальные. Быстрый выход если backslash нет."""
    if not text or "\\" not in text:
        return text
    return (text.replace("\\r\\n", "\n").replace("\\n", "\n")
                .replace("\\r", "\n").replace("\\t", " "))


def extract_from_brackets(text: str) -> str:
    if not text:
        return ''
    cleaned = (text.replace('\\r\\n', '\n').replace('\\n', '\n')
               .replace('\\r', '\n').replace('\\t', ' ')).strip()
    if cleaned.startswith('```'):
        lines = cleaned.splitlines()
        if len(lines) > 2 and lines[-1].strip() == '```':
            cleaned = '\n'.join(lines[1:-1]).strip()
    cleaned = re.sub(r'^(?:Output|Adapted text|Text|Result):\s*', '', cleaned, count=1)
    # The contract has one OUTER pair. Never select an internal annotation and
    # discard the remainder of the story (the production regex does that).
    if cleaned.startswith('[') and cleaned.endswith(']'):
        depth = 0
        for index, char in enumerate(cleaned):
            if char == '[':
                depth += 1
            elif char == ']':
                depth -= 1
            if depth == 0:
                if index == len(cleaned) - 1:
                    cleaned = cleaned[1:-1].strip()
                break
    return cleaned


# === Эвристическая чистка Claude-output (2026-06-03) ===
# Claude иногда (особенно Sonnet/Opus при свободном рерайте) добавляет структурный мусор:
# «Блок 1», «Часть 2», markdown-заголовки, преамбулы «Вот переписанный текст:».
# Эта функция — НЕ LLM, чистый regex/стоп-лист. Консервативная: при любом сомнении НЕ режет.
# Применяется к каждому чанку Claude-rewrite (не для GPT-пути).

_SANITIZE_BLOCK_MARKER_RE = re.compile(
    r"^\s*(?:Блок|Часть|Глава|Раздел|Chunk|Block|Part|Section|Fragment|"
    r"Díl|Část|Część|Rozdział|Capitol|Capitolul|Kapitel|Bölüm|Kısım|"
    r"Capítulo|Capitolo|Capítol|Kapitola|Glava|Pasaje|Pasaj|Bagian|"
    r"Cabang|Bab|Sezione|Stück|Teil)\s*(?:[№#]\s*)?\d+(?:\s*из\s*\d+)?\s*[:.)]?\s*$",
    re.IGNORECASE | re.UNICODE,
)
_SANITIZE_MD_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")
_SANITIZE_MD_SEPARATOR_RE = re.compile(r"^\s*(?:[-=*_]\s*){3,}\s*$")
_SANITIZE_BOLD_ITALIC_MARKERS_RE = re.compile(r"(\*{1,3}|_{2,3})(.+?)\1")

# Префиксные/постфиксные meta-маркеры в стоп-листе (мультиязык, нижний регистр для match)
_SANITIZE_PREAMBLE_KEYWORDS = (
    "rewritten", "rewrite", "переписан", "переписал", "переписала",
    "version", "вариант", "перевод", "перевела", "перевёл", "translation", "translated",
    "here is", "here's", "вот", "below is", "ниже", "result", "результат",
    "adapted", "адаптир", "ось", "ось переписан",
    "přepsán", "přeložen", "wersja", "tłumacz", "versiune", "tradus",
)
_SANITIZE_TAIL_KEYWORDS = (
    "changes", "изменения", "i rewrote", "я переписал", "я переписала",
    "word count", "количество слов", "summary of changes", "сводка изменений",
    "note:", "примечание:", "footnotes", "сноск",
)


def sanitize_claude_output(text: str, lang_name: str = "") -> Tuple[str, int]:
    if not text:
        return text, 0
    cleaned = sanitize_final_text(text, lang_name)
    return cleaned, max(0, len(text.splitlines()) - len(cleaned.splitlines()))


def find_sentences_with_cyrillic(text: str) -> List[Tuple[str, int, int]]:
    """
    Находит предложения с кириллицей в тексте.
    Возвращает список кортежей (предложение, начало_позиция, конец_позиция).
    """
    if not text:
        return []
    
    cyrillic_pattern = re.compile(r'[а-яёА-ЯЁ]')
    
    # Находим все позиции с кириллицей
    cyrillic_positions = []
    for match in cyrillic_pattern.finditer(text):
        cyrillic_positions.append(match.start())
    
    if not cyrillic_positions:
        return []
    
    # Находим границы предложений для каждой позиции с кириллицей
    result = []
    processed_ranges = set()  # Чтобы не дублировать предложения
    
    for pos in cyrillic_positions:
        # Ищем начало предложения (ищем назад до точки/восклицательного/вопросительного или начала текста)
        start = pos
        while start > 0 and text[start-1] not in '.!?\n':
            start -= 1
        
        # Ищем конец предложения (ищем вперед до точки/восклицательного/вопросительного или конца текста)
        end = pos
        while end < len(text) and text[end] not in '.!?\n':
            end += 1
        if end < len(text):
            end += 1  # Включаем знак препинания
        
        # Извлекаем предложение
        sentence = text[start:end].strip()
        
        # Проверяем, что в предложении действительно есть кириллица
        if cyrillic_pattern.search(sentence) and (start, end) not in processed_ranges:
            result.append((sentence, start, end))
            processed_ranges.add((start, end))
    
    # Сортируем по позиции начала
    result.sort(key=lambda x: x[1])
    
    return result


def fix_sentence_with_4o_mini(sentence: str, lang_name: str, api_key: str, 
                                log_queue: queue.Queue) -> Optional[str]:
    """
    Исправляет предложение с русскими словами через 4o-mini.
    Заменяет русские слова на правильные слова целевого языка.
    ВАЖНО: Возвращает ТОЛЬКО исправленное предложение в квадратных скобках.
    """
    if not sentence:
        return None
    
    # Очищаем предложение от лишних пробелов
    sentence = sentence.strip()
    
    system_prompt = f"""You are a translator. The input contains a MIXED sentence with Russian (Cyrillic) and {lang_name} fragments.

YOUR TASK: Output the ENTIRE sentence in 100% pure {lang_name}. Every Cyrillic character MUST be gone.

PROCESS:
1. Read the input sentence fully
2. Translate ALL Russian parts into natural {lang_name}, keeping meaning exact
3. Keep existing correct {lang_name} parts as they are
4. Combine into one fluent {lang_name} sentence

RULES:
- Zero Cyrillic in output
- Numbers/dates stay identical (1943, 88mm, etc. — unchanged)
- Proper names: use native {lang_name} spelling
- Return ONLY the result in [square brackets] — no explanations

Input: "Он смотрел na niebo и думал, что życie jest krótkie."
Output: [Patrzył na niebo i myślał, że życie jest krótkie.]

Input: "Every шотландский cat nie rozumie gospodara."
Output: [Każdy szkocki kot nie rozumie gospodarza.]"""

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Fix this sentence and return ONLY in square brackets:\n\n{sentence}"}
        ]
        
        # Используем модель проверки из настроек
        success, response = call_openai_api(
            messages=messages,
            model=get_postprocess_model(),
            api_key=api_key,
            log_queue=log_queue,
            task_info="fix_russian_words",
            service_tier=POSTPROCESS_SERVICE_TIER,
        )
        
        if success:
            fixed = extract_from_brackets(response)
            
            # Проверяем на артефакты (повторения, странные символы)
            if fixed and len(fixed) > 0:
                # CRITICAL: проверяем, что ВСЕ числа (годы, калибры и т.п.) совпадают
                orig_nums = re.findall(r'\d+', sentence)
                fixed_nums = re.findall(r'\d+', fixed)
                if orig_nums != fixed_nums:
                    if log_queue:
                        log_msg(
                            log_queue,
                            f"    ⚠️ Числа изменились при постобработке (orig={orig_nums}, fixed={fixed_nums}), откатываю фикс"
                        )
                    return None
                # Проверяем на повторения (например, "sebou sebou sebou")
                words = fixed.split()
                if len(words) > 3:
                    # Проверяем, нет ли повторяющихся слов подряд
                    for i in range(len(words) - 2):
                        if words[i] == words[i+1] == words[i+2]:
                            if log_queue:
                                log_msg(log_queue, f"    ⚠️ Обнаружены повторения в ответе, пропускаю")
                            return None
                
                # Проверяем, что кириллицы стало меньше или равно нулю
                cyrillic_in_original = len(re.findall(r'[а-яёА-ЯЁ]', sentence))
                cyrillic_in_fixed = len(re.findall(r'[а-яёА-ЯЁ]', fixed))
                
                # Проверяем, что длина не изменилась кардинально (не более чем в 2 раза)
                if len(fixed) > len(sentence) * 2 or len(fixed) < len(sentence) * 0.3:
                    if log_queue:
                        log_msg(log_queue, f"    ⚠️ Длина изменилась слишком сильно, пропускаю")
                    return None
                
                if cyrillic_in_fixed < cyrillic_in_original:
                    if log_queue:
                        log_msg(log_queue, f"    ✅ Исправлено: {cyrillic_in_original} → {cyrillic_in_fixed} русских символов")
                    return fixed
                elif cyrillic_in_fixed == 0:
                    if log_queue:
                        log_msg(log_queue, f"    ✅ Все русские слова заменены")
                    return fixed
                else:
                    if log_queue:
                        log_msg(log_queue, f"    ⚠️ Частично исправлено: {cyrillic_in_original} → {cyrillic_in_fixed} русских символов")
                    return fixed
        else:
            if log_queue:
                log_msg(log_queue, f"    ⚠️ Не удалось исправить через {get_postprocess_model()}: {response[:80]}")
    except Exception as e:
        if log_queue:
            log_msg(log_queue, f"    ⚠️ Ошибка при исправлении через {get_postprocess_model()}: {str(e)[:100]}")
    
    return None


def postprocess_remove_russian_words(text: str, lang_name: str, api_key: str,
                                     log_queue: queue.Queue) -> str:
    """
    Постобработка текста: находит предложения с кириллицей и заменяет их через 4o-mini.
    Это гарантирует отсутствие русских слов в финальном тексте.
    ВАЖНО: Обрабатывает весь текст ПОСЛЕ того как он готов, заменяет предложения корректно.
    Не вызывается для языков, где кириллица — целевой алфавит (русский/украинский/белорусский/болгарский/македонский).
    Сербский — пишется на латинице (целевой выбор), поэтому проверяется на общих основаниях.
    """
    if not text:
        return text
    # Для языков, где кириллица — норма, не «исправляем» её
    if lang_name and lang_name.lower() in ("russian", "ukrainian", "belarusian", "bulgarian", "macedonian"):
        return text

    # Находим все предложения с кириллицей (теперь с началом и концом)
    sentences_with_cyrillic = find_sentences_with_cyrillic(text)
    
    if not sentences_with_cyrillic:
        return text  # Нет русских слов, возвращаем как есть
    
    if log_queue:
        log_msg(log_queue, f"    🔍 Найдено {len(sentences_with_cyrillic)} предложений с кириллицей")
        log_msg(log_queue, f"    🔧 Запрашиваю исправление через {get_postprocess_model()}...")
    
    # Создаём список замен (позиция начала, позиция конца, исправленный текст)
    replacements = []
    
    # Обрабатываем каждое предложение (до 3 попыток при неудаче)
    MAX_RUSSIAN_FIX_RETRIES = 3
    # Бюджет времени на всю постобработку. Без него мёртвый ключ OpenAI (сплошные 429)
    # держал джоб часами: замерено 11.5 ч на одной задаче и 3.4 ч на другой, прогресс
    # при этом 0% — текст уже готов, но не отдан. Исчерпали бюджет → выходим с тем,
    # что успели: ветка «осталось N русских символов» ниже умеет жить с недочищенным.
    _cyr_deadline = time.monotonic() + float(os.environ.get("REWRITE_CYR_FIX_BUDGET_SEC", "300") or 300)
    for sentence, start_pos, end_pos in sentences_with_cyrillic:
        if time.monotonic() > _cyr_deadline:
            if log_queue:
                log_msg(log_queue, "    ⏱️ Бюджет постобработки кириллицы исчерпан — оставляю как есть")
            break
        fixed = None
        for attempt in range(MAX_RUSSIAN_FIX_RETRIES):
            fixed = fix_sentence_with_4o_mini(sentence, lang_name, api_key, log_queue)
            if fixed and fixed != sentence:
                break
            if attempt < MAX_RUSSIAN_FIX_RETRIES - 1 and log_queue:
                log_msg(log_queue, f"    🔄 Повтор {attempt + 2}/{MAX_RUSSIAN_FIX_RETRIES} для предложения с кириллицей...")
        if fixed and fixed != sentence:  # Только если исправление успешно и отличается
            # Проверяем, что исправленный текст не содержит артефактов
            if len(fixed) > 0 and len(fixed) < len(sentence) * 3:  # Не более чем в 3 раза длиннее
                replacements.append((start_pos, end_pos, fixed))
    
    # Применяем замены в обратном порядке (чтобы позиции не сбивались)
    result_text = text
    for start_pos, end_pos, fixed in reversed(replacements):
        # Проверяем, что позиции валидны
        if 0 <= start_pos < len(result_text) and start_pos < end_pos <= len(result_text):
            # Заменяем предложение
            result_text = result_text[:start_pos] + fixed + result_text[end_pos:]
    
    # Финальная проверка на оставшуюся кириллицу
    remaining_cyr = len(re.findall(r'[а-яёА-ЯЁ]', result_text))
    if remaining_cyr > 0:
        if log_queue:
            log_msg(log_queue, f"    ⚠️ После обработки осталось {remaining_cyr} русских символов")
    else:
        if log_queue:
            log_msg(log_queue, f"    ✅ Все русские слова успешно заменены")

    # Дополнительная защита от артефактов: схлопываем безумные повторы одного и того же слова
    # Например: "превращались превращались превращались ..." → "превращались"
    def _collapse_repeats(match: re.Match) -> str:
        return match.group(1)

    # Три и более одинаковых слова подряд (регистр игнорируем)
    result_text = re.sub(r'\b(\w+)(?:\s+\1){2,}\b', _collapse_repeats, result_text, flags=re.IGNORECASE)
    
    return result_text


# ============== ПОВТОРНАЯ ПРОВЕРКА ПЕРЕВОДА (GPT-4o-mini) ==============

def _count_sentences(text: str) -> int:
    """Приблизительное число предложений по точкам, !, ?."""
    if not text or not text.strip():
        return 0
    return max(1, text.count(".") + text.count("!") + text.count("?"))


def _native_speaker_fix_chunk(
    chunk_text: str,
    lang_name: str,
    api_key: str,
    log_queue: queue.Queue,
    original_source_chunk: Optional[str] = None,
) -> str:
    """
    Отправляет один чанк перевода в GPT-4o-mini: находит некорректный/неестественный
    перевод и возвращает исправленный текст. Если передан original_source_chunk,
    в промпт добавляется оригинал с требованием не терять факты и объём.
    """
    if not chunk_text or len(chunk_text.strip()) < 20:
        return chunk_text

    system_prompt = f"""You are a native {lang_name} speaker and a professional editor. Your job is TRANSLATION NATIVENESS CHECK: verify the whole text and fix anything that is not native-quality.
- Preserve ALL facts, numbers, names, and meaning exactly.
- Fix awkward phrasing, calques, and machine-translation artifacts.
- Make it sound as a native {lang_name} speaker would write or say it.
- Do NOT add new content or remove existing content."""
    if original_source_chunk:
        system_prompt += """

CRITICAL: You will be given the ORIGINAL source text and the TRANSLATION. Your output must preserve ALL facts and the full volume of the ORIGINAL — do not shorten, omit, or summarize. Every fact and idea from the original must remain in your corrected translation."""
    system_prompt += """

EDITOR CHECKLIST (verify and fix):
1. NUMBERS AND DATES IN WORDS: All numbers, years, dates, and centuries must be written IN WORDS in """ + lang_name + """, not digits or "19th"/"XIX". If you see "19th century" or "1800s" — replace with the correct phrase in words (e.g. "nineteenth century" in """ + lang_name + """). "1800s" = nineteenth century, correctly translated in words (not 18th).
2. TECHNICAL (UNITS): Convert feet/inches to metric by RECALCULATING: 1 ft = 0.3048 m (1 m = 3.28 ft), 1 in = 2.54 cm. E.g. "8 feet" → 2.44 m → "about two and a half meters"; "10 feet" → ~3 m. Never keep the digit and only swap unit; never use "feet" as body parts.
3. TERMINOLOGY: Replace mistranslations: "Cabinet cards" → cabinet-format photos / cabinet portraits; "Tintype" / "color tint" → ferrotypes/tintypes where it means photo on metal.
4. AUDIO: Rephrase list-like or robotic sentences for natural flow and voice-over.
5. CULTURAL: Remove or fix misplaced local names/toponyms that don't belong to the target audience's country.
6. IDIOMS: Replace literal calques with natural """ + lang_name + """ equivalents.

After checking everything, output ONLY the corrected text, wrapped in square brackets [ ]. No explanations."""

    if original_source_chunk and original_source_chunk.strip():
        user_content = f"""You are given the ORIGINAL source text and its TRANSLATION into {lang_name}. Correct the translation for nativeness (units, terminology, dates in words, rhythm, idioms). Do NOT lose any facts or reduce the volume relative to the ORIGINAL. Output ONLY the corrected {lang_name} text in square brackets [ ].

ORIGINAL (source):
{original_source_chunk.strip()}

TRANSLATION (to correct):
{chunk_text}

Output ONLY the corrected translation in [ ]."""
    else:
        user_content = f"""Check this {lang_name} text for translation nativeness. Fix: dates/centuries in words (1800s → nineteenth century in {lang_name}), units, terminology, rhythm, cultural. Then output ONLY the corrected text in square brackets [ ].

Text:
{chunk_text}"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]

    success, response = call_openai_api(
        messages=messages,
        model=get_postprocess_model(),
        api_key=api_key,
        log_queue=log_queue,
        task_info="native_check",
        service_tier=POSTPROCESS_SERVICE_TIER,
    )

    if success and response:
        fixed = extract_from_brackets(response)
        if fixed and len(fixed.strip()) > 10:
            # При наличии оригинала проверка длины по оригиналу — в postprocess
            if original_source_chunk:
                return fixed
            # Без оригинала — не принимать сильное укорочение относительно перевода
            if len(fixed) >= len(chunk_text) * 0.85:
                return fixed
    return chunk_text


# ============== 3-СТУПЕНЧАТЫЙ ПОСТПРОЦЕСС (Pass A/B/C) ==============

# Модель для постобработки (Pass A/B/C/D) — по умолчанию gpt-5-mini, настраивается в «Настройки ключей»
POSTPROCESS_MODEL = "gpt-5-mini"
POSTPROCESS_SERVICE_TIER = "flex"


def get_postprocess_model() -> str:
    """Модель для проверки (Pass A/B/C/D, native speaker check). Читается из настроек ключей."""
    return get_keys_config().get("postprocess_check_model", POSTPROCESS_MODEL) or POSTPROCESS_MODEL


def get_postprocess_openai_key() -> str:
    """BYOK-first OpenAI credential for postprocess (own key → server ENV).

    ``user_id`` comes from the thread-local worker context (set_current_user_id).
    """
    return get_effective_openai_key()


# Лимит замен на пасс (избыток ведёт к ошибкам и конфликтам)
MAX_REPLACEMENTS = {"B": 15, "C": 12, "D": 8}

# Минимальная доля длины после Pass B/C — иначе откат к предыдущей версии
MIN_POSTPROCESS_LENGTH_RATIO = 0.85
# Верхний порог длины чанка: при ratio > этого значения — retry. Русский при рерайте с английского естественно расширяется на 15–25%, поэтому 1.40, а не 1.25.
MAX_CHUNK_LENGTH_RATIO = 1.40
# Per-paragraph safety-net (защита от спорадического раздувания одного абзаца ×10-20):
# если len(out_para) > LENGTH_RATIO_LIMIT * max(len(src_para), LENGTH_FLOOR) — length retry.
# LENGTH_FLOOR защищает короткие source-параграфы от ложных срабатываний.
# LENGTH_RATIO_LIMIT=2.0: ловит ×3+ катастрофы, не триггерит на естественное расширение
# RU/DE/TR (диакритики, длиннее английского — 1.3-1.6× нормально).
LENGTH_RATIO_LIMIT = 2.0
LENGTH_FLOOR = 30
MAX_LENGTH_RETRIES = 3

# Fallback intro/outro по языкам (если 4o-mini не вернул suggested_intro/outro)
INTRO_TEMPLATES = {
    "czech": "Jsem {host}, a toto je {channel}.",
    "polish": "Jestem {host}, a to jest {channel}.",
    "romanian": "Sunt {host}, iar acesta este {channel}.",
    "turkish": "Ben {host}, ve bu {channel}.",
    "russian": "Меня зовут {host}, и это {channel}.",
    "ukrainian": "З вами {host}, і це {channel}.",
    "english": "With you is {host}. This is {channel}.",
    "german": "Bei euch ist {host}. Das ist {channel}.",
    "french": "Avec vous, {host}. Voici {channel}.",
    "spanish": "Con vosotros, {host}. Esto es {channel}.",
    "italian": "Con voi è {host}. Questo è {channel}.",
}
OUTRO_TEMPLATES = {
    "czech": "S vámi byl {host}. Uvidíme se v příštím díle {channel}. Podpořte kanál lajkem.",
    "polish": "Z tobą był {host}. Do zobaczenia w kolejnym odcinku {channel}. Wesprzyj kanał lajkiem.",
    "romanian": "Cu tine a fost {host}. Ne vedem în episodul următor al {channel}. Sprijiniți canalul cu un like.",
    "turkish": "Sizinle birlikte olan {host}. {channel} bir sonraki bölümde görüşürüz. Beğeni ile destek olun.",
    "russian": "С вами был {host}. Увидимся в следующем выпуске {channel}. Поддержите канал лайком.",
    "ukrainian": "З вами був {host}. Побачимось у наступному випуску {channel}. Підтримайте канал лайком.",
    "english": "With you was {host}. See you in the next episode of {channel}. Support the channel with a like.",
    "german": "Bei euch war {host}. Bis zur nächsten Folge von {channel}. Unterstützt den Kanal mit einem Like.",
    "french": "Avec vous était {host}. À bientôt pour le prochain épisode de {channel}. Soutenez la chaîne avec un like.",
    "spanish": "Con vosotros estaba {host}. Nos vemos en el próximo episodio de {channel}. Apoya el canal con un like.",
    "italian": "Con voi c'era {host}. Ci vediamo nel prossimo episodio di {channel}. Sostieni il canale con un like.",
}
# Короткий CTA в середине текста (подписка/лайк)
MID_CTA_TEMPLATES = {
    "czech": "Přátelé, malá prosba: podpořte {channel} lajkem a odběrem, pomáhá to kanálu růst.",
    "polish": "Przyjaciele, mała prośba: wesprzyjcie {channel} lajkiem i subskrypcją, to pomaga kanałowi rosnąć.",
    "romanian": "Prieteni, o mică rugăminte: sprijiniți {channel} cu un like și abonare, ajută canalul să crească.",
    "turkish": "Arkadaşlar, küçük bir ricam var: {channel} kanalını beğeni ve abone olarak destekleyin.",
    "russian": "Друзья, небольшая просьба: поддержите канал {channel} лайком и подпиской, это помогает развиваться.",
    "ukrainian": "Друзі, невелика просьба: підтримайте канал {channel} лайком та підпискою.",
    "english": "Friends, a small request: support {channel} with a like and subscribe, it helps the channel grow.",
    "german": "Freunde, eine kleine Bitte: unterstützt {channel} mit einem Like und Abo.",
    "french": "Amis, une petite demande : soutenez {channel} avec un like et un abonnement.",
    "spanish": "Amigos, una pequeña petición: apoyad {channel} con un like y suscripción.",
    "italian": "Amici, una piccola richiesta: sostenete {channel} con un like e iscrizione.",
}


def _lang_key(lang_name: str) -> str:
    """Нормализует имя языка для ключа в INTRO/OUTRO_TEMPLATES."""
    return (lang_name or "").strip().lower().split("(")[0].strip()


def _length_guard(
    original: str,
    processed: str,
    pass_name: str,
    log_queue: queue.Queue,
    min_ratio: float = MIN_POSTPROCESS_LENGTH_RATIO,
) -> str:
    """Если processed короче original более чем на (1 - min_ratio), откат к original."""
    if not original or not processed:
        return original or processed
    ratio = len(processed) / len(original) if len(original) > 0 else 1.0
    if ratio < min_ratio:
        log_msg(log_queue, f"   ⚠️ {pass_name}: текст укоротился до {ratio:.0%}, откат")
        return original
    return processed


def merge_branding(
    text: str,
    audit_json: Dict,
    host_name: str,
    channel_name: str,
    lang_name: str,
    main_character_name: str = "",
    log_queue: Optional[queue.Queue] = None,
    skip_name_replacement: bool = False,
    rewrite_contract: bool = False,
) -> str:
    """
    Применяет фиксы брендинга к тексту по результатам Pass A (Branding Audit).
    Замена старых имён/каналов, вставка intro/outro при отсутствии.

    rewrite_contract (CONTRACT-C-2026-09-15): режим рерайта — один свой призыв (только с именем канала),
    прощание только при обоих именах, представление один раз; чужой брендинг не копируется
    вставляется только если заданы ОБА имени и аудит его не нашёл (шаблон
    с «the host»/«the channel» вместо пустого имени — выдумка, её не вставляем).
    
    ВАЖНО: Разграничение имён:
    - host_name: имя автора канала/диктора (используется в intro/outro для брендинга)
    - main_character_name: имя главного героя истории (НЕ заменяется на host_name)
    
    Args:
        main_character_name: Имя главного героя истории (НЕ заменяется на host_name).
                            Если не указано, определяется автоматически по частоте упоминаний
                            (имя, которое встречается >10 раз, считается именем главного героя).
    """
    if not audit_json:
        return text

    host = (host_name or "").strip()
    channel = (channel_name or "").strip()
    main_char = (main_character_name or "").strip()

    # 1. Замена старых имён автора/канала на host_name (только целые слова)
    # ДЛЯ ГЕНЕРАЦИИ: замену имен ОТКЛЮЧАЕМ полностью - имена персонажей не должны заменяться
    # ДЛЯ РЕРАЙТА: заменяем только редкие имена в intro/outro зонах
    if skip_name_replacement:
        if log_queue:
            log_msg(log_queue, "   ⚠️ Замена имен ОТКЛЮЧЕНА (это генерация, не рерайт)")
    elif host:
        for old_name in audit_json.get("old_names_found") or []:
            on = str(old_name).strip()
            if not on or on.lower() == host.lower():
                continue
            
            # Проверяем частоту упоминаний в тексте
            count = len(re.findall(r"\b" + re.escape(on) + r"\b", text, re.IGNORECASE))
            
            # КРИТИЧЕСКИЙ ФИЛЬТР: если имя встречается >3 раз, это скорее всего имя персонажа
            # Имя ведущего встречается максимум 1-2 раза (в intro/outro, которые еще не вставлены)
            # Если имя встречается 3+ раза - это точно персонаж истории, не заменяем!
            if count > 3:
                if log_queue:
                    log_msg(log_queue, f"   ⚠️ Пропущено имя '{on}' (встречается {count} раз - это имя ПЕРСОНАЖА, не ведущего)")
                continue
            
            # Если указано имя главного героя и оно совпадает - не заменяем
            if main_char and (on.lower() == main_char.lower() or main_char.lower() in on.lower() or on.lower() in main_char.lower()):
                if log_queue:
                    log_msg(log_queue, f"   ⚠️ Пропущено имя '{on}' (главный герой: {main_char})")
                continue
            
            # Проверяем контекст: если имя используется как персонаж (в диалогах, описаниях действий),
            # это НЕ имя ведущего. Имя ведущего должно быть только в предложениях от первого лица
            # типа "Я, [имя], расскажу..." или "Меня зовут [имя]"
            name_pattern = r"\b" + re.escape(on) + r"\b"
            matches = list(re.finditer(name_pattern, text, re.IGNORECASE))
            
            # Если имя встречается в контексте, где оно явно персонаж (перед глаголом действия,
            # в диалогах, в описаниях), пропускаем
            is_character_name = False
            for match in matches:
                start, end = match.span()
                # Берем контекст вокруг имени (50 символов до и после)
                context_start = max(0, start - 50)
                context_end = min(len(text), end + 50)
                context = text[context_start:context_end].lower()
                
                # Если имя используется как подлежащее с глаголом действия (персонаж делает что-то)
                # или в диалогах - это персонаж, не ведущий
                # Универсальные индикаторы для всех языков: глаголы действия, диалоги, описания действий
                action_indicators = [
                    # Диалоги (универсально для всех языков)
                    ' — ', ' "', ' „', ' "', ' „', '"', "'", '«', '»',
                    # Паттерны: имя + глагол (универсально)
                    on.lower() + ' said', on.lower() + ' says', on.lower() + ' told',
                    on.lower() + ' looked', on.lower() + ' looks', on.lower() + ' sat',
                    on.lower() + ' sits', on.lower() + ' stood', on.lower() + ' stands',
                    on.lower() + ' went', on.lower() + ' goes', on.lower() + ' came',
                    on.lower() + ' comes', on.lower() + ' took', on.lower() + ' takes',
                    on.lower() + ' put', on.lower() + ' puts', on.lower() + ' did',
                    on.lower() + ' does', on.lower() + ' made', on.lower() + ' makes',
                    # Русский
                    on.lower() + ' сказал', on.lower() + ' сказала', on.lower() + ' говорит',
                    on.lower() + ' смотрел', on.lower() + ' смотрела', on.lower() + ' сидел',
                    on.lower() + ' сидела', on.lower() + ' встал', on.lower() + ' встала',
                    # Польский
                    on.lower() + ' powiedział', on.lower() + ' powiedziała', on.lower() + ' mówi',
                    on.lower() + ' patrzy', on.lower() + ' patrzył', on.lower() + ' siedzi',
                    on.lower() + ' siedział', on.lower() + ' wstał', on.lower() + ' wstała',
                    # Чешский
                    on.lower() + ' řekl', on.lower() + ' řekla', on.lower() + ' říká',
                    on.lower() + ' seděl', on.lower() + ' seděla', on.lower() + ' vstal',
                    # Румынский
                    on.lower() + ' a spus', on.lower() + ' a zis', on.lower() + ' spune',
                    # Общие паттерны действий
                    ' ' + on.lower() + ' ',  # имя как отдельное слово в предложении (скорее персонаж)
                ]
                
                if any(indicator in context for indicator in action_indicators):
                    is_character_name = True
                    break
                
                # Дополнительная проверка: если имя стоит перед запятой/двоеточием и после него глагол - это персонаж
                # Паттерн: "[Name], [verb]" или "[Name]: [verb]"
                name_pos_in_context = context.find(on.lower())
                if name_pos_in_context > 0:
                    after_name = context[name_pos_in_context + len(on):name_pos_in_context + len(on) + 20]
                    if any(marker in after_name[:10] for marker in [',', ':', ' —', ' "', ' „']):
                        is_character_name = True
                        break
            
            if is_character_name:
                if log_queue:
                    log_msg(log_queue, f"   ⚠️ Пропущено имя '{on}' (используется как имя персонажа в действиях/диалогах)")
                continue
            
            # Если имя встречается редко (≤5 раз) и не в контексте персонажа,
            # заменяем его (это может быть старое имя ведущего в тексте)
            # НО заменяем ТОЛЬКО если оно в начале или конце текста (где может быть intro/outro)
            text_len = len(text)
            intro_zone_end = max(300, int(text_len * 0.08))  # Первые 8% или минимум 300 символов
            outro_zone_start = max(0, text_len - int(text_len * 0.08))  # Последние 8%
            
            intro_text = text[:intro_zone_end]
            middle_text = text[intro_zone_end:outro_zone_start]
            outro_text = text[outro_zone_start:]
            
            # Заменяем ТОЛЬКО в intro и outro зонах
            intro_replaced = re.sub(name_pattern, host, intro_text, flags=re.IGNORECASE)
            outro_replaced = re.sub(name_pattern, host, outro_text, flags=re.IGNORECASE)
            
            # Если были замены в intro/outro, обновляем текст
            if intro_replaced != intro_text or outro_replaced != outro_text:
                text = intro_replaced + middle_text + outro_replaced
                replaced_count = len(re.findall(r"\b" + re.escape(host) + r"\b", intro_replaced + outro_replaced, re.IGNORECASE)) - len(re.findall(r"\b" + re.escape(host) + r"\b", intro_text + outro_text, re.IGNORECASE))
                if log_queue:
                    log_msg(log_queue, f"   ✓ Заменено имя '{on}' → '{host}' в intro/outro зонах ({replaced_count} замен)")
            else:
                if log_queue:
                    log_msg(log_queue, f"   ⚠️ Имя '{on}' не найдено в intro/outro зонах, пропущено")

    # 2. Замена старых названий канала (целые слова / фразы)
    for old_channel in audit_json.get("old_channels_found") or []:
        oc = str(old_channel).strip()
        if oc and oc.lower() != channel.lower():
            text = re.sub(r"\b" + re.escape(oc) + r"\b", channel or "", text)

    # 3. Нет intro — вставляем около 30% текста (после ближайшего параграфа)
    if rewrite_contract and not audit_json.get("has_intro") and not (host and channel):
        if log_queue and (host or channel):
            log_msg(log_queue, "   ⚠️ Контракт рерайта: задано только одно имя — шаблонное представление не вставляю")
    elif not audit_json.get("has_intro") and (host or channel):
        suggested = audit_json.get("suggested_intro", "").strip()
        if suggested and not rewrite_contract:
            intro_line = suggested
        else:
            key = _lang_key(lang_name)
            tpl = INTRO_TEMPLATES.get(key) or INTRO_TEMPLATES.get("english", "With you is {host}. This is {channel}.")
            intro_line = tpl.format(host=host or "the host", channel=channel or "the channel")
        target_pos = min(2000, len(text))
        paragraphs = text.split("\n\n")
        if len(paragraphs) <= 1:
            text = intro_line + "\n\n" + text
        else:
            pos, cum = 0, 0
            for i, p in enumerate(paragraphs):
                next_cum = cum + len(p) + 2
                if next_cum <= target_pos:
                    pos = i + 1
                    cum = next_cum
                else:
                    pos = i
                    break
            else:
                pos = len(paragraphs)
            pos = max(1, pos)  # всегда после минимум 1 параграфа (хука)
            paragraphs.insert(pos, intro_line)
            text = "\n\n".join(paragraphs)

    # 4. Нет mid-CTA — вставляем в середину текста (около 40–50%, по границе параграфа)
    # CONTRACT-C-2026-09-15: в рерайте свой призыв нужен — шаблонная подстраховка, если модель его не написала;
    # подсказанную строку берём только если в ней есть точное имя канала, иначе шаблон.
    if not audit_json.get("has_mid_cta") and channel:
        suggested = audit_json.get("suggested_mid_cta", "").strip()
        if suggested and (not rewrite_contract or channel in suggested):
            mid_cta_line = suggested
        else:
            key = _lang_key(lang_name)
            tpl = MID_CTA_TEMPLATES.get(key) or MID_CTA_TEMPLATES.get("english", "Friends, a small request: support {channel} with a like and subscribe, it helps the channel grow.")
            mid_cta_line = tpl.format(channel=channel)
        paragraphs = text.split("\n\n")
        if len(paragraphs) >= 3:
            # Вставляем после параграфа, ближайшего к 45% длины
            target_ratio = 0.45
            cum, pos = 0, 0
            total_len = len(text)
            for i, p in enumerate(paragraphs):
                cum += len(p) + 2
                if total_len > 0 and cum / total_len <= target_ratio:
                    pos = i + 1
                else:
                    break
            pos = max(1, min(pos, len(paragraphs) - 1))  # не в самое начало и не в конец
            paragraphs.insert(pos, mid_cta_line)
            text = "\n\n".join(paragraphs)

    # 5. Нет outro — добавляем в конец (fallback по языку)
    # CONTRACT-C-2026-09-15: в рерайте прощание подставляется шаблоном только при ОБОИХ именах
    # (шаблон с «the host»/«the channel» — выдуманная сущность); подсказка — только с точными именами.
    if rewrite_contract and not (host and channel):
        if log_queue and not audit_json.get("has_outro") and (host or channel):
            log_msg(log_queue, "   ⚠️ Контракт рерайта: задано только одно имя — шаблонное прощание не вставляю")
    elif not audit_json.get("has_outro") and (host or channel):
        suggested = audit_json.get("suggested_outro", "").strip()
        if suggested and (not rewrite_contract or (host in suggested and channel in suggested)):
            outro_line = suggested
        else:
            key = _lang_key(lang_name)
            tpl = OUTRO_TEMPLATES.get(key) or OUTRO_TEMPLATES.get("english", "With you was {host}. See you in the next episode of {channel}. Support the channel with a like.")
            outro_line = tpl.format(host=host or "the host", channel=channel or "the channel")
        text = text.rstrip() + "\n\n" + outro_line

    return text


def _postprocess_pass_a_branding_audit(
    text: str, lang_name: str, host_name: str, channel_name: str,
    api_key: str, log_queue: queue.Queue,
) -> Tuple[bool, Optional[Dict]]:
    """Pass A: только анализ, возвращает JSON с брендингом."""
    prompt = f"""You are a branding compliance checker.

INPUT: A YouTube script in {lang_name}.
EXPECTED BRANDING:
- Host name: "{host_name or "(not set)"}"
- Channel name: "{channel_name or "(not set)"}"

CRITICAL RULES FOR "old_names_found":
1. Include ONLY names that appear in INTRO/OUTRO sections or in first-person narrator statements like:
   - "I am [name]" / "My name is [name]" / "I, [name], will tell you..."
   - Names in intro/outro sentences
   - Names used as the narrator/host identity

2. DO NOT include character names from the story, even if they appear frequently:
   - If "Tom" appears 10+ times as a character doing actions → DO NOT include "Tom"
   - If "Piotr Zawadzki" is mentioned as a person in the story → DO NOT include "Piotr Zawadzki"
   - If "Tomek" is used in dialogues or action descriptions → DO NOT include "Tomek"
   
3. How to identify CHARACTER names (DO NOT include):
   - Name appears with action verbs: "[Name] said", "[Name] looked", "[Name] sat"
   - Name appears in dialogues: "— [Name] said..."
   - Name appears multiple times (>3) throughout the text
   - Name is used as subject of action sentences

4. How to identify HOST names (DO include):
   - Name appears in intro/outro sections
   - Name appears in first-person statements: "I, [name]..."
   - Name appears 1-2 times total, likely in intro/outro context
   - Name is used as narrator identity, not as story character

Examples:
- Text: "Tom said hello. Tom sat down. Tom looked around." → DO NOT include "Tom" (character)
- Text: "I am Tom and welcome to the channel." → Include "Tom" (host name in intro)
- Text: "Piotr Zawadzki entered the room. Piotr looked at the table." → DO NOT include "Piotr Zawadzki" (character)
- Text: "Welcome, I'm Piotr Zawadzki." → Include "Piotr Zawadzki" (host name)

TASK: Scan the text and return ONLY a JSON object (no markdown, no code block):
{{
  "old_names_found": ["Tom", "Анатолий", ...],
  "old_channels_found": ["Old Channel Name", ...],
  "has_intro": true or false,
  "intro_position_percent": 0-100,
  "has_outro": true or false,
  "has_mid_cta": true or false,
  "mid_cta_position_percent": 30-70 if has_mid_cta else null,
  "issues": ["description of each issue"],
  "suggested_intro": "if has_intro is false, write one intro sentence IN {lang_name} using host name and channel name",
  "suggested_outro": "if has_outro is false, write one outro sentence IN {lang_name} using host name and channel name",
  "suggested_mid_cta": "if has_mid_cta is false, write ONE short sentence IN {lang_name} asking to like and subscribe, use channel name"
}}

Return ONLY valid JSON. No explanations."""

    messages = [
        {"role": "system", "content": "You output only valid JSON. No markdown, no explanation."},
        {"role": "user", "content": prompt + "\n\nTEXT:\n" + text[:30000]},
    ]
    success, response = call_openai_api(messages, get_postprocess_model(), api_key, log_queue, "Pass A (Branding Audit)", max_tokens=8192, service_tier=POSTPROCESS_SERVICE_TIER)
    if not success:
        return (False, None)
    response = response.strip()
    if response.startswith("```"):
        response = re.sub(r"^```\w*\n?", "", response)
        response = re.sub(r"\n?```\s*$", "", response)
    try:
        data = json.loads(response)
        return (True, data)
    except json.JSONDecodeError:
        log_msg(log_queue, "   ⚠️ Pass A: не удалось распарсить JSON")
        return (False, None)


def _normalize_whitespace(s: str) -> str:
    """Нормализует пробелы: множественные пробелы/переносы → один пробел."""
    return re.sub(r'\s+', ' ', s.strip())


# Маркеры тысяч в годах по языкам (для защиты от усечения дат в Pass D)
YEAR_MARKERS = {
    "ru": ["тысяча", "тысяч"],
    "cs": ["tisíc"],
    "pl": ["tysiąc", "tysięc"],
    "ro": ["mie ", "o mie"],
    "uk": ["тисяча", "тисяч"],
    "bg": ["хиляда", "хиляд"],
    "sk": ["tisíc"],
    "hu": ["ezer"],
    "tr": ["bin"],
    "de": ["tausend"],
    "fr": ["mille"],
    "es": ["mil"],
    "it": ["mille"],
    "en": ["thousand"],
}


def lang_name_to_code(name_or_code: str) -> str:
    """Возвращает код языка (ru, cs, ...) из имени или кода."""
    if not name_or_code:
        return ""
    s = (name_or_code or "").strip().lower()[:10]
    mapping = {"russian": "ru", "czech": "cs", "polish": "pl", "romanian": "ro", "ukrainian": "uk",
              "bulgarian": "bg", "slovak": "sk", "hungarian": "hu", "turkish": "tr", "german": "de",
              "french": "fr", "spanish": "es", "italian": "it", "english": "en", "dutch": "nl"}
    return mapping.get(s, s[:2] if len(s) >= 2 else s)


def make_output_filename(base_name: str, lang_code_or_display: str) -> str:
    """Имя выходного файла: {код}_{base_name}.txt (код = cs, pl, ro и т.д.)"""
    code = lang_name_to_code(lang_code_or_display)
    return f"{code}_{base_name}.txt" if code else f"{base_name}.txt"


def validate_date_replacement(old: str, new: str, lang_code: str) -> bool:
    """Отклоняет замены, которые усекают годы (удаляют маркер тысячи)."""
    code = lang_name_to_code(lang_code) if lang_code and len(str(lang_code)) > 2 else (lang_code or "")[:2].lower()
    markers = YEAR_MARKERS.get(code, [])
    if not markers:
        # Универсальная проверка: если new значительно короче и old содержит числительные
        if len(new) < len(old) * 0.5 and (re.search(r'\d{2,}', old) or any(m in old.lower() for m in ["тысяча", "tisíc", "tysiąc", "mie", "тисяча", "thousand"])):
            return False
        return True
    old_lower = old.lower()
    new_lower = new.lower()
    for marker in markers:
        if marker in old_lower and marker not in new_lower:
            return False
    return True


def validate_declutter_replacement(old: str, new: str) -> bool:
    """Базовые проверки замен Pass B (защита от канцелярита и раздувания)."""
    if len(new) > len(old) * 1.8 and len(old) > 15:
        return False
    old_words = old.split()
    new_words = new.split()
    if len(old_words) <= 4 and len(new_words) > len(old_words) * 2:
        return False
    bureaucratic = ["осуществлять", "испытывать", "являться", "производить", "осуществляет",
                    "испытывает", "является", "производит"]
    for word in bureaucratic:
        if word in new.lower() and word not in old.lower():
            return False
    return True


# Паттерны имён собственных (дома, парки, замки и т.д.) — не переводить
PROPER_NOUNS_PATTERNS = [
    r'\b\w+ House\b', r'\b\w+ Palace\b', r'\b\w+ Castle\b',
    r'\b\w+ Park\b', r'\b\w+ Street\b', r'\b\w+ Square\b',
    r'\b\w+ Lane\b', r'\b\w+ Road\b', r'\b\w+ Gardens\b',
    r'\b\w+ Abbey\b', r'\b\w+ Cathedral\b',
]


def is_proper_noun_change(old: str, new: str) -> bool:
    """Проверяет, не пытаемся ли перевести имя собственное."""
    for pattern in PROPER_NOUNS_PATTERNS:
        if re.search(pattern, old, re.IGNORECASE):
            if old.strip().lower() != new.strip().lower():
                return True
    return False


def _dominant_script(s: str) -> str:
    """Определяет доминирующий алфавит: cyrillic, latin или unknown."""
    scripts = {}
    for ch in s:
        if ch.isalpha():
            if '\u0400' <= ch <= '\u04FF':
                scripts['cyrillic'] = scripts.get('cyrillic', 0) + 1
            elif '\u0041' <= ch <= '\u007A' or '\u00C0' <= ch <= '\u024F':
                scripts['latin'] = scripts.get('latin', 0) + 1
    if not scripts:
        return 'unknown'
    return max(scripts, key=scripts.get)


def validate_script_match(old: str, text: str) -> bool:
    """Проверяет, что old написан на том же алфавите что и текст (кириллица vs латиница)."""
    if not old or not text or len(old) < 5:
        return True
    text_script = _dominant_script(text[:500])
    old_script = _dominant_script(old)
    if text_script == 'cyrillic' and old_script == 'latin':
        return False
    return True


def validate_replacement(old: str, new: str, pass_name: str, lang_code: str, text: str = "") -> Tuple[bool, str]:
    """
    Универсальный валидатор замен для всех пассов.
    Возвращает (is_valid, reason).
    """
    if old.strip() == new.strip():
        return False, "noop"
    if not new.strip():
        return False, "empty_replacement"
    if pass_name in ("D", "Pass D", "language_purity"):
        if not validate_date_replacement(old, new, lang_code):
            return False, "date_truncation"
        if text and not validate_script_match(old, text):
            return False, "script_mismatch"
    if pass_name in ("B", "Pass B", "declutter"):
        if not validate_declutter_replacement(old, new):
            return False, "bureaucratic_bloat"
    if pass_name in ("C", "D", "Pass C", "Pass D", "nativeness", "language_purity"):
        if is_proper_noun_change(old, new):
            return False, "proper_noun_translation"
    if len(old) > 10:
        ratio = len(new) / len(old)
        if ratio > 3.0 or ratio < 0.2:
            return False, f"extreme_length_change"
    return True, "ok"


def _find_fuzzy_match(text: str, pattern: str) -> Optional[str]:
    """
    Ищет pattern в text с гибким сопоставлением:
    1. Точное совпадение
    2. С нормализованными пробелами (игнорируя множественные пробелы/переносы)
    """
    # 1. Точное совпадение
    if pattern in text:
        return pattern
    
    # 2. Нормализация пробелов - создаём regex паттерн, который игнорирует множественные пробелы
    pattern_norm = _normalize_whitespace(pattern)
    pattern_words = pattern_norm.split()
    
    if len(pattern_words) < 2:
        return None
    
    # Создаём regex: каждое слово + один или более пробелов между словами
    regex_pattern = r'\s+'.join(re.escape(word) for word in pattern_words)
    regex_pattern = r'\s*' + regex_pattern + r'\s*'
    
    # Ищем в тексте
    match = re.search(regex_pattern, text, re.IGNORECASE)
    if match:
        matched_text = match.group(0)
        # Проверяем, что длина примерно соответствует (не слишком отличается)
        if abs(len(matched_text) - len(pattern)) <= len(pattern) * 0.5:
            return matched_text
    
    return None


def _apply_replacements(
    text: str, replacements: List[Dict], log_queue: queue.Queue, pass_name: str,
    lang_code: str = "",
) -> Tuple[str, List[Dict]]:
    """
    Применяет список замен к тексту с гибким сопоставлением.
    Unicode-нормализация NFC для совместимости (напр. румынские ț, ș).
    Валидирует каждую замену перед применением.
    Возвращает (результат, список деталей: [{old, new, status}]).
    """
    result = unicodedata.normalize("NFC", text)
    details: List[Dict] = []

    # Лимит замен на пасс
    pass_letter = pass_name.replace("Pass ", "").strip() if "Pass" in pass_name else pass_name
    limit = MAX_REPLACEMENTS.get(pass_letter, 15)
    if len(replacements) > limit:
        log_msg(log_queue, f"   ⚠️ {pass_name}: {len(replacements)} замен, обрезаю до {limit}")
        replacements = replacements[:limit]

    valid_replacements = []
    for item in replacements:
        old_val = item.get("old")
        new_val = item.get("new")
        if old_val is None:
            continue
        if not isinstance(old_val, str):
            old_val = str(old_val)
        if new_val is None:
            new_val = ""
        else:
            new_val = str(new_val)
        old_val = unicodedata.normalize("NFC", old_val)
        new_val = unicodedata.normalize("NFC", new_val)
        # Запрет удаления: "new" не должен быть пустым или только пробелы (Pass B не должен удалять предложения)
        if not new_val.strip():
            details.append({"old": (old_val[:80] + "…") if len(old_val) > 80 else old_val, "new": "(empty)", "status": "skipped_empty"})
            continue
        if len(old_val) < 3:
            details.append({"old": old_val[:50] + "…" if len(old_val) > 50 else old_val, "new": new_val[:30] + "…" if len(new_val) > 30 else new_val, "status": "skipped_short"})
            continue
        if old_val.strip() == new_val.strip():
            details.append({"old": old_val[:80] + "…" if len(old_val) > 80 else old_val, "new": new_val[:80] + "…" if len(new_val) > 80 else new_val, "status": "skipped_noop"})
            continue
        
        # ЗАЩИТА ИМЁН СОБСТВЕННЫХ: если old_val — имя собственное (с заглавной буквы, короткое),
        # и new_val — то же слово в другом падеже/форме, пропускаем замену
        if pass_name == "Pass C":  # Только для Pass C (Nativeness)
            old_stripped = old_val.strip().rstrip(".,!?;:")
            new_stripped = new_val.strip().rstrip(".,!?;:")
            
            # Проверяем: начинается с заглавной буквы, короткое (≤15 символов), одно слово
            if (len(old_stripped) <= 15 and 
                old_stripped and old_stripped[0].isupper() and
                len(old_stripped.split()) == 1 and
                len(new_stripped.split()) == 1):
                
                # Проверяем, что new_val — это то же слово, но в другой форме (склонение)
                # Убираем знаки препинания и сравниваем базовую форму
                old_base = old_stripped.lower().rstrip(".,!?;:")
                new_base = new_stripped.lower().rstrip(".,!?;:")
                
                # Если базовые формы похожи (одно слово превратилось в другое с изменением окончания),
                # и это не просто добавление/удаление знаков препинания — пропускаем
                # Также проверяем типичные окончания склонения (польский: -a, -em, -owi, -ie и т.д.)
                common_case_endings = ['a', 'em', 'owi', 'ie', 'ą', 'ego', 'emu', 'im', 'ym', 'ym', 'ej', 'ą']
                old_ends_with_case = any(old_base.endswith(e) for e in common_case_endings)
                new_ends_with_case = any(new_base.endswith(e) for e in common_case_endings)
                
                # Если одно слово склонено в другое (изменение окончания на типичное падежное)
                if (old_base != new_base and 
                    (old_base in new_base or new_base in old_base) and
                    abs(len(old_base) - len(new_base)) <= 5 and  # Изменение на ≤5 символов (окончание)
                    (old_ends_with_case or new_ends_with_case or 
                     any(old_base[:-2] == new_base[:-2] or old_base[:-3] == new_base[:-3] for _ in [1]))):  # Одинаковый корень
                    details.append({"old": old_val[:80] + "…" if len(old_val) > 80 else old_val, 
                                   "new": new_val[:80] + "…" if len(new_val) > 80 else new_val, 
                                   "status": "skipped_proper_noun"})
                    continue
                
                # Дополнительная проверка: если new_val просто добавляет точку/знак препинания к old_val
                if old_stripped == new_stripped.rstrip(".,!?;:"):
                    details.append({"old": old_val[:80] + "…" if len(old_val) > 80 else old_val, 
                                   "new": new_val[:80] + "…" if len(new_val) > 80 else new_val, 
                                   "status": "skipped_proper_noun"})
                    continue

        # Универсальная валидация замен
        is_valid, reason = validate_replacement(old_val, new_val, pass_name, lang_code, text=result)
        if not is_valid:
            details.append({"old": (old_val[:60] + "…") if len(old_val) > 60 else old_val,
                           "new": (new_val[:50] + "…") if len(new_val) > 50 else new_val,
                           "status": f"rejected:{reason}"})
            continue

        valid_replacements.append((old_val, new_val))
    
    # Сортируем по длине (от длинных к коротким)
    valid_replacements.sort(key=lambda x: len(x[0]), reverse=True)
    
    for old_val, new_val in valid_replacements:
        if old_val in result:
            result = result.replace(old_val, new_val)
            details.append({"old": old_val[:80] + "…" if len(old_val) > 80 else old_val, "new": new_val[:80] + "…" if len(new_val) > 80 else new_val, "status": "applied"})
            continue
        matched = _find_fuzzy_match(result, old_val)
        if matched:
            result = result.replace(matched, new_val, 1)
            details.append({"old": (old_val[:80] + "…") if len(old_val) > 80 else old_val, "new": (new_val[:80] + "…") if len(new_val) > 80 else new_val, "status": "applied"})
            continue
        details.append({"old": old_val[:80] + "…" if len(old_val) > 80 else old_val, "new": new_val[:80] + "…" if len(new_val) > 80 else new_val, "status": "skipped_not_found"})
    
    applied = sum(1 for d in details if d.get("status") == "applied")
    skipped_nf = sum(1 for d in details if d.get("status") == "skipped_not_found")
    skipped_short = sum(1 for d in details if d.get("status") == "skipped_short")
    skipped_noop = sum(1 for d in details if d.get("status") == "skipped_noop")
    skipped_empty = sum(1 for d in details if d.get("status") == "skipped_empty")
    skipped_proper = sum(1 for d in details if d.get("status") == "skipped_proper_noun")
    rejected = sum(1 for d in details if str(d.get("status", "")).startswith("rejected:"))
    log_msg(log_queue, f"   {pass_name}: применено {applied} замен" + (
        f", пропущено {skipped_nf} (не найдено)" if skipped_nf else ""
    ) + (f", {skipped_short} коротких" if skipped_short else "") + (f", {skipped_noop} no-op" if skipped_noop else "") + (f", {skipped_empty} пустых" if skipped_empty else "") + (f", {skipped_proper} имён собственных" if skipped_proper else "") + (f", отклонено {rejected}" if rejected else ""))
    return (result, details)


def _postprocess_pass_b_ai_declutter(text: str, lang_name: str, api_key: str, log_queue: queue.Queue, lang_code: str = "") -> Tuple[bool, str, Optional[str], List[Dict]]:
    """Pass B: модель возвращает только список замен (JSON); применяем их к тексту."""
    intensifiers_note = f"""
Intensifiers to limit (max 2 total) — examples: Czech: skutečně, opravdu; Polish: naprawdę, absolutnie; Romanian: cu adevărat; Turkish: gerçekten; Russian: по-настоящему; English: truly, really. Apply for {lang_name}."""
    prompt = f"""You are a style editor for YouTube scripts in {lang_name}. Do NOT return the full text.

Your task: list ONLY the exact replacements to make the text sound human-written (reduce AI patterns).

ПРАВИЛА СТИЛИСТИЧЕСКОЙ ПОЛИРОВКИ:
1. НЕ ЗАМЕНЯЙ короткие эмоциональные фразы длинными объяснениями. "Она боялась" лучше чем "Она испытывала страх" — краткость = сила для TTS.
2. НЕ МЕНЯЙ смысл. Если оригинал говорит "дом" (household), не заменяй на "семья" (family).
3. НЕ ДОБАВЛЯЙ канцеляризмы: "испытывала", "осуществляла", "являлась" — это убивает живой текст.
4. Каждая замена должна УЛУЧШАТЬ звучание при чтении вслух. Если не улучшает — не делай.
5. Максимум 15-20 замен на текст. Не нужно менять всё подряд.

RULES: Reduce repetitive intensifiers (max 2 total). Break formulaic transitions. Break question floods (4+ questions in a row). Vary sentence openings. Do not change facts, names, numbers, or meaning. Do not add content.

CRITICAL: NEVER change character names (protagonists, antagonists, side characters). Keep ALL character names exactly as written. Only change stylistic phrases, not names of people.

NARRATIVE VOICE PROTECTION:
- Do NOT touch a section marker of the source (a chapter line, or a short 1-4 word label that opens a segment). Never rephrase it, never merge it into the next sentence.
- Do NOT touch a forward-looking bridge sentence ("But what came next was stranger still."). It is deliberate retention work, not filler.
- Do NOT replace a key term that repeats through the text with a synonym: the repetition is the through-line.
- Do NOT remove dramatic repetitions if they build tension (e.g. "Dark. Too dark. Impossibly dark.").
- Do NOT flatten emotional escalation patterns (short → shorter → punch).
- Do NOT replace sensory language with abstract descriptions.
- If a phrase sounds like intentional style (staccato rhythm, questions for suspense, sentence fragments for pacing) — LEAVE IT.

OUTPUT FORMAT: A single JSON array. Each item: {{"old": "...", "new": "..."}}.

"new" MUST always contain replacement text — NEVER empty string or whitespace. REPHRASE, don't delete. "old" and "new" must differ (no identical pairs). Do NOT convert written-out numbers to digits. Do NOT change currencies or units. Maximum 15 replacements.

CRITICAL: For "old" you MUST copy the phrase CHARACTER-FOR-CHARACTER from the TEXT below—same spaces, punctuation, line breaks. If "old" is not found in the text, the replacement will be skipped.

Output ONLY the JSON array, no markdown, no explanation. Example: [{{"old": " a really ", "new": " a genuinely "}}, {{"old": "And here's the thing.", "new": "What matters is this."}}]

TEXT (copy "old" strings from here exactly):
""" + text[:50000]

    messages = [
        {"role": "system", "content": "You output only a JSON array of objects with keys 'old' and 'new'. No other text."},
        {"role": "user", "content": prompt},
    ]
    success, response = call_openai_api(messages, get_postprocess_model(), api_key, log_queue, "Pass B (AI Declutter)", max_tokens=8192, service_tier=POSTPROCESS_SERVICE_TIER)
    if not success:
        return (False, text, None, [])
    response = response.strip()
    if response.startswith("```"):
        response = re.sub(r"^```\w*\n?", "", response)
        response = re.sub(r"\n?```\s*$", "", response)
    try:
        replacements = json.loads(response)
        if not isinstance(replacements, list):
            log_msg(log_queue, "   ⚠️ Pass B: ответ не массив")
            return (False, text, None, [])
        result, details = _apply_replacements(text, replacements, log_queue, "Pass B", lang_code)
        return (True, result, response, details)
    except json.JSONDecodeError as e:
        log_msg(log_queue, f"   ⚠️ Pass B: не удалось распарсить JSON — {e}")
        return (False, text, None, [])


def _postprocess_pass_c_nativeness(text: str, lang_name: str, api_key: str, log_queue: queue.Queue, lang_code: str = "") -> Tuple[bool, str, Optional[str], List[Dict]]:
    """Pass C: модель возвращает только список замен (JSON); применяем их к тексту."""
    prompt = f"""You are a native {lang_name} speaker reviewing a YouTube script. Do NOT return the full text.

Your task: list ONLY the exact replacements to fix nativeness and correctness in {lang_name}.

ПРАВИЛА НАТУРАЛИЗАЦИИ:
1. НИКОГДА не переводи имена собственные: названия домов (York House, Clarence House), парков (Green Park, Hyde Park), замков и т.д. — это имена, не описания.
2. НИКОГДА не меняй глагол на его антоним или слово с противоположным значением. Если в тексте "устраивается" — не заменяй на "зарабатывается".
3. Если не уверен в замене — НЕ ДЕЛАЙ ЕЁ. Нулевое действие лучше ошибки.
4. Максимум 10-15 замен на текст. Концентрируйся на действительно неестественных оборотах.

FIX (via replacements): Calques; unnatural word order; wooden syntax; register inconsistency. Terminology: correct {lang_name} terms for the target audience. Cultural references: fix wrong country toponyms; translate foreign place names to {lang_name}.

ABSOLUTELY FORBIDDEN — NEVER CHANGE:
1. DATES AND YEARS: NEVER touch any year, date, or time reference. "в тысяча девятьсот третьем году" must stay EXACTLY as written. "nineteen hundred twenty-three" must stay EXACTLY as written. Do NOT abbreviate, shorten, reformat, or simplify ANY date. If a phrase contains a year — leave the ENTIRE phrase untouched.
2. NUMBERS: NEVER change written-out numbers. "восемь тысяч двести пятьдесят три" stays as is.
3. CURRENCIES: dollars stay dollars, NOT local currency.
4. PROPER NOUNS (ИМЕНА СОБСТВЕННЫЕ): NEVER change proper nouns — names of people, cities, nicknames, surnames. They are ALREADY in the correct form. Examples:
   - "Gruby" stays "Gruby" (NOT "Grubym", NOT "Grubego")
   - "Sokołowski" stays "Sokołowski" (NOT "Sokołowskiego", NOT "Sokołowskim")
   - "Warszawa" stays "Warszawa" (NOT "Warszawą", NOT "Warszawie")
   - "Piotr" stays "Piotr" (NOT "Piotra", NOT "Piotrem")
   - "Mokotów" stays "Mokotów" (NOT "Mokotowie", NOT "Mokotowa")
   - "Polska" stays "Polska" (NOT "Polska.", NOT "Polski")
   Do NOT decline proper nouns. Do NOT add punctuation to proper nouns. They are correct as written.
5. CHARACTER NAMES: NEVER change character names (protagonists, antagonists, side characters). Keep ALL character names exactly as written. "Karel Horáček" stays "Karel Horáček", "Sergej" stays "Sergej", etc.
6. IDENTICAL PAIRS: "old" and "new" MUST differ.

When in doubt about whether something is a date, number, or proper noun — DO NOT TOUCH IT.

CRITICAL: For "old" you MUST copy the phrase CHARACTER-FOR-CHARACTER from the TEXT below—same spaces, punctuation, line breaks. Do not paraphrase. Take the exact substring from TEXT and paste into "old". If "old" is not in the text, the replacement will be skipped. Do not change meaning. Maximum 12 replacements.

OUTPUT FORMAT: A single JSON array. Each item: {{"old": "exact phrase from text", "new": "replacement"}}. Output ONLY this JSON array, no markdown, no explanation.

TEXT (copy "old" strings from here exactly):
""" + text[:50000]

    messages = [
        {"role": "system", "content": "You output only a JSON array of objects with keys 'old' and 'new'. No other text."},
        {"role": "user", "content": prompt},
    ]
    success, response = call_openai_api(messages, get_postprocess_model(), api_key, log_queue, "Pass C (Nativeness)", max_tokens=8192, service_tier=POSTPROCESS_SERVICE_TIER)
    if not success:
        return (False, text, None, [])
    response = response.strip()
    if response.startswith("```"):
        response = re.sub(r"^```\w*\n?", "", response)
        response = re.sub(r"\n?```\s*$", "", response)
    try:
        replacements = json.loads(response)
        if not isinstance(replacements, list):
            log_msg(log_queue, "   ⚠️ Pass C: ответ не массив")
            return (False, text, None, [])
        result, details = _apply_replacements(text, replacements, log_queue, "Pass C", lang_code)
        return (True, result, response, details)
    except json.JSONDecodeError as e:
        log_msg(log_queue, f"   ⚠️ Pass C: не удалось распарсить JSON — {e}")
        return (False, text, None, [])


def _postprocess_pass_d_language_purity(text: str, lang_name: str, api_key: str, log_queue: queue.Queue, lang_code: str = "") -> Tuple[bool, str, Optional[str], List[Dict]]:
    """Pass D: Language Purity — находит слова не того языка и возвращает только реальные замены (JSON)."""
    prompt = f"""You are a {lang_name} language purity auditor for a TTS YouTube script.

Ты работаешь с текстом на {lang_name}. Все значения в поле "old" ДОЛЖНЫ быть фрагментами из текста на {lang_name}, а НЕ на английском или любом другом языке. Ищи в тексте фрагменты, которые нужно заменить, и копируй их ТОЧНО как они есть в тексте.

КРИТИЧЕСКОЕ ПРАВИЛО ДЛЯ ДАТ И ЧИСЛИТЕЛЬНЫХ:
- НИКОГДА не сокращай годы. Год "тысяча девятьсот пятнадцатый" должен оставаться полным.
- Допустимые форматы: "тысяча девятьсот пятнадцатый год" ИЛИ цифрами "1915".
- ЗАПРЕЩЕНО удалять "тысяча" или "девятьсот" из годов — это делает дату бессмысленной.
- Числительные в контексте адресов, возрастов и количеств тоже не трогать.
- Если не уверен — НЕ МЕНЯЙ. Лучше оставить как есть, чем сломать.

YOUR ONLY TASK: Find words/phrases that are NOT {lang_name} and need translation.

FIND these problems ONLY:
1. UNTRANSLATED ENGLISH phrases (e.g. "plain sight" → translate to {lang_name})
2. WORDS FROM WRONG LANGUAGES (e.g. Spanish "palacios" in Romanian, Czech "závoje" in Polish)
3. INVENTED/NONSENSE words that don't exist in any language
4. BARE DIGIT YEARS that should be written out (e.g. "1925" → words)

CRITICAL — DO NOT TOUCH:
- Number words ("bin", "yüz", "mie", "tysiąc") are PART OF {lang_name} — NEVER remove them
- Turkish "bin" = "thousand" and is REQUIRED in dates (bin dokuz yüz = 1900)
- Character names (protagonists, antagonists, side characters) — keep ALL character names exactly as written
- Proper nouns (city names, personal names, building names) — keep as-is
- Established foreign terms (Beaux-Arts, KGB, etc.) — keep as-is
- Words that ARE correct {lang_name} — do NOT list them

OUTPUT: JSON array. Each item: {{"old": "exact foreign/wrong phrase FROM THE TEXT", "new": "{lang_name} equivalent"}}

If text is clean (no foreign words found): return []

IMPORTANT: Return ONLY items that need ACTUAL changes. Maximum 8 items.

TEXT:
""" + text[:50000]

    messages = [
        {"role": "system", "content": "You output only a JSON array of objects with keys 'old' and 'new', or empty array []. No other text."},
        {"role": "user", "content": prompt},
    ]
    success, response = call_openai_api(messages, get_postprocess_model(), api_key, log_queue, "Pass D (Language Purity)", max_tokens=8192, service_tier=POSTPROCESS_SERVICE_TIER)
    if not success:
        return (False, text, None, [])
    response = response.strip()
    if response.startswith("```"):
        response = re.sub(r"^```\w*\n?", "", response)
        response = re.sub(r"\n?```\s*$", "", response)
    try:
        replacements = json.loads(response)
        if not isinstance(replacements, list):
            log_msg(log_queue, "   ⚠️ Pass D: ответ не массив")
            return (False, text, None, [])
        if len(replacements) == 0:
            log_msg(log_queue, "   Pass D: текст чистый, замен 0")
            return (True, text, response, [])
        result, details = _apply_replacements(text, replacements, log_queue, "Pass D", lang_code)
        return (True, result, response, details)
    except json.JSONDecodeError as e:
        log_msg(log_queue, f"   ⚠️ Pass D: не удалось распарсить JSON — {e}")
        return (False, text, None, [])


def _write_postprocess_log(
    output_dir: str,
    base_name: str,
    lang_code: str,
    pass_a_ok: bool,
    audit_json: Optional[Dict],
    pass_b_raw: Optional[str],
    pass_b_details: List[Dict],
    chars_before: int,
    chars_after_b: int,
    merge_actions: List[str],
    chars_after_merge: int,
    pass_c_raw: Optional[str],
    pass_c_details: List[Dict],
    chars_after_c: int,
    pass_d_raw: Optional[str] = None,
    pass_d_details: Optional[List[Dict]] = None,
    chars_after_d: Optional[int] = None,
) -> None:
    """Пишет постпроцесс-лог в {output_dir}/{base_name}_{lang_code}_postprocess.log."""
    if not output_dir or not base_name or not lang_code:
        return
    log_path = os.path.join(output_dir, f"{base_name}_{lang_code}_postprocess.log")
    pass_d_details = pass_d_details or []
    chars_after_d = chars_after_d if chars_after_d is not None else chars_after_c
    lines = [
        "=== Postprocess pipeline log ===",
        f"Chars: before={chars_before} → after B={chars_after_b} → after merge={chars_after_merge} → after C={chars_after_c} → after D={chars_after_d}",
        "",
    ]
    lines.append("--- Pass A (Branding Audit) ---")
    if pass_a_ok and audit_json:
        lines.append(json.dumps(audit_json, ensure_ascii=False, indent=2))
    else:
        lines.append("(failed or no data)")
    lines.append("")
    lines.append("--- Pass B (AI Declutter): raw JSON ---")
    lines.append(pass_b_raw if pass_b_raw else "(no data)")
    lines.append("")
    lines.append("--- Pass B: per-replacement status ---")
    for i, d in enumerate(pass_b_details):
        o, n = str(d.get("old", ""))[:70], str(d.get("new", ""))[:50]
        lines.append(f"  {i+1}. [{d.get('status', '?')}] old: {o} → new: {n}")
    lines.append("")
    lines.append("--- Merge (branding) ---")
    for a in merge_actions:
        lines.append(f"  {a}")
    lines.append("")
    lines.append("--- Pass C (Nativeness): raw JSON ---")
    lines.append(pass_c_raw if pass_c_raw else "(no data)")
    lines.append("")
    lines.append("--- Pass C: per-replacement status ---")
    for i, d in enumerate(pass_c_details):
        o, n = str(d.get("old", ""))[:70], str(d.get("new", ""))[:50]
        lines.append(f"  {i+1}. [{d.get('status', '?')}] old: {o} → new: {n}")
    lines.append("")
    lines.append("--- Pass D (Language Purity): raw JSON ---")
    lines.append(pass_d_raw if pass_d_raw else "(no data)")
    lines.append("")
    lines.append("--- Pass D: per-replacement status ---")
    for i, d in enumerate(pass_d_details):
        o, n = str(d.get("old", ""))[:70], str(d.get("new", ""))[:50]
        lines.append(f"  {i+1}. [{d.get('status', '?')}] old: {o} → new: {n}")
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass
    # Дополнительно дописываем в correction.log (общий лог постпроцесса для запуска)
    correction_path = os.path.join(output_dir, "correction.log")
    header = f"\n\n=== {base_name} / {lang_code} ===\n"
    try:
        with _CORRECTION_LOG_LOCK:
            with open(correction_path, "a", encoding="utf-8") as f:
                f.write(header + "\n".join(lines) + "\n")
    except Exception:
        pass


def _final_sanity_check(original_text: str, processed_text: str, lang_code: str, log_queue: queue.Queue) -> Tuple[str, List[str]]:
    """
    Финальная проверка после всех пассов.
    Возвращает (финальный_текст, список_предупреждений).
    """
    warnings: List[str] = []
    if not original_text or not processed_text:
        return processed_text or original_text, warnings
    ratio = len(processed_text) / len(original_text)
    if ratio < 0.85:
        warnings.append(f"Текст сократился на {(1 - ratio) * 100:.0f}%")
    if ratio > 1.20:
        warnings.append(f"Текст увеличился на {(ratio - 1) * 100:.0f}%")
    return processed_text, warnings


def run_postprocess_pipeline(
    full_text: str,
    lang_name: str,
    lang_display: str,
    host_name: str,
    channel_name: str,
    api_key: str,
    log_queue: queue.Queue,
    output_dir: str = "",
    base_name: str = "",
    lang_code: str = "",
    main_character_name: str = "",
    is_generated_text: bool = False,  # True для генерации, False для рерайта
    rewrite_contract: bool = False,   # CONTRACT-C-2026-09-15: рерайт — свой призыв/прощание по контракту, чужое не копировать
) -> str:
    """
    Постпроцесс: Pass A+B параллельно → merge брендинга → Pass C (Nativeness) → Pass D (Language Purity).
    Если заданы output_dir, base_name, lang_code — пишет лог в {base_name}_{lang_code}_postprocess.log.
    """
    if STOP_FLAG.is_stopped():
        return full_text
    if not full_text or not full_text.strip():
        return full_text

    if full_text.strip().startswith("[ОШИБКА:"):
        return full_text

    log_msg(log_queue, f"🔎 {lang_display}: постпроцесс (Pass A+B → merge → Pass C → Pass D)...")

    pass_a_ok = False
    audit_json: Optional[Dict] = None
    pass_b_text = full_text
    pass_b_raw_json: Optional[str] = None
    pass_b_details: List[Dict] = []
    pass_c_raw_json: Optional[str] = None
    pass_c_details: List[Dict] = []
    pass_d_raw_json: Optional[str] = None
    pass_d_details: List[Dict] = []

    # BYOK-2026-07-30: thread-local user_id не наследуется дочерними потоками —
    # прокидываем явно, иначе постпроцесс партнёра ушёл бы на ключ админа/сервера.
    _postprocess_uid = get_current_user_id()

    def run_a():
        set_current_user_id(_postprocess_uid)
        try:
            return _postprocess_pass_a_branding_audit(
                full_text, lang_name, host_name, channel_name, api_key, log_queue
            )
        finally:
            clear_current_user_id()

    def run_b():
        set_current_user_id(_postprocess_uid)
        try:
            return _postprocess_pass_b_ai_declutter(
                full_text, lang_name, api_key, log_queue, lang_code
            )
        finally:
            clear_current_user_id()

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_a = ex.submit(run_a)
        fut_b = ex.submit(run_b)
        try:
            pass_a_ok, audit_json = fut_a.result(timeout=180)
            # Программная проверка intro: если модель сказала has_intro, но host_name нет в первой четверти — отменяем
            if pass_a_ok and audit_json and audit_json.get("has_intro") and (host_name or "").strip():
                first_quarter = full_text[: max(1, len(full_text) // 4)]
                if (host_name or "").lower() not in first_quarter.lower():
                    audit_json["has_intro"] = False
                    issues = audit_json.get("issues") or []
                    issues.append(f"OVERRIDDEN: host '{host_name}' not found in first 25%")
                    audit_json["issues"] = issues
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            log_msg(log_queue, f"   ⚠️ Pass A failed: {err_msg}")
        try:
            res = fut_b.result(timeout=180)
            pass_b_ok, pass_b_text, pass_b_raw_json, pass_b_details = res[0], res[1], res[2], res[3]
            if not pass_b_ok:
                pass_b_text = full_text
        except Exception as e:
            pass_b_text = full_text
            err_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            log_msg(log_queue, f"   ⚠️ Pass B failed: {err_msg}")

    pass_b_text = _length_guard(full_text, pass_b_text, "Pass B", log_queue)
    len_after_b = len(pass_b_text)
    ratio_b = len_after_b / len(full_text) if full_text else 1.0
    log_msg(log_queue, f"   Pass B: {len(full_text)} → {len_after_b} chars ({ratio_b:.0%})")

    merged = pass_b_text
    merge_actions: List[str] = []
    if pass_a_ok and audit_json:
        merged = merge_branding(pass_b_text, audit_json, host_name or "", channel_name or "", lang_name, main_character_name="", log_queue=log_queue, skip_name_replacement=is_generated_text, rewrite_contract=rewrite_contract)
        for old_name in audit_json.get("old_names_found") or []:
            if old_name and str(old_name).strip():
                merge_actions.append(f"name: {old_name!r} → {host_name!r}")
        for old_ch in audit_json.get("old_channels_found") or []:
            if old_ch and str(old_ch).strip():
                merge_actions.append(f"channel: {old_ch!r} → {channel_name!r}")
        if not audit_json.get("has_intro") and (host_name or channel_name) and (not rewrite_contract or (host_name and channel_name)):
            merge_actions.append("inserted intro")
        if not audit_json.get("has_outro") and (host_name or channel_name) and not rewrite_contract:
            merge_actions.append("inserted outro")
        log_msg(log_queue, f"   ✅ {lang_display}: брендинг применён (merge)")

    if STOP_FLAG.is_stopped():
        _write_postprocess_log(output_dir, base_name, lang_code, pass_a_ok, audit_json,
                               pass_b_raw_json, pass_b_details, len(full_text), len(pass_b_text),
                               merge_actions, len(merged), None, [], len(merged))
        return merged

    pass_c_ok, final, pass_c_raw_json, pass_c_details = _postprocess_pass_c_nativeness(
        merged, lang_name, api_key, log_queue, lang_code
    )
    final = _length_guard(merged, final, "Pass C", log_queue)
    len_after_c = len(final)
    ratio_c = len_after_c / len(merged) if merged else 1.0
    log_msg(log_queue, f"   Pass C: {len(merged)} → {len_after_c} chars ({ratio_c:.0%})")
    if pass_c_ok:
        log_msg(log_queue, f"   ✅ {lang_display}: Pass C (nativeness) готов")
    else:
        final = merged

    # Pass D: Language Purity (после C, перед return)
    if STOP_FLAG.is_stopped():
        _write_postprocess_log(output_dir, base_name, lang_code, pass_a_ok, audit_json,
                               pass_b_raw_json, pass_b_details, len(full_text), len(pass_b_text),
                               merge_actions, len(merged), pass_c_raw_json, pass_c_details, len(final),
                               None, [], len(final))
        return final
    text_before_d = final
    pass_d_ok, pass_d_text, pass_d_raw_json, pass_d_details = _postprocess_pass_d_language_purity(
        final, lang_name, api_key, log_queue, lang_code
    )
    if pass_d_ok:
        final = _length_guard(text_before_d, pass_d_text, "Pass D", log_queue)
    else:
        final = text_before_d
    len_after_d = len(final)
    log_msg(log_queue, f"   Pass D: {len_after_c} → {len_after_d} chars")
    if pass_d_ok and pass_d_details:
        log_msg(log_queue, f"   ✅ {lang_display}: Pass D (language purity) готов")
    elif pass_d_ok:
        log_msg(log_queue, f"   Pass D: текст чистый")

    # Финальная проверка после всех пассов
    final, sanity_warnings = _final_sanity_check(full_text, final, lang_code, log_queue)
    if sanity_warnings:
        for w in sanity_warnings:
            log_msg(log_queue, f"   ⚠️ Финальная проверка: {w}")

    _write_postprocess_log(output_dir, base_name, lang_code, pass_a_ok, audit_json,
                           pass_b_raw_json, pass_b_details, len(full_text), len(pass_b_text),
                           merge_actions, len(merged), pass_c_raw_json, pass_c_details, len_after_c,
                           pass_d_raw_json, pass_d_details, len_after_d)
    return final


def _dedup_sentences(text: str) -> str:
    """Remove exact duplicate sentences, keeping first occurrence."""
    import hashlib
    # Split into paragraphs first (preserve structure)
    paragraphs = text.split('\n\n')
    result_paragraphs = []
    seen_hashes = set()
    
    for para in paragraphs:
        # Split paragraph into sentences
        parts = re.split(r'(?<=[.!?])\s+', para)
        kept = []
        for sentence in parts:
            stripped = sentence.strip()
            if len(stripped) < 40:
                # Short sentences (transitions, etc) - always keep
                kept.append(sentence)
                continue
            h = hashlib.md5(stripped.lower().encode()).hexdigest()
            if h not in seen_hashes:
                seen_hashes.add(h)
                kept.append(sentence)
            # else: skip duplicate
        
        rebuilt = ' '.join(kept)
        if rebuilt.strip():
            result_paragraphs.append(rebuilt)
    
    return '\n\n'.join(result_paragraphs)


_REWRITE_CAPTION_TAG_RE = re.compile(
    r'\[(?:music|applause|laughter|silence|inaudible|background music|музыка|аплодисменты|смех|'
    r'muzyka|oklaski|śmiech|hudba|potlesk|muzică|aplauze)\]', re.I)


def sanitize_final_text(text: str, lang_name: str) -> str:
    if not text:
        return text
    cleaned = _REWRITE_CAPTION_TAG_RE.sub('', text)
    cleaned = re.sub(r'(?m)^[ \t]*>>+[ \t]*', '', cleaned)
    cleaned = re.sub(r'[♪♫♬♩]+', '', cleaned)
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
    cleaned = re.sub(r'[ \t]+([,.!?;:])', r'\1', cleaned)
    # Only presentation artefacts. Names, words, units, dates, quoted repetition,
    # unknown bracketed annotations and cross-script proper names are content.
    return cleaned.strip()


# ============== ПРОМПТЫ ==============

def rewrite_target_language_rule(lang_name: str) -> str:
    """Language purity concerns words, not a blanket ban on the target alphabet."""
    target = str(lang_name or "").strip()
    lower = target.lower()
    if lower == "russian":
        return "Write natural Russian in its native Cyrillic script; translate foreign source-language words."
    if lower in ("ukrainian", "belarusian", "bulgarian", "macedonian"):
        return (f"Write natural {target} in its native Cyrillic script. Cyrillic is REQUIRED, not forbidden. "
                f"Replace Russian/source-language vocabulary and calques with genuine {target} words; "
                "preserve correctly written proper names and faithful historical quotations.")
    return (f"Write only natural {target} in its native script. Replace Russian/source-language "
            f"vocabulary and transliteration with genuine {target} words; do not copy stray Cyrillic "
            "from the source. Correctly written proper names and faithful quotations are not language mixing.")


def preclean_rewrite_source(text: str) -> Tuple[str, int]:
    """Remove caption markup and the measured unquoted ASR startup loop only.

    No general repetition filter: repeated testimony, heat in a diary and unknown
    bracketed annotations must survive. Newlines are retained for 1:1 mode.
    """
    original = text or ""
    tail = re.compile(r"\[music\][ \t]*(?:Heat[.!][ \t]*){2,}\[music\][ \t]*\Z", re.I)
    without_tail, tails = tail.subn("", original, count=1)
    cleaned, removed = _REWRITE_CAPTION_TAG_RE.subn("", without_tail)
    loop = re.compile(
        r"\A[ \t]*(?:(?:Heat|Жара)[.!][ \t]*){2,}"
        r"(?:Begin|Let's begin|Here we go|Начинаем|Начинаем работу)[.!]?[ \t]*"
        r"(?:(?:Heat|Жара)[.!][ \t]*){2,}", re.IGNORECASE,
    )
    cleaned, loops = loop.subn("", cleaned, count=1)
    return cleaned, removed + loops + tails


def get_system_prompt(lang_name: str, lang_display: str = "", analysis: Optional[Dict] = None, address_style: str = "", custom_prompt: str = "") -> str:
    """
    Возвращает системный промт для языка.
    address_style: "vy" = обращение на вы (formal), "ty" = на ты (informal), "" = не задано.
    custom_prompt: произвольные инструкции пользователя для адаптации контента (например, "Замени всех казахов на чехов").
    ВАЖНО: system_prompt должен быть одинаковым для всех чанков одного языка для кэширования!
    """
    # Определяем, нужно ли писать числа словами (для всех языков)
    use_words_for_numbers = True  # Всегда словами для всех языков
    
    # Определяем, нужна ли культурная адаптация для славянских/восточноевропейских языков
    needs_cultural_adaptation = lang_name.lower() in [
        "czech", "polish", "romanian", "ukrainian", "belarusian",
        "slovak", "bulgarian", "serbian", "croatian", "slovenian",
        "hungarian", "estonian", "latvian", "lithuanian",
        "portuguese",  # 2026-06-04: locale-lock pt-BR (см. lang_rules ниже)
    ]
    
    cultural_note = ""
    if needs_cultural_adaptation:
        lang_rules = {
            "polish": """
POLISH RULES:
- Officials: "Pan/Pani" (formal)
- Soviet: KGB→KGB, ZSRR, kosmonauta
- Dates: "14 marca 1978 roku"
- Complex sentences NATURAL - don't simplify!""",
            
            "czech": """
CZECH RULES:
- Officials: "vy" (formal)
- Soviet: KGB→KGB, SSSR, kosmonaut
- Dates: "14. března 1978"
- Long sentences NATURAL!""",

            "serbian": """
SERBIAN RULES — CRITICAL:
- WRITE IN LATIN SCRIPT (gajica) ONLY. NEVER use Cyrillic.
- ✅ CORRECT: "Kosmički aparat veličine manjeg automobila, sletio je na kometu."
- ❌ WRONG: "Космички апарат величине мањег аутомобила, слетио је на комету."
- Use Serbian Latin diacritics: č, ć, š, ž, đ, dž, lj, nj
- Soviet: KGB→KGB, SSSR, kosmonaut
- Officials: "vi" (formal)
- Dates: "14. marta 1978. godine"
- If the source text had Cyrillic — transliterate to Latin naturally (not letter-by-letter).""",

            "bulgarian": """
BULGARIAN RULES:
- Write in Cyrillic (native Bulgarian script).
- Officials: "Вие" (formal)
- Soviet: КГБ, СССР, космонавт
- Dates: "14 март 1978 г."
- Avoid Russian loanwords that differ from Bulgarian (проверяй окончания).""",

            "macedonian": """
MACEDONIAN RULES:
- Write in Macedonian Cyrillic (native script).
- Officials: "Вие" (formal)
- Soviet: КГБ, СССР, космонаут
- Dates: "14 март 1978 година"
- Macedonian-specific letters: ќ, ѓ, љ, њ, џ, ѕ, ј.""",
            
            "romanian": """
ROMANIAN RULES - CRITICAL FOR NATIVITY:

ARTICLES (DEFINITE/INDEFINITE):
- ALWAYS use definite articles when referring to specific nouns: "omul" (the man), "femeia" (the woman), "casa" (the house)
- Use indefinite articles for general/non-specific: "un om" (a man), "o femeie" (a woman)
- NEVER omit articles where they are grammatically required
- Examples: "etnograf" (ethnographer) → "etnograful" (the ethnographer), NOT "etnografic"

PRONOUNS AND REFLEXIVES - CRITICAL:
- "Să-l urmez" (follow him) NOT "Să mă urmez" (follow myself) - this is a catastrophic error!
- Check pronoun cases: accusative (îl, o, îi, le), dative (îi, le, li), reflexive (mă, te, se, ne, vă)
- "Să-l urmez" = "to follow him" (accusative)
- "Să mă urmez" = "to follow myself" (reflexive) - WRONG if meaning "follow him"
- Always verify pronoun reference matches the intended object

CASES AND DECLENSIONS:
- Accusative: "Văd omul" (I see the man), "Văd casa" (I see the house)
- Dative: "Îi dau cartea" (I give him the book), "Le spun" (I tell them)
- Genitive: "casa omului" (the man's house), "cartea femeii" (the woman's book)
- Vocative: "Ionule!" (John!), "doamnelor!" (ladies!)

PROFESSIONS AND NOUNS:
- "etnograf" (ethnographer) NOT "etnografic" (ethnographic - adjective)
- Professions are nouns, not adjectives: "arhitect", "medic", "profesor"
- Use correct gender: "etnograf" (m), "etnografă" (f)

NATURAL SPEECH PATTERNS:
- Use LIVING, MODERN Romanian, not old newspaper style
- Avoid literal translations from Russian - adapt sentence structure naturally
- Use natural Romanian word order, not Russian word order
- Prefer active voice: "El a spus" (He said) over passive constructions
- Use natural Romanian expressions and idioms when appropriate

SENTENCE STRUCTURE:
- Romanian allows flexible word order, but prefer natural patterns
- Don't force Russian sentence structure onto Romanian
- Use subordinating conjunctions naturally: "când" (when), "pentru că" (because), "deși" (although)
- Complex sentences are natural - don't oversimplify

VERB FORMS:
- Use correct verb conjugations for person, number, tense
- Imperative: "urmează-mă" (follow me), "urmează-l" (follow him)
- Subjunctive: "să urmez" (to follow), "să-l urmez" (to follow him)
- Past tense: "am urmărit" (I followed), "a urmărit" (he followed)

SOVIET/HISTORICAL TERMS:
- "tovarășul" (comrade), KGB, URSS
- Use Romanian historical terminology naturally

AVOID TRANSLATIONESE:
- Don't sound like a translator - sound like a native Romanian storyteller
- Don't use "dry" or "newspaper" style - use vivid, engaging narrative
- Make it feel like the story was originally written in Romanian
- Use natural Romanian rhythm and flow, not mechanical translation

WORD VERIFICATION - CRITICAL:
- Use ONLY real Romanian dictionary words. NEVER invent words.
- If unsure about a word, use a simpler synonym.
- Common errors to avoid:
  * "mătăsură" → CORRECT: "mătase" (silk)
  * "ultraginală" → CORRECT: "ultimă" (final/last)
  * "ultraginalimă" → CORRECT: "ultimă"
- "plain sight" = "la vedere" or "sub ochii tuturor", NEVER leave in English
- "Beaux-Arts" can stay as-is (it's the style name), but explain in Romanian if needed
""",
            "turkish": """
TURKISH RULES - CRITICAL:
- Formal: "siz" form throughout
- ALL YEARS MUST INCLUDE "bin" (thousand):
  ✅ CORRECT: "bin sekiz yüz kırk beş" (1845)
  ❌ WRONG: "sekiz yüz kırk beş" (845)
  Every year from 1000-1999 MUST start with "bin".
  "bin yedi yüz" (1700s), "bin sekiz yüz" (1800s), "bin dokuz yüz" (1900s).
  If you write a year without "bin", the TTS voice will say "845" instead of "1845".
  This is a CRITICAL FAILURE.
- Outro: "Sizinle birlikte Emre Kaya'ydı" (NOT "birlikte oydu Emre Kaya")
- Soviet: KGB→KGB, SSCB, kozmonot
- Complex sentences natural for Turkish
- NO repetition in opening sentence (don't repeat subject twice)
""",

            "portuguese": """
PORTUGUESE RULES — BRAZILIAN PORTUGUESE ONLY (pt-BR), CONSISTENTLY across the ENTIRE text:
- Lexis: celular NOT telemóvel, ônibus NOT autocarro, tela NOT ecrã, trem NOT comboio,
  gênio NOT génio, fato NOT facto, time NOT equipa, café da manhã NOT pequeno-almoço,
  banheiro NOT casa de banho, geladeira NOT frigorífico, suco NOT sumo, xícara NOT chávena,
  você NOT tu (verb forms tertia persona singular accordingly).
- Syntax: gerund "estar fazendo / está acontecendo" NOT European "estar a fazer / está a acontecer".
- Orthography: Brazilian spelling — gênio, ideia, neurônios, ônibus, econômico, fenômeno
  (NOT génio, ideia, neurónios, autocarro, económico, fenómeno).
- Common words: legal/bacana (cool), cara (guy), criança (child).
- NEVER mix European Portuguese (pt-PT) forms. If unsure, choose the Brazilian form.
- If the source text contains pt-PT artifacts (telemóvel, autocarro etc.), CONVERT them to pt-BR.
""",
        }
        
        # Используем lang_display если есть, иначе lang_name для консистентности
        display_name = lang_display if lang_display else lang_name
        cultural_note = "\n" + lang_rules.get(lang_name.lower(), 
            f"""
CULTURAL ADAPTATION FOR {display_name.upper()}:
- Adapt historical context to {display_name} perspective
- Use natural {display_name} phrasing, not word-by-word translation
- Consider local historical sensitivities and context
- Write as a native {display_name} speaker would, not as a translator""")
        # Запрет русских отчеств для всех нерусских языков
        if lang_name.lower() not in ("russian",):
            cultural_note += f"""

⚠️ PATRONYMICS BAN: Russian patronymics (отчества ending in -евич/-овна like "Сергеевич", "Алексеевна") 
do NOT exist in {display_name}. Use ONLY first name + surname (e.g. "Marek Wolański", NOT "Marek Siergiejewicz Wolański").
If the source text has patronymics — DROP them completely, keep only first name and surname."""
    
    
    numbers_rule = ("Render EVERY number, year, decade, ordinal, range and quantity as fully spelled-out words IN " + lang_name + ". The output must contain ZERO digits and ZERO English number-words. This includes decade phrases: 'nineteen-nineties', 'the 1990s', 'nineteen-twenties' MUST become the " + lang_name + " words for that decade — never left in English. If you are about to write 'nineteen', 'twenty', '-nineties', a digit, or '19th' — stop and spell it in " + lang_name + ".") if use_words_for_numbers else "Write numbers as DIGITS (e.g., 1943, 17:42, not words)"
    
    # Если есть анализ - включаем его в промпт
    context_section = ""
    if analysis:
        context_section = f"""
═══════════════════════════════════════════════════════════
TEXT CONTEXT (understand this BEFORE adapting):
═══════════════════════════════════════════════════════════

GENRE: {analysis.get('genre', 'unknown')}
STYLE: {analysis.get('writing_style', 'unknown')}
PACING: {analysis.get('pacing', 'unknown')}

CRITICAL ELEMENTS (you MUST preserve ALL):
{chr(10).join(f"  • {elem}" for elem in analysis.get('critical_elements', ['All content'])[:10])}

CONTENT BREAKDOWN:
  Dialogue: {analysis.get('content_breakdown', {}).get('dialogue_percentage', 25)}%
  Description: {analysis.get('content_breakdown', {}).get('description_percentage', 50)}%
  Action: {analysis.get('content_breakdown', {}).get('action_percentage', 15)}%
  Reflection: {analysis.get('content_breakdown', {}).get('reflection_percentage', 10)}%

PRESERVATION PRIORITY:
{chr(10).join(f"  {i+1}. {rule}" for i, rule in enumerate(analysis.get('preservation_priority', ['All content'])[:5]))}

YOUTUBE OPTIMIZATION:
  {analysis.get('youtube_optimization', {}).get('target_retention', 'Detailed content for engagement')}
  Style: {analysis.get('youtube_optimization', {}).get('style_notes', 'Natural pacing')}

═══════════════════════════════════════════════════════════
"""

    # Базовый промпт (расширенный для гарантии ≥4096 символов = ≥1024 токенов для кэширования)
    # ВАЖНО: Этот промпт должен быть одинаковым для всех чанков одного языка!
    base_prompt = f"""{context_section}You are an expert {lang_name} adaptation specialist for YouTube content.

═══════════════════════════════════════════════════════════
MISSION: PERFECT ADAPTATION (80+ quality score)
═══════════════════════════════════════════════════════════

Based on the TEXT CONTEXT above, you understand:
- What genre and style to maintain
- What elements are critical
- What pacing to preserve
- How to optimize for YouTube audience

═══════════════════════════════════════════════════════════
RETENTION — INTEREST HOOKS THROUGHOUT (critical for watch-time & re-watches)
═══════════════════════════════════════════════════════════
Keep the viewer hooked across the ENTIRE text, not only at the opening.
Place an interest trigger at every STRUCTURAL SEAM — seams are visible in the text,
so you can actually check them:
  - immediately BEFORE every section marker of the source;
  - at every switch to a new topic, a new person, a new place or a new time period;
  - immediately AFTER a rhetorical question or a climax, before the next block opens.
NEVER open a block with a bare date or a bare new name right after a question or a
climax — put one connecting sentence there first.
That works out to roughly every 3000-5000 characters, but the SEAM is what you look
for, not the character count. Each trigger makes the listener want to keep going
(and come back to re-watch):
  - open a curiosity loop / pose an intriguing question;
  - tease what is coming ("but the strangest part is still ahead...");
  - a gentle mini-cliffhanger or surprising turn taken FROM the source;
  - a forward-reference that pays off later in the text.
HOW (without breaking the no-invention rule): build these hooks ONLY by
FRAMING and emphasis of material that is ALREADY in the source — rephrase
for intrigue, foreground the most surprising existing fact, vary sentence
rhythm to build a small wave of curiosity. Do NOT invent new facts, events,
characters or details. MATCH THE SOURCE'S OWN ENERGY. If the source is calm and slow
(sleep, relaxation, meditation, bedtime content), keep the calm, sleep-friendly tone
and make the hooks soft pulls of curiosity. If the source is a punchy list, an
investigation, a battle or a revelation, keep THAT energy: short punch sentences and
a direct, confident statement are the CORRECT register there, not a flaw.
Banned in EVERY register is invented drama: fake stakes, fake urgency, exclamation
chains, ALL-CAPS, "you won't believe" phrasing, and any promise the source does not
deliver. A hook may be energetic; it may never promise something that is not there.

ABSOLUTE REQUIREMENTS:

1. LENGTH GUIDANCE
   Per-paragraph output length: 0.7×–1.3× of corresponding input paragraph (in characters).
   Do NOT pad to hit a length target. Do NOT shorten by omitting content.
   If source paragraph is short — output stays short. If source is long — output stays long.
   NEVER add new FACTS: no new actions, events, sensory details, atmosphere,
   characters, names, numbers, dates or claims that are not present in the source.
   Faithful transfer of meaning is the goal, not enrichment.

   ONE BOUNDED EXCEPTION — CONNECTIVE SENTENCES ARE ALLOWED AND EXPECTED:
   At a seam between two blocks you MAY write ONE short bridging sentence of your own
   (max 25 words) whose only job is to point forward to material that ALREADY follows
   in this same source text. Such a sentence is NOT enrichment and is NOT a new fact.
   - It must contain NO new fact, name, number, date, place, event or description.
   - It must promise only what the source actually delivers later in this text.
   - It may withhold a name or an outcome to create curiosity, but may not invent one.
   - At most one such sentence per seam, and never two in a row.
   THE TEST, apply it to every sentence you add: could a listener learn a single new
   fact from this sentence? If NO — it is a legal connective sentence, keep it.
   If YES — it is an invention, delete it.
   ALLOWED: "But what happened to one other patient is stranger still."
   FORBIDDEN: "The case of Karl Liebmann from the nineteen-thirties is stranger still."
   (it names a person and a decade — those are facts, and the source introduces them
   a moment later by itself).

2. CONTENT TYPE PRESERVATION
   Based on content_breakdown above:

   For DIALOGUE (if input has dialogue):
   - Every exchange must be present
   - Preserve speaking style and personality
   - Do not paraphrase away meaning, but do not embellish either

   For DESCRIPTION (if input has descriptions):
   - Preserve EXISTING details
   - Do NOT invent new visual/sensory details, do NOT enrich atmosphere

   For REFLECTION (philosophical/internal monologue):
   - Preserve the thoughts that ARE in the source
   - Do not extend or deepen them beyond what is written

   For ACTION:
   - All steps/events preserved
   - Pacing maintained

3. CRITICAL ELEMENTS CHECKLIST
   Before outputting, verify EACH element from preservation_priority is present
   and that NO new elements were invented.

4. COUNTING VERIFICATION (do in your head)
   Count before submitting:
   - Input paragraphs: ___ → Output paragraphs: ___ (must be EQUAL when 1:1 mode is on)
   - Input sentences per paragraph: roughly preserved
   If counts don't match — re-check; do NOT pad with new content.

5. GENRE-SPECIFIC RULES

   If PSYCHOLOGICAL_HORROR:
   - Preserve existing slow-burn beats; do not add new ones
   - Preserve existing atmospheric descriptions verbatim in spirit; do not invent

   If THRILLER:
   - Preserve existing pacing beats; do not add new ones

   If SCIFI_DOCUMENTARY:
   - Technical details = preserve precisely, do not simplify, do not invent
   - Procedures = step-by-step preservation, no extra steps

═══════════════════════════════════════════════════════════
TECHNICAL REQUIREMENTS ({lang_name}):
═══════════════════════════════════════════════════════════

CRITICAL RULES:

1. TARGET LANGUAGE: {rewrite_target_language_rule(lang_name)}

2. NATIVE LANGUAGE ONLY:
   - Use natural {lang_name} grammar, word order, expressions
   - Sound like native {lang_name} writing, not translation
   - Use modern, living {lang_name}, not bureaucratic style
   - ADDRESS (you): {"Use formal 'you' (vy/вы/pan/Sie/vous/etc.) consistently throughout — no informal 'ty/ты'." if address_style == "vy" else "Use informal 'you' (ty/ты/du/tu/etc.) consistently throughout — no formal 'vy/вы'." if address_style == "ty" else "Use the natural address form for the genre and audience."}

3. TERMINOLOGY - Use EXISTING {lang_name} words, NEVER invent:
   CRITICAL EXAMPLES OF WRONG (DO NOT REPEAT THESE ERRORS):
   * Czech: "zájezdář" (tourist) for "вахтовик" → CORRECT: "turnusový pracovník"
   * Czech: "stážník" (intern) for "часовой" → CORRECT: "strážný" or "hlídka"
   * Czech: "poslechový znak" for "позывной" → CORRECT: "volací znak"
   * Czech: "ztlustlá" (fattened) for "осунувшееся" (emaciated) → use correct antonym
   * Czech: "pravda musí být řekena" → CORRECT: "řečena"
   * Polish: "pozywny" (nutritious) for "позывной" → CORRECT: "kryptonim" or "znak wywoławczy"
   * Polish: "cedrat" (citron) for "Cedr" (cedar) → CORRECT: "Cedr"
   * Polish: "przeleścia" (doesn't exist) for "пролески" → CORRECT: "przecinka" or "prześwit"
   * Polish: "Na wida" (calque) → CORRECT: "Z wyglądu"
   * Romanian: "vachtor" (doesn't exist) for "вахтовик" → CORRECT: "muncitor sezonier"
   * Romanian: "Polonerul" (doesn't exist) for "полковник" → CORRECT: "Colonelul"
   * Romanian: "un masă, o scaun" (wrong articles) → CORRECT: "o masă, un scaun"
   * Romanian: "un lămâi storc" (wrong form) → CORRECT: "o lămâie stoarsă"
   * Romanian: "rost la rești" (nonsense) → CORRECT: "cu gratii"

4. VERB MEANINGS - Use CORRECT meanings:
   * Romanian: "topi" = "to melt" (metal), NOT "to heat stove" → use "făcuse focul" or "încălzise"
   * Polish: "gaszić silnik" sounds wrong → use "zgasł" or "silnik zgasł"

5. CRITICAL TECHNICAL PROTECTION:
   - DO NOT TRANSLATE or distort tank/vehicle names, designations and project names (Jagdtiger, IS-2, IS-7, T-28, Maus, Tiger, Panther, T-34, KV, M103, T-95, etc.). Keep them EXACTLY as in the source. Never invent variants like "Ioškin", "Jaguar-tigr", "Ёшкин-2".
   - Military ranks MUST use proper {lang_name} equivalents (e.g. plukovník / pułkownik / colonel), NOT literal mixes.
   - CALIBERS & NUMBERS ARE SACRED: never change any numeric values (years, dates, calibers like 88 mm, 128 mm, 105 mm, weights, speeds). If you change a number, it is a CRITICAL FAILURE.
   - NO LANGUAGE MIXING: NEVER insert words from other target languages (no Czech in Romanian, no Polish in Czech, etc.).
   - NO HALLUCINATED TECHNICAL TERMS: if you don't know the precise term, use a safe generic native word ("door" = ușa/dveře/drzwi) instead of inventing nonsense ("shoe", "botek", etc.).

6. GRAMMAR - Follow {lang_name} grammar STRICTLY:
   - Articles: correct gender (Romanian: "o masă" not "un masă")
   - Cases: ALL nouns MUST decline correctly (Polish: "majorze" not "majorz", "boksie" not "bokse")
   - Pronouns: correct forms (avoid "Ja jestem" - drop pronouns when natural)
   - Verb forms: all correct
   - Word order: natural for {lang_name}, NOT Russian word order
   - Surnames: MUST decline in {lang_name} (Polish/ Czech surnames decline, not like English)

6. ZERO FOREIGN WORDS - NO LANGUAGE MIXING:
   - {rewrite_target_language_rule(lang_name)}
   - Translate stray words from OTHER languages into {lang_name}; Spanish is correct when the target is Spanish, English is correct when the target is English.
   - Use native {lang_name} vocabulary and grammar; proper names and faithful quotations retain their meaning.
   - Do not confuse the target language's own words with foreign-language contamination.

7. PROPER NOUNS - Preserve EXACTLY:
   - Space stations: "Salut 6" (NOT "Szałyt 6", "Szałt 6", "Salit 6")
   - Call signs: "Kedr" or "Cedr" (NOT "Kiełdr", "Cedru")
   - Historical names: preserve original spelling/transliteration
   - Technical terms: use standard {lang_name} terminology

8. NATIVE SYNTAX - NO CALQUES:
   - NO literal Russian word order
   - NO "I have fear" → use native expressions ("bałem się" not "miałem strach")
   - NO "podnosić się" for rocket launch → use "wystartował" or "poleciał"
   - Use natural {lang_name} idioms and expressions
   - Drop pronouns when natural (avoid "Ja jestem" if "Jestem" is natural)

9. NUMERALS AND DATES — ALWAYS IN WORDS:
   - ABSOLUTE RULE: render EVERY number, year, decade, ordinal, range and quantity as fully spelled-out words IN {lang_name}. The output must contain ZERO digits and ZERO English number-words. This is not a list of cases — it is a hard requirement: nothing numeric may remain as a digit or as an English word.
   - DECADES: phrases like "nineteen-nineties", "the 1990s", "nineteen-twenties" MUST become the {lang_name} words for that decade — never left in English. If you are about to write "nineteen", "twenty", "-nineties", a digit, or "19th" — stop and spell it in {lang_name}.
   - ALL numbers, years, dates, and centuries MUST be written in WORDS in {lang_name}, not digits or Roman numerals.
   - Years: e.g. 1943 → "thousand nine hundred forty-three" (or equivalent in {lang_name}); correct declension.
   - Centuries: e.g. "19th century" → "nineteenth century" in {lang_name} (e.g. Polish: "dziewiętnasty wiek", Czech: "devatenácté století", Romanian: "secolul al nouăsprezecelea"). NEVER output "19th", "XIX", "18th" — only words.
   - Dates: natural {lang_name} format, in words where appropriate for TTS.
   - Long numbers: in words, correct cases. Test: can a native speaker read it smoothly without stumbling?

9b. TRANSLATION/REWRITE ERROR PATTERNS (avoid machine-translation and neural-rewrite artifacts):
   - IDIOM CALQUES: Do not translate English (or source) idioms literally. Use equivalent idioms in {lang_name} (e.g. "advertised as such" → native phrasing for "presented as", not word-for-word).
   - FALSE FRIENDS IN TERMS: Use historically correct terms. Examples: "Cabinet cards" = cabinet-format photos / "cabinet portraits", NOT "cards for cabinet/filing". "Tintype" = ferrotypes/tintypes (photo on metal), NOT "color tint" or "color type images".
   - UNITS (CRITICAL): Convert imperial to metric by RECALCULATING the number, not just replacing the unit. Formula: 1 foot = 0.3048 m (or 1 m = 3.28 ft) → multiply feet by 0.305 to get meters. 1 inch = 2.54 cm. Example: "8 feet" or "8 ft" → 8 × 0.305 ≈ 2.44 m → write "about two and a half meters" (or "two point four meters"); "10 feet" → about 3 m. NEVER keep the same number and only change "feet" to "meters" (that is wrong). NEVER translate "feet" as body parts ("legs"). Output in metric, values in words where appropriate.
   - DATES IN WORDS: "1800s" = nineteenth century — write the century IN WORDS in {lang_name} (e.g. "nineteenth century" / "devatenácté století" / "secolul al nouăsprezecelea"), NOT "19th", "XIX" or "18th". Check context: 1800s = 19th century (1800–1899), NOT 18th. Mid-to-late 1800s = mid-to-late nineteenth century, in words.
   - RHYTHM: Avoid robotic, list-like syntax ("X matched, Y matched, Z fell correctly"). Rephrase for natural flow and voice-over (e.g. "Grain, light, shadows — everything was authentic").
   - REGISTER: Keep one consistent tone (e.g. engaging, slightly informal for video). No mixing formal/colloquial or switching "you" forms within the same text.

10. FOREIGN BRANDING AND NONCONTENT:
   - Remove the donor's greetings, channel/host self-introduction, like/subscribe/comment requests, sponsor lines and farewells
   - Add only what the branding instructions of this chunk allow: at most one brief introduction, one call-to-action for OUR channel where those instructions ask for it, and the sign-off in the last part; never copy the donor's calls-to-action, never add extra ones and never repeat the introduction
   - Historical first-person testimony, letters, diaries and quotations are content: keep them attributed to their real speakers
   - The present-day donor's personal experience is NOT our host's biography. Never claim our host bought letters, visited a battlefield, interviewed someone or owns an object merely because the donor did. Preserve useful evidence neutrally ("the surviving letters show...") or attribute it to the original researcher; delete only irrelevant creator asides. Historical witnesses' first-person quotations must remain first-person and attributed.

11. Numbers and dates: {numbers_rule}. ALL dates and centuries IN WORDS in {lang_name} (e.g. nineteenth century, not "19th" or "XIX").
12. Length: preserve all content. Target 95-115% of the input in characters. The extra room exists ONLY for the connective sentences and the source's own section markers allowed above — never for padding, never for restating what was already said, and never a reason to drop content. Where a PARAGRAPH STRUCTURE 1:1 / HARD CONSTRAINT block appears in the task, that block wins.
13. Remove markers: [музыка], [hudba], etc.
14. Output: [adapted text in {lang_name}]
15. SENTENCE START DIVERSITY:
   - No word should start more than 8% of sentences.
   - After writing, scan the first word of each sentence. If any word appears 10+ times, rephrase some sentences to start differently.
   - Techniques: invert clause order, start with time/place adverb, start with subordinate clause, start with participle.
   - Example: instead of 5x "Ale..." → vary with "Jenže...", "Přesto...", "Na druhou stranu...", "Naopak...", "Zato..."

16. VARIETY RULE (filler words):
   - Never use the same filler word more than 3 times per 1000 words.
   - Words to vary: "один/одна/одно", "jeden/jedna/jedno", "какой-то", "pewien", "某".
   - Use synonyms instead: "некий", "определённый", "тот самый", "pewien", "jakiś", "nějaký".
   - Scan your output: if you see "один" 5+ times in 1000 words, replace some with synonyms.
   - SCOPE — EMPTY FILLER WORDS ONLY. This rule NEVER applies to the key term that names the subject of the video, to a word the source repeats on purpose as a refrain, or to any technical term. Pick ONE short native word for such a key term and use THAT SAME word every single time, however often it occurs. Splitting a key term into two synonyms breaks the through-line and is a FAILURE.
   - Keep the source's parallel constructions parallel: if the source ends three clauses with the same word, your three clauses end with the same word too.

17. DO NOT SOFTEN WHAT THE SOURCE ASSERTS:
   - If the source states something as a fact, state it as a fact. Do NOT add a hedge
     that is not in the source ("perhaps", "it may be", "it seems", "it appears",
     "possibly", "a possible path to", "cannot be regarded as certain", "remains
     uncertain", "researchers suggest").
   - If the SOURCE itself hedges, keep its hedge exactly, in the same place, once —
     never strengthen a claim the source qualifies, and never double its hedge.
   - This matters most in the sentence that closes a block: a hedge there kills the payoff.
   - Keep the source's forward-looking constructions as announcements ("a poet WOULD
     write", "an architect WOULD arrive", "they WERE TO discover"). Flattening them
     into a plain past tense turns a promise into a summary and loses meaning.

18. KEEP THE SOURCE'S GRAMMATICAL PERSON:
   - If the source addresses the listener directly ("your liver", "your heart", "think
     about that for a moment", "picture them"), keep the direct address, the imperative
     and the possessive in {lang_name} — not "the liver", not "one might think", not
     "let us imagine".
   - If the source speaks impersonally, stay impersonal. Never switch person mid-text.
   - Formal vs informal "you" is decided by the ADDRESS rule above; this rule is about
     PERSON (second person vs impersonal), not about formality.

FINAL REMINDER: Your success metric is COMPLETENESS + QUALITY, not brevity.
YouTube viewers want FULL detailed stories, not compressed summaries."""
    
    # 2026-06-04 #12 fix: вклеиваем cultural_note в финальный prompt.
    # Раньше cultural_note собирался (Polish/Romanian/Portuguese rules), но НИКОГДА
    # не попадал в return base_prompt → все language-specific правила игнорировались.
    if cultural_note:
        base_prompt += f"""

═══════════════════════════════════════════════════════════
LANGUAGE-SPECIFIC RULES ({lang_name.upper()})
═══════════════════════════════════════════════════════════
{cultural_note}
═══════════════════════════════════════════════════════════
"""

    # Custom user instructions for content adaptation
    if custom_prompt:
        base_prompt += f"""

=== CUSTOM INSTRUCTIONS (HIGHEST PRIORITY) ===
The user has provided these specific instructions for this language version.
Apply them to EVERY chunk. These override any conflicting rules above.

{custom_prompt}

Apply these instructions naturally and consistently throughout the entire text.
=== END CUSTOM INSTRUCTIONS ===
"""

    return base_prompt

    # Проверяем что промпт достаточно длинный для кэширования
    # Для Haiku 4.5: минимум 2048 токенов = ~8200 символов, но лучше ~8800 для надёжности
    # Для Sonnet: минимум 1024 токена = ~4096 символов
    # Добавляем padding если нужно для гарантии кэширования
    current_length = len(base_prompt)
    min_length_for_haiku = 8800  # ~2200 токенов с запасом (минимум 2048)
    
    if current_length < min_length_for_haiku:
        # Добавляем дополнительный контент для достижения минимума
        additional_content = f"""

EXAMPLES OF CRITICAL ERRORS TO AVOID:

1. GRAMMATICAL HALLUCINATIONS:
Polish:
- WRONG: "majorz Krawcow" → CORRECT: "majorze Krawcowie" (correct declension)
- WRONG: "w izolowanym bokse" → CORRECT: "w izolowanym boksie" (correct case ending)
- WRONG: "Ja jestem" (robotic) → CORRECT: "Jestem" (drop pronoun when natural)

2. LANGUAGE MIXING (CRITICAL - ZERO TOLERANCE):
- WRONG: "jego личное дело spalono" (Russian in Polish text) → CORRECT: "jego osobiste akta spalono"
- WRONG: "Wasze tiempo nie jest" (Spanish in Polish) → CORRECT: "Wasz czas nie jest"
- WRONG: Any Cyrillic or foreign words → CORRECT: Pure {lang_name} only

3. PROPER NOUNS ERRORS:
- WRONG: "Szałyt 6", "Szałt 6", "Salit 6" → CORRECT: "Salut 6" (exact spelling)
- WRONG: "Kiełdr", "Cedru" → CORRECT: "Kedr" or "Cedr" (preserve original)
- Historical/technical terms: preserve EXACT spelling, don't invent

4. CALQUES AND WOODEN SYNTAX:
Polish:
- WRONG: "Oficjalnie Wołgin nie podnosił się w kosmos" → CORRECT: "Oficjalnie Wołgin nie wystartował w kosmos" (native verb)
- WRONG: "miałem strach" (literal "I had fear") → CORRECT: "bałem się" or "czułem lęk" (native expression)
- WRONG: Russian word order → CORRECT: Natural {lang_name} word order

5. UNITS — ADAPT THE VALUE, NOT JUST THE WORD:
- WRONG: "8 meters tall" when original was "8 feet" (you kept the number 8). 8 feet = 8 × 0.305 ≈ 2.44 m.
- CORRECT: "8 feet" → "about two and a half meters" or "two and a half metres" (value recalculated: 1 ft = 0.3048 m, 1 m = 3.28 ft).
- WRONG: "10 feet" → "10 meters". CORRECT: "10 feet" → "about three meters" (~3.05 m).

6. NUMERAL ERRORS:
Polish:
- WRONG: "tysiąc dziewięćset siedemdziesiątego siódmego roku" (wrong cases) → CORRECT: "tysiąc dziewięćset siedemdziesiątego siódmego roku" (verify all cases)
- Test: Can native speaker read date smoothly without stumbling?

Czech errors:
- "zájezdář" (tourist) for "вахтовик" → use "turnusový pracovník"
- "stážník" (intern) for "часовой" → use "strážný" or "hlídka"
- "poslechový znak" for "позывной" → use "volací znak"
- "ztlustlá" (fattened) for "осунувшееся" → use correct antonym

Romanian errors:
- "vachtor" (doesn't exist) for "вахтовик" → use "muncitor sezonier"
- "Polonerul" (doesn't exist) for "полковник" → use "Colonelul"
- "un masă, o scaun" (wrong articles) → use "o masă, un scaun"
- "topi" (to melt) for "топить печь" → use "făcuse focul" or "încălzise"

FINAL CHECKLIST BEFORE OUTPUT:
☐ NO foreign words (Russian, Spanish, English) - scan entire text
☐ Proper nouns spelled correctly (Salut 6, Kedr/Cedr)
☐ NO robotic phrases ("Ja jestem" → "Jestem")
☐ Surnames decline correctly (Polish/Czech requirement)
☐ NO calques - use native expressions
☐ Word order is natural for {lang_name}, not Russian
☐ Numerals and dates: all in WORDS in target language; correct cases
☐ DATES/CENTURIES: In words only (e.g. "nineteenth century"), not "19th"/"XIX"; 1800s = nineteenth century, correctly translated
☐ TECHNICAL: Feet/inches converted using 1 ft = 0.305 m (1 m = 3.28 ft), 1 in = 2.54 cm — number must change (e.g. 8 ft → ~2.5 m), not just the unit; no "feet" as body parts
☐ TERMINOLOGY: Cabinet cards → cabinet-format photos; Tintype → ferrotypes/tintypes (not "color tint")
☐ AUDIO: Read aloud mentally — no stumbling; natural rhythm for voice-over
☐ CULTURAL: No misplaced local names/toponyms for wrong country
☐ Test: Would native speaker read this smoothly? Full nativeness of translation.

REMEMBER:
- Use ONLY existing {lang_name} vocabulary
- Verify verb meanings match the action
- Check grammar: articles, cases, pronouns, verb forms
- Write as native {lang_name} speaker, not translator
- Preserve all details, maintain natural flow"""
        base_prompt += additional_content
    
    return base_prompt


def get_rewrite_prompt(lang_name: str, text: str, chunk_index: int, total_chunks: int,
                      lang_display: str = "", channel_name: str = "", host_name: str = "",
                      analysis: Optional[Dict] = None, prev_context: str = "",
                      preserve_paragraphs: bool = False, expected_paragraphs: int = 0,
                      strict_retry: bool = False) -> str:
    """Возвращает промт для рерайта одного чанка.

    ВАЖНО: просим модель вернуть ТОЛЬКО готовый текст чанка в КВАДРАТНЫХ СКОБКАХ,
    без каких‑либо логов, заголовков, чек-листов и метрик.

    preserve_paragraphs/expected_paragraphs: если True и >0, добавляется жёсткое требование
    выдать ровно столько же абзацев (для покадровой синхронизации 1:1).
    strict_retry: усиленная формулировка для повторной попытки после провала валидации.
    """
    
    # Подсчёт метрик
    input_length = len(text)
    input_sentences = text.count('.') + text.count('!') + text.count('?')
    input_paragraphs = len([p for p in text.split('\n\n') if p.strip()])
    
    # Определить роль чанка в общей структуре
    if total_chunks == 1:
        chunk_role = "COMPLETE TEXT - entire story in one chunk"
    elif chunk_index == 0:
        chunk_role = "OPENING - establish setting, introduce characters, create hook"
    elif chunk_index == total_chunks - 1:
        chunk_role = "ENDING - resolve story, maintain depth, satisfy viewer"
    else:
        chunk_role = f"MIDDLE PART {chunk_index+1}/{total_chunks} - develop story, maintain engagement"
    
    # Определить тип контента в чанке (простая эвристика)
    has_dialogue = text.count('"') > 3 or text.count('—') > 3
    has_description = len(text.split('\n\n')) > 3
    
    content_type_notes = []
    if has_dialogue:
        content_type_notes.append("Contains DIALOGUE - preserve every exchange completely")
    if has_description:
        content_type_notes.append("Contains DESCRIPTIONS - preserve existing details, do NOT add new ones or enrich")
    
    # Простые инструкции брендирования только для нужных чанков
    branding_instructions = ""
    # CONTRACT-2026-09-15 (rewrite noncontent/branding): пустое имя остаётся пустым —
    # раньше сюда подставлялся выдуманный «Anatolij Eugenjanovič Kazancev».
    actual_host_name = (host_name or "").strip()
    actual_channel_name = (channel_name or "").strip()

    # STRICT NAMING block — добавляется во все ветки чтобы убить галлюцинации имён.
    _naming_lines = []
    if actual_host_name:
        _naming_lines.append(
            f'- Host name MUST be EXACTLY: "{actual_host_name}" (character-for-character, including diacritics). '
            f'Do NOT translate it, do NOT replace it with any other name.')
    else:
        _naming_lines.append(
            '- NO host name is given: do NOT introduce, invent or imply any narrator name.')
    if actual_channel_name:
        _naming_lines.append(
            f'- Channel name MUST be EXACTLY: "{actual_channel_name}" (character-for-character, including special chars). '
            f'Do NOT translate it, do NOT replace it with any other channel.')
    else:
        _naming_lines.append(
            '- NO channel name is given: do NOT introduce, invent or imply any channel name.')
    _strict_naming = ("\n\nSTRICT NAMING — NEVER INVENT, NEVER TRANSLATE, NEVER SUBSTITUTE:\n"
                      + "\n".join(_naming_lines)
                      + "\n- Any host/channel name that differs from these rules is a FAILURE.")

    # Что удаляется и что остаётся — одинаково для всех частей и всех режимов имён.
    _noncontent_rules = """

NONCONTENT — REMOVE, NEVER REPLACE:
- Delete the donor's greeting, the donor's channel/host self-introduction, every request to like / subscribe / comment / share / ring the bell, sponsor and promo-code lines, merch and donation asks, production notes, the creator's present-day personal asides (his editing, coffee, holidays, opinions about his channel) and the farewell / promise of the next episode.
- Never turn the present-day donor's purchases, travel, interviews, collection or discoveries into OUR host's personal experience. Retain their useful historical evidence in a neutral form ("the letters show...") or attribute it to the original researcher; do not invent an owner or witness. This does NOT apply to attributed historical diaries, letters, testimony or dialogue: preserve those first-person voices.
- BUT the donor's RETENTION devices are content, not advertising: a tease of what is coming, a promise to return to something later, a direct question to the viewer, a short recap before a new block. KEEP the pull, in the same place, rewritten without the donor's name, channel and links — and WITHOUT any request. Only the request itself is deleted; the pull that carried it stays. This never creates a second call-to-action: asking the viewer for anything is allowed ONLY where the branding rules of THIS part explicitly ask for it.
- Never copy, translate or paraphrase the donor's calls-to-action or farewell. OUR OWN call-to-action and sign-off exist ONLY where the branding rules of THIS part explicitly ask for them below (exact names given there). Where this part's rules ask for none, a text with NO call-to-action and NO sign-off is CORRECT.
- Historical testimony STAYS: a witness's "I / we", diary and letter quotations, dialogue between historical people and the witness's own reflections are story content. Keep them in the witness's voice or clearly attributed to the witness. Never turn them into the narrator's own experience and never delete them as "noncontent".
- Never rename a historical person. Names of soldiers, officers, doctors, civilians and the author of the source stay as in the source (transliterated into {lang_name} spelling if needed)."""

    _who = " and ".join(
        [f'the host "{actual_host_name}"'] * bool(actual_host_name)
        + [f'the channel "{actual_channel_name}"'] * bool(actual_channel_name))
    _intro_once = f"""

INTRODUCTION — EXACTLY ONCE, AFTER THE HOOK:
- Start with the hook — NO branding inside the hook.
- After the hook, insert ONE brief, natural introduction of 1-2 sentences that presents {_who}. Use the exact strings above; nothing else is named.
- The host speaks in FIRST PERSON (the native equivalent of "I am NAME" / "With you is NAME"), never "NAME will tell you" as a third-person announcement. Keep it brief and connected to the story.
- PLACE IT BY MEANING, NOT BY PERCENTAGE: put it at the FIRST CLEAN SEAM after the hook — the end of a scene, the end of a self-contained thought, or the point just before a new section begins. It MUST still fall inside the FIRST QUARTER of this part.
- NEVER place it between a set-up and its punchline, between a question and its answer, between a fact and the line that lands it, or anywhere inside a scene, an anecdote or a quotation. If no clean seam has arrived yet, continue the story until one does.
- FOLD IT INTO THE PROMISE: name {_who} while pointing at what this video is about, instead of stopping the story for an announcement. FORBIDDEN dead formulas: "My name is X and you are listening to channel Y.", "Today we will look at...", "We continue to follow...".
- Do NOT repeat the introduction anywhere else. The host or channel may appear again ONLY inside the call-to-action / sign-off that the rules of THIS part explicitly ask for below — nowhere else.""" if _who else """

NO BRANDING: no host name, no channel name, no narrator identity anywhere in the text. Do not invent one."""

    if actual_channel_name and lang_name.strip().lower() == "greek":
        _strict_naming += (f'\n- Greek channel title is an exact QUOTED name: «{actual_channel_name}». '
                           'Use a natural frame such as στο κανάλι «TITLE»; do not put an extra Η/η '
                           'immediately before a title that already starts with Η. Never duplicate its article.')

    # CONTRACT-C-2026-09-15: свой призыв (лайк+подписка на НАШ канал) — ровно один, в середине;
    # своё прощание с именами + одна просьба — ровно один раз, в самом конце. Только с заданными именами.
    _mid_cta = f"""

CALL-TO-ACTION — EXACTLY ONCE, IN THE MIDDLE OF THIS PART:
- Around the middle of this part, after a natural pause in the story, add ONE short call-to-action of 1-2 sentences in {lang_name} asking the viewer to like and subscribe to "{actual_channel_name}" (exact string) — ONLY like and subscribe, no requests to comment, share or ring the bell. Example to adapt: "Friends, a small request: support {actual_channel_name} with a like and subscribe — it helps the channel grow." Then continue the story. No other call-to-action anywhere in this part.""" if actual_channel_name else ""
    _signoff_example = " ".join(
        ([f"With you was {actual_host_name}."] if actual_host_name else [])
        + ([f"See you in the next episode of {actual_channel_name}."] if actual_channel_name else ["See you in the next episode."])
        + ["Leave a like and subscribe."])
    _signoff = f"""

SIGN-OFF — EXACTLY ONCE, AT THE VERY END:
- Keep the source's own closing/reflective sentences INTACT and attributed to the witness/author. Do NOT make {_who} the grammatical subject of the witness's reflection.
- THEN append the sign-off as NEW, SEPARATE final sentences in {lang_name}: name {_who} (exact strings) and ask ONCE, briefly, to like and subscribe — ONLY like and subscribe: no requests to comment, share, ring the bell or write where viewers are from. Example to adapt: "{_signoff_example}"
- Nothing after the sign-off. No second farewell and no other call-to-action.""" if _who else """

NO SIGN-OFF: end with the source's own ending, in the witness's voice — no farewell, no call-to-action, no narrator identity."""
    _cta_part = total_chunks // 2   # центральная часть (как в build_chunk_prompt); при двух частях это последняя — призыв там не ставим

    if total_chunks == 1:
        # Одна часть: представление после хука, один призыв в середине, прощание в самом конце.
        branding_instructions = (_noncontent_rules.format(lang_name=lang_name) + _intro_once + _mid_cta + _signoff
                                 + _strict_naming)

    elif chunk_index == 0:
        # Первая часть: хук без брендинга, одно представление после хука; призыва и прощания здесь нет.
        branding_instructions = (_noncontent_rules.format(lang_name=lang_name) + _intro_once
                                 + "\n- Later parts of this text will NOT introduce anyone again."
                                 + "\n- NO call-to-action and NO sign-off in this part."
                                 + _strict_naming)
    
    elif chunk_index == total_chunks - 1:
        # Последняя часть: без повторного представления; прощание + одна просьба в самом конце (если имена заданы).
        branding_instructions = (_noncontent_rules.format(lang_name=lang_name) + """

LAST PART:
- The host and the channel were already introduced in the first part. Do NOT introduce them again and do NOT mention them inside the story."""
                                 + _signoff + _strict_naming)

    elif chunk_index == _cta_part and actual_channel_name:
        # Центральная часть: один призыв; без представления и прощания.
        branding_instructions = (_noncontent_rules.format(lang_name=lang_name) + """

MIDDLE PART — CALL-TO-ACTION ONLY:
- Do NOT add any introduction, host name or sign-off in this part. The story continues, plus the single call-to-action below."""
                                 + _mid_cta + _strict_naming)

    else:
        # Остальные средние части: ничего не добавляется — ни представления, ни призыва, ни прощания.
        branding_instructions = (_noncontent_rules.format(lang_name=lang_name) + """

MIDDLE PART — NOTHING ADDED:
- Do NOT add any introduction, host name, channel name, call-to-action or sign-off in this part. Only the story continues."""
                                 + _strict_naming)
    
    # Всегда используем слова для чисел (пример для напоминания модели)
    if lang_name.lower() == "russian":
        numbers_ex = "1943→тысяча девятьсот сорок три"
    elif lang_name.lower() == "polish":
        numbers_ex = "1943→tysiąc dziewięćset czterdzieści trzy"
    elif lang_name.lower() == "czech":
        numbers_ex = "1943→tisíc devět set čtyřicet tři"
    elif lang_name.lower() == "romanian":
        numbers_ex = "1943→o mie nouă sute patruzeci și trei"
    else:
        numbers_ex = f"1943→write as words in {lang_name}"
    
    # Краткая текстовая подсказка по роли чанка (без служебных заголовков в ответе!)
    role_note = f"This part is the {chunk_role} of the overall story."

    content_note = ""
    if content_type_notes:
        content_note = "Content hints for this chunk:\n" + "\n".join(f"- {note}" for note in content_type_notes)

    # Блок жёстких требований к структуре абзацев (для покадровой синхронизации)
    paragraph_block = ""
    if preserve_paragraphs and expected_paragraphs > 0:
        prefix = "STRUCTURE FAILURE — RETRY. " if strict_retry else ""
        paragraph_block = f"""

═══════════════════════════════════════════════════════════
{prefix}HARD CONSTRAINT — paragraph length:
═══════════════════════════════════════════════════════════
- Output paragraphs must match input count exactly. Length 0.7×–2.0× per paragraph.
- Translation expansion 1.0×–1.4× is normal for Russian/German/Turkish (longer words, diacritics).
- Output <0.8× of input = under-compression — restore content; do NOT over-summarize.
- Output >2.0× of input = bloat — compress; do NOT enrich.
- NEVER add new FACTS: no new actions, events, sensory details, atmosphere, characters, names, numbers or dates that are not present in the source.
- The ONE bounded exception from ABSOLUTE REQUIREMENTS #1 still applies here: a short connective sentence that only points forward to material already present in the source, and carries no new fact, is allowed — but it must live INSIDE an existing paragraph and must never add a paragraph.
- NEVER expand a short action like "X tips water" into a multi-sentence reflection.

═══════════════════════════════════════════════════════════
{prefix}PARAGRAPH STRUCTURE — NON-NEGOTIABLE (1:1 ALIGNMENT REQUIRED)
═══════════════════════════════════════════════════════════
- The input below has EXACTLY {expected_paragraphs} paragraphs separated by blank lines.
- Your output MUST have EXACTLY {expected_paragraphs} paragraphs in the SAME order.
- Mapping is strictly 1:1: input paragraph #N → output paragraph #N.
- DO NOT merge two input paragraphs into one output paragraph.
- DO NOT split one input paragraph into two output paragraphs.
- DO NOT reorder, omit, or insert extra paragraphs.
- An empty input paragraph → an empty output paragraph at the same position.

⚠️ PARAGRAPH SEPARATOR — CRITICAL FORMAT (most common failure mode):
- Between every two paragraphs put EXACTLY ONE COMPLETELY EMPTY LINE.
- That means the bytes between paragraphs are: <newline><newline> — a blank line.
- Single newline inside a paragraph keeps it as the SAME paragraph.
- A blank line (two consecutive newlines) starts a NEW paragraph.
- DO NOT join all paragraphs into one big block with single newlines — that produces 1 paragraph and FAILS the check.
- Visual layout your output must look like:
    Paragraph 1 text.
    <blank line>
    Paragraph 2 text.
    <blank line>
    Paragraph 3 text.
- This is required for frame-by-frame video alignment. Failure breaks the downstream pipeline.
═══════════════════════════════════════════════════════════"""

    # Очень жёстко задаём формат вывода: только текст в одних скобках
    prompt = f"""You are adapting CHUNK {chunk_index+1} of {total_chunks} from the source's language into natural, high‑quality {lang_name}.

{role_note}
{content_note}
{paragraph_block}

Branding instructions (apply only inside the story text, not as separate headers):
{branding_instructions}

TASK:
- Rewrite the following source text into fluent, native‑sounding {lang_name}, preserving ALL facts, technical details, dates, names and narrative flow.
- RETENTION IN THIS PART (applies to EVERY part, the last one included): put a bridge sentence or an open question before every switch to a new topic, scene or section; keep every section marker the source has, in its own position and shape; never let the final third of this part run without a forward pull. A bridge adds NO new fact, and every promise must be paid off by the text that follows it.
- DO NOT WEAKEN THE SOURCE: keep its assertions unhedged, keep its concrete images concrete (never replace a vivid detail of the source with a generic summary), keep its address to the viewer in the same grammatical person, and keep its punchlines short.
- Follow the NONCONTENT and branding rules above: remove the donor's greetings, self-introductions, calls-to-action, sponsor lines and farewells; add ONLY what the branding rules of this part explicitly ask for (introduction / call-to-action / sign-off, each at most once, exact names), nothing else of that kind.
- Remove any subtitle markers like [музыка], [music], [muzika], [muzică], [muzyka], etc.
- Render EVERY number, year, decade, ordinal, range and quantity as fully spelled-out words IN {lang_name} (e.g. {numbers_ex}). The output must contain ZERO digits and ZERO English number-words. Decade phrases like "nineteen-nineties", "the 1990s", "nineteen-twenties" MUST become the {lang_name} words for that decade — never left in English. If you are about to write "nineteen", "twenty", "-nineties", a digit, or "19th" — stop and spell it in {lang_name}.
- {rewrite_target_language_rule(lang_name)}
- Avoid literal calques, invented words and wooden syntax as described in the system instructions.
- UNITS: Convert feet/inches by RECALCULATING: 1 foot = 0.3048 m (1 m = 3.28 ft), 1 inch = 2.54 cm. E.g. 8 ft → ~2.5 m, 10 ft → ~3 m. Write the new value in metric (in words where natural); never keep the same number with "meters"; never use "feet" as body parts.
- DATES AND CENTURIES: Write ALL dates and centuries IN WORDS in {lang_name} (e.g. "nineteenth century", not "19th" or "XIX"). "1800s" = nineteenth century — translate correctly and in words.
- TERMINOLOGY: Use correct terms (e.g. Cabinet cards → cabinet-format photos; Tintype → ferrotypes/tintypes), not literal mistranslations.

OUTPUT FORMAT (STRICT, FOR TTS):
- Return ONLY the final adapted story text for this chunk, ready to be read by a voice over.
- NO service labels of YOUR OWN: NO \"analysis\", NO \"verification\", NO checklists, NO bullet lists, NO metrics, NO debug/log lines, and no part/chunk numbering of your own invention.
- THE SOURCE'S OWN CHAPTER LINES ARE CONTENT, NOT FORMATTING — KEEP EVERY ONE OF THEM. A chapter line is a sentence of the source that names a section, e.g. \"Chapter 5, McLean's famous patients.\" Reproduce it in {lang_name} at the SAME position in the text, and spell any number inside it in {lang_name} WORDS, never digits.
- A chapter line NEVER becomes a separate paragraph of its own: keep it as the first sentence of the paragraph it opens. This rule is about chapter lines only. How many paragraphs you write is decided elsewhere: when a PARAGRAPH STRUCTURE 1:1 block is present, keep the paragraph count equal to it; otherwise split the story into normal readable paragraphs separated by a blank line, even if the source arrived as one unbroken block.
- NEVER invent a chapter line, section title or segment label that has no counterpart in the source. If the source has no chapter lines, your output has none.
- Do NOT include things like: "KONTROLA PŘED VÝSTUPEM", "VERIFICATION CHECKLIST", "INPUT METRICS", "ANALÝZA", \"CHECKLIST\", \"PART 3/9\", etc.
- Do NOT include any Markdown syntax such as '#', '##', '---', '***', lists, checkboxes, emojis or decorative separators.
- Enclose the ENTIRE adapted story text in ONE SINGLE pair of square brackets, like:
  [тук идёт полный переписанный текст на {lang_name}]
- Do NOT use any other '[' or ']' characters inside the text. Only the outermost brackets are allowed.
- Do NOT add any text before or after the brackets.

{"CONTEXT FROM PREVIOUS CHUNK (use for continuity of names, style, and references — do NOT repeat this content):" + chr(10) + prev_context + chr(10) + chr(10) if prev_context else ""}SOURCE TEXT (Russian, keep all information, but fully rewrite in natural {lang_name}):
{text}

Now output ONLY the adapted {lang_name} text in ONE pair of square brackets, with no extra explanations.

CRITICAL: Do NOT describe what you will do. Do NOT write any plan or preface like "I need to" or "I will". Output ONLY the rewritten text in [square brackets], in full, starting immediately with the first word of the rewrite."""

    return prompt


# ============== API ==============

def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


_OPENAI_MAX_CONCURRENT_CALLS = _bounded_env_int(
    "REWRITE_OPENAI_MAX_CONCURRENT_CALLS", 32, 1, 128
)
_OPENAI_CALL_SEMAPHORE = threading.BoundedSemaphore(_OPENAI_MAX_CONCURRENT_CALLS)


@redact_provider_output
def call_openai_api(
    messages: List[Dict],
    model: str,
    api_key: str,
    log_queue: queue.Queue,
    task_info: str = "",
    max_tokens: int = 8192,
    service_tier: Optional[str] = None,
    use_caching: Optional[bool] = None,
) -> Tuple[bool, str]:
    """BYOK-first OpenAI call behind a process-wide concurrency cap.

    ``api_key`` remains in the signature for legacy internal callers but is
    deliberately ignored: the credential is resolved from the tenant's own
    settings, falling back to the server pool.
    """
    if not get_effective_openai_key():
        return (
            False,
            "OpenAI key is not configured: neither your own key nor a server key",
        )
    with _OPENAI_CALL_SEMAPHORE:
        return _call_openai_api_impl(
            messages=messages,
            model=model,
            api_key="",
            log_queue=log_queue,
            task_info=task_info,
            max_tokens=max_tokens,
            service_tier=service_tier,
            use_caching=use_caching,
        )


@redact_provider_output
def _call_openai_api_impl(
    messages: List[Dict],
    model: str,
    api_key: str,
    log_queue: queue.Queue,
    task_info: str = "",
    max_tokens: int = 8192,
    service_tier: Optional[str] = None,
    use_caching: Optional[bool] = None,
) -> Tuple[bool, str]:
    """Security boundary: ignore caller key, lease a resolved credential.

    BYOK-2026-07-30: the lease comes from the tenant's own pool when the user
    configured a key, otherwise from the shared server pool. A request/job value
    can still never seed a pool.
    """
    try:
        ensure_provider_execution_allowed("openai")
        lease = get_effective_credential_pool("openai").acquire(timeout=300.0)
    except (CredentialPoolExhausted, RuntimeError) as exc:
        return False, str(exc)
    with lease as server_key:
        return _call_openai_api_with_server_key(
            messages=messages,
            model=model,
            server_key=server_key,
            log_queue=log_queue,
            task_info=task_info,
            max_tokens=max_tokens,
            service_tier=service_tier,
            use_caching=use_caching,
        )


@redact_provider_output
def _call_openai_api_with_server_key(
    messages: List[Dict],
    model: str,
    server_key: str,
    log_queue: queue.Queue,
    task_info: str = "",
    max_tokens: int = 8192,
    service_tier: Optional[str] = None,
    use_caching: Optional[bool] = None,
) -> Tuple[bool, str]:
    """
    Вызов OpenAI API с ограниченными retry на rate limit.
    max_tokens: по умолчанию 8192 для длинных глав генерации и постпроцесса.
    service_tier: "default" (standard) или "flex" (50% дешевле, медленнее).
    use_caching: включить prompt cache retention 24h для длинных промптов (аналогично Anthropic).
    """
    if STOP_FLAG.is_stopped():
        return (False, "Остановлено пользователем")

    cfg = get_keys_config()
    tier = service_tier if service_tier is not None else cfg.get("gpt_service_tier", "default")
    caching = use_caching if use_caching is not None else cfg.get("gpt_use_cache", True)

    # Кэш неподдерживаемых возможностей — не повторять ошибку 100 раз
    caps = _load_model_capabilities().get(model, {})
    no_flex = caps.get("no_flex", False)
    no_cache = caps.get("no_cache", False)
    no_temperature = caps.get("no_temperature", False)

    # 21.09.2026: адрес и ключ чат-вызова выносятся в переменные. Повод — у счёта OpenAI
    # ноль (429 insufficient_quota), и весь текст встаёт, хотя рядом лежит оплаченный
    # провайдер с OpenAI-совместимым /v1/chat/completions. Форма запроса та же, имена
    # моделей он принимает и беспрефиксные (`gpt-4.1-mini` — проверено), неизвестные поля
    # молча игнорирует, а 240 с на чтение ниже хватает его медленному бэкенду.
    #
    # По умолчанию НИЧЕГО не меняется: пустые переменные — прежний OpenAI и прежний ключ.
    # Адрес задаётся ЦЕЛИКОМ, а не базой: склейка с /v1 — известный способ получить 404.
    # Ключ обязан ехать вместе с адресом: у чужого провайдера ключ OpenAI бессмыслен, и
    # отправить его туда — значит засветить секрет на стороне, которой он не предназначен.
    _chat_url = _alt_setting("REWRITE_GPT_CHAT_URL")
    _chat_key = _alt_setting("REWRITE_GPT_CHAT_KEY")
    # Адрес и ключ — ПАРА, и отсутствие любой половины опасно по-своему.
    # Адрес без ключа отправил бы ключ OpenAI чужому провайдеру. Ключ без адреса —
    # наоборот, отправил бы чужой ключ в OpenAI: тоже утечка секрета третьей стороне,
    # и внешне это выглядит как «Неверный API ключ», то есть как сломанный аккаунт.
    # Оба случая — отказ, а не тихий откат: молчание здесь стоит секрета.
    if bool(_chat_url) != bool(_chat_key):
        missing = "REWRITE_GPT_CHAT_KEY" if _chat_url else "REWRITE_GPT_CHAT_URL"
        return (False, f"задана только половина маршрута чат-вызова: не хватает {missing}. "
                       "Ключ и адрес обязаны меняться вместе — иначе секрет уедет не тому "
                       "провайдеру. Денег это не стоило, повтор не поможет.")
    if _chat_key:
        _register_server_secret_tree({"rewrite_chat_key": _chat_key})   # чтобы не попал в логи
    url = _chat_url or "https://api.openai.com/v1/chat/completions"
    _chat_key = _chat_key or server_key

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_chat_key}"
    }

    # gpt-5*, o*, o1*, o3*, o4* используют max_completion_tokens вместо max_tokens
    use_max_completion = any(x in model.lower() for x in ["gpt-5", "o3", "o4", "o1"])
    # gpt-5-mini: temperature только 1; prompt_cache_retention не поддерживается —
    # кэш работает автоматически (Cached input в прайсе), но явные параметры API отклоняет
    gpt5_mini_restrictions = "gpt-5-mini" in model.lower()
    data = {
        "model": model,
        "messages": messages,
    }
    if not gpt5_mini_restrictions and not no_temperature:
        data["temperature"] = 0.7
    if use_max_completion:
        data["max_completion_tokens"] = max_tokens
    else:
        data["max_tokens"] = max_tokens
    # Flex поддерживают не все модели (o3, o4-mini, gpt-5-mini и др.)
    if tier == "flex" and not no_flex:
        data["service_tier"] = "flex"
    if caching and not gpt5_mini_restrictions and not no_cache:
        data["prompt_cache_retention"] = "in_memory"
        system_content = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        if isinstance(system_content, str) and len(system_content) >= 1024:
            cache_key = hashlib.md5(system_content.encode()).hexdigest()[:16]
            data["prompt_cache_key"] = f"rewrite_{cache_key}"
    
    # urllib's default context verifies hostname and the system CA chain.
    # Never downgrade TLS verification for a billable server credential.
    import ssl
    ssl_context = ssl.create_default_context()
    
    attempt = 0
    max_network_retries = 10  # Сетевые ошибки - ограниченные retry
    rate_limit_attempt = 0
    max_rate_limit_retries = _bounded_env_int(
        "REWRITE_OPENAI_MAX_429_RETRIES", 6, 0, 20
    )
    # FIX-2026-07-30 (P1 №7): backoff считается по владельцу ключа, которым идёт
    # этот вызов. 429 от чужого (в т.ч. мёртвого) BYOK-ключа больше не тормозит
    # вызовы остальных тенантов. Резолвим один раз на вызов, не в цикле ретраев.
    limiter = rate_limiter_for("openai")

    while True:
        try:
            ensure_provider_execution_allowed("openai")
        except RuntimeError as exc:
            return (False, str(exc))
        if STOP_FLAG.is_stopped():
            return (False, "Остановлено пользователем")
        
        try:
            # Добавляем базовую задержку между запросами.
            # Потолок обязателен: get_delay() растёт при каждом 429 и снижается ТОЛЬКО при
            # успехе, поэтому на мёртвом ключе залипает на max_delay=300 — по пять минут
            # сна перед каждым вызовом, и задача стоит часами.
            delay = min(limiter.get_delay(),
                        float(os.environ.get("REWRITE_OPENAI_PRECALL_DELAY_MAX", "30") or 30))
            if delay > 0.5:
                time.sleep(delay)
            
            # Debug: только summary (полный content перегружает CPU и GUI)
            debug_summary = {"url": url, "model": model, "messages": len(data.get("messages", [])), "content_lens": [len(str(m.get("content", ""))) for m in data.get("messages", [])]}
            debug_log(log_queue, "OpenAI REQUEST:", json.dumps(debug_summary, ensure_ascii=False))
            
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode('utf-8'),
                headers=headers,
                method='POST'
            )
            
            # 21.09.2026: пока провайдер думает, лог молчит — и «долго» неотличимо от
            # «умерло». Владелец увидел одну строку «Analyzing text structure...» и
            # через шесть минут спросил, не сломалось ли. Не сломалось: связь с
            # провайдером была установлена всё это время, просто альтернативный канал
            # работает поверх сохранённых веб-аккаунтов и на длинном тексте отвечает
            # минутами. Раз в полминуты говорим, что ждём и сколько уже.
            _beat_stop = threading.Event()

            def _beat(started=time.time()):
                while not _beat_stop.wait(30):
                    log_msg(log_queue, f"    ⏳ жду ответа провайдера… "
                                       f"{int(time.time() - started)} с"
                                       + (f" [{task_info}]" if task_info else ""))
            threading.Thread(target=_beat, daemon=True).start()
            try:
                with urllib.request.urlopen(req, timeout=240,
                                            context=ssl_context) as response:
                    _raw = response.read().decode('utf-8')
            finally:
                _beat_stop.set()
            result = json.loads(_raw)
            content = result['choices'][0]['message']['content']
            limiter.on_success()
            if log_queue and caching:
                usage = result.get("usage", {})
                pt_details = usage.get("prompt_tokens_details", {})
                cached = pt_details.get("cached_tokens", 0)
                if cached > 0:
                    log_msg(log_queue, f"  💾 GPT cache hit: {cached} токенов")
            return (True, content)
                
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode('utf-8')
                error_json = json.loads(error_body)
                error_msg = error_json.get('error', {}).get('message', str(e))
            except:
                error_msg = str(e)
            debug_log(log_queue, "OpenAI HTTPError:", f"code={e.code} body={error_body} error_msg={error_msg}")
            
            if e.code == 429:
                # НАША ПРАВКА 20.09.2026 (седьмая в форке, не потерять при ребейзе).
                # OpenAI отвечает 429 на ДВЕ разные вещи: «слишком часто» (ждать
                # помогает) и «кончились деньги/квота» (ждать бесполезно НИКОГДА).
                # Код считал лимитом любой 429 и уходил в откат с ростом паузы до
                # нескольких минут: полчаса ожидания, десять повторов и отказ, а в
                # логе — только слово «Rate limit». Настоящий текст провайдера уходил
                # в debug_log, который по умолчанию выключен. Владелец 20.09 смотрел
                # ровно на это и не мог понять, чего ждёт.
                if _quota_exhausted(error_body, error_msg):
                    log_msg(log_queue, "    ❌ На ключе кончилась квота или деньги — "
                                       "ждать бесполезно: " + (error_msg or "")[:160])
                    return False, ("Квота ключа исчерпана (429 insufficient_quota): "
                                   + (error_msg or "")[:200])
                rate_limit_attempt += 1
                if rate_limit_attempt > max_rate_limit_retries:
                    return (
                        False,
                        f"Rate limit after {max_rate_limit_retries} retries: "
                        + (error_msg or "")[:200],
                    )
                # Prefer provider Retry-After, fall back to adaptive backoff.
                wait_time = limiter.on_rate_limit()
                try:
                    retry_after = float((e.headers or {}).get("Retry-After", "0"))
                    if retry_after > 0:
                        wait_time = min(300.0, retry_after)
                except (TypeError, ValueError):
                    pass
                log_msg(log_queue, f"    ⏳ Rate limit{' ['+task_info+']' if task_info else ''}, "
                                   f"жду {wait_time:.0f}с — {(error_msg or 'провайдер просит подождать')[:120]}")

                # Ждём с проверкой стоп-флага
                waited = 0
                while waited < wait_time:
                    if STOP_FLAG.is_stopped():
                        return (False, "Остановлено пользователем")
                    time.sleep(min(1, wait_time - waited))
                    waited += 1
                continue  # Пробуем снова
                
            elif e.code == 401:
                return (False, "Неверный API ключ")
            elif e.code == 404:
                return (False, f"Модель '{model}' не найдена")
            elif e.code == 400:
                err_lower = error_msg.lower()
                if "temperature" in err_lower and ("unsupported" in err_lower or "only the default" in err_lower):
                    if "temperature" in data:
                        del data["temperature"]
                        _save_model_capability(model, "no_temperature", True)
                        log_msg(log_queue, f"    ⚠️ Модель {model} поддерживает только temperature=1, запомнил")
                        continue
                if "invalid service_tier" in err_lower or ("service_tier" in err_lower and "unsupported" in err_lower):
                    if "service_tier" in data:
                        del data["service_tier"]
                        _save_model_capability(model, "no_flex", True)
                        log_msg(log_queue, f"    ⚠️ Модель {model} не поддерживает Flex, запомнил (standard)")
                        continue
                if "max_tokens" in err_lower and ("max_completion" in err_lower or "not supported" in err_lower):
                    if "max_tokens" in data:
                        data["max_completion_tokens"] = data.pop("max_tokens")
                        log_msg(log_queue, f"    🔄 Использую max_completion_tokens для {model}")
                        continue
                if "prompt_cache" in err_lower and "not supported" in err_lower:
                    for key in ["prompt_cache_retention", "prompt_cache_key"]:
                        if key in data:
                            del data[key]
                    _save_model_capability(model, "no_cache", True)
                    log_msg(log_queue, f"    ⚠️ Модель {model} не поддерживает кэш, запомнил")
                    continue
            elif e.code >= 500:
                # Ошибка сервера - retry с лимитом
                attempt += 1
                if attempt >= max_network_retries:
                    return (False, f"Ошибка сервера после {max_network_retries} попыток")
                log_msg(log_queue, f"    ⚠️ Ошибка сервера ({e.code}), повтор {attempt}...")
                time.sleep(3 * attempt)
                continue
            else:
                return (False, f"HTTP {e.code}: {error_msg[:80]}")
                
        except urllib.error.URLError as e:
            # Сетевая ошибка - retry с экспоненциальной задержкой (3, 6, 9, ... до 30 сек)
            debug_log(log_queue, "OpenAI URLError:", str(e))
            attempt += 1
            if attempt >= max_network_retries:
                return (False, f"Сетевая ошибка после {max_network_retries} попыток: {e.reason}")
            delay = min(3 * attempt, 30)
            log_msg(log_queue, f"    ⚠️ Сетевая ошибка, повтор {attempt} (пауза {delay}с)...")
            time.sleep(delay)
            continue
            
        except Exception as e:
            import traceback
            debug_log(log_queue, "OpenAI Exception:", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            attempt += 1
            if attempt >= max_network_retries:
                return (False, f"Ошибка после {max_network_retries} попыток: {str(e)[:80]}")
            delay = min(3 * attempt, 30)
            time.sleep(delay)
            continue


@redact_provider_output
def _call_claude_api_once(
    messages: List[Dict],
    api_key: str,
    model: str,
    log_queue: queue.Queue,
    task_info: str = "",
    use_caching: bool = True
) -> Tuple[bool, str]:
    """Одиночный вызов Claude API (без ретраев). Возвращает __RETRY__:... при ошибках, пригодных для ретрая."""
    if not ANTHROPIC_AVAILABLE:
        return False, "Anthropic API недоступен: в Python процесса сервиса не установлены anthropic/httpx. Администратору: установите зависимости requirements-anthropic.txt и перезапустите сервис."
    client = None
    httpx_client = None
    try:
        ensure_provider_execution_allowed("claude")
        # Создаём кастомный HTTPX клиент с увеличенными таймаутами
        # Проблема: HTTPX по умолчанию имеет connect timeout 60 секунд, что вызывает "Client disconnected"
        # Решение: создаём HTTPX клиент с увеличенными таймаутами для всех операций
        # connect=300: время на установку соединения (5 минут)
        # read=300: время на чтение данных (5 минут) 
        # write=300: время на запись данных (5 минут)
        # pool=300: время на получение соединения из пула (5 минут)
        httpx_timeout = httpx.Timeout(
            connect=300.0,  # 5 минут на подключение (критично для создания кэша)
            read=300.0,     # 5 минут на чтение
            write=300.0,    # 5 минут на запись
            pool=300.0      # 5 минут на получение соединения
        )
        # Добавляем beta‑заголовок напрямую в HTTPX‑клиент, чтобы включить prompt caching
        httpx_client = httpx.Client(
            timeout=httpx_timeout,
            headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        
        # Передаём кастомный HTTPX клиент в Anthropic SDK
        # Это переопределяет дефолтные таймауты HTTPX (60 секунд)
        # ВАЖНО: не используем параметр beta в конструкторе (у тебя старая версия SDK)
        client = anthropic.Anthropic(api_key=api_key, http_client=httpx_client)
        
        # Конвертируем формат OpenAI → Claude
        system_content = None
        claude_messages = []
        
        for msg in messages:
            if msg["role"] == "system":
                content = msg["content"]
                
                # Если уже в формате Claude (список с cache_control)
                if isinstance(content, list):
                    system_content = content
                    log_msg(log_queue, f"  💾 Кэширование включено (формат Claude)")
                # Если строка - добавляем cache_control если достаточно длинная
                # Для Sonnet: минимум 1024 токена (~4096 символов)
                # Для Haiku 4.5: минимум 2048 токенов (~8192 символов)
                # Для Haiku 3.5: минимум 2048 токенов (~8192 символов)
                # Проверяем по символам с запасом
                elif isinstance(content, str):
                    # Приблизительная оценка: 1 токен ≈ 4 символа
                    estimated_tokens = len(content) // 4
                    # Определяем минимальный размер в зависимости от модели
                    if "haiku" in model.lower():
                        min_chars_for_cache = 8800  # ~2200 токенов (минимум для Haiku 4.5 - 2048, с запасом 2200)
                    else:
                        min_chars_for_cache = 4200  # ~1050 токенов (минимум для Sonnet - 1024 токена)
                    
                    if use_caching and len(content) >= min_chars_for_cache:
                        # Создаём с cache_control
                        # ВАЖНО: формат должен быть точно как в документации
                        # Используем 1h вместо 5m для более надёжного кэширования
                        system_content = [{
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral", "ttl": "1h"}
                        }]
                        log_msg(log_queue, f"  💾 Кэш включён: {len(content)} символов (~{estimated_tokens} токенов, TTL=1h)")
                    else:
                        # Слишком короткий для кэша
                        system_content = content
                        if use_caching:
                            log_msg(log_queue, f"  ⚠️  Кэш НЕ включён: {len(content)} символов < {min_chars_for_cache} минимума")
                else:
                    system_content = content
            else:
                claude_messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })
        
        # Если нет user message, добавляем пустой
        if not claude_messages:
            claude_messages.append({"role": "user", "content": "Please proceed."})
        
        # Создаём запрос (увеличен max_tokens для длинных глав генерации: 1500+ слов ≈ 6000 токенов)
        kwargs = {
            "model": model,
            "max_tokens": 8192,
            "messages": claude_messages
        }
        
        # Добавляем system (может быть строка или список с cache_control)
        if system_content:
            kwargs["system"] = system_content
            # Логируем формат system для отладки
            if isinstance(system_content, list):
                text_len = len(system_content[0].get('text', '')) if system_content and isinstance(system_content[0], dict) else 0
                has_cache = any('cache_control' in item for item in system_content if isinstance(item, dict))
                # Вычисляем хеш для проверки идентичности
                system_text = system_content[0].get('text', '') if system_content and isinstance(system_content[0], dict) else ''
                system_hash = hashlib.md5(system_text.encode()).hexdigest()[:8]
                log_msg(log_queue, f"  🔍 System: список, {text_len} символов, cache_control={'✅' if has_cache else '❌'}, hash={system_hash}")
            else:
                system_hash = hashlib.md5(system_content.encode()).hexdigest()[:8]
                log_msg(log_queue, f"  🔍 System: строка, {len(system_content)} символов (без кэша), hash={system_hash}")
        
        # Логируем параметры запроса для отладки
        if isinstance(system_content, list) and system_content:
            cache_control_info = system_content[0].get('cache_control', {})
            log_msg(log_queue, f"  🔍 Отправляю запрос с cache_control: {cache_control_info}")
        
        # Таймауты теперь управляются через HTTPX клиент (300 секунд для всех операций)
        # Не нужно передавать timeout в messages.create, так как он уже настроен в httpx_client
        debug_log(log_queue, "Claude REQUEST:", json.dumps({"model": model, "max_tokens": kwargs.get("max_tokens"), "messages_count": len(claude_messages), "system_len": len(str(system_content)) if system_content else 0}, ensure_ascii=False))
        response = client.messages.create(**kwargs)
        
        # Детальное логирование кэша
        usage = response.usage
        
        # Базовая статистика
        total_input = usage.input_tokens
        if log_queue:
            log_msg(log_queue, f"  📊 Input: {usage.input_tokens} tokens")
            log_msg(log_queue, f"  📊 Output: {usage.output_tokens} tokens")
        
        # Статистика кэша
        cache_created = 0
        cache_read = 0
        
        try:
            # Используем model_dump() если доступен (pydantic модель)
            if hasattr(usage, 'model_dump'):
                usage_dict = usage.model_dump()
                cache_created = usage_dict.get('cache_creation_input_tokens', 0) or 0
                cache_read = usage_dict.get('cache_read_input_tokens', 0) or 0
                
                # Также проверяем cache_creation.ephemeral_1h_input_tokens (для 1h TTL)
                if 'cache_creation' in usage_dict:
                    cache_creation_info = usage_dict['cache_creation']
                    if isinstance(cache_creation_info, dict):
                        ephemeral_1h = cache_creation_info.get('ephemeral_1h_input_tokens', 0) or 0
                        if ephemeral_1h > 0:
                            cache_created = ephemeral_1h  # Используем 1h кэш если он есть
                
                # Детальное логирование для отладки
                if log_queue and use_caching:
                    log_msg(log_queue, f"  🔍 Usage dict keys: {list(usage_dict.keys())}")
                    if 'cache_creation_input_tokens' in usage_dict:
                        log_msg(log_queue, f"  🔍 cache_creation_input_tokens = {usage_dict['cache_creation_input_tokens']}")
                    if 'cache_read_input_tokens' in usage_dict:
                        log_msg(log_queue, f"  🔍 cache_read_input_tokens = {usage_dict['cache_read_input_tokens']}")
                    if 'cache_creation' in usage_dict:
                        cache_creation_info = usage_dict['cache_creation']
                        if isinstance(cache_creation_info, dict):
                            ephemeral_1h = cache_creation_info.get('ephemeral_1h_input_tokens', 0) or 0
                            if ephemeral_1h > 0:
                                log_msg(log_queue, f"  🔍 cache_creation.ephemeral_1h_input_tokens = {ephemeral_1h}")
            # Или через атрибуты напрямую
            elif hasattr(usage, 'cache_creation_input_tokens'):
                cache_created = getattr(usage, 'cache_creation_input_tokens', 0) or 0
                cache_read = getattr(usage, 'cache_read_input_tokens', 0) or 0
            # Или через __dict__
            elif hasattr(usage, '__dict__'):
                usage_dict = usage.__dict__
                cache_created = usage_dict.get('cache_creation_input_tokens', 0) or 0
                cache_read = usage_dict.get('cache_read_input_tokens', 0) or 0
                if log_queue and use_caching:
                    log_msg(log_queue, f"  🔍 Usage __dict__ keys: {list(usage_dict.keys())}")
        except Exception as e:
            if log_queue:
                log_msg(log_queue, f"  ⚠️ Ошибка чтения cache статистики: {e}")
                import traceback
                log_msg(log_queue, f"  🔍 Traceback: {traceback.format_exc()}")
        
        # Логируем кэш создание и чтение
        if cache_created > 0:
            if log_queue:
                log_msg(log_queue, f"  💾 CACHE CREATED: {cache_created} tokens")
            PROGRESS_MANAGER.log_cache_usage(cache_create_tokens=cache_created)
        elif cache_read > 0:
            # Если кэш читается, но не создаётся - значит он был создан ранее
            if log_queue:
                log_msg(log_queue, f"  ℹ️  Кэш из предыдущего запроса (в пределах TTL)")
                log_msg(log_queue, f"  💡 Это нормально - кэш работает!")
            
        if cache_read > 0:
            if log_queue:
                savings = cache_read * 0.003 * 0.9 / 1000  # 90% экономия
                log_msg(log_queue, f"  ⚡ CACHE HIT: {cache_read} tokens (saved ${savings:.4f})")
            PROGRESS_MANAGER.log_cache_usage(cache_hit_tokens=cache_read)
        
        # Дополнительная информация о кэше
        if log_queue and use_caching:
            try:
                if hasattr(usage, 'model_dump'):
                    usage_dict = usage.model_dump()
                    if 'cache_creation' in usage_dict:
                        cache_creation_info = usage_dict['cache_creation']
                        if isinstance(cache_creation_info, dict):
                            ephemeral_5m = cache_creation_info.get('ephemeral_5m_input_tokens', 0)
                            ephemeral_1h = cache_creation_info.get('ephemeral_1h_input_tokens', 0)
                            if ephemeral_5m > 0:
                                log_msg(log_queue, f"  🔍 Cache creation (5m): {ephemeral_5m} tokens")
                            if ephemeral_1h > 0:
                                log_msg(log_queue, f"  🔍 Cache creation (1h): {ephemeral_1h} tokens")
            except:
                pass
        
        # Предупреждение если кэш не работает (только если оба = 0 И есть cache_control)
        if use_caching and cache_created == 0 and cache_read == 0:
            # Проверяем, был ли установлен cache_control
            has_cache_control = False
            if system_content and isinstance(system_content, list):
                has_cache_control = any('cache_control' in item for item in system_content if isinstance(item, dict))
            
            if has_cache_control:
                if log_queue:
                    log_msg(log_queue, f"  ⚠️  Кэш не использовался!")
                    log_msg(log_queue, f"  💡 Возможные причины:")
                    # Получаем estimated_tokens из system_content если доступен
                    est_tokens = '?'
                    if system_content and isinstance(system_content, list) and system_content:
                        text = system_content[0].get('text', '')
                        est_tokens = len(text) // 4 if text else '?'
                    elif system_content and isinstance(system_content, str):
                        est_tokens = len(system_content) // 4
                    log_msg(log_queue, f"     - System prompt <1024 токенов (у вас ~{est_tokens})")
                    log_msg(log_queue, f"     - Промпт изменился между запросами")
                    log_msg(log_queue, f"     - Модель {model} не поддерживает кэширование")
                    # Пробуем вывести полный usage для отладки
                    try:
                        if hasattr(usage, 'model_dump_json'):
                            usage_json = usage.model_dump_json()
                            log_msg(log_queue, f"  🔍 Полный usage JSON: {usage_json[:500]}")
                    except:
                        pass
        
        # Извлекаем текст
        text_content = ""
        for block in response.content:
            if block.type == "text":
                text_content += block.text
        
        return True, text_content
        
    except anthropic.APIError as e:
        error_str = str(e)
        error_code = getattr(e, 'status_code', None)
        debug_log(log_queue, "Claude APIError:", f"code={error_code} {error_str}")
        return False, f"__RETRY__:{error_code or 0}:{error_str}"
    except Exception as e:
        import traceback
        error_str = str(e)
        debug_log(log_queue, "Claude Exception:", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        return False, f"__RETRY__:EXC:{error_str}"

    finally:
        if client is not None:
            client.close()
        elif httpx_client is not None:
            httpx_client.close()


def _wait_with_stop_check(wait_time: float) -> bool:
    """Ждём wait_time секунд с проверкой STOP_FLAG. Возвращает True если остановлено."""
    waited = 0.0
    while waited < wait_time:
        if STOP_FLAG.is_stopped():
            return True
        time.sleep(min(1.0, wait_time - waited))
        waited += 1.0
    return False


_ANTHROPIC_CALL_SEMAPHORE = threading.BoundedSemaphore(
    _bounded_env_int("REWRITE_ANTHROPIC_MAX_CONCURRENT_CALLS", 32, 1, 128)
)


@redact_provider_output
def call_claude_api(
    messages: List[Dict],
    api_key: str,
    model: str,
    log_queue: queue.Queue,
    task_info: str = "",
    use_caching: bool = True
) -> Tuple[bool, str]:
    """
    Вызывает Claude API с итеративными ретраями (без рекурсии).
    Счётчики ретраев — локальные, не расшарены между потоками.
    """
    if not ANTHROPIC_AVAILABLE:
        return False, "Anthropic API недоступен: в Python процесса сервиса не установлены anthropic/httpx. Администратору: установите зависимости requirements-anthropic.txt и перезапустите сервис."
    # Ignore request/job-provided keys at the provider boundary. Resolve the
    # current owner pool for every attempt, so settings edits also take effect.
    if not get_effective_anthropic_keys():
        return False, "Anthropic credential is not configured for this account"
    max_retries = {"disconnect": 3, "server": 5, "timeout": 3, "connection": 3, "rate_limit": 20}
    retries = {"disconnect": 0, "server": 0, "timeout": 0, "connection": 0, "rate_limit": 0}
    # FIX-2026-07-30 (P1 №7): backoff по владельцу Anthropic-ключа, а не общий
    # на весь сервис (мёртвый BYOK-ключ одного тенанта душил всех).
    limiter = rate_limiter_for("anthropic")

    while True:
        try:
            ensure_provider_execution_allowed("claude")
            pool = get_effective_credential_pool("anthropic")
            with _ANTHROPIC_CALL_SEMAPHORE:
                with pool.acquire(timeout=300.0) as leased_key:
                    success, response = _call_claude_api_once(messages, leased_key, model, log_queue, task_info, use_caching)
        except (CredentialPoolExhausted, RuntimeError) as exc:
            return False, str(exc)

        if success:
            limiter.on_success()
            return True, response

        # Обычная ошибка — не ретрай
        if not response.startswith("__RETRY__:"):
            return False, response

        # Разбираем тип ошибки
        _, error_code_str, error_str = response.split(":", 2)
        error_code = int(error_code_str) if error_code_str.isdigit() else None
        error_lower = error_str.lower()

        if STOP_FLAG.is_stopped():
            return False, "Остановлено пользователем"

        # 404 — модель не найдена, fallback на Sonnet 4.6 (актуальный pinned alias).
        # ВАЖНО: не fallback на claude-sonnet-4-20250514 — он deprecated и retire 2026-06-15.
        if error_code == 404 or "not_found" in error_lower:
            if "sonnet" in model.lower() and model != "claude-sonnet-4-6":
                log_msg(log_queue, f"    ⚠️ {model} недоступен, fallback на claude-sonnet-4-6")
                model = "claude-sonnet-4-6"
                continue
            return False, f"Модель '{model}' не найдена."

        # 499 / disconnect
        if error_code == 499 or "disconnected" in error_lower:
            if retries["disconnect"] >= max_retries["disconnect"]:
                return False, f"Client disconnected после {max_retries['disconnect']} попыток."
            retries["disconnect"] += 1
            backoff = 10 * retries["disconnect"]
            log_msg(log_queue, f"    🔄 Client disconnected, повтор {retries['disconnect']}/{max_retries['disconnect']} через {backoff}с...")
            time.sleep(backoff)
            continue

        # 429 / rate limit
        if error_code == 429 or "rate_limit" in error_lower:
            # НАША ПРАВКА 20.09.2026 (см. такой же разбор выше): исчерпанная квота —
            # это не «подожди», это «денег нет». Повторять её десять раз с растущей
            # паузой значит потратить полчаса и всё равно отказать.
            if _quota_exhausted(error_lower, str(error_msg or "")):
                log_msg(log_queue, "    ❌ На ключе кончилась квота или деньги — "
                                   "ждать бесполезно: " + str(error_msg or "")[:160])
                return False, ("Квота ключа исчерпана (429 insufficient_quota): "
                               + str(error_msg or "")[:200])
            if retries["rate_limit"] >= max_retries["rate_limit"]:
                return False, f"Rate limit после {max_retries['rate_limit']} попыток."
            retries["rate_limit"] += 1
            wait_time = limiter.on_rate_limit()
            log_msg(log_queue, f"    ⏳ Rate limit{' ['+task_info+']' if task_info else ''}, жду {wait_time:.0f}с...")
            if _wait_with_stop_check(wait_time):
                return False, "Остановлено пользователем"
            continue

        # 500-599 server errors
        if error_code and 500 <= error_code < 600:
            if retries["server"] >= max_retries["server"]:
                return False, f"Ошибка сервера после {max_retries['server']} попыток (code {error_code})"
            retries["server"] += 1
            backoff = min(2 ** (retries["server"] - 1), 60)
            log_msg(log_queue, f"    ⚠️ Ошибка сервера ({error_code}), повтор {retries['server']}/{max_retries['server']} через {backoff}с...")
            time.sleep(backoff)
            continue

        # Timeout
        if "timeout" in error_lower or "timed out" in error_lower:
            if retries["timeout"] >= max_retries["timeout"]:
                return False, f"Timeout после {max_retries['timeout']} попыток"
            retries["timeout"] += 1
            backoff = 5 * retries["timeout"]
            log_msg(log_queue, f"    ⏳ Timeout, повтор {retries['timeout']}/{max_retries['timeout']} через {backoff}с...")
            time.sleep(backoff)
            continue

        # Connection error (generic Exception path)
        if "connection" in error_lower or "499" in error_str:
            if retries["connection"] >= max_retries["connection"]:
                return False, f"Connection error после {max_retries['connection']} попыток."
            retries["connection"] += 1
            backoff = 10 * retries["connection"]
            log_msg(log_queue, f"    🔄 Connection error, повтор {retries['connection']}/{max_retries['connection']} через {backoff}с...")
            time.sleep(backoff)
            continue

        # Неизвестная ошибка — не ретраим
        return False, f"Claude API error: {error_str[:200]}"


def _is_token_switch_signal(text_lower: str) -> bool:
    """FEATURE-2026-06-28-multitoken: токен ИСЧЕРПАН (лимит подписки) ИЛИ СДОХ (логаут/протух) →
    сигнал переключиться на следующий токен в ротации. Возвращает True если в выводе claude CLI
    есть признак того, что текущим токеном дальше ходить нельзя."""
    t = text_lower or ""
    return (
        # лимит подписки исчерпан
        "hit your limit" in t or "hit the limit" in t or ("limit" in t and "reset" in t)
        or "usage limit" in t or "rate limit" in t
        # токен сдох / разлогинен / протух → тоже «кончился», переключаемся
        or "not logged in" in t or "please run /login" in t or "/login" in t
        or "invalid bearer token" in t or "authentication" in t and "fail" in t
        # 04.09.2026: у мёртвого токена CLI отвечает «Your organization does not
        # have access to Claude… contact your administrator» и выходит с кодом 1.
        # Раньше этот текст не распознавался: пул не переключался на следующий
        # токен, а кусок падал как «сбой обработки». Так канал простоял 9 дней.
        or "does not have access" in t
        or "contact your administrator" in t
        or "please login again" in t
    )


_CLAUDE_PROBE_TTL_SEC = 600
_CLAUDE_PROBE_CACHE: Dict[str, Any] = {}
_CLAUDE_PROBE_LOCK = threading.Lock()


def _claude_faceless_reason(timeout: float = 60.0) -> str:
    """Причина безликого «exit code 1» у CLI Claude — одним дешёвым зондом.

    SDK читает stdout как поток JSON, а отказ авторизации CLI печатает туда же
    ОБЫЧНЫМ текстом: поток рвётся, наружу выходит «Command failed with exit code 1»
    при пустом stderr, и настоящая причина теряется совсем. Спрашиваем её у CLI
    напрямую тем же токеном и кэшируем: зонд стоит один вызов на токен в 10 минут,
    а не по вызову на каждый упавший кусок.
    """
    token = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if not token:
        return ""
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    with _CLAUDE_PROBE_LOCK:
        cached = _CLAUDE_PROBE_CACHE.get(key)
        if cached and now - cached[0] < _CLAUDE_PROBE_TTL_SEC:
            return cached[1]
    reason = ""
    try:
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        probe = subprocess.run(
            ["claude", "-p", "ping"],
            env=env, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=timeout,
        )
        if probe.returncode != 0:
            reason = " ".join(
                ((probe.stdout or "") + " " + (probe.stderr or "")).split()
            )[:300]
    except Exception as exc:                                  # noqa: BLE001
        reason = f"зонд не ответил: {type(exc).__name__}"
    with _CLAUDE_PROBE_LOCK:
        _CLAUDE_PROBE_CACHE[key] = (now, reason)
    return reason


class _ClaudeSlotUnavailable(RuntimeError):
    """Нет свободного слота под claude-CLI (стоп юзера или таймаут ожидания)."""


# FIX-2026-07-30 (P0 deadlock): глубина владения слотом В ЭТОМ ПОТОКЕ.
# ``_claude_sema`` — обычный threading.Semaphore, он НЕ реентрантный: второй
# acquire из того же потока не вернётся никогда, пока слот не отпустит кто-то
# другой (а отпустить должен как раз этот поток) → перманентный дедлок.
_claude_slot_depth = threading.local()


def _claude_slot_held() -> bool:
    """Держит ли ТЕКУЩИЙ поток глобальный claude-слот."""
    return getattr(_claude_slot_depth, "depth", 0) > 0


@contextmanager
def _claude_cli_slot(timeout_sec: Optional[int] = None):
    """Глобальный кап на одновременные claude-CLI процессы (RAM-guard).

    FIX-2026-07-30 (P1 №11): агрегированная параллельность claude-CLI должна быть
    ограничена глобально. С BYOK у каждого тенанта СВОЙ пул кредов, поэтому лиза
    (серверная или пользовательская) сама по себе ничего не ограничивает:
    ≈250 MB RSS на процесс × N тенантов → OOM сразу для всех сервисов на боксе.
    Лимит — серверный (``CLAUDE_MAX_PARALLEL``), клиент его не поднимает.

    FIX-2026-07-30 (P0 deadlock): захват — РОВНО ОДИН на вызов. Семафор брался и
    в обёртке (``call_claude_subscription`` / ``call_claude_conversation``), и
    внутри impl-функций (``with _claude_sema``) — то есть ДВАЖДЫ из одного
    потока. Нереентрантный семафор на втором acquire вешал поток навсегда.
    Теперь вложенный вход — no-op: слот уже принадлежит этому потоку.
    """
    if _claude_slot_held():
        # Вложенный вход: слот уже наш, повторно НЕ занимаем (иначе дедлок).
        _claude_slot_depth.depth = getattr(_claude_slot_depth, "depth", 0) + 1
        try:
            yield
        finally:
            _claude_slot_depth.depth -= 1
        return
    if timeout_sec is None:
        timeout_sec = SUBSCRIPTION_CALL_TIMEOUT_SEC
    started = time.monotonic()
    while True:
        if STOP_FLAG.is_stopped():
            raise _ClaudeSlotUnavailable("Остановлено пользователем")
        if _claude_sema.acquire(timeout=0.25):
            break
        if time.monotonic() - started >= timeout_sec:
            raise _ClaudeSlotUnavailable(
                f"{SUBSCRIPTION_TIMEOUT_PREFIX} timed out waiting for a Claude slot"
            )
    _claude_slot_depth.depth = 1
    try:
        yield
    finally:
        _claude_slot_depth.depth = 0
        _claude_sema.release()


@redact_provider_output
def call_claude_subscription(
    messages: List[Dict],
    log_queue: queue.Queue,
    task_info: str = "",
    model: str = "claude-sonnet-4-5-20250929",
    oauth_token: str = ""
) -> Tuple[bool, str]:
    """Обёртка: держит глобальный кап на claude-CLI (FIX-2026-07-30 P1 №11)."""
    try:
        with _claude_cli_slot():
            return _call_claude_subscription_impl(
                messages, log_queue, task_info, model, oauth_token
            )
    except _ClaudeSlotUnavailable as exc:
        return False, str(exc)


def _call_claude_subscription_impl(
    messages: List[Dict],
    log_queue: queue.Queue,
    task_info: str = "",
    model: str = "claude-sonnet-4-5-20250929",
    oauth_token: str = ""
) -> Tuple[bool, str]:
    """
    Вызов Claude через Pro/Max подписку (claude-code-sdk).
    Использует oauth_token из параметра или переменной окружения.
    НЕ требует API ключа, НЕ тратит API credits.
    """
    if not CLAUDE_CODE_SDK_AVAILABLE:
        return False, "claude-code-sdk не установлен. Установите: pip install claude-code-sdk anyio"
    
    import os
    import io
    import subprocess
    
    # Используем токен из параметра или переменной окружения
    if not oauth_token:
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    
    if not oauth_token:
        return False, "OAuth токен не задан. Введите токен в GUI или установите CLAUDE_CODE_OAUTH_TOKEN"
    
    # Очищаем токен: убираем переносы строк, пробелы и префикс "Bearer "
    # Токен может содержать переносы строк и лишние пробелы
    oauth_token = "".join(oauth_token.split())  # Убираем все пробелы и переносы строк
    # Убираем префикс "Bearer" или "bearer" если есть
    if oauth_token.lower().startswith("bearer"):
        oauth_token = oauth_token[6:]  # Убираем "Bearer" (6 символов)
    
    if not oauth_token:
        return False, "OAuth токен пустой после очистки"
    
    # Логируем очищенный токен (первые и последние 10 символов для безопасности)
    if log_queue:
        # M6 PATCH-2026-06-25-secfix: не логируем хвост секрета и точную длину (раньше last10 + len).
        log_msg(log_queue, "  🔀 Через альт-ключ" if oauth_token == ALT_ROUTE_TOKEN
                else "  🔍 OAuth токен загружен из серверных настроек")

    # Извлекаем system prompt и user message из формата OpenAI
    system_prompt = ""
    user_content = ""
    for msg in messages:
        if msg["role"] == "system":
            content = msg["content"]
            if isinstance(content, list):
                if content and isinstance(content[0], dict):
                    system_prompt = content[0].get("text", "")
            else:
                system_prompt = str(content)
        elif msg["role"] == "user":
            user_content = str(msg["content"])
    
    if not user_content:
        user_content = "Please proceed."

    # Токен передаётся per-call через options.env (SDK 0.0.25+ поддерживает),
    # больше НЕ через global os.environ — это убрало serial-mutex и позволяет
    # реальный parallelism для subscription calls (раньше _env_lock сериализовал
    # все вызовы из любых worker-threads).

    # Перехватываем stderr для диагностики
    stderr_capture = io.StringIO()

    debug_log(log_queue, "Subscription REQUEST:", f"model={model} system_len={len(system_prompt)} user_len={len(user_content)}")

    result_text = ""

    async def _run():
        nonlocal result_text
        from dataclasses import replace

        options, child_env, isolation_root = _claude_isolated_options(
            oauth_token,
            system_prompt=system_prompt if system_prompt else None,
            model=model,
            max_turns=1,
            include_partial_messages=True,
        )
        transport_options = replace(
            options,
            permission_prompt_tool_name="stdio",
        )

        async def _prompt_stream():
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": user_content,
                },
                "parent_tool_use_id": None,
                "session_id": "rewrite-isolated",
            }

        transport = _SanitizedClaudeTransport(
            prompt=_prompt_stream(),
            options=transport_options,
            process_env=child_env,
            isolation_root=isolation_root,
        )
        # Двухуровневый watchdog (Anthropic issue #25979 recommendation):
        #  - STREAM_DATA_TIMEOUT_SEC (120s): max тишина МЕЖДУ chunks стрима — ловит
        #    mid-turn freeze (когда API замолкает и CLI ждёт бесконечно).
        #  - SUBSCRIPTION_CALL_TIMEOUT_SEC (600s): hard cap — на случай slow-stream.
        last_data_time = [time.monotonic()]
        iterate_done = anyio.Event()

        async def _stream_watchdog():
            # Каждые ≤15s проверяем не превышен ли silence-timeout. Выходим как только
            # iterate_done.set() (нормальное завершение стрима).
            while not iterate_done.is_set():
                with anyio.move_on_after(15):
                    await iterate_done.wait()
                    return  # стрим завершился — выходим
                # 15s прошло без события — проверяем тишину
                silence = time.monotonic() - last_data_time[0]
                if silence > STREAM_DATA_TIMEOUT_SEC:
                    raise TimeoutError(
                        f"stream-data watchdog: no data for {int(silence)}s "
                        f"(known CLI hang — anthropics/claude-code#25979)"
                    )

        # got_assistant: чтобы избежать дубля AssistantMessage+ResultMessage —
        # ResultMessage используем ТОЛЬКО как fallback, если AssistantMessage не пришёл.
        got_assistant = [False]

        async def _do_iter():
            nonlocal result_text
            try:
                async for message in query(
                    prompt=_prompt_stream(),
                    options=options,
                    transport=transport,
                ):
                    last_data_time[0] = time.monotonic()  # любая активность (включая StreamEvent partial) ресетает silence
                    msg_type = type(message).__name__

                    if msg_type == "AssistantMessage":
                        content = getattr(message, "content", None)
                        if isinstance(content, str):
                            result_text += content
                            got_assistant[0] = True
                        elif isinstance(content, list):
                            for block in content:
                                text = getattr(block, "text", None)
                                if text and isinstance(text, str):
                                    result_text += text
                                    got_assistant[0] = True

                    elif msg_type == "ResultMessage" and not got_assistant[0]:
                        # fallback: AssistantMessage не пришёл, используем ResultMessage
                        result = getattr(message, "result", None)
                        if result and isinstance(result, str):
                            result_text += result
                    # StreamEvent / прочее — игнорируем для контента, но watchdog уже обновлён выше
            finally:
                iterate_done.set()

        with anyio.fail_after(SUBSCRIPTION_CALL_TIMEOUT_SEC):
            async with anyio.create_task_group() as tg:
                tg.start_soon(_stream_watchdog)
                await _do_iter()

    try:
        if log_queue:
            log_msg(log_queue, f"  🔑 Использую Pro подписку (модель: {model})")
        # Preflight CLI check — one-time cache (2026-06-04). Раньше делалось на каждый
        # вызов с timeout=5 → лишний node-spawn + 'version timed out' под параллелью.
        _ensure_cli_ready(log_queue)

        # Semaphore: лимит одновременных claude subprocess'ов между всеми джобами.
        # Держим внутри forensics+pkill чтобы зомби-CLI не занимал слот после release.
        # FIX-2026-07-30 (P0 deadlock): через _claude_cli_slot(), а НЕ через голый
        # `with _claude_sema`. Слот уже взят обёрткой call_claude_subscription в
        # этом же потоке → второй acquire нереентрантного семафора не вернулся бы
        # никогда. Здесь вход вложенный (no-op); прямой вызов impl (в обход
        # обёртки) слот всё равно возьмёт — кап не обойти.
        with _claude_cli_slot():
            # Inner retry на stream-stall: 1 retry до того как стрелять наверх в server.py
            # (там external retry с 30s backoff). Inner retry освобождает новое TCP — без
            # backoff, потому что подвисший TCP не разлепит сам, а новый subprocess сразу
            # открывает свежее соединение.
            # PATCH-2026-06-12-transient-inner-retry
            # Inner retry: stream-stall (timeout) НЕ ретраим (outer retry в server.py),
            # а транзиентные НЕ-timeout ошибки SDK/API (overloaded/rate-limit/decode/
            # process — приходят как TaskGroup sub-exception) РЕТРАИМ свежим subprocess.
            # Вернул transient inner-retry, потерянный при #13 hard-cap 2026-06-04.
            _OPUS_INNER_RETRIES = 0        # timeout stalls: без inner retry (как было)
            _TRANSIENT_INNER_RETRIES = 2   # транзиентные не-timeout: 2 повтора
            _timeout_attempt = 0
            _transient_done = 0
            _attempts_done = 0
            while True:
                _attempts_done += 1
                result_text = ""  # reset на каждом attempt
                try:
                    anyio.run(_run)
                    break  # success
                except BaseException as _attempt_err:
                    if isinstance(_attempt_err, (KeyboardInterrupt, SystemExit)):
                        raise
                    _to = _find_timeout_error(_attempt_err)
                    if _to is None:
                        # Транзиентная не-timeout ошибка — ретраим свежим subprocess (новый TCP).
                        if _transient_done < _TRANSIENT_INNER_RETRIES:
                            _transient_done += 1
                            # PATCH-2026-06-12-no-pkill-transient: НЕ pkill здесь. Под параллелью
                            # pkill -P <pid> -f claude убил бы ВСЕ соседние claude-субпроцессы
                            # (общий PID процесса, до CLAUDE_MAX_PARALLEL детей одновременно).
                            # Транзиентная ошибка = query() уже упал, subprocess закрыт
                            # SDK-контекстом; свежий retry откроет новый. Чистить нечего.
                            if log_queue:
                                log_msg(
                                    log_queue,
                                    f"  🔁 [transient] inner retry {_transient_done}/{_TRANSIENT_INNER_RETRIES} "
                                    f"after: {str(_attempt_err)[:140]}"
                                )
                            time.sleep(3 * _transient_done)  # короткий backoff
                            continue
                        raise  # транзиентные ретраи исчерпаны — outer handler разберётся
                    # timeout path (как было): forensics + kill, inner retry по _OPUS_INNER_RETRIES
                    try:
                        _collect_hung_cli_forensics(log_queue, f"{task_info or 'unknown'}_a{_attempts_done}")
                    except Exception:
                        pass
                    # M2 FIX-2026-07-02: НЕ pattern-pkill. Под общим PID до CLAUDE_MAX_PARALLEL
                    # детей-claude сразу; `pkill -P <pid> -f claude` убил бы субпроцессы
                    # СОСЕДНИХ джоб (cross-job SIGKILL). Достоверно вычленить PID именно
                    # ЭТОГО зависшего вызова нельзя (pgrep в forensics видит всех детей),
                    # поэтому полагаемся на закрытие субпроцесса самим SDK/anyio при отмене
                    # task group — как делает transient-путь выше (no-pkill-transient).
                    if _timeout_attempt < _OPUS_INNER_RETRIES:
                        _timeout_attempt += 1
                        if log_queue:
                            log_msg(
                                log_queue,
                                f"  🔁 [opus-stall] inner retry {_timeout_attempt}/{_OPUS_INNER_RETRIES} "
                                f"after stream stall ({_to})"
                            )
                        time.sleep(2)
                        try:
                            import gc as _gc
                            _gc.collect()
                        except Exception:
                            pass
                        continue
                    raise TimeoutError(f"{_to} (after {_attempts_done} inner attempts)") from _attempt_err

        # Логируем результат перед возвратом
        if log_queue:
            if result_text:
                log_msg(log_queue, f"  ✅ Успешно получен ответ ({_attempts_done} attempt): {len(result_text)} символов")
                log_msg(log_queue, f"  🔍 Превью ответа: {result_text[:200]}...")
            else:
                log_msg(log_queue, f"  ⚠️ Ответ пустой! Проверьте структуру сообщения от SDK")

        if not result_text:
            return False, "Ответ от Claude пустой. Проверьте токен и модель."

        return True, result_text
    except BaseException as outer_e:
        # Распаковываем (Base)ExceptionGroup от anyio.create_task_group, чтобы
        # TimeoutError от stream-watchdog или anyio.fail_after был пойман корректно.
        _to_err = _find_timeout_error(outer_e)
        if _to_err is not None:
            # Forensics + kill УЖЕ сделаны в inner-retry loop (на каждом attempt).
            # Здесь только формируем error для server.py (наружный retry).
            timeout_reason = str(_to_err) if str(_to_err) else f"hard cap {SUBSCRIPTION_CALL_TIMEOUT_SEC}s"
            msg = (
                f"Claude Code CLI stream stalled ({timeout_reason}). "
                f"Inner retries exhausted. Known upstream bug — anthropics/claude-code#25979. "
                f"Forensics: /tmp/cli_hang_<task>_a<N>_<ts>/; prompt ~{len(user_content)} chars."
            )
            debug_log(log_queue, "Subscription TIMEOUT:", msg)
            if log_queue:
                log_msg(log_queue, f"  ⏱️  Subscription timeout: {msg}")
            return False, SUBSCRIPTION_TIMEOUT_PREFIX + msg
        # Не timeout: KeyboardInterrupt/SystemExit пропускаем, остальное обрабатываем как Exception.
        if not isinstance(outer_e, Exception):
            raise
        e = outer_e
        # PATCH-2026-06-12-transient-inner-retry: распаковываем реальную sub-exception из (Base)ExceptionGroup,
        # чтобы лог/классификация видели причину, а не "unhandled errors in a TaskGroup".
        _flat_subs = []
        def _flatten_eg(_x):
            _ss = getattr(_x, "exceptions", None)
            if _ss:
                for _y in _ss:
                    _flatten_eg(_y)
            elif _x is not None:
                _flat_subs.append(_x)
        try:
            _flatten_eg(outer_e)
        except Exception:
            pass
        if _flat_subs:
            e = _flat_subs[0]
        import traceback
        error_msg = str(e) or type(e).__name__
        stderr_output = stderr_capture.getvalue()
        is_limit_error = False
        debug_log(log_queue, "Subscription Exception:", f"{type(e).__name__}: {error_msg}\nstderr: {stderr_output}\n{traceback.format_exc()}")
        faceless = (
            not stderr_output
            and ("exit code 1" in error_msg.lower() or "command failed" in error_msg.lower())
        )
        alt_call = oauth_token == ALT_ROUTE_TOKEN
        # FIX-2026-09-17-alt-route: проба идёт на токене владельца — на альт-ключе её не зовём.
        probe_reason = _claude_faceless_reason() if faceless and not alt_call else ""
        if probe_reason:
            error_msg = f"{error_msg} | CLAUDE_AUTH: {probe_reason}"
        if log_queue:
            log_msg(log_queue, f"  ❌ Subscription error: {error_msg[:300]}")
            if stderr_output:
                log_msg(log_queue, f"  ❌ Stderr: {stderr_output[:500]}")
            elif probe_reason:
                log_msg(log_queue, f"  ❌ Причина от CLI: {probe_reason[:300]}")
            else:
                log_msg(log_queue, "  ❌ Stderr пустой")
        if alt_call:
            return False, f"{ALT_ERROR_PREFIX} {error_msg}"
        if not is_limit_error and ("exit code 1" in error_msg.lower() or "command failed" in error_msg.lower()):
            is_limit_error = _is_token_switch_signal(error_msg)
        if is_limit_error:
            return False, "SUBSCRIPTION_LIMIT:" + error_msg
        return False, error_msg
    # finally блок c restore os.environ + _env_lock.release() удалён 2026-05-24:
    # токен теперь идёт per-call через options.env, глобальная env не трогается → нечего восстанавливать.


HOST_BUDGET_WAIT_PREFIX = "HOST_BUDGET_WAIT:"


# Места подписки Codex, которые задачи рерайта НЕ занимают: их ждут короткие вызовы без
# задачи — название ролика и сверка обложки (chat-once). 04.10.2026 все четыре места на
# каждой учётке держали куски длинных рерайтов: освободившееся место тут же перехватывал
# соседний кусок, а название ждало до пяти минут и падало, и готовые ролики стояли перед
# заливкой. Резерв меньше потолка всегда хотя бы на одно место: иначе рерайт встал бы.
SHORT_CALL_RESERVE = int(os.environ.get("REWRITE_SHORT_CALL_RESERVE", "1") or 0)


def short_call_reserve(family, job_id) -> int:
    """Сколько мест оставить свободными, занимая место для этого вызова. Только Codex и
    только для вызова изнутри задачи рерайта; короткому вызову без задачи — ноль."""
    if family != "codex" or not job_id or SHORT_CALL_RESERVE <= 0:
        return 0
    cap = _host_budget.capacity(family) or 1
    return max(0, min(SHORT_CALL_RESERVE, cap - 1))
_JOB_HOST_BUDGET_WAITS = {}
_JOB_HOST_BUDGET_STREAK: Dict[str, int] = {}
_HOST_BUDGET_WAIT_LOCK = threading.Lock()
HOST_BUDGET_WAIT_BASE_SEC = 5
HOST_BUDGET_WAIT_MAX_SEC = int(os.environ.get("REWRITE_HOST_BUDGET_WAIT_MAX_SEC", "300") or 300)


def host_budget_wait_delay(streak: int) -> float:
    """Пауза перед N-м ПОДРЯД возвратом в очередь: 5, 10, 20, 40 … до потолка."""
    return float(min(HOST_BUDGET_WAIT_BASE_SEC * (2 ** max(0, int(streak) - 1)),
                     HOST_BUDGET_WAIT_MAX_SEC))


def remember_host_budget_wait(job_id):
    """Общий лимит ключа занят — вернуть джоб в очередь и не трогать какое-то время.

    03.10.2026, ролик на 455 тысяч знаков. Пауза была пять секунд всегда: джоб
    возвращался в очередь, через семь секунд брался снова, две секунды ждал ключ и
    снова возвращался — 1015 кругов и три тысячи строк «Задача принята / Подготовка»
    в журнале одной задачи, пока рестарт службы её не прервал. Теперь пауза удваивается
    с каждым кругом подряд (5, 10, 20 … до HOST_BUDGET_WAIT_MAX_SEC) и сбрасывается,
    как только ключ действительно получен (forget_host_budget_streak из acquire).
    """
    if job_id:
        with _HOST_BUDGET_WAIT_LOCK:
            n = _JOB_HOST_BUDGET_STREAK.get(str(job_id), 0) + 1
            _JOB_HOST_BUDGET_STREAK[str(job_id)] = n
            _JOB_HOST_BUDGET_WAITS[str(job_id)] = time.time() + host_budget_wait_delay(n)


def host_budget_wait_streak(job_id) -> int:
    """Сколько раз подряд этот джоб возвращался в очередь из-за общего лимита."""
    with _HOST_BUDGET_WAIT_LOCK:
        return _JOB_HOST_BUDGET_STREAK.get(str(job_id or ""), 0)


def forget_host_budget_streak(job_id) -> None:
    """Ключ получен — серия возвратов прервана, следующая пауза снова короткая."""
    if job_id:
        with _HOST_BUDGET_WAIT_LOCK:
            _JOB_HOST_BUDGET_STREAK.pop(str(job_id), None)


def host_budget_wait_until(job_id, pop=False):
    with _HOST_BUDGET_WAIT_LOCK:
        return (_JOB_HOST_BUDGET_WAITS.pop(str(job_id), 0) if pop else
                _JOB_HOST_BUDGET_WAITS.get(str(job_id), 0))


SUBSCRIPTION_LIMIT_PREFIX = "SUBSCRIPTION_LIMIT:"

# job_id → unix-время сброса окна. Пишется там, где чанк упал по лимиту, читается
# при финализации джоба: по этому факту джоб откладывается, а не хоронится.
_JOB_LIMIT_HITS: Dict[str, float] = {}
_JOB_LIMIT_LOCK = threading.Lock()
_LIMIT_DEFAULT_WAIT_SEC = int(os.environ.get("REWRITE_LIMIT_DEFAULT_WAIT_SEC", "900") or 900)


def parse_limit_reset(text) -> float:
    """Вытащить unix-время сброса из ответа. 0 — не нашлось."""
    m = re.search(r"reset=(\d{9,12})", str(text or ""))
    if m:
        return float(m.group(1))
    m = re.search(r"limit reached\|(\d{9,12})", str(text or ""))
    return float(m.group(1)) if m else 0.0


def remember_limit_hit(job_id: str, text) -> None:
    """Запомнить, что этот джоб упёрся в окно подписки (и когда оно откроется)."""
    if not job_id or not str(text or "").startswith(SUBSCRIPTION_LIMIT_PREFIX):
        return
    until = parse_limit_reset(text) or (time.time() + _LIMIT_DEFAULT_WAIT_SEC)
    with _JOB_LIMIT_LOCK:
        _JOB_LIMIT_HITS[str(job_id)] = max(_JOB_LIMIT_HITS.get(str(job_id), 0.0), until)


def limit_hit_until(job_id: str) -> float:
    """Посмотреть отметку о лимите, НЕ снимая её. 0 — джоб в окно не упирался."""
    with _JOB_LIMIT_LOCK:
        return _JOB_LIMIT_HITS.get(str(job_id or ""), 0.0)


def pop_limit_hit(job_id: str) -> float:
    """Забрать и очистить отметку о лимите. 0 — джоб падал не из-за окна."""
    with _JOB_LIMIT_LOCK:
        return _JOB_LIMIT_HITS.pop(str(job_id), 0.0)


# FIX-2026-09-13-models: job_id → причина «модель недоступна у провайдера». Пишется на
# первом таком отказе; читается перед каждой частью (соседний язык не тратит вызовы),
# авто-ретраем языка (повтор той же модели даст тот же отказ) и финализацией джоба.
_JOB_MODEL_UNAVAILABLE: Dict[str, str] = {}


def remember_model_unavailable(job_id: str, model: str) -> None:
    """Запомнить, что провайдер отказал этому джобу в самой модели."""
    if not job_id:
        return
    with _JOB_LIMIT_LOCK:
        _JOB_MODEL_UNAVAILABLE[str(job_id)] = codex_model_unavailable_message(model)


def model_unavailable_reason(job_id: str) -> str:
    """Причина для человека, если джоб уже упёрся в недоступную модель. "" — нет."""
    with _JOB_LIMIT_LOCK:
        return _JOB_MODEL_UNAVAILABLE.get(str(job_id or ""), "")


def pop_model_unavailable(job_id: str) -> str:
    """Забрать и очистить отметку: повторный запуск снова пробует модель."""
    with _JOB_LIMIT_LOCK:
        return _JOB_MODEL_UNAVAILABLE.pop(str(job_id or ""), "")
SUBSCRIPTION_TIMEOUT_PREFIX = "SUBSCRIPTION_TIMEOUT:"
# Watchdog для Claude Code CLI hang — known upstream bug, regression в 2.1.42.
# Anthropic issue: https://github.com/anthropics/claude-code/issues/25979
# Симптом: CLI streaming замолкает mid-turn, ждёт следующий пакет вечно, висит
# в epoll_wait с открытым TCP к Anthropic. Anthropic сами рекомендуют external
# watchdog daemon с timeout 5 мин и kill -9 — наша реализация именно это.
#
# Двухуровневый watchdog:
#  STREAM_DATA_TIMEOUT_SEC — макс тишина между chunks (ловит mid-turn freeze)
#  SUBSCRIPTION_CALL_TIMEOUT_SEC — hard cap на весь вызов (на случай slow-stream)
STREAM_DATA_TIMEOUT_SEC = 780  # was 120, bumped 2026-05-24 — Opus/Sonnet streaming pacing pauses up to ~130s between complete JSON events; re-applied after userKeys-refactor clobber
SUBSCRIPTION_CALL_TIMEOUT_SEC = 600
# Conversation-mode (ClaudeSDKClient) использует include_partial_messages=True →
# StreamEvent'ы летят каждые ~0.5-1.5с (heartbeat для watchdog'а). Поэтому жёстче, чем
# one-shot path, но с запасом — 420s, чтобы реальный hang не висел 10 мин.
CONV_STREAM_DATA_TIMEOUT_SEC = 780

# === Global claude-subprocess semaphore (2026-06-04) ===
# Лимитирует ОБЩЕЕ количество одновременных claude CLI subprocess'ов между всеми
# джобами/языками/юзерами. Защита от RAM-OOM (каждый claude ≈ 200MB).
# Дефолт 3 RAM-safe для 957MB VDS; на 12GB Contabo systemd ставит =15 через Environment.
_CLAUDE_MAX_PARALLEL = int(os.environ.get("CLAUDE_MAX_PARALLEL", "3"))
_claude_sema = threading.Semaphore(_CLAUDE_MAX_PARALLEL)

# === Preflight CLI check — ОДИН раз на процесс (2026-06-04) ===
# До этого `claude --version` спавнил лишний node-процесс на КАЖДЫЙ вызов
# и таймаутил под параллелью (timeout=5 → 'version timed out' в логах).
# Теперь — module-level cache + timeout=30 на единственную проверку.
_cli_checked: Dict[str, Any] = {"ok": None, "lock": threading.Lock()}


def _ensure_cli_ready(log_queue=None) -> bool:
    """One-time `claude --version` check (cached на процесс). Возвращает True если CLI OK."""
    if _cli_checked["ok"] is not None:
        return _cli_checked["ok"]
    with _cli_checked["lock"]:
        if _cli_checked["ok"] is not None:
            return _cli_checked["ok"]  # double-check после lock
        try:
            result = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=30,
            )
            _cli_checked["ok"] = (result.returncode == 0)
            if not _cli_checked["ok"] and log_queue:
                log_msg(log_queue, f"  ⚠️ claude CLI вернул код {result.returncode}: {(result.stderr or '')[:200]}")
        except FileNotFoundError:
            _cli_checked["ok"] = False
            if log_queue:
                log_msg(log_queue, "  ⚠️ claude CLI не найден")
        except Exception as e:
            _cli_checked["ok"] = False
            if log_queue:
                log_msg(log_queue, f"  ⚠️ claude preflight failed: {e}")
        return _cli_checked["ok"]


# Python 3.10 не имеет BaseExceptionGroup в builtins (только 3.11+). anyio.create_task_group
# оборачивает internal exceptions в (Base)ExceptionGroup — нужно распаковать чтобы
# `except TimeoutError` корректно сработал.
try:
    _BaseExceptionGroup = BaseExceptionGroup  # Python 3.11+
except NameError:
    try:
        from exceptiongroup import BaseExceptionGroup as _BaseExceptionGroup
    except ImportError:
        _BaseExceptionGroup = None


def _find_timeout_error(exc):
    """Возвращает TimeoutError если он есть в exc (включая вложенные ExceptionGroup), иначе None."""
    if isinstance(exc, TimeoutError):
        return exc
    if _BaseExceptionGroup and isinstance(exc, _BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _find_timeout_error(sub)
            if found is not None:
                return found
    return None


def _collect_hung_cli_forensics(log_queue, job_id):
    """Сохраняет диагностический snapshot всех claude-процессов перед kill'ом.

    Вызывается из TimeoutError handler в call_claude_subscription. Собирает
    в /tmp/cli_hang_<job_id>_<ts>/pid_<PID>/:
      - cmdline.txt — точные argv (\\0 → пробелы)
      - status.txt, stack.txt, wchan.txt, syscall.txt — где CLI заклинило (без ptrace)
      - fds.txt — какие файловые дескрипторы открыты (особенно stdin)
      - lsof.txt — full FD list с типами / pipes / sockets
      - tcp.txt — есть ли TCP к api.anthropic.com (через `ss -tnp`)
      - strace.txt — syscalls 3 сек (может упасть permission denied при ptrace_scope=1)

    Returns: (base_dir, pids) или None если ничего не найдено.
    """
    import subprocess as _sp
    import os as _os
    import time as _time

    ts = int(_time.time())
    safe_jid = re.sub(r"[^\w-]", "_", str(job_id or "unknown"))[:40]
    base_dir = f"/tmp/cli_hang_{safe_jid}_{ts}"
    try:
        _os.makedirs(base_dir, exist_ok=True)
    except Exception:
        return None

    # Найти PID(s) claude процессов — дети текущего worker процесса
    pids = []
    try:
        out = _sp.run(
            ["pgrep", "-P", str(_os.getpid()), "-f", "claude"],
            capture_output=True, text=True, timeout=2,
        )
        pids = [p.strip() for p in out.stdout.splitlines() if p.strip()]
    except Exception:
        pass
    # Fallback: все claude процессы на хосте
    if not pids:
        try:
            out = _sp.run(
                ["pgrep", "-f", "/usr/bin/claude"],
                capture_output=True, text=True, timeout=2,
            )
            pids = [p.strip() for p in out.stdout.splitlines() if p.strip()]
        except Exception:
            pass

    try:
        with open(f"{base_dir}/_summary.txt", "w", encoding="utf-8") as f:
            f.write(f"forensics_ts={ts}\njob_id={job_id}\nfound_pids={pids}\n")
    except Exception:
        pass

    for pid in pids:
        pid_dir = f"{base_dir}/pid_{pid}"
        try:
            _os.makedirs(pid_dir, exist_ok=True)
        except Exception:
            continue

        # 1. cmdline (\\0 → space)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as src:
                content = src.read().replace(b"\x00", b" ")
            with open(f"{pid_dir}/cmdline.txt", "wb") as dst:
                dst.write(content)
        except Exception as e:
            try:
                with open(f"{pid_dir}/cmdline.err", "w") as f:
                    f.write(str(e))
            except Exception:
                pass

        # 2-5. /proc snapshots без ptrace
        for fname in ("status", "stack", "wchan", "syscall"):
            try:
                _sp.run(
                    ["cp", f"/proc/{pid}/{fname}", f"{pid_dir}/{fname}.txt"],
                    capture_output=True, timeout=2,
                )
            except Exception:
                pass

        # 6. FDs
        try:
            with open(f"{pid_dir}/fds.txt", "w", encoding="utf-8") as f:
                _sp.run(
                    ["ls", "-la", f"/proc/{pid}/fd/"],
                    stdout=f, stderr=_sp.STDOUT, timeout=2,
                )
        except Exception:
            pass

        # 7. lsof
        try:
            with open(f"{pid_dir}/lsof.txt", "w", encoding="utf-8") as f:
                _sp.run(["lsof", "-p", pid], stdout=f, stderr=_sp.STDOUT, timeout=5)
        except Exception:
            pass

        # 8. TCP via ss
        try:
            with open(f"{pid_dir}/tcp.txt", "w", encoding="utf-8") as f:
                _sp.run(["ss", "-tnp"], stdout=f, stderr=_sp.STDOUT, timeout=2)
        except Exception:
            pass

        # 9. strace 3 сек (может упасть permission denied при ptrace_scope=1)
        try:
            with open(f"{pid_dir}/strace.txt", "w", encoding="utf-8") as f:
                _sp.run(
                    ["timeout", "3", "strace", "-p", pid,
                     "-e", "trace=read,write,close,connect,recvfrom,sendto,epoll_wait"],
                    stdout=f, stderr=_sp.STDOUT, timeout=5,
                )
        except Exception:
            pass

    if log_queue is not None:
        try:
            log_msg(log_queue, f"  🔬 Hang forensics saved: {base_dir} (pids: {pids or 'none found'})")
        except Exception:
            pass
    return base_dir, pids


def _gen_count_script_words(raw: str) -> int:
    """PATCH-2026-06-27-length: слова в script из JSON-ответа генерации. -1 если не распарсить."""
    p = parse_chunk_response(raw, expect_funnel=False)
    if not p:
        return -1
    return len((p.get("script") or "").split())


def _gen_merge_continuation(main_raw: str, cont_raw: str, insert_before_last_para: bool = False) -> str:
    """PATCH-2026-06-27-length: мерджит script из cont_raw в script main_raw,
    сохраняя preview/title/hook/plan/log_update. При неудаче — main_raw без изменений.
    PATCH-2026-07-18-v5.2: insert_before_last_para=True (recovery ПОСЛЕДНЕГО turn'а) —
    добор вставляется ПЕРЕД финальным абзацем main, финал остаётся последним."""
    pm = parse_chunk_response(main_raw, expect_funnel=False)
    pc = parse_chunk_response(cont_raw, expect_funnel=False)
    if not pm or not pc or not (pc.get("script") or "").strip():
        return main_raw
    # PATCH-2026-07-18-v5: идемпотентный merge — recovery-дожим не должен вклеивать пере-нарратив.
    _pm_s = (pm.get("script") or "").rstrip()
    _pc_s = (pc.get("script") or "").lstrip()

    def _norm_p(t):
        return " ".join(t.lower().split())

    _seen_p = {_norm_p(p) for p in _pm_s.split("\n\n") if len(p.split()) >= 15}
    _kept_p = [p for p in _pc_s.split("\n\n") if not (len(p.split()) >= 15 and _norm_p(p) in _seen_p)]
    _pc_s = "\n\n".join(_kept_p).strip()
    if not _pc_s:
        return main_raw
    if insert_before_last_para:
        # PATCH-2026-07-18-v5.5: добор — в тело, перед абзацем на ~85% слов;
        # финальный блок (последние ~15%: пэйофф-хвост, залп, кода, образ) не разрывается.
        _mp = _pm_s.split("\n\n")
        _tot = sum(len(p.split()) for p in _mp) or 1
        _acc, _cut = 0, max(1, len(_mp) - 1)
        for _pi, _pp in enumerate(_mp):
            _acc += len(_pp.split())
            if _acc >= _tot * 0.85:
                _cut = max(1, _pi)
                break
        merged_script = "\n\n".join(_mp[:_cut]) + "\n\n" + _pc_s + "\n\n" + "\n\n".join(_mp[_cut:])
    else:
        merged_script = _pm_s + "\n\n" + _pc_s
    obj = {}
    try:
        raw_obj = json.loads(main_raw[main_raw.find("{"): main_raw.rfind("}") + 1])
        if isinstance(raw_obj, dict):
            obj = dict(raw_obj)
    except Exception:
        obj = {}
    if not obj:
        obj = {"preview": pm.get("preview", ""), "title": pm.get("title", ""), "log_update": pm.get("log_update", [])}
    obj["script"] = merged_script
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return main_raw


@redact_provider_output
def call_claude_conversation(
    turn_prompts: List[str],
    system_prompt: str,
    model: str,
    oauth_token: str,
    log_queue: queue.Queue,
    task_info: str = "",
    target_words_per_turn: int = 0,
    max_recovery_turns: int = 2,
    progress_cb=None,
    hard_cap_sec: int = 0,
    _credential_leased: bool = False,
) -> Tuple[bool, List[str], Optional[str]]:
    """Обёртка: глобальный кап на claude-CLI (FIX-2026-07-30 P1 №11).

    Слот берётся ПОСЛЕ лизы credential'а — тот же порядок, что в
    ``call_api_universal`` (лиза → слот). Обратный порядок в двух путях дал бы
    классический дедлок «один держит лизу и ждёт слот, другой наоборот».
    """
    kwargs = dict(
        turn_prompts=turn_prompts,
        system_prompt=system_prompt,
        model=model,
        oauth_token=oauth_token,
        log_queue=log_queue,
        task_info=task_info,
        target_words_per_turn=target_words_per_turn,
        max_recovery_turns=max_recovery_turns,
        progress_cb=progress_cb,
        hard_cap_sec=hard_cap_sec,
        _credential_leased=_credential_leased,
    )
    if not _credential_leased:
        # Ветка лизы: сам CLI здесь не запускается, слот не занимаем.
        return _call_claude_conversation_impl(**kwargs)
    try:
        with _claude_cli_slot():
            return _call_claude_conversation_impl(**kwargs)
    except _ClaudeSlotUnavailable as exc:
        return False, [], str(exc)


def _call_claude_conversation_impl(
    turn_prompts: List[str],
    system_prompt: str,
    model: str,
    oauth_token: str,
    log_queue: queue.Queue,
    task_info: str = "",
    target_words_per_turn: int = 0,   # PATCH-2026-06-27-length: >0 включает in-conversation recovery до коридора
    max_recovery_turns: int = 2,
    progress_cb=None,   # FIX-2026-07-08-genprogress: callback(turn_no, total_turns, words) после каждого чанка
    hard_cap_sec: int = 0,  # PATCH-2026-07-20-judge: >0 переопределяет per-turn hard cap (короткие служебные вызовы — судья)
    _credential_leased: bool = False,
) -> Tuple[bool, List[str], Optional[str]]:
    """Multi-turn разговор с Claude через ClaudeSDKClient (continue-conversation).

    Открывает ОДИН ClaudeSDKClient → проходит turn_prompts последовательно. Модель
    видит ВЕСЬ предыдущий разговор сама — никаких previous_tail или re-вставки preset.
    Кэш промпта (system + первый turn) переиспользуется на turn 2+ → дешевле.

    Защиты (как в call_claude_subscription):
      - per-turn stream-data watchdog STREAM_DATA_TIMEOUT_SEC (тишина внутри turn'а)
      - per-turn hard cap SUBSCRIPTION_CALL_TIMEOUT_SEC
      - env per-call через ClaudeCodeOptions.env (не os.environ)
      - forensics _collect_hung_cli_forensics при stall
      - TaskGroup unwrap через _find_timeout_error

    Стратегия retry: **abandon-to-partial** (см. отчёт 2026-06-03):
      если turn K заглох — НЕ replay (сессия может быть в битом состоянии),
      закрываем client, возвращаем done_count=K-1 ответов + error для caller'а.

    Returns:
      (success_all, raw_responses, error_msg_if_any)
      success_all=True  → len(raw_responses) == len(turn_prompts)
      success_all=False → len(raw_responses) = число завершённых turn'ов (0..N-1), error_msg описывает причину
    """
    if not _credential_leased:
        try:
            alt = alt_route_for("claude")
        except Exception as exc:
            return False, [], f"{ALT_ERROR_PREFIX} {exc}"
        if alt:
            # FIX-2026-09-17-alt-route: слот держится на весь разговор, лизы OAuth нет.
            alt_model = _alt_model(model)
            try:
                with _alt_slot("claude", alt_model):
                    return call_claude_conversation(
                        turn_prompts=turn_prompts,
                        system_prompt=system_prompt,
                        model=alt_model,
                        oauth_token=ALT_ROUTE_TOKEN,
                        log_queue=log_queue,
                        task_info=task_info,
                        target_words_per_turn=target_words_per_turn,
                        max_recovery_turns=max_recovery_turns,
                        progress_cb=progress_cb,
                        hard_cap_sec=hard_cap_sec,
                        _credential_leased=True,
                    )
            except RuntimeError as exc:
                return False, [], str(exc)
        try:
            ensure_provider_execution_allowed("claude")
            # BYOK-2026-07-30: own tenant token if configured, else server pool.
            lease = get_effective_credential_pool("claude").acquire(timeout=300.0)
        except (CredentialPoolExhausted, RuntimeError) as exc:
            return False, [], str(exc)
        with lease as server_token:
            return call_claude_conversation(
                turn_prompts=turn_prompts,
                system_prompt=system_prompt,
                model=model,
                oauth_token=server_token,
                log_queue=log_queue,
                task_info=task_info,
                target_words_per_turn=target_words_per_turn,
                max_recovery_turns=max_recovery_turns,
                progress_cb=progress_cb,
                hard_cap_sec=hard_cap_sec,
                _credential_leased=True,
            )
    if not CLAUDE_CODE_SDK_AVAILABLE:
        return False, [], "claude-code-sdk не установлен"
    if not turn_prompts:
        return False, [], "no turn_prompts"

    oauth_token = "".join((oauth_token or "").split())
    if oauth_token.lower().startswith("bearer"):
        oauth_token = oauth_token[6:]
    if not oauth_token:
        return False, [], "OAuth токен пустой"

    if log_queue:
        log_msg(log_queue, f"  💬 Conversation: {len(turn_prompts)} turn(s), model={model}")
        # M6 PATCH-2026-06-25-secfix: не логируем хвост секрета и точную длину (раньше last10 + len).
        log_msg(log_queue, "  🔀 Через альт-ключ" if oauth_token == ALT_ROUTE_TOKEN
                else "  🔍 OAuth токен загружен из серверных настроек")

    options, claude_child_env, claude_isolation_root = _claude_isolated_options(
        oauth_token,
        system_prompt=system_prompt if system_prompt else None,
        model=model,
        max_turns=max(
            1,
            len(turn_prompts) * (max(0, int(max_recovery_turns)) + 1),
        ),
        include_partial_messages=True,
    )

    raw_responses: List[str] = []
    error_msg: Optional[str] = None

    async def _run():
        nonlocal error_msg
        client = _SanitizedClaudeSDKClient(
            options=options,
            process_env=claude_child_env,
            isolation_root=claude_isolation_root,
        )
        try:
            await client.connect()
        except Exception as e:
            error_msg = f"connect failed: {type(e).__name__}: {e}"
            if log_queue:
                log_msg(log_queue, f"  ❌ Conversation connect failed: {error_msg}")
            return

        try:
            # PATCH-2026-06-27-length: один отправитель turn'а (переиспользуется для recovery-turn'ов).
            async def _send_one(prompt_text, turn_label):
                ensure_provider_execution_allowed("claude")
                last_data_time = [time.monotonic()]
                iterate_done = anyio.Event()
                turn_text: List[str] = []
                # FIX-2026-09-17-convdup: как got_assistant в call_claude_subscription —
                # ResultMessage.result повторяет текст AssistantMessage этого же хода
                # («Париж.Париж.»), поэтому берём его ТОЛЬКО если текста ассистента не было.
                got_assistant = [False]

                async def _stream_watchdog():
                    while not iterate_done.is_set():
                        with anyio.move_on_after(15):
                            await iterate_done.wait()
                            return
                        silence = time.monotonic() - last_data_time[0]
                        if silence > CONV_STREAM_DATA_TIMEOUT_SEC:
                            raise TimeoutError(
                                f"stream-data watchdog {turn_label}: no data for {int(silence)}s "
                                f"(known CLI hang — anthropics/claude-code#25979)"
                            )

                async def _do_turn():
                    try:
                        await client.query(prompt_text)
                        async for message in client.receive_response():
                            last_data_time[0] = time.monotonic()
                            msg_type = type(message).__name__
                            if msg_type == "AssistantMessage":
                                content = getattr(message, "content", None)
                                if isinstance(content, str):
                                    turn_text.append(content)
                                    if content:
                                        got_assistant[0] = True
                                elif isinstance(content, list):
                                    for block in content:
                                        text = getattr(block, "text", None)
                                        if text and isinstance(text, str):
                                            turn_text.append(text)
                                            got_assistant[0] = True
                            elif msg_type == "ResultMessage" and not got_assistant[0]:
                                result = getattr(message, "result", None)
                                if result and isinstance(result, str):
                                    turn_text.append(result)
                    finally:
                        iterate_done.set()

                # per-turn watchdog + hard cap (PATCH-2026-07-20-judge: hard_cap_sec>0 переопределяет)
                with anyio.fail_after(hard_cap_sec if hard_cap_sec > 0 else SUBSCRIPTION_CALL_TIMEOUT_SEC):
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(_stream_watchdog)
                        await _do_turn()
                return "".join(turn_text)

            for turn_idx, prompt in enumerate(turn_prompts):
                turn_no = turn_idx + 1
                if log_queue:
                    log_msg(log_queue, f"  💬 Turn {turn_no}/{len(turn_prompts)} → query (prompt_len={len(prompt)})")

                raw = await _send_one(prompt, f"turn {turn_no}")
                raw_responses.append(raw)
                if log_queue:
                    _scl = len(raw)
                    try:
                        import json as _json
                        _obj = _json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
                        if isinstance(_obj, dict) and isinstance(_obj.get("script"), str):
                            _scl = len(_obj["script"])
                    except Exception:
                        pass
                    log_msg(log_queue, f"  ✅ Turn {turn_no}/{len(turn_prompts)} done: {_scl} chars (script) / {len(raw)} raw")

                # PATCH-2026-06-28-words: in-conversation recovery — дожать ТОЛЬКО если короче >20% (floor 0.8, как рерайт). Меряем «Слов».
                if target_words_per_turn and target_words_per_turn > 0:
                    _floor_k = float(os.environ.get("GEN_RECOVERY_FLOOR", "0.8"))
                    _lo = int(target_words_per_turn * _floor_k)
                    _hi = int(target_words_per_turn * 1.15)
                    _is_last_turn = (turn_idx == len(turn_prompts) - 1)
                    _rec = 0
                    while _rec < max_recovery_turns:
                        _w = _gen_count_script_words(raw)
                        if _w < 0 or _w >= _lo:
                            break
                        _rec += 1
                        _deficit = _lo - _w
                        # outro-aware: последний turn должен ЗАКРЫТЬ аутро в конце продолжения (иначе аутро уедет в середину).
                        if _is_last_turn:
                            # PATCH-2026-07-18-v5: не заказывать ВТОРОЙ именной sign-off (порождал двойной финал).
                            _close = ("Your continuation will be INSERTED BEFORE the existing ending automatically. "
                                      "Therefore write PURE middle-story material only: new scenes, aftermath facts, "
                                      "concrete period texture. Do NOT write any ending, sign-off, closing image, "
                                      "recap, punchline or farewell — the script already has its ending and it will "
                                      "remain the final words.")
                        else:
                            _close = "Do NOT write any closing or sign-off yet."
                        _rp = (
                            f"Your previous part is only {_w} words, but it MUST reach at least {_lo} words "
                            f"(target band {_lo}-{_hi}). CONTINUE the SAME part, picking up seamlessly right where it "
                            f"ended. Add about {_deficit} more words of real substance — more depth, concrete detail, an "
                            f"extra scene or development that belongs here. Do NOT repeat anything already written, do NOT "
                            f"start a new section/chunk. EVERY added paragraph must contain at least one element not yet "
                            f"in the text — a new fact, name, detail, scene development or period texture; re-telling or "
                            f"summarizing does NOT count toward the word target — if verified facts are exhausted, extend "
                            f"the CURRENT scene with concrete sensory period detail instead. {_close} "
                            f"Output ONLY JSON: {{\"script\": \"<the additional continuation text only>\"}}."
                        )
                        if log_queue:
                            log_msg(log_queue, f"  ↪ length recovery {_rec}/{max_recovery_turns} (turn {turn_no}): {_w}<{_lo} words → +{_deficit}")
                        # FIX-2026-06-29: дожим НЕ-фатальный — если _send_one/merge падают (в т.ч. SDK
                        # «cannot unpack non-iterable NoneType» на recovery-turn'е) → оставляем текущую длину,
                        # НЕ роняем всю джобу. Ловим BaseException (anyio оборачивает в ExceptionGroup).
                        try:
                            _cont = await _send_one(_rp, f"turn {turn_no} recovery {_rec}")
                            raw = _gen_merge_continuation(raw, _cont, insert_before_last_para=_is_last_turn)
                            raw_responses[-1] = raw
                        except (KeyboardInterrupt, SystemExit):
                            raise
                        except BaseException as _rec_e:
                            if log_queue:
                                log_msg(log_queue, f"  ⚠️ recovery {_rec} (turn {turn_no}) сбой ({type(_rec_e).__name__}: {str(_rec_e)[:70]}) — оставляю текущую длину, джобу НЕ роняю")
                            break
                    if log_queue and _rec > 0:
                        log_msg(log_queue, f"  ✅ after recovery (turn {turn_no}): {_gen_count_script_words(raw)} words / band {_lo}-{_hi}")

                # FIX-2026-07-08-genprogress: шаг прогресс-бара по завершении чанка (turn'а) — не после всей беседы.
                if progress_cb:
                    try:
                        progress_cb(turn_no, len(turn_prompts), _gen_count_script_words(raw))
                    except Exception:
                        pass
        finally:
            try:
                await client.disconnect()
            except Exception as e:
                if log_queue:
                    log_msg(log_queue, f"  ⚠️ disconnect: {e}")

    # Preflight CLI check — one-time cache (2026-06-04)
    _ensure_cli_ready(log_queue)

    # Semaphore: общий лимит claude subprocess'ов. Держим вокруг ВСЕГО цикла включая
    # forensics+pkill при stall — иначе зомби-CLI жив, а слот свободен → лишний CLI.
    # FIX-2026-07-30 (P0 deadlock): через _claude_cli_slot() — слот уже взят
    # обёрткой call_claude_conversation (ветка _credential_leased=True) в этом же
    # потоке, повторный acquire голого семафора вешал поток навсегда.
    outer_e = None
    with _claude_cli_slot():
        try:
            anyio.run(_run)
        except BaseException as e:
            outer_e = e
            _to_inner = _find_timeout_error(e)
            if _to_inner is not None:
                # forensics+pkill INSIDE семафора (зомби не занимает свободный слот)
                try:
                    _collect_hung_cli_forensics(log_queue, f"{task_info or 'conv'}_turn{len(raw_responses) + 1}")
                except Exception:
                    pass
                # M2 FIX-2026-07-02: НЕ pattern-pkill. `pkill -P <pid> -f claude` под общим
                # PID убил бы claude-субпроцессы СОСЕДНИХ джоб (cross-job SIGKILL), а PID
                # именно этого зависшего вызова достоверно не вычленить (forensics-pgrep
                # видит всех детей). Полагаемся на закрытие субпроцесса самим SDK/anyio при
                # отмене task group — как no-pkill-transient путь в call_claude_subscription.

    # вышли из семафора — обрабатываем исход
    if outer_e is None:
        if error_msg:
            return False, raw_responses, error_msg
        if len(raw_responses) != len(turn_prompts):
            return False, raw_responses, f"completed {len(raw_responses)}/{len(turn_prompts)} turns"
        return True, raw_responses, None

    _to_err = _find_timeout_error(outer_e)
    done_count = len(raw_responses)
    if _to_err is not None:
        # abandon-to-partial: НЕ пытаемся replay (сессия может быть битой)
        reason = str(_to_err) if str(_to_err) else f"hard cap {SUBSCRIPTION_CALL_TIMEOUT_SEC}s"
        msg = (
            f"Conversation stalled at turn {done_count + 1}/{len(turn_prompts)} ({reason}). "
            f"Completed {done_count} turns. abandon-to-partial."
        )
        if log_queue:
            log_msg(log_queue, f"  ⏱️  {msg}")
        return False, raw_responses, SUBSCRIPTION_TIMEOUT_PREFIX + msg
    # M1 FIX-2026-07-02: не timeout — обрабатываем fall-through (раньше блок был
    # ошибочно вложен в `if _to_err is not None:` ПОСЛЕ return → мёртвый код,
    # а при _to_err is None функция неявно возвращала None → TypeError у вызывающего).
    # KeyboardInterrupt/SystemExit пробрасываем, остальное → корректный 3-кортеж.
    if not isinstance(outer_e, Exception):
        raise outer_e
    msg = f"Conversation error at turn {done_count + 1}: {type(outer_e).__name__}: {outer_e}"
    if log_queue:
        log_msg(log_queue, f"  ❌ {msg}")
    return False, raw_responses, msg


_CODEX_SUBSCRIPTION_SEMAPHORE = threading.Semaphore(
    max(1, int(os.environ.get("REWRITE_CODEX_CONCURRENCY", "32")))
)


_CODEX_TEXT_ONLY_CONFIG = [
    "--strict-config",
    "--disable",
    "shell_tool",
    "--disable",
    "unified_exec",
    "--disable",
    "multi_agent",
    "--disable",
    "apps",
    "--disable",
    "remote_plugin",
    "--disable",
    "browser_use",
    "--disable",
    "computer_use",
    "--disable",
    "image_generation",
    "--disable",
    "goals",
    "--disable",
    "shell_snapshot",
    "-c",
    'approval_policy="never"',
    "-c",
    'sandbox_mode="read-only"',
    "-c",
    'shell_environment_policy.inherit="none"',
    "-c",
    "shell_environment_policy.include_only=[]",
    "-c",
    'web_search="disabled"',
]
_CODEX_AUTH_COPY_LOCKS: Dict[str, Any] = {}
_CODEX_AUTH_COPY_LOCKS_GUARD = threading.Lock()


def _secure_copy_file(source: str, destination: str) -> None:
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with open(source, "rb") as src, os.fdopen(fd, "wb") as dst:
            fd = -1
            shutil.copyfileobj(src, dst)
    finally:
        if fd >= 0:
            os.close(fd)


def _prepare_codex_isolation(
    isolation_root: str,
    leased_codex_home: str,
) -> Tuple[str, str, Dict[str, str]]:
    home_dir = os.path.join(isolation_root, "home")
    work_dir = os.path.join(isolation_root, "work")
    auth_dir = os.path.join(isolation_root, "codex-auth")
    for directory in (home_dir, work_dir, auth_dir):
        os.makedirs(directory, mode=0o700, exist_ok=True)
    isolated_codex_home = ""
    if leased_codex_home:
        source_auth = os.path.join(leased_codex_home, "auth.json")
        if not os.path.isfile(source_auth):
            raise RuntimeError("CODEX_AUTH: leased CODEX_HOME has no auth.json")
        with open(source_auth, "r", encoding="utf-8") as source_fh:
            source_payload = json.load(source_fh)
        try:
            validate_codex_auth_document(source_payload)
        except ValueError as exc:
            raise RuntimeError(f"CODEX_AUTH: leased auth.json is invalid: {exc}") from exc
        _register_server_secret_tree(source_payload)
        _secure_copy_file(source_auth, os.path.join(auth_dir, "auth.json"))
        isolated_codex_home = auth_dir
    child_env = build_provider_child_env(
        "codex",
        isolated_home=home_dir,
        codex_home=isolated_codex_home,
    )
    return work_dir, isolated_codex_home, child_env


def _codex_file_digest(path: str) -> str:
    """SHA-256 файла; "" — файла нет или не прочитался."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def _codex_refresh_possible(codex_home: str) -> bool:
    """Может ли ЭТОТ вызов задеть ротацию refresh-токена → нужен полный замок.

    Полный замок на весь вызов CLI сериализует канал начисто: ёмкость Codex у
    рерайтов была 1 при любых настройках, и задача из трёх языков ждала сама себя
    (у одного партнёра 36 кусков отвалились по «codex credential lease timeout»).
    При этом ротация — событие редкое: access_token живёт неделями, а auth.json
    не меняется сотнями вызовов подряд.

    Поэтому замок берём только у границы обновления токена. Всё остальное время
    вызовы идут параллельно, а возврат обновлённого auth.json защищён отдельно —
    compare-and-swap в ``_sync_codex_auth_back``: более старый вызов не может
    затереть более новый токен.

    Порог — ``REWRITE_CODEX_REFRESH_MARGIN_H`` (48 ч). 0 возвращает прежнее
    поведение «замок всегда». Не смогли прочитать срок — считаем, что ротация
    возможна (перестраховка).
    """
    raw = os.environ.get("REWRITE_CODEX_REFRESH_MARGIN_H")
    try:
        margin = float(raw) if raw else 48.0
    except ValueError:
        margin = 48.0
    if margin <= 0:
        return True
    import base64 as _b64                      # в модуле не импортирован
    try:
        with open(os.path.join(codex_home, "auth.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        token = ((data.get("tokens") or {}).get("access_token") or "").strip()
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(_b64.urlsafe_b64decode(payload)).get("exp")
        if not exp:
            return True
        return (float(exp) - time.time()) < margin * 3600.0
    except Exception:                                           # noqa: BLE001
        return True


def _codex_auth_copy_lock(codex_home: str):
    key = os.path.realpath(codex_home)
    with _CODEX_AUTH_COPY_LOCKS_GUARD:
        return _CODEX_AUTH_COPY_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _codex_auth_lifecycle_lock(codex_home: str):
    """Serialize a complete auth lifecycle across threads and OS processes."""
    thread_lock = _codex_auth_copy_lock(codex_home)
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


def _sync_codex_auth_back(
    isolated_codex_home: str,
    leased_codex_home: str,
    original_digest: str = "",
) -> None:
    """Вернуть обновлённый Codex'ом auth.json в общий каталог — только если он свой.

    ``original_digest`` — то, что было в каталоге на момент копирования внутрь.
    Два следствия, обязательных для параллельных вызовов:
      * токен не менялся → не пишем вовсе (это подавляющее большинство вызовов);
      * каталог успел измениться, пока мы работали → НЕ затираем: там уже более
        свежий токен, а наш устарел. Иначе долгий вызов откатывал бы семью токенов.
    """
    if not isolated_codex_home or not leased_codex_home:
        return
    source = os.path.join(isolated_codex_home, "auth.json")
    destination = os.path.join(leased_codex_home, "auth.json")
    if original_digest and _codex_file_digest(source) == original_digest:
        return
    with open(source, "r", encoding="utf-8") as fh:
        parsed = json.load(fh)
    try:
        validate_codex_auth_document(parsed)
    except ValueError as exc:
        raise RuntimeError(f"CODEX_AUTH: refreshed auth.json is invalid: {exc}") from exc
    _register_server_secret_tree(parsed)
    # Замок берём межпроцессный: каталог общий с gen-сервером, и одного
    # threading-замка мало. Он нужен ТОЛЬКО здесь — на редкой записи, а не на
    # каждом вызове CLI, поэтому параллельности не мешает.
    with _codex_auth_lifecycle_lock(leased_codex_home):
        if original_digest and _codex_file_digest(destination) != original_digest:
            return                      # каталог обновил кто-то другой — он новее
        temp_path = os.path.join(
            leased_codex_home,
            f".auth.json.rewrite-{os.getpid()}-{threading.get_ident()}-"
            f"{time.time_ns()}",
        )
        fd = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with open(source, "rb") as src, os.fdopen(fd, "wb") as dst:
                fd = -1
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(temp_path, destination)
            try:
                directory_fd = os.open(leased_codex_home, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    _persist_refreshed_codex_auth(leased_codex_home, source)


def _persist_refreshed_codex_auth(leased_codex_home: str, source_path: str) -> None:
    """Вернуть обновлённый Codex'ом ``auth.json`` тенанта обратно в БД.

    FIX-2026-07-30 (P2 №3): раньше обновлённый токен писался ТОЛЬКО в файловый
    CODEX_HOME и никогда не попадал в ``user_keys.codex_auth``. Каталог живёт вне
    БД (и раньше — прямо в ``/tmp``), поэтому после чистки/переезда тенант
    оказывался разлогинен со старым (уже отозванным) refresh-токеном в
    настройках. Серверных homes это не касается — только своих, пользовательских.
    """
    owner = _codex_home_owner(leased_codex_home)
    if not owner:
        return
    try:
        refreshed = _read_private_file(source_path).strip()
        if not refreshed:
            return
        validate_codex_auth_document(json.loads(refreshed))
        _register_server_secret_tree(_codex_secret_strings(refreshed))
        stamp_path = os.path.join(leased_codex_home, ".rewrite_source_sha256")
        expected_hash = _read_private_file(stamp_path).strip()
        identity = _CODEX_HOME_AUTH_IDENTITIES.get(os.path.realpath(leased_codex_home), "")
        with _KEYS_WRITE_LOCK:
            current_raw = get_user_codex_auth(owner)
            current = json.loads(current_raw) if current_raw else None
            payloads = _codex_payloads(current_raw)
            match = next((i for i, item in enumerate(payloads)
                          if _codex_account_identity(item) == identity), None)
            if match is None or hashlib.sha256(payloads[match].encode()).hexdigest() != expected_hash:
                return  # account removed/replaced while this call was running
            if isinstance(current, list):
                docs = [json.loads(item) for item in payloads]
                docs[match] = json.loads(refreshed)
                updated = json.dumps(docs, ensure_ascii=False, sort_keys=True)
                saved_payload = json.dumps(docs[match], ensure_ascii=False, sort_keys=True)
            else:
                updated = saved_payload = refreshed
            with closing(sqlite3.connect(SESSIONS_DB_PATH, timeout=5.0)) as conn, conn:
                cur = conn.execute("UPDATE user_keys SET codex_auth=?,updated_at=? WHERE user_id=? AND codex_auth=?",
                                   (updated, time.time(), owner, current_raw))
                if cur.rowcount != 1:
                    return
        _write_private_file(stamp_path, hashlib.sha256(saved_payload.encode()).hexdigest())
    except Exception as exc:  # хранилище недоступно — вызов не валим
        print(f"[codex_auth] cannot persist refreshed auth for {owner}: {exc}")


@contextmanager
def _codex_isolated_call(
    leased_codex_home: str,
    prefix: str,
):
    # Refresh tokens are single-use on some auth backends. Serialize the full
    # copy-in/call/copy-back lifecycle per leased home to prevent stale writes.
    call_lock = (
        _codex_auth_lifecycle_lock(leased_codex_home)
        if leased_codex_home and _codex_refresh_possible(leased_codex_home)
        else nullcontext()
    )
    with call_lock:
        with tempfile.TemporaryDirectory(prefix=prefix) as isolation_root:
            work_dir, isolated_auth, child_env = _prepare_codex_isolation(
                isolation_root,
                leased_codex_home,
            )
            original_digest = (
                _codex_file_digest(os.path.join(isolated_auth, "auth.json"))
                if isolated_auth
                else ""
            )
            try:
                yield isolation_root, work_dir, isolated_auth, child_env
            finally:
                _sync_codex_auth_back(
                    isolated_auth, leased_codex_home, original_digest
                )


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
        for candidate in root.glob(
            "codex-linux-*/vendor/*/codex-resources/bwrap"
        ):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return ""


def _wrap_codex_linux_sandbox(
    command: List[str],
    isolation_root: str,
    work_dir: str,
    codex_bin: str,
) -> List[str]:
    """Outer filesystem boundary; Codex's inner profile blocks tool network."""
    if not sys.platform.startswith("linux"):
        return command
    bwrap = _find_codex_bwrap()
    if not bwrap:
        raise RuntimeError("CODEX_ISOLATION: bubblewrap is unavailable")
    real_bin = os.path.realpath(codex_bin)
    if not any(
        real_bin == prefix or real_bin.startswith(prefix + os.sep)
        for prefix in ("/usr", "/bin", "/lib", "/lib64")
    ):
        raise RuntimeError(
            "CODEX_ISOLATION: codex binary is outside the minimal runtime"
        )
    wrapped = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/etc",
        "--dir",
        "/var",
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
    ):
        if os.path.exists(path):
            wrapped.extend(["--ro-bind", path, path])
    wrapped.extend(
        [
            "--bind",
            isolation_root,
            isolation_root,
            "--chdir",
            work_dir,
        ]
    )
    false_bin = "/usr/bin/false"
    if os.path.isfile(false_bin):
        for shell_path in (
            "/usr/bin/bash",
            "/usr/bin/dash",
            "/usr/bin/sh",
            "/usr/bin/zsh",
        ):
            if os.path.exists(shell_path):
                wrapped.extend(["--ro-bind", false_bin, shell_path])
    return wrapped + command


def _codex_command_base(
    codex_bin: str,
    work_dir: str,
    *,
    ephemeral: bool,
) -> List[str]:
    command = [
        codex_bin,
        "exec",
        *_CODEX_TEXT_ONLY_CONFIG,
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "-C",
        work_dir,
    ]
    if getattr(_alt_ctx, "active", False):
        command.extend(_alt_codex_flags(getattr(_alt_ctx, "model", "")))
    if ephemeral:
        command.append("--ephemeral")
    return command


# FIX-2026-09-13-models: отказ провайдера в самой модели — не временный сбой.
# 400: «The 'gpt-5.4-mini' model is not supported when using Codex with a ChatGPT account.»
# 404: «The model `gpt-5.5` does not exist or you do not have access to it.»
# «Unable to verify model access right now. Please retry.» — временная, сюда не входит.
CODEX_MODEL_UNAVAILABLE_PREFIX = "CODEX_MODEL_UNAVAILABLE"
_CODEX_MODEL_UNAVAILABLE_RE = re.compile(
    r"not allowed to use the requested model"  # FIX-2026-09-17-alt-route: отказ альт-ключа
    r"|"
    r"model is not supported when using codex"
    r"|\bmodel\b.{0,120}?does not exist or you do not have access"
    r"|\bmodel_not_found\b",
    re.IGNORECASE,
)


def _codex_model_unavailable(
    stderr_text: str, event_errors: Optional[List[str]] = None
) -> str:
    """Строка отказа в модели или "".

    Смотрим только строки самого CLI («ERROR: …») и события --json: в хвост stderr
    codex exec дублирует промпт, а в тексте статьи легко встретить и «400», и
    цитату с теми же словами.
    """
    candidates = [
        line.strip()
        for line in str(stderr_text or "").splitlines()
        if line.strip().startswith("ERROR:")
    ]
    candidates.extend(str(item) for item in (event_errors or []))
    for line in candidates:
        if _CODEX_MODEL_UNAVAILABLE_RE.search(line):
            return line[:300]
    return ""


# FIX-2026-09-13 (log-bloat): Codex печатает в stderr весь присланный промпт — текст
# статьи клиента. В сообщение об ошибке уходили последние 4000 символов stderr, и на
# каждой упавшей части лог задачи рос на кусок статьи (abd7dc2b0c02: 4 МБ за 588
# вызовов на мёртвой модели). Берём суть: строки ERROR/warning (код и текст
# провайдера) и последние строки вывода, остальное вырезаем с пометкой.
_CODEX_ERROR_DETAIL_MAX_CHARS = 1500
_CODEX_ERROR_TAIL_LINES = 3
_CODEX_ERROR_LINE_RE = re.compile(
    r"^\s*(?:\S+Z\s+)?(?:ERROR|WARN)\b"  # ERROR: … и 2026-…Z ERROR codex_api::…
    r"|^\s*(?i:error|warning|fatal)\s*:"  # warning: … / Error: …
    r"|panicked at"
    r"|\"type\"\s*:\s*\"(?:error|turn\.failed)\""  # события codex --json
)


def _compact_codex_error_detail(
    text: str, max_chars: int = _CODEX_ERROR_DETAIL_MAX_CHARS
) -> str:
    """Суть ошибки Codex без эха промпта: короткий вывод — как есть, длинный — сжатый."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    tail_from = max(0, len(lines) - _CODEX_ERROR_TAIL_LINES)
    kept: List[str] = []
    dropped = 0
    for idx, line in enumerate(lines):
        if idx >= tail_from or _CODEX_ERROR_LINE_RE.search(line):
            if dropped:
                kept.append(f"…обрезано {dropped} символов…")
                dropped = 0
            kept.append(line)
        else:
            dropped += len(line) + 1
    detail = "\n".join(kept)
    if len(detail) > max_chars:
        # одна гигантская строка или сотни строк ERROR: начало и конец, середину вон
        head = max_chars // 3
        tail = max_chars - head
        cut = len(detail) - head - tail
        detail = f"{detail[:head]}\n…обрезано {cut} символов…\n{detail[-tail:]}"
    return detail


@redact_provider_output
def call_codex_subscription(
    messages: List[Dict],
    model: str,
    log_queue: Optional[queue.Queue] = None,
    task_info: str = "",
    _credential_leased: bool = False,
    _codex_home_override: str = "",
    images: Optional[List[bytes]] = None,
) -> Tuple[bool, str]:
    """Вызвать GPT через авторизованный Codex CLI, не читая его auth.json.

    `images` — картинки для задач со зрением (анализ обложки). CLI принимает их
    флагом `-i`, это проверено живым вызовом 24.09.2026: картинка с надписью
    «STUDIO 42» была прочитана. Файлы кладутся внутрь той же временной изоляции,
    что и весь вызов, и исчезают вместе с ней. Без картинок путь остаётся прежним
    до последнего флага — текстовые задачи не должны почувствовать разницы.
    """
    if not _credential_leased:
        try:
            alt = alt_route_for("codex")
        except Exception as exc:
            return False, f"CODEX_ERROR: {ALT_ERROR_PREFIX} {exc}"
        if alt:
            try:
                with _alt_slot("codex", model):
                    return call_codex_subscription(
                        messages=messages,
                        model=model,
                        log_queue=log_queue,
                        task_info=task_info,
                        images=images,  # 04.10.2026: картинки терялись на этом повороте
                        _credential_leased=True,
                        _codex_home_override="",
                    )
            except RuntimeError as exc:
                return False, f"CODEX_STOPPED: {exc}"
        try:
            ensure_provider_execution_allowed("codex")
            # BYOK-2026-07-30: своя подписка тенанта, иначе серверная — и только
            # если она вообще разрешена этому тенанту (задача B).
            pool = get_effective_credential_pool("codex")
            lease = pool.acquire(timeout=300.0)
        except (CredentialPoolExhausted, RuntimeError) as exc:
            return False, str(exc)
        with lease as codex_home:
            result = call_codex_subscription(
                messages=messages,
                model=model,
                log_queue=log_queue,
                task_info=task_info,
                images=images,  # 04.10.2026: картинки терялись на этом повороте
                _credential_leased=True,
                _codex_home_override=codex_home,
            )
        _note_codex_result(codex_home, result[0], result[-1])
        return _codex_limit_to_cooldown(pool, codex_home, result)
    codex_bin_setting = os.environ.get("REWRITE_CODEX_BIN", "codex")
    codex_bin = shutil.which(codex_bin_setting)
    if not codex_bin and os.path.isfile(codex_bin_setting):
        codex_bin = codex_bin_setting
    if not codex_bin:
        return False, "CODEX_ERROR: codex CLI not found (set REWRITE_CODEX_BIN)"

    actual_model = model.split(":", 1)[1] if model.lower().startswith("codex:") else model
    timeout_sec = max(10, int(os.environ.get("REWRITE_CODEX_TIMEOUT", "600")))
    prompt_parts = [
        "Act only as a text-generation engine. Do not inspect files, call tools, or modify anything. "
        "Follow the messages below and return only the requested final content."
    ]
    for message in messages:
        role = str(message.get("role", "user")).upper()
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        prompt_parts.append(f"\n<{role}>\n{content}\n</{role}>")
    prompt = "\n".join(prompt_parts)

    acquired = False
    started = time.monotonic()
    while not acquired:
        if STOP_FLAG.is_stopped():
            return False, "CODEX_STOPPED: stopped by user"
        acquired = _CODEX_SUBSCRIPTION_SEMAPHORE.acquire(timeout=0.25)
        if not acquired and time.monotonic() - started >= timeout_sec:
            return False, "CODEX_TIMEOUT: timed out waiting for Codex slot"

    try:
        if log_queue:
            log_msg(log_queue, f"🤖 Codex {'через альт-ключ' if getattr(_alt_ctx, 'active', False) else 'subscription'}: model={actual_model}, task={task_info or '-'}")
        with _codex_isolated_call(
            _codex_home_override,
            "rewrite-codex-isolated-",
        ) as (isolation_root, work_dir, _isolated_auth, child_env):
            output_path = os.path.join(work_dir, "final.txt")
            stdout_path = os.path.join(work_dir, "stdout.log")
            stderr_path = os.path.join(work_dir, "stderr.log")
            cmd = _codex_command_base(
                codex_bin,
                work_dir,
                ephemeral=True,
            ) + [
                "-m",
                actual_model,
                "-o",
                output_path,
            ]
            # Картинки — файлами рядом, внутри изоляции: CLI берёт их флагом `-i`.
            # Их содержимое в лог не попадает, только количество.
            for number, blob in enumerate(images or [], 1):
                image_path = os.path.join(work_dir, f"image-{number}.png")
                with open(image_path, "wb") as fh:
                    fh.write(blob)
                cmd += ["-i", image_path]
            # 04.10.2026: `-i/--image <FILE>...` принимает НЕСКОЛЬКО значений и съедал
            # следующий `-` как ещё одну картинку — модель отвечала «картинки не
            # переданы», и сверка надписи обложек не работала ни разу. `--` закрывает
            # список флагов, после него `-` снова значит «задание из stdin».
            if images:
                cmd.append("--")
            cmd.append("-")
            cmd = _wrap_codex_linux_sandbox(
                cmd,
                isolation_root,
                work_dir,
                codex_bin,
            )
            with open(stdout_path, "w", encoding="utf-8") as stdout_file, open(
                stderr_path, "w", encoding="utf-8"
            ) as stderr_file:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=stdout_file, stderr=stderr_file,
                    text=True, env=child_env, cwd=work_dir,
                )
                assert proc.stdin is not None
                proc.stdin.write(prompt)
                proc.stdin.close()
                deadline = time.monotonic() + timeout_sec
                while proc.poll() is None:
                    try:
                        ensure_provider_execution_allowed("codex")
                    except RuntimeError:
                        proc.terminate()
                        proc.wait(timeout=5)
                        return False, "CODEX_STOPPED: execution cancelled"
                    if STOP_FLAG.is_stopped() or time.monotonic() >= deadline:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=5)
                        if STOP_FLAG.is_stopped():
                            return False, "CODEX_STOPPED: stopped by user"
                        return False, f"CODEX_TIMEOUT: no response within {timeout_sec}s"
                    time.sleep(0.2)
                return_code = proc.returncode

            final_text = ""
            if os.path.exists(output_path):
                with open(output_path, "r", encoding="utf-8", errors="replace") as fh:
                    final_text = fh.read().strip()
            with open(stderr_path, "r", encoding="utf-8", errors="replace") as fh:
                stderr_text = fh.read()[-4000:].strip()
            if return_code == 0 and final_text:
                return True, final_text

            error_lower = stderr_text.lower()
            unavailable = _codex_model_unavailable(stderr_text)
            if unavailable:
                # FIX-2026-09-13-models: модель снята — повтор даст тот же отказ.
                return False, (
                    f"{CODEX_MODEL_UNAVAILABLE_PREFIX}: "
                    f"{codex_model_unavailable_message(model)} [{unavailable}]"
                )
            if any(x in error_lower for x in ("not logged in", "authentication", "unauthorized", "401")):
                prefix = "CODEX_AUTH"
            elif any(x in error_lower for x in ("usage limit", "rate limit", "quota", "limit reached")):
                prefix = "CODEX_LIMIT"
            else:
                prefix = "CODEX_ERROR"
            # класс ошибки — по прежнему хвосту, в сообщение (и лог задачи) — только суть
            detail = _compact_codex_error_detail(stderr_text)
            return False, f"{prefix}: {detail or f'codex exited with code {return_code}'}"
    except Exception as exc:
        return False, f"CODEX_ERROR: {type(exc).__name__}: {exc}"
    finally:
        _CODEX_SUBSCRIPTION_SEMAPHORE.release()


def _codex_json_script(raw: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Best-effort extraction of the first JSON object and its script field."""
    if not raw:
        return None, ""
    stripped = raw.strip()
    obj: Optional[Dict[str, Any]] = None
    try:
        candidate = json.loads(stripped)
        if isinstance(candidate, dict):
            obj = candidate
    except (json.JSONDecodeError, TypeError):
        pass
    if obj is None:
        start = stripped.find("{")
        if start >= 0:
            try:
                candidate, _ = json.JSONDecoder().raw_decode(stripped[start:])
                if isinstance(candidate, dict):
                    obj = candidate
            except (json.JSONDecodeError, TypeError):
                pass
    if obj is None:
        return None, ""
    script = obj.get("script")
    return obj, script if isinstance(script, str) else ""


@redact_provider_output
def call_codex_conversation(
    turn_prompts: List[str],
    system_prompt: str,
    model: str,
    log_queue: Optional[queue.Queue] = None,
    task_info: str = "",
    target_words_per_turn: int = 0,
    max_recovery_turns: int = 2,
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
    _credential_leased: bool = False,
    _codex_home_override: str = "",
) -> Tuple[bool, List[str], Optional[str]]:
    """Run all chunks in one persisted Codex session via ``codex exec resume``."""
    if not _credential_leased:
        try:
            alt = alt_route_for("codex")
        except Exception as exc:
            return False, [], f"CODEX_ERROR: {ALT_ERROR_PREFIX} {exc}"
        if alt:
            try:
                with _alt_slot("codex", model):
                    return call_codex_conversation(
                        turn_prompts=turn_prompts,
                        system_prompt=system_prompt,
                        model=model,
                        log_queue=log_queue,
                        task_info=task_info,
                        target_words_per_turn=target_words_per_turn,
                        max_recovery_turns=max_recovery_turns,
                        progress_cb=progress_cb,
                        _credential_leased=True,
                        _codex_home_override="",
                    )
            except RuntimeError as exc:
                return False, [], f"CODEX_STOPPED: {exc}"
        try:
            ensure_provider_execution_allowed("codex")
            # BYOK-2026-07-30: своя подписка тенанта, иначе серверная (задача B).
            pool = get_effective_credential_pool("codex")
            lease = pool.acquire(timeout=300.0)
        except (CredentialPoolExhausted, RuntimeError) as exc:
            return False, [], str(exc)
        with lease as codex_home:
            result = call_codex_conversation(
                turn_prompts=turn_prompts,
                system_prompt=system_prompt,
                model=model,
                log_queue=log_queue,
                task_info=task_info,
                target_words_per_turn=target_words_per_turn,
                max_recovery_turns=max_recovery_turns,
                progress_cb=progress_cb,
                _credential_leased=True,
                _codex_home_override=codex_home,
            )
        _note_codex_result(codex_home, result[0], result[-1])
        return _codex_limit_to_cooldown(pool, codex_home, result)
    if not turn_prompts:
        return False, [], "no turn_prompts"

    codex_bin_setting = os.environ.get("REWRITE_CODEX_BIN", "codex")
    codex_bin = shutil.which(codex_bin_setting)
    if not codex_bin and os.path.isfile(codex_bin_setting):
        codex_bin = codex_bin_setting
    if not codex_bin:
        return False, [], "CODEX_ERROR: codex CLI not found (set REWRITE_CODEX_BIN)"

    actual_model = model.split(":", 1)[1] if model.lower().startswith("codex:") else model
    timeout_sec = max(10, int(os.environ.get("REWRITE_CODEX_TIMEOUT", "600")))

    acquired = False
    wait_started = time.monotonic()
    while not acquired:
        if STOP_FLAG.is_stopped():
            return False, [], "CODEX_STOPPED: stopped by user"
        acquired = _CODEX_SUBSCRIPTION_SEMAPHORE.acquire(timeout=0.25)
        if not acquired and time.monotonic() - wait_started >= timeout_sec:
            return False, [], "CODEX_TIMEOUT: timed out waiting for Codex slot"

    raw_responses: List[str] = []
    session_id = ""

    def _classify_error(stderr_text: str, event_errors: List[str], return_code: int) -> str:
        detail = ("\n".join(event_errors) or stderr_text or f"codex exited with code {return_code}")[-4000:]
        lowered = detail.lower()
        unavailable = _codex_model_unavailable(stderr_text, event_errors)
        if unavailable:
            # FIX-2026-09-13-models: модель снята — повтор даст тот же отказ.
            return (
                f"{CODEX_MODEL_UNAVAILABLE_PREFIX}: "
                f"{codex_model_unavailable_message(model)} [{unavailable}]"
            )
        if any(x in lowered for x in ("not logged in", "authentication", "unauthorized", "401")):
            prefix = "CODEX_AUTH"
        elif any(x in lowered for x in ("usage limit", "rate limit", "quota", "limit reached")):
            prefix = "CODEX_LIMIT"
        else:
            prefix = "CODEX_ERROR"
        return f"{prefix}: {_compact_codex_error_detail(detail)}"

    try:
        if log_queue:
            log_msg(log_queue, f"  💬 Codex conversation{' (альт-ключ)' if getattr(_alt_ctx, 'active', False) else ''}: {len(turn_prompts)} turn(s), model={actual_model}")
        with _codex_isolated_call(
            _codex_home_override,
            "rewrite-codex-conv-isolated-",
        ) as (isolation_root, work_dir, _isolated_auth, child_env):
            call_no = 0

            def _run_turn(prompt: str, resume_id: str = "") -> Tuple[bool, str, str, str]:
                nonlocal call_no
                try:
                    ensure_provider_execution_allowed("codex")
                except RuntimeError:
                    return False, "", resume_id, "CODEX_STOPPED: execution cancelled"
                call_no += 1
                output_path = os.path.join(work_dir, f"turn-{call_no}.txt")
                stdout_path = os.path.join(work_dir, f"turn-{call_no}.jsonl")
                stderr_path = os.path.join(work_dir, f"turn-{call_no}.stderr")
                cmd = _codex_command_base(
                    codex_bin,
                    work_dir,
                    ephemeral=False,
                )
                if resume_id:
                    cmd += [
                        "resume", "--json", "-m", actual_model,
                        "-o", output_path, resume_id, "-",
                    ]
                else:
                    cmd += [
                        "--json", "-m", actual_model, "-o", output_path, "-",
                    ]
                cmd = _wrap_codex_linux_sandbox(
                    cmd,
                    isolation_root,
                    work_dir,
                    codex_bin,
                )

                with open(stdout_path, "w", encoding="utf-8") as stdout_file, open(
                    stderr_path, "w", encoding="utf-8"
                ) as stderr_file:
                    proc = subprocess.Popen(
                        cmd, stdin=subprocess.PIPE, stdout=stdout_file, stderr=stderr_file,
                        text=True, env=child_env, cwd=work_dir,
                    )
                    try:
                        assert proc.stdin is not None
                        proc.stdin.write(prompt)
                        proc.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                    deadline = time.monotonic() + timeout_sec
                    while proc.poll() is None:
                        if STOP_FLAG.is_stopped() or time.monotonic() >= deadline:
                            proc.terminate()
                            try:
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.wait(timeout=5)
                            reason = "CODEX_STOPPED: stopped by user" if STOP_FLAG.is_stopped() else (
                                f"CODEX_TIMEOUT: no response within {timeout_sec}s"
                            )
                            return False, "", resume_id, reason
                        time.sleep(0.2)
                    return_code = proc.returncode

                final_text = ""
                if os.path.exists(output_path):
                    with open(output_path, "r", encoding="utf-8", errors="replace") as fh:
                        final_text = fh.read().strip()
                with open(stderr_path, "r", encoding="utf-8", errors="replace") as fh:
                    stderr_text = fh.read()[-4000:].strip()

                found_session = resume_id
                event_errors: List[str] = []
                try:
                    with open(stdout_path, "r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if event.get("type") == "thread.started" and event.get("thread_id"):
                                found_session = str(event["thread_id"])
                            if event.get("type") in ("turn.failed", "error"):
                                event_errors.append(json.dumps(event, ensure_ascii=False))
                except OSError:
                    pass

                if return_code == 0 and final_text:
                    return True, final_text, found_session, ""
                return False, final_text, found_session, _classify_error(stderr_text, event_errors, return_code)

            for turn_idx, user_prompt in enumerate(turn_prompts):
                turn_no = turn_idx + 1
                if STOP_FLAG.is_stopped():
                    return False, raw_responses, "CODEX_STOPPED: stopped by user"
                if turn_idx == 0:
                    prompt = (
                        "Act only as a text-generation engine. Do not inspect files, call tools, or modify anything.\n"
                        f"<SYSTEM>\n{system_prompt}\n</SYSTEM>\n\n<USER>\n{user_prompt}\n</USER>"
                    )
                else:
                    prompt = user_prompt
                if log_queue:
                    log_msg(log_queue, f"  💬 Codex turn {turn_no}/{len(turn_prompts)} (prompt_len={len(prompt)})")
                ok, raw, new_session_id, err = _run_turn(prompt, session_id)
                if new_session_id:
                    session_id = new_session_id
                if not ok:
                    return False, raw_responses, (
                        f"Codex conversation failed at turn {turn_no}/{len(turn_prompts)}: {err}"
                    )
                if turn_idx == 0 and len(turn_prompts) > 1 and not session_id:
                    raw_responses.append(raw)
                    return False, raw_responses, "CODEX_ERROR: thread.started did not provide a session id"

                parsed_obj, script = _codex_json_script(raw)
                recovery_no = 0
                lower_bound = int(target_words_per_turn * 0.8) if target_words_per_turn else 0
                while parsed_obj is not None and lower_bound and len(script.split()) < lower_bound and recovery_no < max_recovery_turns:
                    recovery_no += 1
                    deficit = lower_bound - len(script.split())
                    recovery_prompt = (
                        f"The current chunk has {len(script.split())} words and must reach at least {lower_bound}. "
                        f"Continue it seamlessly with about {deficit} additional words of concrete substance. "
                        "Do not restart, recap, or write a conclusion unless this is the final chunk. "
                        "Output ONLY JSON: {\"script\": \"<additional continuation text only>\"}."
                    )
                    rok, rraw, new_session_id, rerr = _run_turn(recovery_prompt, session_id)
                    if new_session_id:
                        session_id = new_session_id
                    if not rok:
                        if log_queue:
                            log_msg(log_queue, f"  ⚠️ Codex length recovery {recovery_no} failed: {rerr[:200]}")
                        break
                    _, addition = _codex_json_script(rraw)
                    if not addition.strip():
                        if log_queue:
                            log_msg(log_queue, f"  ⚠️ Codex length recovery {recovery_no}: empty/invalid JSON")
                        break
                    script = script.rstrip() + "\n\n" + addition.lstrip()
                    parsed_obj["script"] = script
                    raw = json.dumps(parsed_obj, ensure_ascii=False)
                    if log_queue:
                        log_msg(log_queue, f"  ↪ Codex turn {turn_no} recovery {recovery_no}: {len(script.split())} words")

                raw_responses.append(raw)
                word_count = len(script.split()) if script else 0
                if progress_cb:
                    try:
                        progress_cb(turn_no, len(turn_prompts), word_count)
                    except Exception:
                        pass
                if log_queue:
                    log_msg(log_queue, f"  ✅ Codex turn {turn_no}/{len(turn_prompts)}: {word_count} words")

        if len(raw_responses) == len(turn_prompts):
            return True, raw_responses, None
        return False, raw_responses, f"completed {len(raw_responses)}/{len(turn_prompts)} turns"
    except Exception as exc:
        return False, raw_responses, f"CODEX_ERROR: {type(exc).__name__}: {exc}"
    finally:
        _CODEX_SUBSCRIPTION_SEMAPHORE.release()


@redact_provider_output
def call_api_universal(
    messages: List[Dict],
    api_key: str,
    model: str,
    log_queue: queue.Queue,
    task_info: str = "",
    provider: str = "claude",
    oauth_token: str = ""
) -> Tuple[bool, str]:
    """
    Универсальная функция вызова API (Claude, OpenAI, Claude Pro или ChatGPT/Codex).
    
    Args:
        provider: "claude", "openai", "subscription" или "codex_subscription"
        oauth_token: OAuth токен для подписки (только для provider="subscription")
    """
    debug_log(log_queue, "API ROUTE:", f"provider={provider}, model={model}, task={task_info}")
    if provider == "codex_subscription":
        return call_codex_subscription(messages, model, log_queue, task_info)
    if provider == "subscription":
        try:
            alt = alt_route_for("claude")
        except Exception as exc:
            return False, f"{ALT_ERROR_PREFIX} {exc}"
        if alt:
            # FIX-2026-09-17-alt-route: без лизы OAuth, без проб и парковки токенов владельца.
            alt_model = _alt_model(model)
            try:
                with _alt_slot("claude", alt_model):
                    return call_claude_subscription(
                        messages, log_queue, task_info, alt_model, ALT_ROUTE_TOKEN
                    )
            except RuntimeError as exc:
                return False, str(exc)
        try:
            ensure_provider_execution_allowed("claude")
            # BYOK-2026-07-30: own tenant token if configured, else server pool.
            lease = get_effective_credential_pool("claude").acquire(timeout=300.0)
        except (CredentialPoolExhausted, RuntimeError) as exc:
            return False, str(exc)
        with lease as server_token:
            ok, resp = call_claude_subscription(
                messages, log_queue, task_info, model, server_token
            )
        # Лиза отпущена и слот CLI свободен — только теперь зовём пробу: она спавнит
        # второго `claude`, и внутри слота упёрлась бы в собственную ёмкость.
        pool = get_effective_credential_pool("claude")
        if ok:
            if hasattr(pool, "clear_limit"):
                pool.clear_limit(server_token)
            _note_subscription("claude", server_token, "ok")
            return True, resp
        text = str(resp)
        if text.startswith(SUBSCRIPTION_LIMIT_PREFIX):
            return False, text
        state, reset_ts = _probe_token_state(server_token, log_queue)
        if state == "limited":
            reset_ts = reset_ts or (time.time() + _LIMIT_DEFAULT_WAIT_SEC)
            if hasattr(pool, "mark_limited"):
                pool.mark_limited(server_token, reset_ts)
            _note_subscription("claude", server_token, "limit", until=reset_ts)
            log_msg(log_queue,
                    f"    ⏳ окно подписки исчерпано, ключ отдыхает до "
                    f"{time.strftime('%H:%M', time.localtime(reset_ts))} — джоб будет отложен")
            return False, f"{SUBSCRIPTION_LIMIT_PREFIX}reset={int(reset_ts)}|{text}"
        if state == "dead":
            # Протухший токен ждать бессмысленно: короткий бан, дальше решает ротация.
            if hasattr(pool, "mark_limited"):
                pool.mark_limited(server_token, time.time() + 300)
            _note_subscription("claude", server_token, "auth", detail=text)
        return False, text
    elif provider == "claude":
        return call_claude_api(messages, api_key, model, log_queue, task_info, use_caching=True)
    else:
        return call_openai_api(messages, model, api_key, log_queue, task_info)


_TOKEN_LIMIT_CACHE: Dict[str, Tuple[bool, float]] = {}   # token -> (на_лимите?, ts) — кэш пред-проверки
_TOKEN_LIMIT_TTL_SEC = int(os.environ.get("TOKEN_LIMIT_TTL_SEC", "90"))  # сколько доверять пробе (лимит не меняется секунда-в-секунду)


def _token_is_limited(token: str, log_queue=None) -> bool:
    """PROACTIVE-2026-06-28: дешёвая проба `claude -p test` ИМЕННО этим токеном — на лимите/мёртв ли он.
    Кэш на TTL чтоб не пробивать каждый чанк. Проба упала по другой причине → считаем OK (пусть реальный звонок решит)."""
    if not token:
        return False
    now = time.time()
    c = _TOKEN_LIMIT_CACHE.get(token)
    if c and (now - c[1]) < _TOKEN_LIMIT_TTL_SEC:
        return c[0]
    limited = False
    try:
        with tempfile.TemporaryDirectory(prefix="rewrite-claude-probe-") as root:
            home_dir = os.path.join(root, "home")
            work_dir = os.path.join(root, "work")
            os.makedirs(home_dir, mode=0o700)
            os.makedirs(work_dir, mode=0o700)
            child_env = build_provider_child_env(
                "claude",
                credential=token,
                isolated_home=home_dir,
            )
            r = subprocess.run(
                [
                    "claude",
                    "-p",
                    "test",
                    "--tools",
                    "",
                    "--strict-mcp-config",
                    "--disable-slash-commands",
                    "--no-chrome",
                    "--no-session-persistence",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                env=child_env,
                cwd=work_dir,
            )
            out = ((r.stdout or "") + " " + (r.stderr or "")).lower()
            limited = _is_token_switch_signal(out)
    except Exception:
        limited = False
    _TOKEN_LIMIT_CACHE[token] = (limited, now)
    return limited


_TOKEN_STATE_CACHE: Dict[str, Tuple[str, float, float]] = {}   # token -> (state, reset, ts)
_DEAD_TOKEN_MARKERS = ("not logged in", "please run /login", "invalid bearer token",
                       "unauthorized", "authentication_error")


def _probe_token_state(token: str, log_queue=None) -> Tuple[str, float]:
    host = _host_budget.try_acquire("claude_oauth", token, 1) if token else None
    if host is None:
        return "busy", 0.0
    with host:
        return _probe_token_state_reserved(token, log_queue)


def _probe_token_state_reserved(token: str, log_queue=None) -> Tuple[str, float]:
    """Спросить провайдера правду про ЭТОТ токен: ('ok'|'limited'|'dead', время сброса).

    Единственный способ отличить исчерпанное окно от настоящей поломки на пути
    подписки: SDK стирает текст ошибки CLI, а `claude -p test` печатает
    «Claude AI usage limit reached|<unix>» открытым текстом и отдаёт точное время.
    Кэш тот же TTL, что у пред-проверки: лимит не меняется секунда-в-секунду.
    Проба упала по своей причине → 'ok', пусть реальный вызов решает (иначе транзиент
    увёл бы живой токен в кулдаун).
    """
    if not token:
        return "ok", 0.0
    now = time.time()
    c = _TOKEN_STATE_CACHE.get(token)
    if c and (now - c[2]) < _TOKEN_LIMIT_TTL_SEC:
        return c[0], c[1]
    state, reset = "ok", 0.0
    try:
        with tempfile.TemporaryDirectory(prefix="rewrite-claude-probe-") as root:
            home_dir = os.path.join(root, "home")
            work_dir = os.path.join(root, "work")
            os.makedirs(home_dir, mode=0o700)
            os.makedirs(work_dir, mode=0o700)
            child_env = build_provider_child_env(
                "claude", credential=token, isolated_home=home_dir)
            r = subprocess.run(
                ["claude", "-p", "test", "--tools", "", "--strict-mcp-config",
                 "--disable-slash-commands", "--no-chrome", "--no-session-persistence"],
                capture_output=True, text=True, timeout=15, env=child_env, cwd=work_dir,
            )
            out = ((r.stdout or "") + " " + (r.stderr or "")).lower()
            if any(d in out for d in _DEAD_TOKEN_MARKERS):
                state = "dead"
            elif _is_token_switch_signal(out):
                state, reset = "limited", parse_limit_reset(out)
    except Exception:
        state, reset = "ok", 0.0
    _TOKEN_STATE_CACHE[token] = (state, reset, now)
    if state != "ok":
        log_msg(log_queue, f"    🔎 проба токена: {state}"
                           + (f", сброс в {time.strftime('%H:%M', time.localtime(reset))}"
                              if reset else ""))
    return state, reset


@redact_provider_output
def call_api_universal_with_token_rotation(
    messages: List[Dict],
    api_key: str,
    model: str,
    log_queue: queue.Queue,
    task_info: str,
    provider: str,
    oauth_tokens: List[str],
    current_token_index: List[int],
) -> Tuple[bool, str]:
    """
    Вызов API с переключением на другой OAuth-токен при лимите подписки.
    current_token_index — список из одного int (мутабельный), обновляется при переключении.
    """
    if provider != "subscription":
        return call_api_universal(
            messages, api_key, model, log_queue, task_info, provider, ""
        )
    # Legacy oauth_tokens/current_token_index arguments cannot select or seed a
    # credential. Rotation uses fresh leases from the resolved pool:
    # BYOK-2026-07-30 — own tenant tokens first, otherwise the server pool.
    candidates = get_effective_claude_oauth_tokens()
    attempts = max(1, len(dict.fromkeys(candidates)))
    last_response = ""
    for attempt in range(attempts):
        fallback_token = candidates[attempt % len(candidates)] if candidates else ""
        success, response = call_api_universal(
            messages, api_key, model, log_queue, task_info, provider, fallback_token
        )
        if success:
            return True, response
        last_response = response
        if not str(response).startswith(SUBSCRIPTION_LIMIT_PREFIX):
            return False, response
        if log_queue and attempt + 1 < attempts:
            log_msg(
                log_queue,
                f"  🔄 Серверный Claude slot {attempt + 1}/{attempts} на лимите — следующий",
            )
    return False, last_response or "SUBSCRIPTION_LIMIT: server pool unavailable"




def translate_paragraphs_recursive(
    paragraphs: List[str],
    lang_name: str,
    lang_display: str,
    chunk_index: int,
    total_chunks: int,
    channel_name: str,
    host_name: str,
    analysis: Optional[Dict],
    prev_context: str,
    system_prompt: str,
    api_call_fn,
    log_queue: queue.Queue,
    depth: int = 0,
    max_depth: int = 6,
) -> str:
    """
    Рекурсивно переводит блок из нескольких абзацев с гарантией 1:1.

    Логика:
    - Один API-вызов на весь блок. Если число абзацев совпало — возвращаем.
    - Если не совпало — режем pополам по абзацам и рекурсивно переводим каждую половину.
    - Базовый случай: непустой успешный ответ нормализуем в один абзац.
      Ошибку провайдера, пустой ответ и исчерпание глубины не выдаём за перевод.

    Возвращает строку с абзацами через \\n\\n.
    """
    n = len(paragraphs)
    if n == 0:
        return ""
    indent = "      " + "  " * depth
    chunk_text = "\n\n".join(paragraphs)
    user_prompt = get_rewrite_prompt(
        lang_name, chunk_text, chunk_index, total_chunks,
        lang_display, channel_name, host_name, analysis, prev_context,
        preserve_paragraphs=True,
        expected_paragraphs=n,
        strict_retry=(depth > 0),
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    success, response = api_call_fn(messages)
    if success:
        text = extract_from_brackets(response)
        text = normalize_paragraph_separators(text, n)
        if count_paragraphs(text) == n:
            if depth > 0:
                log_msg(log_queue, f"{indent}✅ split d={depth}: {n} абзацев OK")
            return text
        actual = count_paragraphs(text)
        log_msg(log_queue, f"{indent}⚠️ split d={depth}: {actual}/{n} — режу пополам")
    else:
        raise RuntimeError("Paragraph recovery API failed; do not split a provider failure")

    # Базовый случай: сохраняем только непустой успешный ответ.
    if n == 1 or depth >= max_depth:
        # n == 1 — корректный лист, гарантируем 1 абзац (без \n\n внутри).
        # n > 1 при исчерпанной глубине — явная ошибка, не текст с маркерами.
        if n == 1:
            if success:
                text = extract_from_brackets(response).strip()
                if not text:
                    raise RuntimeError("Paragraph recovery returned empty output")
                text = re.sub(r"\n\s*\n+", " ", text).strip()
                return text
            raise RuntimeError("Paragraph recovery API failed; source is not a translation")
        # n > 1: depth исчерпан
        raise RuntimeError("Paragraph recovery exhausted its depth")

    # Делим пополам и рекурсивно
    half = n // 2
    left = translate_paragraphs_recursive(
        paragraphs[:half], lang_name, lang_display, chunk_index * 2, total_chunks * 2,
        channel_name, host_name, analysis, prev_context,
        system_prompt, api_call_fn, log_queue, depth + 1, max_depth,
    )
    right = translate_paragraphs_recursive(
        paragraphs[half:], lang_name, lang_display, chunk_index * 2 + 1, total_chunks * 2,
        channel_name, host_name, analysis, " ".join(re.split(r"(?<=[.!?])\s+", left)[-3:]),  # preserve legacy naming contract; child index controls role
        system_prompt, api_call_fn, log_queue, depth + 1, max_depth,
    )
    return left + "\n\n" + right


# ============== ОДНОПРОХОДНАЯ ЧИСТКА ТЕКСТА (CLEANUP-2026-09-16) ==============
# Зачем: постпроцесс Pass A/B/C/D мёртв — серверный ключ OpenAI пуст, свой ключ есть
# у 7 кабинетов из 20, поэтому все четыре прохода падали на отказе авторизации и молча
# возвращали текст без изменений. Для Claude постпроцесс отключён веткой кода с 03.06.
#
# Что вместо: ОДИН вызов подписки Codex на язык (текст длиннее лимита режется по
# границе абзаца). Модель НЕ переписывает текст — она возвращает список точечных
# замен «было → стало» (JSON); применяет их код, по границам слова, с проверкой
# каждой правки и полным откатом к исходному тексту при любом сомнении.
#
# Замер (8 текстов, 3 языка): 36 с и 13.5К входных токенов на текст, мусор
# распознавания 41 вхождение → 1, длина 100.0-101.4%, число абзацев не изменилось
# ни разу.
#
# Три вещи, которые этот код делает НЕ ради качества текста, а ради сохранности задачи:
#   1. На время чистки с потока снимается метка задачи (_rewrite_job_id). Иначе
#      ожидание общего слота Codex (ёмкость машины — 1) поставило бы пометку
#      «общий лимит ключа занят» на ВСЮ задачу, и соседний язык, который в этот
#      момент спокойно рерайтит через Claude, оборвался бы без повтора.
#   2. Чистка зовётся один раз на (задача, язык): авто-ретрай таски крутит
#      rewrite_single_language до трёх раз, и без этой памяти чистка платила бы
#      за каждый круг.
#   3. Первый же отказ подписки (лимит, нет кредов, снятая модель) гасит чистку
#      для всей задачи — остальные языки не долбят выбранный аккаунт.
#
# Переменные окружения:
#   REWRITE_CLEANUP             — общий выключатель (по умолчанию включено)
#   REWRITE_CLEANUP_PRESERVE    — чистка в режиме сохранения абзацев (по умолчанию включено)
#   REWRITE_CLEANUP_MODEL       — модель (по умолчанию codex:gpt-5.6-sol)
#   REWRITE_CLEANUP_MAX_CALLS   — legacy-потолок (3); mandatory Codex: не меньше числа частей + retry
#   REWRITE_CLEANUP_RETRIES     — mandatory Codex: 0 или 1 повтор (по умолчанию 1)
#   REWRITE_CLEANUP_BUDGET_SEC  — потолок времени на язык (по умолчанию 900)
#   REWRITE_CLEANUP_CHUNK_CHARS — длина куска для одного вызова (по умолчанию 45000)
#   REWRITE_CLEANUP_MIN_CHARS   — короче этого текст не чистим (по умолчанию 400)

# Правок за один вызов: реальному тексту нужно 12-25, 40 — с запасом.
CLEANUP_MAX_FIXES = 40
# Одна правка не имеет права переписать весь текст.
CLEANUP_MAX_OCCURRENCES = 60
# Допустимый рост длины: замер 100.0-101.4%, потолок с запасом на разворачивание
# цифр в слова, но не больше 15% — иначе проверка перестаёт быть проверкой.
CLEANUP_GROWTH_PER_FIX = 60
CLEANUP_GROWTH_MIN_SHARE = 0.06
CLEANUP_GROWTH_MAX_SHARE = 0.15
# Усадка — главный риск (пропало содержание), поэтому порог жёстче роста.
CLEANUP_SHRINK_PER_FIX = 20
CLEANUP_SHRINK_MIN_SHARE = 0.01
CLEANUP_SHRINK_MAX_SHARE = 0.03
# Абсолютный пол на коротком тексте: доля от 400-1000 знаков меньше одной честной
# правки «цифра → слова» (до ~60 знаков), и законная чистка откатывалась бы целиком.
# На боевых длинах (40К+) правят доли, а не пол: 15% и 3% остаются в силе.
CLEANUP_GROWTH_FLOOR = 400
CLEANUP_SHRINK_FLOOR = 60
# Замена короче 60% цитаты — это не правка, а срезание фрагмента.
CLEANUP_MIN_REPLACEMENT_SHARE = 0.6

_CLEANUP_CODE_RE = re.compile(r"\b[A-ZА-Я]{1,6}[- ]?\d+[A-Za-z\-]*\b")
_CLEANUP_DIGIT_RE = re.compile(r"(?<![A-Za-z])\d[\d\s.,-]*")
_CLEANUP_BROKEN_RE = re.compile(r"(?<=\w)\s+([^\W\d_])(?=\s)", re.UNICODE)
_CLEANUP_GLUED_RE = re.compile(r"\b\w{24,}\b")
_CLEANUP_ABBR_RE = re.compile(r"\b[A-Z]{2,6}\b")
# Имя собственное = заглавное слово в середине предложения (после строчной буквы и пробела).
_CLEANUP_PROPER_NAME_RE = re.compile(r"[a-zа-яё]\s+[A-ZА-ЯЁ][\wÀ-ɏ]{2,}")
# Предложение: до конечной пунктуации либо до конца строки.
_CLEANUP_SENTENCE_RE = re.compile(r"[^.!?…\n]*[.!?…]+|[^.!?…\n]+", re.UNICODE)
_CLEANUP_LATIN_RE = re.compile(r"[A-Za-zÀ-ɏ]")
_CLEANUP_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
# Одиночные буквы, которые в наших языках — нормальные слова (pl/cs/ro/sk и др.).
# Без этого фильтра детектор отдаёт модели сотни предлогов «i, w, z, o» с приказом
# «почини каждое», то есть прямо подталкивает склеивать самые частые слова языка.
_CLEANUP_REAL_ONE_LETTER_WORDS = frozenset(
    "aáàâäeéèêiíîoóòôöuúùûüwyzsvkéşăâîєіїяю"
)
# Языки, где кириллица — целевой алфавит (тот же кортеж, что в postprocess_remove_russian_words).
_CLEANUP_CYRILLIC_LANGS = ("russian", "ukrainian", "belarusian", "bulgarian", "macedonian")
# Что нельзя трогать никогда: ссылки и упоминания каналов (контракт брендинга).
_CLEANUP_ALWAYS_PROTECTED = ("http", "www.", "youtube", "@")
_CLEANUP_FALSE_VALUES = ("0", "false", "no", "off", "нет", "")

# Память на процесс: чистка один раз на (задача, язык) и общий выключатель на задачу.
_CLEANUP_STATE_LOCK = threading.Lock()
_CLEANUP_DONE: Dict[str, float] = {}
_CLEANUP_VERIFIED: Dict[str, str] = {}
_QUALITY_JOB_ERRORS: Dict[str, str] = {}
_CLEANUP_JOB_OFF: Dict[str, str] = {}
_CLEANUP_STATE_MAX = 2000
# Отказы, после которых пробовать снова в этой же задаче бессмысленно.
_CLEANUP_FATAL_MARKERS = (
    "CODEX_LIMIT",
    "CODEX_AUTH",
    "CODEX_MODEL_UNAVAILABLE",
    "SUBSCRIPTION_LIMIT",
    "HOST_BUDGET_WAIT",
    "credential is not configured",
    "credential pool is empty",
    "Own-only mode",
    "codex CLI not found",
)

QUALITY_REVIEW_VERSION = "codex-editorial-2026-10-11-v1"


class RewriteQualityError(RuntimeError):
    """A complete paid rewrite remains checkpointed, but is not safe to publish yet."""


def remember_quality_review_error(job_id: str, lang_name: str, base_name: str, reason: str) -> None:
    if job_id:
        _cleanup_state_put(_QUALITY_JOB_ERRORS, "\x00".join((job_id, lang_name, base_name)), str(reason)[:500])


def clear_quality_review_error(job_id: str, lang_name: str, base_name: str) -> None:
    with _CLEANUP_STATE_LOCK:
        _QUALITY_JOB_ERRORS.pop("\x00".join((job_id, lang_name, base_name)), None)


def pop_quality_review_error(job_id: str) -> str:
    """The queue finalizer can surface precise QA failure instead of 'no files'."""
    with _CLEANUP_STATE_LOCK:
        keys = [key for key in _QUALITY_JOB_ERRORS if key.startswith(str(job_id) + "\x00")]
        errors = [_QUALITY_JOB_ERRORS.pop(key) for key in keys]
    return " | ".join(errors)[:1500]


def cleanup_quarantine_unverified_output(output_file: str, output_dir: str) -> str:
    """Keep an old unverified script without exposing it among downloadable .txt outputs."""
    if not output_file or not os.path.isfile(output_file):
        return ""
    root = os.path.realpath(output_dir)
    if os.path.commonpath((root, os.path.realpath(output_file))) != root:
        raise RewriteQualityError("QUALITY_REVIEW_FAILED: old output is outside this job directory")
    quarantine = os.path.join(root, ".unverified")
    os.makedirs(quarantine, exist_ok=True)
    suffix = hashlib.sha256((output_file + str(time.time_ns())).encode()).hexdigest()[:12]
    destination = os.path.join(quarantine, os.path.basename(output_file) + "." + suffix + ".unverified")
    os.replace(output_file, destination)
    if os.path.isfile(output_file + ".quality.json"):
        os.replace(output_file + ".quality.json", destination + ".quality.json")
    return destination


def cleanup_receipt_valid(output_file: str, lang_name: str, channel_name: str, host_name: str) -> bool:
    try:
        with open(output_file, "rb") as source:
            digest = hashlib.sha256(source.read()).hexdigest()
        with open(output_file + ".quality.json", encoding="utf-8") as source:
            receipt = json.load(source)
        return (receipt.get("version") == QUALITY_REVIEW_VERSION
                and receipt.get("sha256") == digest
                and receipt.get("language") == lang_name
                and receipt.get("channel") == channel_name
                and receipt.get("host") == host_name)
    except (OSError, ValueError, TypeError):
        return False


def cleanup_write_receipt(output_file: str, lang_name: str, channel_name: str, host_name: str) -> None:
    with open(output_file, "rb") as source:
        digest = hashlib.sha256(source.read()).hexdigest()
    receipt = {"version": QUALITY_REVIEW_VERSION, "sha256": digest,
               "language": lang_name, "channel": channel_name, "host": host_name}
    temporary = output_file + ".quality.json.tmp"
    with open(temporary, "w", encoding="utf-8") as target:
        json.dump(receipt, target, ensure_ascii=False)
    os.replace(temporary, output_file + ".quality.json")


def _cleanup_flag(name: str, default: bool = True) -> bool:
    """Выключатель через переменную окружения: 0/false/no/off/нет — выключено."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in _CLEANUP_FALSE_VALUES


def _cleanup_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(str(os.environ.get(name, default)).strip()))
    except (TypeError, ValueError):
        return default


def cleanup_enabled(preserve_paragraphs: bool = False) -> bool:
    """Включена ли чистка. Общий выключатель + отдельный для режима сохранения абзацев."""
    if not _cleanup_flag("REWRITE_CLEANUP", True):
        return False
    if preserve_paragraphs:
        return _cleanup_flag("REWRITE_CLEANUP_PRESERVE", True)
    return True


def cleanup_model_name() -> str:
    """Модель чистки. Только подписка Codex: ключа OpenAI на сервере нет."""
    # 02.10.2026 — sol вместо terra, вместе с основной моделью рерайта: владелец на
    # вопрос «вычитку тоже?» ответил «ну если рерайт, то без проблем». Вычитка — это
    # один редакторский проход по уже написанному тексту, и держать его на
    # другой модели значило бы платить за medium-размышление там, где основной текст
    # уже пишется на low. В окружении переменная не задана нигде, поэтому умолчание
    # здесь и есть действующее значение.
    return (os.environ.get("REWRITE_CLEANUP_MODEL") or "codex:gpt-5.6-sol").strip()


def _cleanup_state_put(store: Dict[str, Any], key: str, value: Any) -> None:
    """Запоминает факт с ограничением размера — процесс живёт неделями."""
    with _CLEANUP_STATE_LOCK:
        max_entries = 64 if store is _CLEANUP_VERIFIED else _CLEANUP_STATE_MAX
        if key not in store and len(store) >= max_entries:
            try:
                store.pop(next(iter(store)))
            except StopIteration:
                pass
        store[key] = value


def cleanup_job_disabled(job_id: str) -> str:
    """Причина, по которой чистка выключена для всей задачи (пусто — не выключена)."""
    if not job_id:
        return ""
    with _CLEANUP_STATE_LOCK:
        return _CLEANUP_JOB_OFF.get(str(job_id), "")


def cleanup_disable_job(job_id: str, reason: str) -> None:
    """Первый же отказ подписки гасит чистку для остальных языков этой задачи."""
    if job_id:
        _cleanup_state_put(_CLEANUP_JOB_OFF, str(job_id), str(reason)[:200])


def cleanup_already_done(job_id: str, lang_name: str, base_name: str = "") -> bool:
    """Чистка для этого языка задачи уже отработала (идёт круг авто-ретрая)."""
    if not job_id:
        return False
    key = f"{job_id}\x00{lang_name}\x00{base_name}"
    with _CLEANUP_STATE_LOCK:
        return key in _CLEANUP_DONE


def cleanup_mark_done(job_id: str, lang_name: str, base_name: str = "") -> None:
    if job_id:
        _cleanup_state_put(_CLEANUP_DONE, f"{job_id}\x00{lang_name}\x00{base_name}", time.time())


@contextmanager
def cleanup_detached_job_id():
    """Снимает с потока метку задачи на время вызова чистки.

    ServerCredentialPool.acquire читает job_id из thread-local stop_event и, если
    общий слот провайдера занят дольше двух секунд, ставит на ВСЮ задачу пометку
    «ожидание общего лимита ключа». Ёмкость Codex на машине — один вызов, поэтому
    без этого снятия косметическая чистка обрывала бы соседние языки той же задачи
    (в том числе идущие через Claude) и отправляла готовую задачу обратно в очередь.
    Сам вызов чистки при занятом слоте просто мягко откажет через две секунды.
    """
    holder = getattr(STOP_FLAG, "_thread_local", None)
    stop_event = getattr(holder, "stop_event", None)
    saved = getattr(stop_event, "_rewrite_job_id", None) if stop_event is not None else None
    detached = False
    if stop_event is not None and saved is not None:
        try:
            delattr(stop_event, "_rewrite_job_id")
            detached = True
        except Exception:
            detached = False
    try:
        yield
    finally:
        if detached:
            try:
                setattr(stop_event, "_rewrite_job_id", saved)
            except Exception:
                pass


def cleanup_collect_suspects(text: str) -> Dict[str, List[str]]:
    """Бесплатный детектор: собирает подозрительные места без обращения к модели.

    Модель получает список готовым, поэтому не тратит рассуждения на поиск
    и не пропускает механические дефекты.
    """
    out: Dict[str, List[str]] = {}
    # буквенно-цифровые коды: MG42, FW190A-8, WN62
    out["codes"] = sorted(set(_CLEANUP_CODE_RE.findall(text)))[:30]
    # голые цифры (для озвучки всё должно быть словами)
    out["digits"] = sorted(set(value.strip() for value in _CLEANUP_DIGIT_RE.findall(text)))[:40]
    # разорванные слова: осиротевшая буква между словами. Одиночные буквы, которые
    # в целевых языках являются словами (i, w, z, o, a, u, k, s, v), отсеиваются —
    # иначе это сотни ложных кандидатов и приказ «почини каждое».
    broken = set()
    for match in _CLEANUP_BROKEN_RE.finditer(text):
        letter = match.group(1)
        if letter.lower() in _CLEANUP_REAL_ONE_LETTER_WORDS:
            continue
        start = max(0, match.start() - 24)
        broken.add(text[start:match.end() + 12].replace("\n", " ").strip())
    out["broken"] = sorted(broken)[:20]
    # слипшиеся: неправдоподобно длинные токены
    out["glued"] = sorted(set(_CLEANUP_GLUED_RE.findall(text)))[:20]
    # латинские аббревиатуры без расшифровки
    out["abbr"] = sorted(set(_CLEANUP_ABBR_RE.findall(text)))[:30]
    return {key: values for key, values in out.items() if values}


def cleanup_has_proper_names(text: str) -> bool:
    """Есть ли в тексте имена собственные — заглавное слово в середине предложения."""
    return bool(_CLEANUP_PROPER_NAME_RE.search(text or ""))


def cleanup_is_cyrillic_language(lang_name: str) -> bool:
    """Кириллица — целевой алфавит этого языка."""
    return str(lang_name or "").strip().lower() in _CLEANUP_CYRILLIC_LANGS


CLEANUP_PROMPT_TEMPLATE = """Ты редактор {lang} текста для озвучки. Текст сделан рерайтом транскрипта
с распознавания речи, поэтому в нём могли остаться искажённые имена и мусор распознавания.

Найди и верни замены по категориям:
1. ИСКАЖЁННЫЕ ИМЕНА СОБСТВЕННЫЕ и несуществующие слова. Имена исторических лиц, библейских
   персонажей и географические названия сверяй с реальностью: искажение пришло из распознавания
   английской речи, поэтому {name_example}.
   Имя меняй, только если старое — искажение ТОГО ЖЕ имени и звучит похоже. Никогда не подставляй
   другого человека, учёного, место или книгу вместо того, что написано.
2. РАЗНОБОЙ НАПИСАНИЙ: одно имя в разных местах текста написано по-разному. Приведи к одной форме
   и укажи ту, что встречается в начале текста.
3. СЛОВА ИЗ ЧУЖИХ ЯЗЫКОВ и кальки, которых нет в {lang}.
4. ЦИФРЫ, КОДЫ И АББРЕВИАТУРЫ: запиши словами на {lang}. Код MG42 читается как «эм-ге сорок два».
5. ГРУБЫЕ ОШИБКИ: разорванные и слипшиеся слова, опечатки внутри слова.
   Падежные и родовые окончания НЕ меняй: это грамматика, а не мусор распознавания
   (в чешском «dnešního Polska» — верный родительный падеж).

{alphabet_rule}
НЕ ТРОГАЙ: содержание, порядок предложений, длину текста, имена, которые написаны верно,
устоявшиеся иноязычные термины. Отдельно запрещено трогать брендинг: обращения к зрителю,
призыв подписаться, прощание в конце, имя ведущего, названия каналов и ссылки.

Цитата old должна быть САМОСТОЯТЕЛЬНЫМ фрагментом: целое слово или несколько слов подряд.
Не давай обрывок слова — замена применяется только по границам слова.

Подозрительные места, найденные автоматически (проверь каждое, но не ограничивайся ими):
{suspects}

Верни ТОЛЬКО JSON-массив: [{{"old": "точная цитата из текста", "new": "исправление", "why": "категория"}}]
Цитата old должна встречаться в тексте ДОСЛОВНО. Максимум {limit} пунктов. Если ошибок нет, верни [].

ТЕКСТ:
{text}"""


def cleanup_build_prompt(
    text: str,
    lang_name: str,
    suspects: Dict[str, List[str]],
    limit: int = CLEANUP_MAX_FIXES,
    editorial: bool = False,
) -> str:
    """Собирает промпт чистки: список найденных кандидатов + сам текст."""
    titles = {
        "codes": "коды",
        "digits": "цифры",
        "broken": "разрывы слов",
        "glued": "слипшиеся слова",
        "abbr": "аббревиатуры",
    }
    lines: List[str] = []
    for key, values in suspects.items():
        shown = ", ".join(str(value).strip() for value in values[:20] if str(value).strip())
        if shown:
            lines.append(f"- {titles.get(key, key)}: {shown}")
    if cleanup_is_cyrillic_language(lang_name):
        # Для кириллического текста пример с латинским написанием — прямая инструкция
        # завести латиницу в текст для озвучки. Даём пример в целевом алфавите.
        name_example = "«Ирвин Рамль» это «Эрвин Роммель», «Вельзебуб» это «Вельзевул»"
        if lang_name.strip().lower() == "ukrainian":
            name_example = "«Ірвін Рамль» це «Ервін Роммель»; виправлення мають бути українською"
        alphabet_rule = (
            "АЛФАВИТ: текст кириллический. Все исправления пиши кириллицей — "
            "латинских букв в тексте для озвучки быть не должно.\n"
        )
    else:
        name_example = "«Irvin Raml» это Erwin Rommel, «Beelible» это Beelzebub"
        alphabet_rule = (
            f"АЛФАВИТ: пиши исправления в том же алфавите, что и текст ({lang_name}), "
            "не вставляй кириллицу.\n"
        )
    template = CLEANUP_PROMPT_TEMPLATE
    if editorial:
        template = template.replace(
            "Найди и верни замены по категориям:",
            "Проверь ВЕСЬ переданный текст, включая начало и конец, как носитель языка. "
            "Это один финальный редакторский проход, НЕ полный рерайт. "
            "Верни только необходимые точечные исправления по категориям:",
        ).replace(
            "   Падежные и родовые окончания НЕ меняй: это грамматика, а не мусор распознавания\n"
            "   (в чешском «dnešního Polska» — верный родительный падеж).",
            "6. ЯВНЫЕ ГРАММАТИЧЕСКИЕ ОШИБКИ, неестественные кальки и сломанная связность "
            "ВНУТРИ предложения. Для грамматики цитируй короткую фразу с контекстом, "
            "никогда одиночное слово без контекста: разные падежи — не разнобой написаний. "
            "Не улучшай стиль ради синонимов. Не добавляй фактов и не меняй степень уверенности.\n"
            "7. ЯВНЫЙ МУСОР РАСПОЗНАВАНИЯ и случайно продублированная одинаковая фраза. "
            "Не удаляй осмысленные повторы, рефрены, диалоги, дневники или цитаты. "
            "Удаление допустимо лишь для явной метки [music] либо механического дубля "
            "точно той же фразы; укажи kind=asr_noise или kind=duplicate.\n"
            "8. Не присваивай ведущему личный опыт современного донора; исторические "
            "свидетельства от первого лица и их атрибуцию сохраняй.",
        )
    return template.format(
        lang=lang_name,
        name_example=name_example,
        alphabet_rule=alphabet_rule,
        suspects="\n".join(lines) or "не найдено",
        limit=limit,
        text=text,
    )


def cleanup_split(text: str, limit: int = 0) -> List[str]:
    """Режет по границе абзаца, только если текст длиннее лимита.

    Обычный ролик уходит одним куском. Склейка обратно через "\\n\\n" возвращает
    ровно исходный текст — разделители абзацев не теряются.
    """
    limit = limit or _cleanup_int("REWRITE_CLEANUP_CHUNK_CHARS", 45000, 4000)
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    current = ""
    for para in text.split("\n\n"):
        if current and len(current) + len(para) + 2 > limit:
            parts.append(current)
            current = para
        else:
            current = (current + "\n\n" + para) if current else para
    parts.append(current)
    return parts


def cleanup_parse_fixes(raw: str, strict: bool = False) -> Optional[List[Dict[str, Any]]]:
    """Разбирает ответ модели в список правок.

    None — ответ не разобрался (мягкий отказ), [] — модель честно не нашла правок.
    """
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3].rstrip()
    payload: Any = None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        start = text.find("[")
        if start >= 0:
            try:
                payload, _end = json.JSONDecoder().raw_decode(text[start:])
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = None
    if isinstance(payload, dict):
        # модель иногда заворачивает массив в объект
        for key in ("fixes", "replacements", "items", "result", "edits"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        return None
    if strict and (len(payload) > CLEANUP_MAX_FIXES or any(
            not isinstance(item, dict) or not isinstance(item.get("old"), str)
            or not item["old"].strip() or not isinstance(item.get("new"), str)
            for item in payload)):
        return None
    return [item for item in payload if isinstance(item, dict)]


def cleanup_protected_terms(channel_name: str = "", host_name: str = "") -> List[str]:
    """Термины брендинга в нижнем регистре: полные имена и их основы.

    Основа нужна из-за склонений: в польском и чешском имя ведущего в призыве
    стоит не в именительном падеже, и сравнение по точной строке промахивается.
    """
    terms: List[str] = []
    for raw in (channel_name, host_name):
        value = str(raw or "").strip()
        if not value:
            continue
        terms.append(value.lower())
        for word in re.findall(r"[^\W\d_]{4,}", value, re.UNICODE):
            stem = word.lower()[: max(4, len(word) - 2)]
            terms.append(stem)
    terms.extend(_CLEANUP_ALWAYS_PROTECTED)
    return sorted({term for term in terms if len(term) >= 3})


def cleanup_protected_spans(text: str, terms: List[str]) -> List[Tuple[int, int]]:
    """Границы предложений, в которых живёт брендинг.

    Защищаем ПРЕДЛОЖЕНИЕ целиком, а не строку с именем: призыв подписаться,
    представление и прощание — это предложения, и правка, нацеленная в обычное
    слово, не должна докатиться до них глобальной заменой.
    """
    if not terms:
        return []
    spans: List[Tuple[int, int]] = []
    for match in _CLEANUP_SENTENCE_RE.finditer(text):
        piece = match.group(0)
        if not piece.strip():
            continue
        low = piece.lower()
        if any(term in low for term in terms):
            spans.append((match.start(), match.end()))
    return spans


# FIX-2026-09-17-cleanup-guards: проверка 489 правок чистки по исходникам (17.09) нашла две системные
# порчи. 1) Правка только окончания: «Polska → Polsko» девять раз сломала чешский падеж,
# «Knocking → Knockin'» исказила название. 2) Подмена фамилии другим человеком: «Wasmer → Schwarz»,
# «Steinhauser → Much» — это выдуманный факт, а не мусор распознавания. Настоящие исправления
# распознавания сохраняют начальный звук («Verluskart → Pluskat», «Seau → Severloh»,
# «Leonare → Lion-sur-Mer») или приводят имя к форме, которая уже есть в тексте.
_CLEANUP_EDGE_PUNCT = "\"'«»„“”‚‘’()[]{}.,;:!?…—–-"
_CLEANUP_SOUND_PREFIXES = (("sch", "S"), ("sh", "S"), ("sz", "S"), ("ts", "S"), ("tz", "S"),
                           ("ch", "K"), ("kh", "K"), ("ph", "P"), ("th", "T"))
_CLEANUP_SOUND_LETTERS = {
    "p": "P", "b": "P", "v": "P", "w": "P", "f": "P", "s": "S", "z": "S", "c": "K", "k": "K", "g": "K",
    "q": "K", "x": "K", "t": "T", "d": "T", "m": "N", "n": "N", "l": "L", "r": "R", "j": "J",
}


def _cleanup_fold(word: str) -> str:
    return unicodedata.normalize("NFKD", word or "").encode("ascii", "ignore").decode().lower()


_CLEANUP_VOWELS = set("aeiouyáàâäãåąéèêëěęíìîïıóòôöõőúùûüůűýÿ")
CLEANUP_INFLECTION_MIN_OCCURRENCES = 2


def _cleanup_sounds_compatible(a: str, b: str) -> bool:
    """Совпадает ли начальный звук. «Ch» читается по-разному (Cherbourg — «ш», Christ — «к»)."""
    sounds_a, sounds_b = {_cleanup_initial_sound(a)}, {_cleanup_initial_sound(b)}
    for word, sounds in ((a, sounds_a), (b, sounds_b)):
        folded = _cleanup_fold(word)
        if folded.startswith("ch"):
            sounds |= {"S", "K"}
        if folded[:1] in ("y", "j"):
            sounds |= {"J", "V"}  # Yahweh → Jahve, Yalta → Jalta: английская запись против славянской
    return bool(sounds_a & sounds_b)


def _cleanup_initial_sound(word: str) -> str:
    folded = _cleanup_fold(word)
    if not folded:
        return ""
    for prefix, sound in _CLEANUP_SOUND_PREFIXES:
        if folded.startswith(prefix):
            return sound
    first = folded[0]
    if first in "aeiouyh":
        return "V"
    return _CLEANUP_SOUND_LETTERS.get(first, first.upper())


def cleanup_semantic_guard(old: str, new: str, text: str) -> str:
    """Причина отказа для правки, которая портит смысл или грамматику. Пусто — правка допустима.

    Оба правила узкие намеренно: на 273 реальных правках 17.09 широкий запрет «только окончание»
    отсекал и верные исправления (грамматика с контекстом, «Stag → Stagg», «Beliel → Belial»).
    """
    if re.search(r"\d", old or ""):
        return ""  # цифры и коды разворачиваются в слова — это не про смысл
    old_words, new_words = (old or "").split(), (new or "").split()
    # 1) Одно слово без контекста, меняется только гласный хвост, слово частое и новая форма в тексте
    #    уже есть: модель «унифицирует» два падежа одного слова как разнобой написаний.
    if len(old_words) == 1 and len(new_words) == 1:
        a, b = old_words[0].strip(_CLEANUP_EDGE_PUNCT), new_words[0].strip(_CLEANUP_EDGE_PUNCT)
        la, lb = a.lower(), b.lower()
        prefix = 0
        while prefix < min(len(la), len(lb)) and la[prefix] == lb[prefix]:
            prefix += 1
        tail_a, tail_b = la[prefix:], lb[prefix:]
        if (prefix >= 3 and tail_a and tail_b and len(tail_a) <= 2 and len(tail_b) <= 2
                and all(ch in _CLEANUP_VOWELS for ch in tail_a + tail_b)):
            word_re = lambda w: r"(?<!\w)" + re.escape(w) + r"(?!\w)"
            if (len(re.findall(word_re(a), text or "")) >= CLEANUP_INFLECTION_MIN_OCCURRENCES
                    and re.search(word_re(b), text or "")):
                return ("меняет падежную форму частого слова на другую его форму, которая уже есть в тексте "
                        "— это грамматика, а не разнобой написаний")
    # 2) Подмена имени другим: заглавное слово меняется на непохожее по начальному звуку,
    #    которого нет в тексте и которое не является перестановкой слов цитаты.
    if len(old_words) == len(new_words):
        pairs = [(x.strip(_CLEANUP_EDGE_PUNCT), y.strip(_CLEANUP_EDGE_PUNCT)) for x, y in zip(old_words, new_words)]
    else:
        caps_old = [w.strip(_CLEANUP_EDGE_PUNCT) for w in old_words if w.strip(_CLEANUP_EDGE_PUNCT)[:1].isupper()]
        caps_new = [w.strip(_CLEANUP_EDGE_PUNCT) for w in new_words if w.strip(_CLEANUP_EDGE_PUNCT)[:1].isupper()]
        pairs = [(caps_old[0], caps_new[0])] if caps_old and caps_new else []
    old_stems = {_cleanup_fold(w.strip(_CLEANUP_EDGE_PUNCT))[:4] for w in old_words}
    for a, b in pairs:
        if not a or not b or a == b or not (a[:1].isupper() and b[:1].isupper()):
            continue
        if _cleanup_fold(b)[:4] in old_stems:
            continue  # перестановка или склонение слов самой цитаты
        if re.search(r"(?<!\w)" + re.escape(b) + r"(?!\w)", text or ""):
            continue  # приведение к написанию, которое уже есть в тексте
        if not _cleanup_sounds_compatible(a, b):
            return f"подменяет «{a}» на непохожее по звучанию «{b}», которого нет в тексте — похоже на другой факт"
    return ""


def cleanup_apply_fixes(
    text: str,
    fixes: List[Dict[str, Any]],
    terms: Optional[List[str]] = None,
    limit: int = CLEANUP_MAX_FIXES,
) -> Tuple[str, Dict[str, Any]]:
    """Применяет правки с защитой. Возвращает (текст, отчёт).

    Замена идёт ТОЛЬКО по границам слова: обычный str.replace переписывает куски
    посторонних слов (правка «Ana» → «Anna» превратила бы Anatol в Annatol), и ни
    длина, ни абзацы, ни алфавит этого не видят. Всё, что непохоже на точечную
    замену целого слова, отбрасывается.
    """
    report: Dict[str, Any] = {
        "предложено": 0,
        "применено": 0,
        "замен": 0,
        "отброшено": 0,
        "причины": [],
    }
    out = text
    terms = terms or []

    def _drop(quote: str, reason: str) -> None:
        report["отброшено"] += 1
        if len(report["причины"]) < 8:
            report["причины"].append(f"«{quote[:40]}» — {reason}")

    for item in (fixes or [])[:limit]:
        old = str(item.get("old") or "")
        new = str(item.get("new") or "")
        report["предложено"] += 1
        kind = str(item.get("kind") or "").strip().lower()
        if not kind and str(item.get("why") or "").strip().lower() in ("duplicate", "asr_noise"):
            kind = str(item["why"]).strip().lower()
        is_caption_deletion = kind == "asr_noise" and not new.strip() and bool(_REWRITE_CAPTION_TAG_RE.fullmatch(old))
        # Only exact mechanical duplication is removable. Not a licence to shorten a story.
        is_duplicate = (kind == "duplicate" and len(new.split()) >= 4
                        and not any(mark in old for mark in ('"', '«', '»', '“', '”'))
                        and old.strip() == (new.strip() + " " + new.strip()))
        if kind == "duplicate" and not is_duplicate:
            _drop(old, "не точный механический дубль либо цитата")
            continue
        if not old or (not new.strip() and not is_caption_deletion):
            _drop(old, "пустая замена")
            continue
        if old == new:
            _drop(old, "ничего не меняет")
            continue
        if len(old) < 3:
            _drop(old, "слишком короткая цитата")
            continue
        if "\n" in old or "\n" in new:
            # Перенос строки внутри правки — это сдвиг структуры, а не чистка слова.
            # Запрещаем во ВСЕХ режимах: в режиме сохранения абзацев такая правка
            # ломает 1:1, в обычном — незаметно склеивает или разрывает абзацы.
            _drop(old, "правка содержит перенос строки")
            continue
        low_old, low_new = old.lower(), new.lower()
        if any(term in low_old or term in low_new for term in terms):
            _drop(old, "трогает брендинг или ссылку")
            continue
        # Правка не должна раздувать фрагмент. Исключение — цифры и коды: «1944» →
        # «tysiąc dziewięćset czterdzieści cztery» это законное разворачивание в слова
        # для озвучки, поэтому короткой цитате с цифрой даётся запас по длине.
        has_digit = bool(re.search(r"\d", old))
        max_new = (len(old) * 12 + 40) if (has_digit and len(old) <= 16) else (2 * len(old) + 20)
        if len(new) > max_new:
            _drop(old, "замена неправдоподобно длинная")
            continue
        # Срезание — главный риск чистки: так из текста пропадает содержание
        # (и прощание в конце ролика).
        if len(new) < CLEANUP_MIN_REPLACEMENT_SHARE * len(old) - 8 and not (is_caption_deletion or is_duplicate):
            _drop(old, "замена срезает фрагмент")
            continue
        guard_reason = cleanup_semantic_guard(old, new, out)
        if guard_reason:
            _drop(old, guard_reason)
            continue
        pattern = r"(?<!\w)" + re.escape(old) + r"(?!\w)"
        try:
            matches = list(re.finditer(pattern, out))
        except re.error:
            _drop(old, "цитата не разобралась")
            continue
        if not matches:
            _drop(old, "цитаты нет в тексте как отдельного фрагмента")
            continue
        if len(matches) > CLEANUP_MAX_OCCURRENCES:
            _drop(old, f"слишком много вхождений ({len(matches)})")
            continue
        if is_duplicate or is_caption_deletion:
            quotes = list(re.finditer(r'["«“][^"»”\n]*["»”]', out))
            if any(m.start() < quote.end() and quote.start() < m.end()
                   for m in matches for quote in quotes):
                _drop(old, "мусор/повтор находится в цитате")
                continue
        spans = cleanup_protected_spans(out, terms)
        if spans and any(
            any(start < span_end and span_start < end for span_start, span_end in spans)
            for start, end in ((m.start(), m.end()) for m in matches)
        ):
            _drop(old, "попадает в предложение с брендингом")
            continue
        out = re.sub(pattern, lambda _m, value=new: value, out)
        report["применено"] += 1
        report["замен"] += len(matches)
    return out, report


def cleanup_verify(
    before: str,
    after: str,
    lang_name: str = "",
    protected_names: Optional[List[str]] = None,
    applied: int = 0,
) -> Tuple[bool, List[str]]:
    """Сверяет текст до и после. Любое подозрение — откат на стороне вызывающего.

    Порог по длине несимметричный и с абсолютным потолком: рост — ожидаемое
    поведение (цифры разворачиваются в слова), срезание — пропавшее содержание.
    Алфавит проверяется в ОБЕ стороны: для латинского текста ловим появление
    кириллицы, для кириллического — появление латиницы (промпт чистки сам склонен
    подсунуть «Erwin Rommel» в русский текст).
    """
    problems: List[str] = []
    base = max(1, len(before))
    applied = max(1, applied)
    allowed_growth = min(
        max(CLEANUP_GROWTH_MIN_SHARE * base, CLEANUP_GROWTH_PER_FIX * applied),
        max(CLEANUP_GROWTH_MAX_SHARE * base, CLEANUP_GROWTH_FLOOR),
    )
    allowed_shrink = min(
        max(CLEANUP_SHRINK_MIN_SHARE * base, CLEANUP_SHRINK_PER_FIX * applied),
        max(CLEANUP_SHRINK_MAX_SHARE * base, CLEANUP_SHRINK_FLOOR),
    )
    delta = len(after) - len(before)
    if delta > allowed_growth:
        problems.append(f"текст вырос на {delta} знаков (допустимо {int(allowed_growth)})")
    if -delta > allowed_shrink:
        problems.append(f"текст срезан на {-delta} знаков (допустимо {int(allowed_shrink)})")
    paras_before, paras_after = count_paragraphs(before), count_paragraphs(after)
    if paras_before != paras_after:
        problems.append(f"число абзацев {paras_before} → {paras_after}")
    breaks_before, breaks_after = before.count("\n"), after.count("\n")
    if breaks_before != breaks_after:
        problems.append(f"число переносов строк {breaks_before} → {breaks_after}")
    digits_before = len(re.findall(r"\d", before))
    digits_after = len(re.findall(r"\d", after))
    if digits_after > digits_before:
        problems.append(f"появились цифры: было {digits_before}, стало {digits_after}")
    if cleanup_is_cyrillic_language(lang_name):
        lat_before = len(_CLEANUP_LATIN_RE.findall(before))
        lat_after = len(_CLEANUP_LATIN_RE.findall(after))
        if lat_after > lat_before:
            problems.append(f"появилась латиница: было {lat_before}, стало {lat_after}")
    else:
        cyr_before = len(_CLEANUP_CYRILLIC_RE.findall(before))
        cyr_after = len(_CLEANUP_CYRILLIC_RE.findall(after))
        if cyr_after > cyr_before:
            problems.append(f"появилась кириллица: было {cyr_before}, стало {cyr_after}")
    for name in [str(n).strip() for n in (protected_names or []) if n and str(n).strip()]:
        was = len(re.findall(re.escape(name), before, re.IGNORECASE))
        now = len(re.findall(re.escape(name), after, re.IGNORECASE))
        if now < was:
            problems.append(f"пропало имя «{name}»: было {was}, стало {now}")
    return (not problems), problems


def cleanup_one_piece(
    piece: str,
    lang_name: str,
    lang_display: str,
    model: str,
    log_queue: queue.Queue,
    terms: Optional[List[str]] = None,
    protected_names: Optional[List[str]] = None,
    task_info: str = "",
    label: str = "",
    strict: bool = False,
    stats: Optional[Dict[str, Any]] = None,
) -> Tuple[str, bool, str]:
    """Чистит один кусок. Возвращает (текст, был_ли_вызов, причина_отказа).

    Любой сбой возвращает ИСХОДНЫЙ кусок. strict дополнительно возвращает причину:
    обязательный Codex gate не выдаст этот непроверенный кусок как готовый.
    """
    if not piece or not piece.strip():
        return piece, False, ""
    if stats is not None:
        stats["applied"] = 0
    suspects = cleanup_collect_suspects(piece)
    candidates = sum(len(values) for values in suspects.values())
    if not strict and not suspects and not cleanup_has_proper_names(piece):
        log_msg(log_queue, f"🧹 {lang_display}{label}: чистка не нужна — ни кандидатов, ни имён собственных")
        return piece, False, ""
    if STOP_FLAG.is_stopped():
        return piece, False, "CODEX_STOPPED" if strict else ""

    messages = [{"role": "user", "content": cleanup_build_prompt(piece, lang_name, suspects, editorial=strict)}]
    # Mandatory Codex QA keeps job identity: the existing queue can park it on
    # shared-slot/subscription cooldown and later resume the paid rewrite chunks.
    with (nullcontext() if strict else cleanup_detached_job_id()):
        ok, response = call_codex_subscription(messages, model, log_queue, task_info)
    if not ok:
        reason = str(response)[:200]
        log_msg(
            log_queue,
            f"🧹 {lang_display}{label}: чистка пропущена — модель не ответила ({reason[:160]}); "
            f"текст оставлен как был"
        )
        return piece, True, reason

    fixes = cleanup_parse_fixes(response, strict=strict)
    if fixes is None:
        log_msg(
            log_queue,
            f"🧹 {lang_display}{label}: чистка пропущена — ответ не разобрался как JSON; текст оставлен как был"
        )
        return piece, True, "invalid JSON quality response" if strict else ""
    if not fixes:
        log_msg(log_queue, f"🧹 {lang_display}{label}: кандидатов {candidates}, правок модель не нашла")
        return piece, True, ""

    cleaned, report = cleanup_apply_fixes(piece, fixes, terms=terms)
    ok_verify, problems = cleanup_verify(
        piece, cleaned, lang_name=lang_name,
        protected_names=protected_names, applied=report["применено"],
    )
    if not ok_verify:
        log_msg(
            log_queue,
            f"🧹 {lang_display}{label}: сверка целостности не прошла ({'; '.join(problems)}) — "
            f"откат, текст оставлен как был"
        )
        return piece, True, ("integrity check failed: " + "; ".join(problems)) if strict else ""

    log_msg(
        log_queue,
        f"🧹 {lang_display}{label}: кандидатов {candidates}, предложено {report['предложено']}, "
        f"применено {report['применено']} (замен {report['замен']}), отброшено {report['отброшено']}"
    )
    if report["причины"]:
        log_msg(log_queue, f"🧹 {lang_display}{label}: отброшено — {'; '.join(report['причины'])}")
    if stats is not None:
        stats["applied"] = report["применено"]
    return cleaned, True, ""


def _cleanup_rewrite_results(
    results: List[str],
    lang_name: str,
    lang_display: str,
    log_queue: queue.Queue,
    channel_name: str = "",
    host_name: str = "",
    preserve_paragraphs: bool = False,
    job_id: str = "",
    base_name: str = "",
    task_info: str = "",
    allow: bool = True,
) -> List[str]:
    """Однопроходная чистка готового рерайта. Чистится склейка, а не куски по отдельности.

    Почему склейкой: ёмкость Codex на машине — ОДИН одновременный вызов на все три
    сервиса, а кусков рерайта на язык бывает 6-26. По кускам это десятки занятий
    единственного слота на один язык; склейка даёт один вызов и вдобавок ловит
    разнобой написаний МЕЖДУ кусками, который по кускам не виден в принципе.
    Возврат одного элемента безопасен: ниже по коду results всё равно склеиваются
    через "\\n\\n" в final_text.
    """
    if not results:
        return results
    if not allow:
        return results
    if not cleanup_enabled(preserve_paragraphs):
        log_msg(
            log_queue,
            f"🧹 {lang_display}: чистка выключена переменной окружения"
            f"{' (режим сохранения абзацев)' if preserve_paragraphs else ''}"
        )
        return results
    off_reason = cleanup_job_disabled(job_id)
    if off_reason:
        log_msg(log_queue, f"🧹 {lang_display}: чистка выключена для этой задачи — {off_reason[:120]}")
        return results
    if cleanup_already_done(job_id, lang_name, base_name):
        log_msg(log_queue, f"🧹 {lang_display}: чистка уже выполнялась для этого языка — повторно не зову")
        return results

    model = cleanup_model_name()
    if classify_provider(model) != "codex":
        log_msg(
            log_queue,
            f"🧹 {lang_display}: чистка пропущена — модель «{model}» не из подписки Codex "
            f"(проверь REWRITE_CLEANUP_MODEL)"
        )
        return results
    if model not in get_allowed_codex_models():
        log_msg(
            log_queue,
            f"🧹 {lang_display}: чистка пропущена — модель «{model}» вне списка разрешённых "
            f"(REWRITE_ALLOWED_CODEX_MODELS)"
        )
        return results

    texts = [value if isinstance(value, str) else str(value or "") for value in results]
    if any(text.startswith("[ОШИБКА:") for text in texts):
        return results
    joined = "\n\n".join(texts)
    min_chars = _cleanup_int("REWRITE_CLEANUP_MIN_CHARS", 400, 0)
    if len(joined.strip()) < min_chars:
        return results
    if STOP_FLAG.is_stopped():
        return results

    terms = cleanup_protected_terms(channel_name, host_name)
    protected_names = [str(n).strip() for n in (channel_name, host_name) if n and str(n).strip()]
    deadline = time.monotonic() + _cleanup_int("REWRITE_CLEANUP_BUDGET_SEC", 900, 60)
    calls_left = _cleanup_int("REWRITE_CLEANUP_MAX_CALLS", 3, 1)
    pieces = cleanup_split(joined)
    cleaned_pieces: List[str] = []
    calls_made = 0
    budget_reported = False
    fatal_reason = ""

    for index, piece in enumerate(pieces):
        if fatal_reason or calls_left <= 0 or time.monotonic() >= deadline or STOP_FLAG.is_stopped():
            if not budget_reported and not fatal_reason:
                log_msg(
                    log_queue,
                    f"🧹 {lang_display}: бюджет чистки исчерпан — оставшиеся части оставлены как были"
                )
                budget_reported = True
            cleaned_pieces.append(piece)
            continue
        label = f" часть {index + 1}/{len(pieces)}" if len(pieces) > 1 else ""
        cleaned_piece, called, reason = cleanup_one_piece(
            piece,
            lang_name,
            lang_display,
            model,
            log_queue,
            terms=terms,
            protected_names=protected_names,
            task_info=f"{task_info or 'cleanup'}_{index + 1}",
            label=label,
        )
        if called:
            calls_left -= 1
            calls_made += 1
        if reason and any(marker.lower() in reason.lower() for marker in _CLEANUP_FATAL_MARKERS):
            # Подписка отказала по существу: остальным частям и языкам этой задачи
            # ответят тем же. Дальше не платим ни слотом, ни минутами.
            fatal_reason = reason
            cleanup_disable_job(job_id, reason)
        cleaned_pieces.append(cleaned_piece)

    if calls_made:
        cleanup_mark_done(job_id, lang_name, base_name)
    if fatal_reason:
        log_msg(
            log_queue,
            f"🧹 {lang_display}: чистка отключена до конца задачи — подписка отказала ({fatal_reason[:120]})"
        )

    cleaned_full = "\n\n".join(cleaned_pieces)
    if cleaned_full == joined:
        return results

    # Вторая, независимая сверка — уже по всему тексту языка. В режиме сохранения
    # абзацев это тот же инвариант, который проверяется при сохранении файла,
    # и считается он той же функцией count_paragraphs.
    ok_verify, problems = cleanup_verify(
        joined, cleaned_full, lang_name=lang_name,
        protected_names=protected_names, applied=max(1, calls_made),
    )
    if not ok_verify:
        log_msg(
            log_queue,
            f"🧹 {lang_display}: общая сверка не прошла ({'; '.join(problems)}) — "
            f"чистка отменена целиком, текст оставлен как был"
        )
        return results

    log_msg(
        log_queue,
        f"🧹 {lang_display}: чистка завершена — вызовов модели {calls_made}, модель {model}, "
        f"длина {len(joined)} → {len(cleaned_full)} знаков, абзацев {count_paragraphs(cleaned_full)}"
    )
    return [cleaned_full]


def _cleanup_strict_rewrite_results(
    results: List[str], lang_name: str, lang_display: str, log_queue: queue.Queue,
    channel_name: str, host_name: str, preserve_paragraphs: bool,
    job_id: str, base_name: str, task_info: str, allow: bool,
) -> List[str]:
    """Codex final editorial gate: complete coverage or an observable, retryable failure."""
    if not allow:  # Incomplete rewrite is handled by the existing chunk-failure gate.
        return results
    if not results or any(str(value).startswith("[ОШИБКА:") for value in results):
        raise RewriteQualityError("QUALITY_REVIEW_FAILED: no complete text to review")
    if not cleanup_enabled(preserve_paragraphs):
        raise RewriteQualityError("QUALITY_REVIEW_FAILED: REWRITE_CLEANUP is disabled for Codex")
    model = cleanup_model_name()
    if classify_provider(model) != "codex" or model not in get_allowed_codex_models():
        raise RewriteQualityError("QUALITY_REVIEW_FAILED: configure an allowed Codex cleanup model")
    joined = "\n\n".join(str(value or "") for value in results)
    # Also removes the measured edge loop from legacy completed chunks, without
    # changing the source split plan or redoing any paid rewrite chunk.
    joined, removed_noise = preclean_rewrite_source(joined)
    if removed_noise:
        log_msg(log_queue, f"🧹 {lang_display}: перед финальной редактурой снято ASR-меток/петель {removed_noise}")
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    cache_key = "\x00".join((str(job_id), lang_name, base_name, channel_name, host_name,
                             QUALITY_REVIEW_VERSION, digest))
    if job_id:
        with _CLEANUP_STATE_LOCK:
            verified = _CLEANUP_VERIFIED.get(cache_key)
        if verified is not None:
            log_msg(log_queue, f"🧹 {lang_display}: проверенный результат Codex из кэша (совпадает SHA256)")
            return [verified]
    terms = cleanup_protected_terms(channel_name, host_name)
    protected_names = [name.strip() for name in (channel_name, host_name) if name and name.strip()]
    pieces = cleanup_split(joined)
    retries = min(1, _cleanup_int("REWRITE_CLEANUP_RETRIES", 1, 0))
    calls_left = max(len(pieces), _cleanup_int("REWRITE_CLEANUP_MAX_CALLS", 3, 1)) + retries
    deadline = time.monotonic() + max(_cleanup_int("REWRITE_CLEANUP_BUDGET_SEC", 900, 60), len(pieces) * 120)
    cleaned_pieces: List[str] = []
    calls_made = 0
    total_applied = 0
    for index, piece in enumerate(pieces):
        if not piece.strip():
            cleaned_pieces.append(piece)
            continue
        reason = ""
        for attempt in range(1 + retries):  # Bounded retries; enough budget to review every piece.
            if STOP_FLAG.is_stopped():
                raise RewriteQualityError("QUALITY_REVIEW_FAILED: CODEX_STOPPED")
            if calls_left <= 0 or time.monotonic() >= deadline:
                raise RewriteQualityError(
                    f"QUALITY_REVIEW_FAILED: budget exhausted; reviewed {len(cleaned_pieces)}/{len(pieces)} "
                    f"pieces, calls {calls_made}; rewrite chunks retained")
            piece_stats: Dict[str, Any] = {}
            cleaned, called, reason = cleanup_one_piece(
                piece, lang_name, lang_display, model, log_queue,
                terms=terms, protected_names=protected_names,
                task_info=f"{task_info or 'cleanup'}_{index + 1}_try{attempt + 1}",
                label=f" часть {index + 1}/{len(pieces)}", strict=True, stats=piece_stats,
            )
            if called:
                calls_left -= 1
                calls_made += 1
            if not reason:
                cleaned_pieces.append(cleaned)
                total_applied += int(piece_stats.get("applied", 0))
                break
            log_msg(log_queue, f"🧹 {lang_display}: проверка части {index + 1} не завершена: {reason[:160]}")
            if any(marker.lower() in reason.lower() for marker in _CLEANUP_FATAL_MARKERS):
                break
        if reason:
            raise RewriteQualityError(
                f"QUALITY_REVIEW_FAILED: piece {index + 1}/{len(pieces)}: {reason[:200]}; "
                "rewrite chunks retained for retry")
    cleaned_full = "\n\n".join(cleaned_pieces)
    ok, problems = cleanup_verify(joined, cleaned_full, lang_name, protected_names, max(1, total_applied))
    if not ok:
        raise RewriteQualityError("QUALITY_REVIEW_FAILED: full-text integrity: " + "; ".join(problems))
    if job_id:
        _cleanup_state_put(_CLEANUP_VERIFIED, cache_key, cleaned_full)
    log_msg(log_queue, f"✅ {lang_display}: финальная редактура Codex проверена — "
                      f"{len(pieces)}/{len(pieces)} частей, {calls_made} вызовов, модель {model}, "
                      f"{len(joined)} → {len(cleaned_full)} знаков")
    return [cleaned_full]


def cleanup_rewrite_results(
    results: List[str],
    lang_name: str,
    lang_display: str,
    log_queue: queue.Queue,
    channel_name: str = "",
    host_name: str = "",
    preserve_paragraphs: bool = False,
    job_id: str = "",
    base_name: str = "",
    task_info: str = "",
    allow: bool = True,
    strict: bool = False,
) -> List[str]:
    """Единственная точка входа чистки — и последняя страховка.

    Legacy providers retain the cosmetic fallback. Mandatory Codex checking raises
    RewriteQualityError; paid source chunks stay checkpointed for queue recovery.
    """
    try:
        if strict:
            return _cleanup_strict_rewrite_results(
                results, lang_name, lang_display, log_queue, channel_name, host_name,
                preserve_paragraphs, job_id, base_name, task_info, allow,
            )
        return _cleanup_rewrite_results(
            results,
            lang_name,
            lang_display,
            log_queue,
            channel_name=channel_name,
            host_name=host_name,
            preserve_paragraphs=preserve_paragraphs,
            job_id=job_id,
            base_name=base_name,
            task_info=task_info,
            allow=allow,
        )
    except Exception as exc:
        if strict:
            if isinstance(exc, RewriteQualityError):
                raise
            raise RewriteQualityError(f"QUALITY_REVIEW_FAILED: {type(exc).__name__}: {exc}") from exc
        try:
            log_msg(
                log_queue,
                f"🧹 {lang_display}: чистка пропущена — внутренняя ошибка "
                f"{type(exc).__name__}: {exc}; текст оставлен как был"
            )
        except Exception:
            pass
        return results



# ============== ОСНОВНАЯ ФУНКЦИЯ РЕРАЙТА ==============

def rewrite_single_language(
    source_file: str,
    lang_display: str,
    lang_name: str,
    chunk_size: int,
    model: str,
    api_key: str,
    output_dir: str,
    log_queue: queue.Queue,
    channel_name: str = "",
    host_name: str = "",
    address_style: str = "",
    custom_prompt: str = "",
    use_subscription: bool = False,
    oauth_token: str = "",
    oauth_tokens: Optional[List[str]] = None,
    start_token_index: int = 0,
    preserve_paragraphs: bool = False,
    job_id: str = "",
    output_basename: str = "",
    progress_scope: str = "",
) -> Tuple[str, bool]:
    """
    Рерайтит файл на один язык с сохранением прогресса.
    При лимите подписки переключается на следующий токен из oauth_tokens.

    preserve_paragraphs=True: разбиение по абзацам (\\n\\n), на каждом чанке
    проверяется что число абзацев на выходе == входу, до 3 retry; при провале —
    язык помечается failed.
    """
    if STOP_FLAG.is_stopped():
        return (lang_display, False)
    
    tokens = (oauth_tokens or []) if oauth_tokens is not None else ([oauth_token] if oauth_token else [])
    if use_subscription and not tokens:
        tokens = [oauth_token] if oauth_token else []
    current_token_index = [start_token_index % len(tokens)] if tokens else [0]
    _is_codex = classify_provider(model) == "codex"
    _strict_codex = _is_codex and not preserve_paragraphs
    
    try:
        # Quarantine stale final files before source reading/analysis can fail.
        _progress_job_id = progress_scope or job_id
        _uid = get_current_user_id() or (os.path.basename(os.path.normpath(output_dir)) if output_dir else "")
        if _strict_codex:
            early_base = output_basename or os.path.splitext(os.path.basename(source_file))[0][:50]
            early_task = PROGRESS_MANAGER.tasks.get(PROGRESS_MANAGER._task_key(source_file, lang_name, _uid, _progress_job_id))
            early_outputs = {os.path.join(output_dir, make_output_filename(early_base, lang_name))}
            if early_task and getattr(early_task, "output_file", ""):
                early_outputs.add(early_task.output_file)
            for old_output in early_outputs:
                if os.path.isfile(old_output) and not cleanup_receipt_valid(old_output, lang_name, channel_name, host_name):
                    cleanup_quarantine_unverified_output(old_output, output_dir)
                    if early_task:
                        PROGRESS_MANAGER.set_task_output(source_file, lang_name, "", user_id=_uid, job_id=_progress_job_id)
                    log_msg(log_queue, f"🔎 {lang_display}: прежний непроверенный файл сохранён отдельно до повторной проверки")
        # Читаем файл (поддержка txt, docx, pdf, odt, rtf)
        text = read_file_as_text(source_file)
        # Mandatory QA is scoped to ordinary Codex scripts, the WW2 mode. The
        # paragraph-preserving workflow retains its existing optional-cleanup contract.
        
        # Анализ текста: кэш по (файл, модель) — один анализ на файл для всех языков
        try:
            _amt = os.path.getmtime(source_file)
        except OSError:
            _amt = 0
        cache_key = (source_file, model, _amt)
        with _ANALYSIS_CACHE_LOCK:
            analysis = _ANALYSIS_CACHE.get(cache_key)
        if analysis is None:
            log_msg(log_queue, f"📊 {lang_display}: Analyzing text structure...")
            analysis = analyze_text_structure(
                text, log_queue,
                use_subscription=use_subscription,
                oauth_token=tokens[current_token_index[0]] if tokens else oauth_token,
                model=model,
                api_key=api_key,
                oauth_tokens=tokens if use_subscription and len(tokens) > 1 else None,
                current_token_index=current_token_index if use_subscription and len(tokens) > 1 else None,
            )
            with _ANALYSIS_CACHE_LOCK:
                _ANALYSIS_CACHE[cache_key] = analysis
        else:
            log_msg(log_queue, f"📊 {lang_display}: анализ из кэша (файл уже обработан)")

        # === Claude rewrite mode (2026-06-03) ===
        # Для Claude — крупный chunk (60-80K chars ≈ один кусок на ≤14K слов) и
        # natural reroll (preserve_paragraphs=False) уже передаётся snippet сверху.
        # GPT-путь (preserve=True, мелкий chunk) не трогаем.
        _is_claude = ("claude" in (model or "").lower()) or (use_subscription and not _is_codex)
        if _is_claude and not preserve_paragraphs:
            original_chunk_size = chunk_size
            chunk_size = int(os.environ.get("CLAUDE_CHUNK_SIZE", "20000"))  # 2026-06-11: 60000->20000, промпт ~35K (opus стабилен; на 65K падал stream-stall #25979)
            if chunk_size != original_chunk_size:
                log_msg(log_queue, f"🔧 {lang_display}: chunk_size bump для Claude {original_chunk_size}→{chunk_size} chars (меньше швов = лучше связность)")

        # PATCH-2026-06-13-e2big-guard: preserve режет по абзацам (\n\n) и НЕ дробит один
        # огромный абзац. Текст без структуры абзацев (1 гигантский абзац) ушёл бы
        # одним промптом-аргументом → CLI 'Argument list too long' (E2BIG, >128КБ/арг).
        # В таком случае preserve бессмыслен — откатываемся на size-chunking ≤20К.
        if preserve_paragraphs:
            _paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
            _maxpara = max((len(p) for p in _paras), default=0)
            if _maxpara > 30000:
                log_msg(log_queue, f"\u26a0\ufe0f {lang_display}: \u043c\u0430\u043a\u0441. \u0430\u0431\u0437\u0430\u0446 {_maxpara} \u0441\u0438\u043c\u0432 > 30000 \u2014 \u043d\u0435\u0442 \u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u044b \u0430\u0431\u0437\u0430\u0446\u0435\u0432, preserve \u0432\u044b\u043a\u043b\u044e\u0447\u0435\u043d, \u0440\u0435\u0436\u0443 \u043f\u043e \u0440\u0430\u0437\u043c\u0435\u0440\u0443 (\u0437\u0430\u0449\u0438\u0442\u0430 \u043e\u0442 E2BIG)")
                preserve_paragraphs = False
                chunk_size = int(os.environ.get("CLAUDE_CHUNK_SIZE", "20000"))

        if preserve_paragraphs:
            chunks, chunk_para_counts = split_text_to_chunks_by_paragraphs(text, chunk_size)
            total_input_paragraphs = sum(chunk_para_counts) if chunk_para_counts else count_paragraphs(text)
            log_msg(log_queue, f"📐 {lang_display}: режим сохранения абзацев — {total_input_paragraphs} абзацев → {len(chunks)} чанков")
        else:
            chunks = split_text_to_chunks(text, chunk_size)
            chunk_para_counts = []
            total_input_paragraphs = 0
        total_chunks = len(chunks)
        
        # B5 PATCH-2026-06-25-secfix: user_id для tenant-scoped ключа прогресса.
        # output_dir = outputs/<user_id> -> basename = user_id (== job.user_id).
        _progress_job_id = progress_scope or job_id
        _uid = get_current_user_id() or (os.path.basename(os.path.normpath(output_dir)) if output_dir else "")
        # Получаем или создаём задачу в менеджере прогресса
        task = PROGRESS_MANAGER.get_or_create_task(
            source_file, lang_name, lang_display, total_chunks,
            user_id=_uid, job_id=_progress_job_id,
        )
        
        # Проверяем, не завершена ли уже
        if (task.status == TaskStatus.COMPLETED.value
                and getattr(task, "output_file", "")
                and os.path.isfile(task.output_file)
                and os.path.getsize(task.output_file) > 0
                and (not _strict_codex or cleanup_receipt_valid(task.output_file, lang_name, channel_name, host_name))):
            log_msg(log_queue, f"✅ {lang_display}: уже завершено ранее")
            if _strict_codex:
                clear_quality_review_error(job_id, lang_name, output_basename or os.path.splitext(os.path.basename(source_file))[0][:50])
            return (lang_display, True)
        candidate_base = output_basename or os.path.splitext(os.path.basename(source_file))[0][:50]
        existing_output = getattr(task, "output_file", "") or os.path.join(output_dir, make_output_filename(candidate_base, lang_name))
        if (_strict_codex and os.path.isfile(existing_output)
                and not cleanup_receipt_valid(existing_output, lang_name, channel_name, host_name)):
            log_msg(log_queue, f"🔎 {lang_display}: сохранённый текст без действующей квитанции проверки — редактура сохранённых частей")
            cleanup_quarantine_unverified_output(existing_output, output_dir)
            PROGRESS_MANAGER.set_task_output(source_file, lang_name, "", user_id=_uid, job_id=_progress_job_id)
        
        # Определяем какие чанки нужно обработать
        incomplete_chunks = PROGRESS_MANAGER.get_incomplete_chunks(task)
        
        if not incomplete_chunks:
            log_msg(log_queue, f"🔄 {lang_display}: все части готовы; завершаю сохранение")
        
        completed_count = total_chunks - len(incomplete_chunks)
        if completed_count > 0:
            log_msg(log_queue, f"🔄 {lang_display}: продолжаю с части {completed_count + 1}/{total_chunks}")
        else:
            log_msg(log_queue, f"🌍 {lang_display}: начинаю ({total_chunks} частей)")
        
        # Определяем провайдера из модели и флага подписки
        keys_cfg = get_keys_config()
        provider = _detect_provider(model, use_subscription)
        if provider in ("subscription", "codex_subscription"):
            actual_api_key = ""
        elif provider == "claude":
            # BYOK-2026-07-30: свой Anthropic-ключ тенанта, иначе серверный —
            # но серверный только владельцу (задача B).
            actual_api_key = api_key or get_effective_anthropic_key()
        else:
            actual_api_key = get_effective_openai_key()
        # key_source сообщает ТОЛЬКО источник (user/server), не значение.
        log_msg(
            log_queue,
            f"🔧 {lang_display}: model='{model}', provider='{provider}', "
            f"use_sub={use_subscription}, "
            f"key_source={credential_source(classify_provider(model))}",
        )

        # Обрабатываем только незавершённые чанки
        for chunk_idx in incomplete_chunks:
            if STOP_FLAG.is_stopped():
                log_msg(log_queue, f"⏹️ {lang_display}: остановлено, прогресс сохранён")
                return (lang_display, False)
            # FIX-2026-09-13-models: соседний язык уже упёрся в недоступную модель —
            # эта часть получит тот же отказ, вызов не тратим.
            _model_reason = model_unavailable_reason(job_id)
            if _model_reason:
                log_msg(log_queue, f"❌ {lang_display}: {CODEX_MODEL_UNAVAILABLE_PREFIX}: {_model_reason}")
                return (lang_display, False)
            
            chunk_text = chunks[chunk_idx]
            if _strict_codex:
                # Preserve raw 8000-character boundaries and old checkpoint indexes.
                # Cleaning the full source before splitting would corrupt partial resume.
                chunk_text, source_noise_removed = preclean_rewrite_source(chunk_text)
                if source_noise_removed:
                    log_msg(log_queue, f"🧹 {lang_display}: часть {chunk_idx + 1}, удалено явных ASR-меток/петель {source_noise_removed}")
            
            # Контекст из предыдущих чанков: последние 3 предложения для согласованности имён и стиля
            prev_chunk_context = ""
            if chunk_idx > 0:
                prev_task_key = PROGRESS_MANAGER._task_key(
                    source_file, lang_name, user_id=_uid, job_id=_progress_job_id
                )
                prev_task = PROGRESS_MANAGER.tasks.get(prev_task_key)
                if prev_task:
                    prev_chunk = prev_task.chunks[chunk_idx - 1] if chunk_idx - 1 < len(prev_task.chunks) else None
                    if prev_chunk and prev_chunk.get("status") == "completed" and prev_chunk.get("result"):
                        prev_text = prev_chunk["result"]
                        prev_sentences = re.split(r"(?<=[.!?])\s+", prev_text)
                        prev_chunk_context = " ".join(prev_sentences[-3:]) if len(prev_sentences) > 3 else prev_text[-500:]

            # Отмечаем как in_progress
            PROGRESS_MANAGER.update_chunk(
                source_file, lang_name, chunk_idx, "in_progress",
                user_id=_uid, job_id=_progress_job_id,
            )
            
            # Системный промт (для кэширования!)
            # ВАЖНО: system_prompt должен быть одинаковым для всех чанков одного языка!
            # ВАЖНО: lang_display должен быть одинаковым для всех чанков!
            system_prompt = get_system_prompt(lang_name, lang_display, analysis, address_style, custom_prompt)
            
            # Логируем хеш system prompt для проверки идентичности
            system_hash = hashlib.md5(system_prompt.encode()).hexdigest()[:8]
            # CONTRACT-2026-09-15: retry-промпты ниже не строятся через get_rewrite_prompt, поэтому
            # правило брендинга повторяется здесь — иначе повтор по длине терял единственное
            # представление диктора/канала (smoke 15.09, режим «оба имени»: 154% → retry → без имён).
            _brand_names = " and ".join(
                ([f'the host "{(host_name or "").strip()}"'] if (host_name or "").strip() else [])
                + ([f'the channel "{(channel_name or "").strip()}"'] if (channel_name or "").strip() else []))
            # CONTRACT-C-2026-09-15: роли частей те же, что в get_rewrite_prompt: представление в первой,
            # один призыв в центральной (или в единственной), прощание + просьба в последней.
            _ch = (channel_name or "").strip()
            _brand_bits = []
            if _brand_names and chunk_idx == 0:
                _brand_bits.append(f"after the hook, introduce ONCE (1-2 sentences) {_brand_names} — exact strings, nowhere else in the story")
            if _ch and (total_chunks == 1 or (chunk_idx == total_chunks // 2 and chunk_idx != total_chunks - 1)):
                _brand_bits.append(f'ONE short like-and-subscribe call-to-action for the channel "{_ch}" around the middle of this part (in the MIDDLE of the story, NOT at the end; only like and subscribe — no comment/share/bell requests)')
            if _brand_names and chunk_idx == total_chunks - 1:
                _brand_bits.append(f"at the very END, after the source's own ending, a separate sign-off naming {_brand_names} plus ONE short like-and-subscribe request — these are the LAST sentences of the text; only like and subscribe, no comment/share/bell requests, no 'write where you are from', no thanks-for-watching")
            if _brand_bits:
                _brand_line = "- Branding (CONTRACT, exactly as listed, nothing more): " + "; ".join(_brand_bits) + "."
            elif _brand_names:
                _brand_line = "- Branding: the host/channel are handled in other parts — do NOT mention them, add NO call-to-action and NO sign-off in this part."
            else:
                _brand_line = "- Branding: NO host name, NO channel name, NO narrator identity, NO call-to-action, NO sign-off anywhere; do not invent one."
            system_length = len(system_prompt)
            estimated_tokens = system_length // 4
            
            if chunk_idx == 0:
                log_msg(log_queue, f"  🔑 System prompt: hash={system_hash}, длина={system_length} символов (~{estimated_tokens} токенов)")
                log_msg(log_queue, f"  🔑 lang_name='{lang_name}', lang_display='{lang_display}' (должны быть одинаковыми для всех чанков)")
                if custom_prompt:
                    log_msg(log_queue, f"  📝 Custom prompt: {custom_prompt[:80]}{'...' if len(custom_prompt) > 80 else ''}")
            else:
                log_msg(log_queue, f"  🔑 System prompt: hash={system_hash} (должен совпадать с первым: {system_hash})")
            
            # Проверяем минимальную длину для кэширования
            if system_length < 4096:
                log_msg(log_queue, f"  ⚠️ ВНИМАНИЕ: System prompt слишком короткий ({system_length} < 4096), кэш не будет работать!")
            
            # Ожидаемое число абзацев в этом чанке (если включён режим)
            expected_paragraphs = (
                chunk_para_counts[chunk_idx]
                if preserve_paragraphs and chunk_idx < len(chunk_para_counts)
                else 0
            )

            # Формируем сообщения
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": get_rewrite_prompt(
                    lang_name, chunk_text, chunk_idx, total_chunks,
                    lang_display, channel_name, host_name, analysis, prev_chunk_context,
                    preserve_paragraphs=preserve_paragraphs,
                    expected_paragraphs=expected_paragraphs,
                )}
            ]
            
            # Вызываем API (с ротацией токенов при лимите подписки)
            task_info = f"{lang_display} {chunk_idx+1}/{total_chunks}"
            if use_subscription and tokens:
                success, response = call_api_universal_with_token_rotation(
                    messages, actual_api_key, model, log_queue, task_info, provider, tokens, current_token_index
                )
            else:
                success, response = call_api_universal(
                    messages, actual_api_key, model, log_queue, task_info, provider,
                    tokens[current_token_index[0]] if tokens else oauth_token,
                )
            
            if STOP_FLAG.is_stopped():
                log_msg(log_queue, f"⏹️ {lang_display}: остановлено")
                return (lang_display, False)
            
            if not success:
                # Критическая ошибка (не rate limit)
                remember_limit_hit(job_id, response)
                if str(response).startswith(HOST_BUDGET_WAIT_PREFIX):
                    remember_host_budget_wait(job_id)
                    log_msg(log_queue, f"⏳ {lang_display}: ожидание общего лимита ключа")
                    return (lang_display, False)
                log_msg(log_queue, f"❌ {lang_display}: часть {chunk_idx+1} - {response}")
                PROGRESS_MANAGER.update_chunk(
                    source_file, lang_name, chunk_idx, "failed",
                    error=response, user_id=_uid, job_id=_progress_job_id,
                )
                
                if "API ключ" in response or "не найдена" in response:
                    return (lang_display, False)
                if response.startswith(CODEX_MODEL_UNAVAILABLE_PREFIX):
                    # FIX-2026-09-13-models: провайдер не даёт модель — остальные части,
                    # круги авто-ретрая и соседние языки получат тот же отказ.
                    remember_model_unavailable(job_id, model)
                    return (lang_display, False)
                continue
            
            # Успех!
            extracted = extract_from_brackets(response)

            # ---- Валидация структуры абзацев (1:1 alignment) ----
            if preserve_paragraphs and expected_paragraphs > 0:
                # Эвристика: если модель использовала одинарный \n вместо \n\n — восстанавливаем
                extracted = normalize_paragraph_separators(extracted, expected_paragraphs)
                actual_paragraphs = count_paragraphs(extracted)
                paragraph_retries = 0
                MAX_PARAGRAPH_RETRIES = 3
                while actual_paragraphs != expected_paragraphs and paragraph_retries < MAX_PARAGRAPH_RETRIES:
                    paragraph_retries += 1
                    log_msg(
                        log_queue,
                        f"   ⚠️ Chunk {chunk_idx+1}: paragraphs mismatch ({actual_paragraphs}/{expected_paragraphs}), retry {paragraph_retries}/{MAX_PARAGRAPH_RETRIES}"
                    )
                    para_retry_user = get_rewrite_prompt(
                        lang_name, chunk_text, chunk_idx, total_chunks,
                        lang_display, channel_name, host_name, analysis, prev_chunk_context,
                        preserve_paragraphs=True,
                        expected_paragraphs=expected_paragraphs,
                        strict_retry=True,
                    )
                    # Дополняем явным указанием что предыдущая попытка дала {actual_paragraphs}
                    para_retry_user += (
                        f"\n\nPrevious attempt produced {actual_paragraphs} paragraphs — that is WRONG. "
                        f"Output EXACTLY {expected_paragraphs}."
                    )
                    para_messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": para_retry_user},
                    ]
                    if use_subscription and tokens:
                        ok_p, resp_p = call_api_universal_with_token_rotation(
                            para_messages, actual_api_key, model, log_queue,
                            f"{task_info}_para_retry{paragraph_retries}", provider, tokens, current_token_index
                        )
                    else:
                        ok_p, resp_p = call_api_universal(
                            para_messages, actual_api_key, model, log_queue,
                            f"{task_info}_para_retry{paragraph_retries}", provider,
                            tokens[current_token_index[0]] if tokens else oauth_token,
                        )
                    if ok_p:
                        extracted_p = extract_from_brackets(resp_p)
                        extracted_p = normalize_paragraph_separators(extracted_p, expected_paragraphs)
                        actual_p = count_paragraphs(extracted_p)
                        if actual_p == expected_paragraphs:
                            extracted = extracted_p
                            actual_paragraphs = actual_p
                            log_msg(
                                log_queue,
                                f"   ✅ Chunk {chunk_idx+1}: paragraph structure OK ({expected_paragraphs}/{expected_paragraphs}) after {paragraph_retries} retries"
                            )
                            break
                        else:
                            actual_paragraphs = actual_p
                if actual_paragraphs != expected_paragraphs:
                    # Fallback: рекурсивный split — режем чанк пополам по абзацам и
                    # переводим каждую половину отдельно. На малом размере модель
                    # справляется лучше; в худшем случае дойдём до 1 абзаца.
                    log_msg(
                        log_queue,
                        f"   🪓 Chunk {chunk_idx+1}: 3 retries не помогли ({actual_paragraphs}/{expected_paragraphs}), запускаю рекурсивный split"
                    )

                    def _api_call(messages_):
                        if use_subscription and tokens:
                            return call_api_universal_with_token_rotation(
                                messages_, actual_api_key, model, log_queue,
                                f"{task_info}_split", provider, tokens, current_token_index
                            )
                        return call_api_universal(
                            messages_, actual_api_key, model, log_queue,
                            f"{task_info}_split", provider,
                            tokens[current_token_index[0]] if tokens else oauth_token,
                        )

                    input_paragraphs = chunk_text.split("\n\n")
                    fallback_text = translate_paragraphs_recursive(
                        input_paragraphs, lang_name, lang_display,
                        chunk_idx, total_chunks, channel_name, host_name,
                        analysis, prev_chunk_context, system_prompt,
                        _api_call, log_queue, depth=0,
                    )
                    fallback_count = count_paragraphs(fallback_text)
                    if fallback_count == expected_paragraphs:
                        extracted = fallback_text
                        actual_paragraphs = fallback_count
                        log_msg(
                            log_queue,
                            f"   ✅ Chunk {chunk_idx+1}: paragraphs восстановлены через split → {fallback_count}/{expected_paragraphs}"
                        )
                    else:
                        log_msg(
                            log_queue,
                            f"   ⚠️ {lang_display}: chunk {chunk_idx+1} paragraphs FAILED ({fallback_count}/{expected_paragraphs}) даже после split — помечаю чанк как failed, продолжаю остальные"
                        )
                        PROGRESS_MANAGER.update_chunk(
                            source_file, lang_name, chunk_idx, "failed",
                            error=f"paragraphs {fallback_count}/{expected_paragraphs}",
                            user_id=_uid, job_id=_progress_job_id,
                        )
                        continue
                else:
                    log_msg(
                        log_queue,
                        f"   ✅ Chunk {chunk_idx+1}: paragraphs preserved {expected_paragraphs}/{expected_paragraphs}"
                    )

            # Проверка длины с retry
            orig_len = len(chunk_text)
            extracted_len = len(extracted)
            ratio = extracted_len / orig_len if orig_len > 0 else 1.0
            _cta_short_ok = _strict_codex and final_cta_short_output_ok(
                chunk_text, extracted, chunk_idx, total_chunks, channel_name, host_name,
            )
            if _cta_short_ok and ratio < SHORT_RETRY_RATIO:
                log_msg(log_queue, f"   ✅ Chunk {chunk_idx+1}: исходная часть целиком CTA донора — принимаю короткое обращение, финальная редактура обязательна")

            # Детальное логирование для отладки
            if extracted_len < 100:  # Подозрительно короткий результат
                log_msg(log_queue, f"   ⚠️ WARNING: Extracted text very short ({extracted_len} chars), response length: {len(response)}")
                log_msg(log_queue, f"   🔍 Response preview: {response[:200]}...")

            if ratio < SHORT_RETRY_RATIO and not _cta_short_ok and not (ratio >= DENSE_SCRIPT_SKIP_FROM and chunk_is_dense_script(extracted)):  # короткий ИЛИ нарратив-преамбула
                log_msg(log_queue, f"   ⚠️ Chunk {chunk_idx+1}: SHORT ({ratio:.1%}, {extracted_len}/{orig_len} chars) — анти-нарратив retry...")
                # FIX-2026-06-28-narration: нарратив opus-4-6 интермиттентный (~17%, детерминированно не убрать).
                # Лечим ПОВТОРАМИ с жёстким анти-нарратив-промптом (старт с '[', запрет плана/«I need to»/«Let me»).
                # До N попыток: 0.17^N → при 4 ≈ 0.08% что все нарративят. Берём лучший (самый длинный) результат.
                _NARR_MAX = short_retry_limit(ratio, int(os.environ.get("NARRATION_MAX_RETRIES", "4")))
                best_extracted, best_len = extracted, extracted_len
                for _ratt in range(1, _NARR_MAX + 1):
                    retry_prompt = f"""STOP. Your previous reply was NOT the translation — it was planning / a preamble (like "I need to...", "Let me first...") or far too short. That is a FAILURE.

Output ONLY the COMPLETE translation/adaptation of the text below, inside [square brackets].
- Your VERY FIRST character MUST be '[' — start translating IMMEDIATELY.
- Do NOT write ANY analysis, planning, thinking, notes, or commentary.
- NEVER begin with "I need to", "Let me", "I will", "First", "Here", "I'll", "Okay", "The".
- Translate EVERY sentence and keep ALL facts. Numbers and dates as words.
- Do NOT add anything that is not in the text below to make the output longer: no new facts, quotes, examples, explanations, disclaimers, summaries and no second ending. A complete {lang_name} version is often shorter in characters than the original — that is fine.
- This is a REWRITE for a voice-over, not a plain translation: keep the role of this part, keep every section marker of the source in its own position and shape, keep a bridge sentence before every switch of topic or scene, do not hedge what the source asserts, and spell every name exactly as it was spelled earlier in this text.
- Do NOT restore or add the donor's greetings, like/subscribe/comment requests, sponsor lines or farewells even if that makes the text longer; add no call-to-action and no sign-off except exactly what the Branding line below asks for. Keep historical testimony and quotations attributed to their real speakers; never rename a historical person.
{_brand_line}

TEXT TO TRANSLATE:
{chunk_text}

Now output the full translation, starting with '[':"""
                    retry_messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": retry_prompt},
                    ]
                    success_retry, response_retry = call_api_universal(
                        retry_messages, actual_api_key, model, log_queue,
                        f"{task_info}_nretry{_ratt}", provider,
                        tokens[current_token_index[0]] if tokens else oauth_token,
                    )
                    if not success_retry:
                        log_msg(log_queue, f"   ⚠️ анти-нарратив retry {_ratt}/{_NARR_MAX}: вызов упал, дальше")
                        continue
                    extracted_retry = extract_from_brackets(response_retry)
                    retry_len = len(extracted_retry)
                    retry_ratio = retry_len / orig_len if orig_len > 0 else 1.0
                    if _strict_codex and final_cta_short_output_ok(
                            chunk_text, extracted_retry, chunk_idx, total_chunks, channel_name, host_name):
                        best_extracted = extracted_retry
                        log_msg(log_queue, f"   ✅ анти-нарратив retry {_ratt}/{_NARR_MAX}: принято короткое обращение вместо рекламного CTA донора")
                        break
                    _cand_ok, _cand_why = short_retry_candidate_ok(ratio, retry_ratio)
                    if not _cand_ok:
                        log_msg(log_queue, f"   ⛔ retry {_ratt}/{_NARR_MAX}: {retry_len} chars отклонён — {_cand_why}")
                        continue
                    if retry_len > best_len:
                        best_extracted, best_len = extracted_retry, retry_len
                    if retry_ratio >= SHORT_RETRY_RATIO:
                        log_msg(log_queue, f"   ✅ анти-нарратив retry {_ratt}/{_NARR_MAX} OK: {retry_len} chars ({retry_ratio:.1%})")
                        break
                    log_msg(log_queue, f"   ⚠️ retry {_ratt}/{_NARR_MAX}: всё ещё {retry_ratio:.1%} ({retry_len}/{orig_len}) — повтор")
                extracted = _accept_retry_if_paragraphs_ok(
                    extracted, best_extracted,
                    preserve_paragraphs, expected_paragraphs,
                    log_queue, chunk_idx, "narration-retry-loop",
                )
            elif ratio > MAX_CHUNK_LENGTH_RATIO:  # Раздувание сверх порога — retry без добавления воды
                log_msg(log_queue, f"   ⚠️ Chunk {chunk_idx+1}: TOO LONG ({ratio:.1%}, {extracted_len}/{orig_len} chars), retrying...")
                max_allowed = int(orig_len * MAX_CHUNK_LENGTH_RATIO)
                retry_long_prompt = f"""Your output is TOO LONG. You added {extracted_len - orig_len} extra characters.

Original: {orig_len} characters. Your output: {extracted_len} (max allowed: {max_allowed}).

Do NOT add new content, examples, or padding. Preserve meaning and style but stay within 95-{int(MAX_CHUNK_LENGTH_RATIO*100)}% of original length.
Do NOT add or restore the donor's greetings, calls-to-action (like/subscribe/comment), sponsor lines or farewells; add only what the Branding line below asks for; keep historical testimony and quotations attributed to their real speakers.
{_brand_line}

Original text:
{chunk_text}

TASK: Return a complete adaptation that is NOT longer than {int(MAX_CHUNK_LENGTH_RATIO*100)}% of the original. Output ONLY the adapted text in square brackets."""

                retry_long_messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": retry_long_prompt}
                ]
                success_retry, response_retry = call_api_universal(
                    retry_long_messages, actual_api_key, model, log_queue,
                    f"{task_info}_retry_long", provider,
                    tokens[current_token_index[0]] if tokens else oauth_token,
                )
                if success_retry:
                    extracted_retry = extract_from_brackets(response_retry)
                    retry_len = len(extracted_retry)
                    retry_ratio = retry_len / orig_len if orig_len > 0 else 1.0
                    if retry_ratio <= MAX_CHUNK_LENGTH_RATIO and retry_ratio >= 0.90:
                        log_msg(log_queue, f"   ✅ Retry LONG success: {retry_len} chars ({retry_ratio:.1%})")
                        extracted = _accept_retry_if_paragraphs_ok(
                            extracted, extracted_retry,
                            preserve_paragraphs, expected_paragraphs,
                            log_queue, chunk_idx, "long-retry-success",
                        )
                    elif retry_len < extracted_len:
                        log_msg(log_queue, f"   📉 Using retry version (shorter: {retry_len} vs {extracted_len})")
                        extracted = _accept_retry_if_paragraphs_ok(
                            extracted, extracted_retry,
                            preserve_paragraphs, expected_paragraphs,
                            log_queue, chunk_idx, "long-retry-shorter",
                        )
                else:
                    log_msg(log_queue, f"   ⚠️ Retry LONG failed, keeping current output")
            else:
                log_msg(log_queue, f"   ✅ Chunk {chunk_idx+1}: {extracted_len} chars ({ratio:.1%})")

            # === Per-paragraph length safety-net ===
            # Defence-in-depth поверх prompt-rules: если хоть один out_para раздут >LENGTH_RATIO_LIMIT
            # от src_para — length retry с явным указанием модели на проблемный абзац.
            # Лимит MAX_LENGTH_RETRIES; после исчерпания принимаем как есть с warning (не блокируем).
            if preserve_paragraphs and expected_paragraphs > 0:
                length_retry_count = 0
                while length_retry_count < MAX_LENGTH_RETRIES:
                    input_paras = chunk_text.split("\n\n")
                    output_paras = re.split(r"\n\s*\n", extracted)
                    violation = None
                    for _i, (src_p, out_p) in enumerate(zip(input_paras, output_paras)):
                        if len(out_p) > LENGTH_RATIO_LIMIT * max(len(src_p), LENGTH_FLOOR):
                            violation = (_i, len(src_p), len(out_p))
                            break
                    if violation is None:
                        break
                    _idx, _src_len, _out_len = violation
                    length_retry_count += 1
                    log_msg(
                        log_queue,
                        f"   ⚠️ Chunk {chunk_idx+1}: paragraph {_idx+1} раздут {_out_len}/{_src_len} ({_out_len/max(_src_len,1):.1f}×) — length retry {length_retry_count}/{MAX_LENGTH_RETRIES}"
                    )
                    len_retry_user = get_rewrite_prompt(
                        lang_name, chunk_text, chunk_idx, total_chunks,
                        lang_display, channel_name, host_name, analysis, prev_chunk_context,
                        preserve_paragraphs=True,
                        expected_paragraphs=expected_paragraphs,
                        strict_retry=True,
                    )
                    len_retry_user += (
                        f"\n\nPrevious attempt produced paragraph #{_idx+1} with {_out_len} chars from "
                        f"a source paragraph of {_src_len} chars (ratio {_out_len/max(_src_len,1):.1f}×). "
                        f"That is INFLATION. Output ALL paragraphs within 0.7×–1.5× of source length. "
                        f"Do NOT add sensory details, descriptions, or actions absent from source."
                    )
                    len_retry_messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": len_retry_user},
                    ]
                    if use_subscription and tokens:
                        ok_l, resp_l = call_api_universal_with_token_rotation(
                            len_retry_messages, actual_api_key, model, log_queue,
                            f"{task_info}_len_retry{length_retry_count}", provider, tokens, current_token_index
                        )
                    else:
                        ok_l, resp_l = call_api_universal(
                            len_retry_messages, actual_api_key, model, log_queue,
                            f"{task_info}_len_retry{length_retry_count}", provider,
                            tokens[current_token_index[0]] if tokens else oauth_token,
                        )
                    if ok_l:
                        extracted_l = extract_from_brackets(resp_l)
                        extracted_l = normalize_paragraph_separators(extracted_l, expected_paragraphs)
                        if count_paragraphs(extracted_l) == expected_paragraphs:
                            extracted = extracted_l
                        else:
                            log_msg(
                                log_queue,
                                f"   ⚠️ Length retry дал {count_paragraphs(extracted_l)}/{expected_paragraphs} абзацев — отклоняю, оставляю прежний"
                            )
                    else:
                        log_msg(log_queue, f"   ⚠️ Length retry API failed — оставляю прежний")
                if length_retry_count >= MAX_LENGTH_RETRIES and violation is not None:
                    log_msg(
                        log_queue,
                        f"   ❌ Chunk {chunk_idx+1}: {MAX_LENGTH_RETRIES} length retries провалены, принимаю с warning (не блокирую сохранение)"
                    )

            # Снимок до постобработки — на случай отката если структура абзацев сломается
            extracted_before_postproc = extracted

            # ПОСТОБРАБОТКА: Удаляем русские слова через 4o-mini
            # Находим предложения с кириллицей и заменяем их через дешевую модель
            if not _is_codex:
                extracted = postprocess_remove_russian_words(
                    extracted,
                    lang_name,
                    get_postprocess_openai_key(),
                    log_queue
                )

            # Финальная санитарная очистка перед записью в .txt для TTS
            extracted = sanitize_final_text(extracted, lang_name)

            # Нормализация: trailing/leading blank на границе чанка после "\n\n".join
            # сливается с межчанковым разделителем и теряет абзац — приводим к канону.
            extracted = extracted.strip("\n")
            extracted = re.sub(r"\n\s*\n", "\n\n", extracted)

            # Если включена защита абзацев — постобработка не должна изменить их количество
            if preserve_paragraphs and expected_paragraphs > 0:
                final_para = count_paragraphs(extracted)
                if final_para != expected_paragraphs:
                    log_msg(
                        log_queue,
                        f"   ⚠️ Chunk {chunk_idx+1}: postprocess сломал абзацы ({final_para}/{expected_paragraphs}), откатываю на до-постобработочный вариант"
                    )
                    extracted = extracted_before_postproc
                    extracted = extracted.strip("\n")
                    extracted = re.sub(r"\n\s*\n", "\n\n", extracted)

                # Двойная страховка: если после rollback структура всё ещё битая —
                # помечаем чанк failed и пропускаем его (errors>0 не даст сохранить файл).
                final_para_after = count_paragraphs(extracted)
                if final_para_after != expected_paragraphs:
                    log_msg(
                        log_queue,
                        f"   ❌ Chunk {chunk_idx+1}: после rollback структура абзацев всё ещё битая ({final_para_after}/{expected_paragraphs}) — помечаю как FAILED"
                    )
                    PROGRESS_MANAGER.update_chunk(
                        source_file, lang_name, chunk_idx, "failed",
                        error=f"paragraph structure broken after rollback: {final_para_after}/{expected_paragraphs}",
                        user_id=_uid, job_id=_progress_job_id,
                    )
                    continue

            # Anti-plan ratio-guard (2026-06-03): после всех retry — если финал катастрофически
            # короткий, это план/преамбула («I need to adapt...»), а не рерайт. НЕ сохраняем
            # как completed — помечаем failed, чтобы партнёр НЕ получил огрызок 0.7% в COMPLETED.
            # Порог 0.30 — НЕ ронять компрессирующие языки (cs/de часто 50-60%), но ловить мусор.
            _final_ratio = len(extracted) / orig_len if orig_len > 0 else 1.0
            _cta_final_ok = _strict_codex and final_cta_short_output_ok(
                chunk_text, extracted, chunk_idx, total_chunks, channel_name, host_name,
            )
            if _final_ratio < 0.30 and not _cta_final_ok:
                log_msg(
                    log_queue,
                    f"   ❌ {lang_display}: chunk {chunk_idx+1} вернул {_final_ratio:.1%} от оригинала "
                    f"({len(extracted)}/{orig_len} chars) — FAILED (не сохраняю огрызок: похоже на план/преамбулу)"
                )
                PROGRESS_MANAGER.update_chunk(
                    source_file, lang_name, chunk_idx, "failed",
                    error=f"output too short: {_final_ratio:.1%} ({len(extracted)}/{orig_len} chars) — likely plan/preface instead of rewrite",
                    user_id=_uid, job_id=_progress_job_id,
                )
                continue

            PROGRESS_MANAGER.update_chunk(
                source_file, lang_name, chunk_idx, "completed",
                result=extracted, user_id=_uid, job_id=_progress_job_id,
            )
            log_msg(log_queue, f"   ✅ {lang_display}: часть {chunk_idx+1}/{total_chunks}")
        
        # Собираем результат
        task = PROGRESS_MANAGER.tasks.get(
            PROGRESS_MANAGER._task_key(
                source_file, lang_name, user_id=_uid, job_id=_progress_job_id
            )
        )
        
        if task:
            # Собираем все завершённые чанки
            results = []
            errors = 0
            for chunk in task.chunks:
                if chunk["status"] == "completed":
                    results.append(chunk["result"])
                else:
                    results.append(f"[ОШИБКА: {chunk.get('error', 'не обработано')}]")
                    errors += 1

            # 3-ступенчатый постпроцесс: Pass A (Branding) + Pass B (AI Declutter) параллельно → merge → Pass C (Nativeness)
            base_name = output_basename or os.path.splitext(os.path.basename(source_file))[0][:50]
            lang_code = lang_name.lower().replace(" ", "_")[:15]  # для логов/постпроцесса
            # При preserve_paragraphs пропускаем pipeline: intro/outro/merge ломают 1:1 структуру
            if _is_codex:
                log_msg(log_queue, f"🧹 {lang_display}: OpenAI Pass A/B/C/D не используются — одна финальная редактура Codex")
            elif preserve_paragraphs:
                log_msg(log_queue, f"📐 {lang_display}: постпроцесс Pass A/B/C/D пропущен (режим сохранения абзацев)")
            elif _is_claude:
                # Claude rewrite (2026-06-03): OpenAI Pass A/B/C/D отключён (gpt-5-mini
                # на сильной модели → synonym swaps + factual errors). Вместо него —
                # лёгкая эвристическая чистка структурного мусора (block markers,
                # markdown headings, преамбулы «Вот переписанный текст:»).
                total_removed = 0
                cleaned_results = []
                for r in results:
                    if r and not str(r).startswith("[ОШИБКА:"):
                        c, removed = sanitize_claude_output(r, lang_name)
                        cleaned_results.append(c)
                        total_removed += removed
                    else:
                        cleaned_results.append(r)
                results = cleaned_results
                log_msg(log_queue, f"🧹 {lang_display}: sanitize Claude output — removed {total_removed} meta lines (postprocess Pass A/B/C/D пропущен для Claude)")
            elif ENABLE_NATIVE_SPEAKER_CHECK and results:
                full_text = "\n\n".join(results)
                full_text = run_postprocess_pipeline(
                    full_text,
                    lang_name,
                    lang_display,
                    host_name,
                    channel_name,
                    get_postprocess_openai_key(),
                    log_queue,
                    output_dir=output_dir,
                    base_name=base_name,
                    lang_code=lang_code,
                    is_generated_text=True,  # имена персонажей не подменяются на диктора
                    rewrite_contract=True,   # CONTRACT-C-2026-09-15: свой призыв/прощание по контракту
                )
                results = [full_text]

            # CLEANUP-2026-09-16: однопроходная чистка вместо мёртвого Pass A/B/C/D.
            # Pass A/B/C/D падал на отказе авторизации (серверный ключ OpenAI пуст), а у
            # Claude был отключён веткой кода с 03.06 — ветки выше оставлены как есть и
            # служат точкой отката. Здесь ОДИН вызов подписки Codex на язык: модель
            # возвращает список точечных замен, применяет их код — по границам слова,
            # с проверкой каждой правки и полным откатом при любом сомнении.
            # Чистка идёт во ВСЕХ режимах, включая сохранение абзацев: правки точечные,
            # переносы строк там запрещены, а число абзацев сверяется дважды (внутри
            # чистки и штатным гейтом ниже). Откат без правки кода: REWRITE_CLEANUP=0.
            # При провале чанков не зовём вовсе — файл всё равно не сохранится.
            try:
                results = cleanup_rewrite_results(
                    results,
                    lang_name,
                    lang_display,
                    log_queue,
                    channel_name=channel_name,
                    host_name=host_name,
                    preserve_paragraphs=preserve_paragraphs,
                    job_id=job_id,
                    base_name=base_name,
                    task_info=f"cleanup_{lang_code}",
                    allow=(errors == 0),
                    strict=_strict_codex,
                )
            except Exception as _cleanup_exc:
                if _strict_codex:
                    remember_quality_review_error(job_id, lang_name, base_name, str(_cleanup_exc))
                    log_msg(log_queue, f"❌ {lang_display}: {_cleanup_exc}; файл не выдаётся, готовые части сохранены")
                    return (lang_display, False)
                # Preserve the pre-existing non-Codex fallback contract.
                log_msg(
                    log_queue,
                    f"🧹 {lang_display}: чистка упала ({type(_cleanup_exc).__name__}: {_cleanup_exc}) — "
                    f"текст оставлен как был"
                )

            # M5 PATCH-2026-06-25-secfix: НЕ сохраняем файл при ЛЮБЫХ ошибках чанков.
            # Раньше гард был только для preserve_paragraphs, а Claude/non-preserve путь писал
            # брак с маркерами [ОШИБКА:...] и отдавал как PARTIAL-downloadable. Теперь любой провал
            # чанка -> файл не пишем, failed chunks дозальются через "🔁 Повторить незавершённые".
            if errors > 0:
                log_msg(
                    log_queue,
                    f"⚠️ {lang_display}: {errors} чанков провалились — файл НЕ сохранён (не отдаём брак со вставками [ОШИБКА:…]). "
                    f"Жми «🔁 Повторить незавершённые» чтобы дозалить только их."
                )
                return (lang_display, False)

            # Сохраняем файл: код языка в начале (cs_, pl_, ro_...)
            output_file = os.path.join(output_dir, make_output_filename(base_name, lang_name))
            os.makedirs(os.path.dirname(output_file), exist_ok=True)
            final_text = "\n\n".join(results)

            # Финальная проверка структуры абзацев — должно быть РОВНО как в оригинале
            if preserve_paragraphs and total_input_paragraphs > 0:
                final_paragraphs = count_paragraphs(final_text)
                if final_paragraphs != total_input_paragraphs:
                    log_msg(
                        log_queue,
                        f"❌ {lang_display}: финальная структура абзацев не совпадает "
                        f"({final_paragraphs}/{total_input_paragraphs}). Файл НЕ сохранён."
                    )
                    return (lang_display, False)
                log_msg(
                    log_queue,
                    f"📐 {lang_display}: paragraphs preserved {final_paragraphs}/{total_input_paragraphs}"
                )

            # ШАГ 4: детектор утечки чисел/англ-слов-чисел — ФЛАГ needs_review, НЕ автофикс.
            # digit-часть универсальна (любой target: все числа должны быть словами).
            # English-word-часть валидна, т.к. пайплайн всегда English→X (для не-английских
            # источников не сработает — но абсолютное правило чисел в промпте защитит на
            # уровне модели). Исключены слова-коллизии: 'one' (pl "one"=они), 'ten'
            # (pl/cs "ten"=этот), 'million'/'billion' (de "Million"/"Billion" — родные) —
            # иначе постоянные ложные срабатывания на нормальном тексте этих языков.
            if lang_name.lower() != "english":
                _digits = re.search(r"\d", final_text)
                _eng_numwords = re.search(
                    r"\b(two|three|four|five|six|seven|eight|nine|"
                    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
                    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|"
                    r"twenties|thirties|forties|fifties|sixties|seventies|eighties|nineties)\b",
                    final_text, re.IGNORECASE
                )
                if _digits or _eng_numwords:
                    _what = []
                    if _digits:
                        _what.append(f"digit '{_digits.group(0)}'")
                    if _eng_numwords:
                        _what.append(f"EN number-word '{_eng_numwords.group(0)}'")
                    _note = "leftover " + " + ".join(_what) + " — числа спелить словами вручную (вычитка)"
                    log_msg(log_queue, f"⚠️ {lang_display}: {_note} → флаг needs_review (не автофикс)")
                    PROGRESS_MANAGER.set_needs_review(
                        source_file, lang_name, _note,
                        user_id=_uid, job_id=_progress_job_id,
                    )

            if _strict_codex:
                pending_file = output_file + ".pending-" + str(time.time_ns())
                with open(pending_file, 'w', encoding='utf-8') as f:
                    f.write(final_text)
                cleanup_write_receipt(pending_file, lang_name, channel_name, host_name)
                if not cleanup_receipt_valid(pending_file, lang_name, channel_name, host_name):
                    raise RewriteQualityError("QUALITY_REVIEW_FAILED: output receipt could not be verified")
                # Only a complete verified payload becomes a downloadable .txt.
                os.replace(pending_file + ".quality.json", output_file + ".quality.json")
                os.replace(pending_file, output_file)
                clear_quality_review_error(job_id, lang_name, base_name)
            else:
                with open(output_file, 'w', encoding='utf-8') as f:
                    f.write(final_text)

            PROGRESS_MANAGER.set_task_output(
                source_file, lang_name, output_file,
                user_id=_uid, job_id=_progress_job_id,
            )

            if errors == 0:
                log_msg(log_queue, f"✅ {lang_display}: готово → {os.path.basename(output_file)}")
                return (lang_display, True)
            else:
                log_msg(log_queue, f"⚠️ {lang_display}: готово с {errors} ошибками")
                return (lang_display, False)
        
        return (lang_display, False)
        
    except Exception as e:
        if isinstance(e, RewriteQualityError):
            remember_quality_review_error(job_id, lang_name, output_basename or os.path.splitext(os.path.basename(source_file))[0][:50], str(e))
        log_msg(log_queue, f"❌ {lang_display}: {e}")
        return (lang_display, False)


def generate_and_postprocess_single_text(*args, **kwargs) -> Tuple[str, bool]:
    """DEPRECATED (Round 2, 2026-05-23): старая all-in-one generation+postprocess function.
    Заменена в новом worker (_execute_generation_job в server.py) chunk-loop через build_chunk_prompt()
    + run_postprocess_pipeline() как отдельной стадией. Любой её вызов теперь — явная ошибка,
    не silent корруптом со старой schema preset'ов.
    """
    raise NotImplementedError(
        "generate_and_postprocess_single_text removed in Round 2 (2026-05-23). "
        "New flow: server.py:_execute_generation_job + rewrite_core:build_chunk_prompt + run_postprocess_pipeline."
    )

# ============== СКАЧИВАНИЕ СУБТИТРОВ И ОБЛОЖЕК ==============
