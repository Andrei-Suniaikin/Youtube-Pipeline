# -*- coding: utf-8 -*-
"""
Общие helpers для cover_server.py и api_cover.py.

Vendor credentials берутся из private per-user store или server env;
никогда не записываются в Job/queue SQLite/API responses.
"""

import json
import os
import re
import threading
import time
import hashlib
import sys
sys.path.insert(0, os.environ.get("VIDEO_STUDIO_SHARED_DIR", "/opt/video-studio-shared"))
import credential_budget as _host_budget
from fastapi import HTTPException
from cover_credentials import (
    read_user_credentials, job_credential_mode, own_key_job_limit,
    own_owner_job_limit, CredentialSettingsError,
    estimate_job_memory_mb, memory_budget_mb, physical_own_key_limits,
)

from provider_policy import (
    InvalidGenerationProvider,
    canonical_generation_provider,
    effective_simple_mode_provider,
    provider_credential_family,
)


class ServerKeyBusy(RuntimeError):
    """Ordinary shared-budget contention; return the job to pending before POST."""


class ServerKeyConfigurationError(RuntimeError):
    """Для worker: нужный server-side credential не настроен."""


_CREDENTIAL_ENV = {
    "fastgen": ("COVER_FAST_GEN_KEYS", "COVER_FAST_GEN_KEY"),
    "openai": ("COVER_OPENAI_KEYS", "COVER_OPENAI_KEY"),
    "veo": ("COVER_VEO_KEYS", "COVER_VEO_KEY"),
    "xai": ("COVER_XAI_KEYS", "COVER_XAI_KEY"),
}


def _parse_plural_keys(raw: str, env_name: str) -> list:
    value = str(raw or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except Exception as exc:
            raise ServerKeyConfigurationError(
                f"{env_name}: ожидается JSON array или список через запятую/новую строку"
            ) from exc
        if not isinstance(parsed, list) or any(
            not isinstance(item, str) for item in parsed
        ):
            raise ServerKeyConfigurationError(
                f"{env_name}: JSON должен быть массивом строк"
            )
        items = parsed
    else:
        items = re.split(r"[,\r\n]+", value)
    return [item.strip() for item in items if item and item.strip()]


def configured_server_keys(credential_family: str) -> tuple:
    """Configured unique keys in deterministic order; values never leave workers."""
    if credential_family not in _CREDENTIAL_ENV:
        raise ServerKeyConfigurationError(
            f"Неизвестное семейство серверных ключей: {credential_family}"
        )
    plural_name, scalar_name = _CREDENTIAL_ENV[credential_family]
    values = _parse_plural_keys(os.getenv(plural_name, ""), plural_name)
    scalar = os.getenv(scalar_name, "").strip()
    if scalar:
        values.append(scalar)
    # dict preserves order and makes duplicate list entries capacity-neutral.
    return tuple(dict.fromkeys(values))


def credential_pool_summary() -> dict:
    """Safe config visibility: counts/sources only, never credential material."""
    summary = {}
    for family, (plural_name, scalar_name) in _CREDENTIAL_ENV.items():
        keys = configured_server_keys(family)
        plural_set = bool(os.getenv(plural_name, "").strip())
        scalar_set = bool(os.getenv(scalar_name, "").strip())
        summary[family] = {
            "unique_keys": len(keys),
            "plural_configured": plural_set,
            "scalar_configured": scalar_set,
        }
    return summary


def _missing_key_error(family: str) -> ServerKeyConfigurationError:
    plural_name, scalar_name = _CREDENTIAL_ENV[family]
    return ServerKeyConfigurationError(
        "Серверный ключ не настроен: "
        f"{scalar_name} (или {plural_name}). Обратитесь к администратору."
    )


class ServerKeyLease:
    """Job-scoped in-memory assignments; idempotent release."""

    def __init__(self, pool, assignments: dict, openai_key: str, generation_key: str):
        self._pool = pool
        self._assignments = assignments
        self.openai_key = openai_key
        self.generation_key = generation_key
        self._released = False
        self._release_lock = threading.Lock()

    def release(self):
        with self._release_lock:
            if self._released:
                return
            self._released = True
            self._pool.release(self._assignments)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.release()


_PHYSICAL_LOCK = threading.Lock()
_PHYSICAL_LOADS = {}
_PHYSICAL_LIMITS = {}


def _physical_key(family, value):
    return family, hashlib.sha256(value.encode()).hexdigest()


class _Assignments(dict):
    def __init__(self):
        super().__init__()
        self.host_leases = []
        self.released = False


def _host_job_units(job):
    languages = json.loads(getattr(job, "languages", "[]") or "[]")
    mode = getattr(job, "mode", "url")
    urls = json.loads(getattr(job, "urls", "[]") or "[]")
    count = max(1, len(languages)) * (max(1, len(urls)) if mode == "url" else
            max(1, int(getattr(job, "n_variants", 1))) if mode == "seed" else 1)
    return min(max(1, int(getattr(job, "max_threads", 1))), count)


class _ServerCredentialPool:
    """Thread-safe least-loaded selection with RR tie-breaking per family."""

    def __init__(self):
        self._lock = threading.Lock()
        self._loads = {family: {} for family in _CREDENTIAL_ENV}
        self._cursor = {family: 0 for family in _CREDENTIAL_ENV}

    def acquire(self, families, configured=None, limits=None, host_units=1) -> dict:
        unique_families = tuple(dict.fromkeys(families))
        configured = configured if configured is not None else {
            family: configured_server_keys(family)
            for family in unique_families
        }
        for family, keys in configured.items():
            if not keys:
                raise _missing_key_error(family)

        known_limits = physical_own_key_limits()
        assignments = _Assignments()
        with self._lock, _PHYSICAL_LOCK:
            for physical, limit in known_limits.items():
                _PHYSICAL_LIMITS[physical] = min(limit, _PHYSICAL_LIMITS.get(physical, limit))
            if limits:
                for family in unique_families:
                    for key in configured[family]:
                        physical = _physical_key(family, key)
                        _PHYSICAL_LIMITS[physical] = min(limits[family], _PHYSICAL_LIMITS.get(physical, limits[family]))
            eligible = {
                family: tuple(key for key in configured[family]
                              if _PHYSICAL_LOADS.get(_physical_key(family, key), 0)
                              < _PHYSICAL_LIMITS.get(_physical_key(family, key), float('inf')))
                for family in unique_families
            }
            if any(not eligible[family] for family in unique_families):
                return {}  # Reserve all families together, never hold a partial lease.
            try:
                for family in unique_families:
                    keys = eligible[family]
                    loads = self._loads[family]
                    start = self._cursor[family] % len(keys)
                    ordered = sorted(range(len(keys)), key=lambda i: (loads.get(keys[i], 0), (i-start)%len(keys)))
                    for chosen_index in ordered:
                        chosen = keys[chosen_index]
                        host = (_host_budget.try_acquire(family, chosen, host_units)
                                if family in _host_budget.CAPACITIES else _host_budget.Lease())
                        if host is not None:
                            assignments.host_leases.append(host)
                            assignments[family] = chosen
                            break
                    else:
                        for host in assignments.host_leases:
                            host.release()
                        return {}
                # Commit process-local load only after every family has a host lease.
                for family, chosen in assignments.items():
                    loads = self._loads[family]
                    loads[chosen] = loads.get(chosen, 0) + 1
                    physical = _physical_key(family, chosen)
                    _PHYSICAL_LOADS[physical] = _PHYSICAL_LOADS.get(physical, 0) + 1
                    self._cursor[family] = (eligible[family].index(chosen) + 1) % len(eligible[family])
            except Exception:
                for host in assignments.host_leases:
                    host.release()
                raise
        return assignments

    def release(self, assignments: dict):
        with self._lock, _PHYSICAL_LOCK:
            if isinstance(assignments, _Assignments):
                if assignments.released:
                    return
                assignments.released = True
                for host in assignments.host_leases:
                    host.release()
            for family, key in assignments.items():
                loads = self._loads.get(family, {})
                if key in loads:
                    loads[key] = max(0, loads[key] - 1)
                    physical = _physical_key(family, key)
                    _PHYSICAL_LOADS[physical] = max(0, _PHYSICAL_LOADS.get(physical, 0) - 1)

    def reset(self):
        with self._lock:
            self._loads = {family: {} for family in _CREDENTIAL_ENV}
            self._cursor = {family: 0 for family in _CREDENTIAL_ENV}


_SERVER_CREDENTIAL_POOL = _ServerCredentialPool()
_USER_CREDENTIAL_POOLS = {}
_USER_CREDENTIAL_POOLS_LOCK = threading.Lock()


def _user_pool(user_id):
    with _USER_CREDENTIAL_POOLS_LOCK:
        if user_id not in _USER_CREDENTIAL_POOLS:
            _USER_CREDENTIAL_POOLS[user_id] = _ServerCredentialPool()
        return _USER_CREDENTIAL_POOLS[user_id]


def _job_keys(job, families):
    if job_credential_mode(job) != "own":
        return {family: configured_server_keys(family) for family in families}
    config = read_user_credentials(getattr(job, "user_id", ""))
    keys = {family: tuple(config["keys"].get(family, [])) for family in families}
    for family, values in keys.items():
        if not values:
            raise ServerKeyConfigurationError(f"Личный ключ {family} не настроен; серверные ключи не используются")
    return keys


def job_key_requirements(job):
    data = json.loads(getattr(job, "preset_data", "") or "{}")
    return _key_requirements(getattr(job, "mode", "url") or "url", _provider_from_job(job), bool(data.get("simple_mode", False)))


def own_job_capacity(job):
    if job_credential_mode(job) != "own":
        return None
    _, _, families = job_key_requirements(job)
    keys = _job_keys(job, families)
    return min(own_owner_job_limit(), *(len(keys[f]) * own_key_job_limit(f) for f in families))


def own_job_can_start(job, running):
    _, _, families = job_key_requirements(job)
    keys = _job_keys(job, families)
    own_running = [j for j in running if j.user_id == job.user_id and job_credential_mode(j) == "own"]
    if len(own_running) >= own_owner_job_limit():
        return False
    running_families = [job_key_requirements(j)[2] for j in own_running]
    return all(sum(family in required for required in running_families) < len(keys[family]) * own_key_job_limit(family) for family in families)



def physical_job_can_start(job, running):
    """Do not claim a job just to wait on another owner's identical credential.

    Running jobs not yet leased reserve one possible unit conservatively. This
    closes the dispatcher/worker handoff window without persisting secret values.
    """
    _, _, families = job_key_requirements(job)
    configured = _job_keys(job, families)
    known = physical_own_key_limits()
    with _PHYSICAL_LOCK:
        loads = dict(_PHYSICAL_LOADS)
        known = {**known, **_PHYSICAL_LIMITS}
    for family in families:
        if family in _host_budget.CAPACITIES and not any(
                _host_budget.can_acquire(family, key, _host_job_units(job)) for key in configured[family]):
            return False
        candidate = {_physical_key(family, key) for key in configured[family]}
        if any(key not in known for key in candidate):
            continue  # Unshared server key retains its existing server policy.
        available = sum(max(0, known[key] - loads.get(key, 0)) for key in candidate)
        if available <= 0:
            return False
        for active in running:
            if getattr(active, '_credential_fingerprints', None):
                continue  # Its actual reservation is already in physical loads.
            _, _, active_families = job_key_requirements(active)
            if family not in active_families:
                continue
            active_keys = _job_keys(active, (family,))[family]
            if candidate.intersection(_physical_key(family, key) for key in active_keys):
                available -= 1
        if available <= 0:
            return False
    return True

def _provider_from_job(job) -> str:
    try:
        data = json.loads(getattr(job, "preset_data", "") or "{}")
    except Exception:
        data = {}
    preset_provider = (data or {}).get("generation_provider")
    job_provider = getattr(job, "generation_provider", "") or ""
    provider = canonical_generation_provider(job_provider or preset_provider or "flow")
    if preset_provider not in (None, ""):
        canonical_preset = canonical_generation_provider(preset_provider)
        if canonical_preset != provider:
            raise InvalidGenerationProvider(
                "Job generation_provider does not match preset_data "
                f"({provider!r} != {canonical_preset!r})"
            )
    return provider


def _key_requirements(mode: str, generation_provider: str, simple_mode: bool):
    mode = (mode or "url").strip().lower()
    provider = canonical_generation_provider(generation_provider or "flow")
    if simple_mode:
        provider = effective_simple_mode_provider(
            provider,
            os.getenv("SIMPLE_MODE_PROVIDER", ""),
        )
    generation_family = provider_credential_family(provider)
    families = []
    if mode != "seed":
        families.append("openai")
    families.append(generation_family)
    return provider, generation_family, tuple(dict.fromkeys(families))


def resolve_server_keys(
    mode: str,
    generation_provider: str,
    *,
    simple_mode: bool = False,
) -> tuple:
    """Preflight-compatible `(openai_key, generation_key)` from server env."""
    _, credential_family, families = _key_requirements(
        mode, generation_provider, simple_mode
    )
    for family in families:
        if not configured_server_keys(family):
            raise _missing_key_error(family)
    openai_keys = configured_server_keys("openai")
    openai_key = openai_keys[0] if openai_keys else ""
    generation_key = configured_server_keys(credential_family)[0]
    return openai_key, generation_key


def resolve_server_keys_for_job(job) -> tuple:
    if job_credential_mode(job) == "own":
        _, family, families = job_key_requirements(job)
        keys = _job_keys(job, families)
        return (keys.get("openai") or ("",))[0], keys[family][0]
    try:
        data = json.loads(getattr(job, "preset_data", "") or "{}")
    except Exception:
        data = {}
    return resolve_server_keys(
        getattr(job, "mode", "url") or "url",
        _provider_from_job(job),
        simple_mode=bool((data or {}).get("simple_mode", False)),
    )


def acquire_server_keys_for_job(job, cancel_check=None, required_fingerprints=None) -> ServerKeyLease:
    """Lease least-loaded credentials for the whole job execution."""
    try:
        data = json.loads(getattr(job, "preset_data", "") or "{}")
    except Exception:
        data = {}
    provider = _provider_from_job(job)
    _, generation_family, families = _key_requirements(
        getattr(job, "mode", "url") or "url",
        provider,
        bool((data or {}).get("simple_mode", False)),
    )
    own = job_credential_mode(job) == "own"
    pool = _user_pool(job.user_id) if own else _SERVER_CREDENTIAL_POOL
    deadline = time.monotonic() + 2
    while True:
        if cancel_check and cancel_check():
            raise ServerKeyConfigurationError("Задача отменена во время ожидания ключа")
        keys = _job_keys(job, families)
        for family, fingerprint in (required_fingerprints or {}).items():
            keys[family] = tuple(key for key in keys.get(family, ()) if hashlib.sha256(key.encode()).hexdigest() == fingerprint)
            if not keys[family]:
                raise ServerKeyConfigurationError('Для восстановления нужен исходный ключ '+family+'; другой ключ не используется')
        assignments = pool.acquire(families, keys, {f: own_key_job_limit(f) for f in families} if own else None, host_units=_host_job_units(job))
        if assignments:
            job._credential_fingerprints = tuple(_physical_key(f, k) for f, k in assignments.items())
            break
        if time.monotonic() >= deadline:
            raise ServerKeyBusy("Общий лимит ключа занят; задача ожидает в очереди без вызова провайдера")
        time.sleep(0.05)
    try:
        optional_openai = () if own else configured_server_keys("openai")
        openai_key = assignments.get(
            "openai",
            optional_openai[0] if optional_openai else "",
        )
    except Exception:
        pool.release(assignments)
        raise
    return ServerKeyLease(
        pool,
        assignments,
        openai_key,
        assignments[generation_family],
    )


def ensure_server_keys_for_job(job) -> tuple:
    """HTTP admission-check перед enqueue/retry/resume; Job не мутирует."""
    try:
        if estimate_job_memory_mb(job) > memory_budget_mb():
            raise ServerKeyConfigurationError("Задача превышает бюджет памяти Cover; уменьшите число потоков или разделите пакет")
        # Pin own-only on the durable job before enqueue: later settings changes
        # cannot silently send this job to the server's billing account.
        if not getattr(job, "credential_mode", ""):
            job.credential_mode = job_credential_mode(job)
        return resolve_server_keys_for_job(job)
    except InvalidGenerationProvider as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ServerKeyConfigurationError, CredentialSettingsError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def ensure_ai_keys_from_env(job) -> tuple:
    """Deprecated compatibility alias: проверяет server keys, но не пишет их в Job."""
    return ensure_server_keys_for_job(job)


def _reset_credential_pool_for_tests():
    _SERVER_CREDENTIAL_POOL.reset()


def idempotent_request(queue, user_id, request, body):
    from job_queue_cover import IdempotencyConflict
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return "", "", None
    if not 8 <= len(key) <= 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", key):
        raise HTTPException(status_code=422, detail="Idempotency-Key: 8–128 ASCII букв/цифр/._:-")
    data = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    try:
        existing = queue.get_idempotent_job(user_id, key, fingerprint)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return key, fingerprint, existing


def consume_recovery_attempt(job):
    """Shared web/API recovery budget; persisted atomically with the job CAS."""
    from fastapi import HTTPException
    if getattr(job, "restore_hold", False):
        raise HTTPException(status_code=409, detail="RESTORE_HOLD: сначала сверить remote IDs и файлы после восстановления")
    if job.recovery_attempts >= 3:
        raise HTTPException(status_code=409, detail="Лимит восстановления исчерпан; задача изолирована, результаты сохранены")
    job.recovery_attempts += 1
