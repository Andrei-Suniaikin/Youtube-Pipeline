"""Private per-user vendor keys. Public callers receive counts, never secrets."""
import json
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from contextlib import contextmanager

FAMILIES = ("fastgen", "openai", "veo", "xai")
_secrets = set()
_secret_lock = threading.Lock()


class CredentialSettingsError(ValueError):
    pass


def _path():
    base = os.environ.get("COVER_SERVER_DIR", str(Path(__file__).parent))
    return Path(os.environ.get("COVER_USER_KEYS_DB", str(Path(base) / ".cover-user-keys.db")))


@contextmanager
def _connect():
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise CredentialSettingsError("Хранилище личных ключей должно иметь права 0600")
    else:
        os.close(fd)
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS user_credentials (user_id TEXT PRIMARY KEY, mode TEXT NOT NULL, keys_json TEXT NOT NULL, updated_at REAL NOT NULL)")
        with conn:
            yield conn
    finally:
        conn.close()


def _register(keys):
    with _secret_lock:
        _secrets.update(key for values in keys.values() for key in values)


def redact_credentials(value):
    text = str(value)
    with _secret_lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    return text


def read_user_credentials(user_id):
    if not user_id:
        return {"mode": "server", "keys": {}}
    with _connect() as conn:
        row = conn.execute("SELECT mode,keys_json FROM user_credentials WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        return {"mode": "server", "keys": {}}
    try:
        keys = json.loads(row[1])
        if row[0] not in ("own", "server") or not isinstance(keys, dict):
            raise ValueError()
        for family, values in keys.items():
            if family not in FAMILIES or not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                raise ValueError()
    except (ValueError, TypeError):
        raise CredentialSettingsError("Хранилище личных ключей повреждено; генерация остановлена") from None
    _register(keys)
    return {"mode": row[0], "keys": keys}


def public_credentials(user_id):
    config = read_user_credentials(user_id)
    return {"mode": config["mode"], "counts": {family: len(config["keys"].get(family, [])) for family in FAMILIES}, "max_keys_per_provider": 100}


def save_user_credentials(user_id, body):
    if not user_id:
        raise CredentialSettingsError("Требуется авторизация")
    if not isinstance(body, dict) or set(body) - {"mode", "keys"}:
        raise CredentialSettingsError("Допустимы только mode и keys")
    mode = body.get("mode")
    if mode is not None and mode not in ("own", "server"):
        raise CredentialSettingsError("Выберите личные или серверные ключи")
    changes = body.get("keys", {})
    if not isinstance(changes, dict) or set(changes) - set(FAMILIES):
        raise CredentialSettingsError("Неизвестный провайдер ключей")
    clean = {}
    for family, values in changes.items():
        if not isinstance(values, list) or len(values) > 100:
            raise CredentialSettingsError("Для провайдера допустим массив до 100 ключей")
        normalized = []
        for value in values:
            if not isinstance(value, str) or not 8 <= len(value.strip()) <= 4096 or any(c.isspace() for c in value.strip()):
                raise CredentialSettingsError("Ключ должен содержать 8–4096 символов без пробелов")
            normalized.append(value.strip())
        clean[family] = list(dict.fromkeys(normalized))
    # Merge and mode change form one transaction; parallel updates cannot lose a family.
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT mode,keys_json FROM user_credentials WHERE user_id=?", (user_id,)).fetchone()
        current_mode, keys = (row[0], json.loads(row[1])) if row else ("server", {})
        keys.update(clean)
        conn.execute("INSERT INTO user_credentials VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode,keys_json=excluded.keys_json,updated_at=excluded.updated_at", (user_id, mode or current_mode, json.dumps(keys), time.time()))
    _register(keys)
    return public_credentials(user_id)


def job_credential_mode(job):
    pinned = getattr(job, "credential_mode", "")
    if pinned in ("own", "server"):
        return pinned
    return read_user_credentials(getattr(job, "user_id", ""))["mode"]


def own_key_job_limit(family):
    default = 6 if family == "openai" else 1
    try:
        return max(1, min(16, int(os.getenv("COVER_OWN_" + family.upper() + "_JOBS_PER_KEY", str(default)))))
    except ValueError:
        return default


def own_owner_job_limit():
    try:
        return max(1, min(100, int(os.getenv("COVER_OWN_MAX_JOBS_PER_USER", "10"))))
    except ValueError:
        return 10


def memory_budget_mb():
    try:
        return max(512, min(8192, int(os.getenv("COVER_MEMORY_BUDGET_MB", "2048"))))
    except ValueError:
        return 2048


def estimate_job_memory_mb(job):
    # Server releases decoded output images immediately after saving; only live
    # workers and downloaded source thumbnails remain resident. Conservative
    # allowance: 384 MiB base + 128 MiB/live image worker + 6 MiB/source thumbnail.
    urls = json.loads(getattr(job, "urls", "[]") or "[]")
    languages = json.loads(getattr(job, "languages", "[]") or "[]")
    mode = getattr(job, "mode", "url")
    count = max(1, len(languages)) * (max(1, len(urls)) if mode == "url" else max(1, int(getattr(job, "n_variants", 1))) if mode == "seed" else 1)
    threads = min(max(1, int(getattr(job, "max_threads", 1))), count)
    return 384 + 128 * threads + (6 * len(urls) if threads > 1 else 0)


def physical_own_key_limits():
    """Persisted physical limits also apply if the same key appears in server env.

    Only hashes escape this function; reading on acquisition handles restart and
    another settings request before either owner has started a job.
    """
    result = {}
    with _connect() as conn:
        rows = conn.execute("SELECT keys_json FROM user_credentials").fetchall()
    for row in rows:
        keys = json.loads(row[0])
        _register(keys)
        for family, values in keys.items():
            for key in values:
                result[(family, hashlib.sha256(key.encode()).hexdigest())] = own_key_job_limit(family)
    return result
