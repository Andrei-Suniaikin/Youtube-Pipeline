# -*- coding: utf-8 -*-
"""
Общие helpers для cover_server.py и api_cover.py.

Vendor credentials разрешаются только из server env и никогда не записываются
в Job/SQLite/API payload.
"""

import json
import os
import re
import threading
from fastapi import HTTPException

from provider_policy import (
    InvalidGenerationProvider,
    canonical_generation_provider,
    effective_simple_mode_provider,
    provider_credential_family,
)


class ServerKeyConfigurationError(RuntimeError):
    """Для worker: нужный server-side credential не настроен."""


_CREDENTIAL_ENV = {
    "fastgen": ("COVER_FAST_GEN_KEYS", "COVER_FAST_GEN_KEY"),
    "openai": ("COVER_OPENAI_KEYS", "COVER_OPENAI_KEY"),
    "veo": ("COVER_VEO_KEYS", "COVER_VEO_KEY"),
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

    def release(self):
        if self._released:
            return
        self._released = True
        self._pool.release(self._assignments)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.release()


class _ServerCredentialPool:
    """Thread-safe least-loaded selection with RR tie-breaking per family."""

    def __init__(self):
        self._lock = threading.Lock()
        self._loads = {family: {} for family in _CREDENTIAL_ENV}
        self._cursor = {family: 0 for family in _CREDENTIAL_ENV}

    def acquire(self, families) -> dict:
        unique_families = tuple(dict.fromkeys(families))
        configured = {
            family: configured_server_keys(family)
            for family in unique_families
        }
        for family, keys in configured.items():
            if not keys:
                raise _missing_key_error(family)

        assignments = {}
        with self._lock:
            for family in unique_families:
                keys = configured[family]
                loads = self._loads[family]
                for key in keys:
                    loads.setdefault(key, 0)
                minimum = min(loads.get(key, 0) for key in keys)
                start = self._cursor[family] % len(keys)
                chosen_index = next(
                    index
                    for offset in range(len(keys))
                    for index in ((start + offset) % len(keys),)
                    if loads.get(keys[index], 0) == minimum
                )
                chosen = keys[chosen_index]
                loads[chosen] = loads.get(chosen, 0) + 1
                self._cursor[family] = (chosen_index + 1) % len(keys)
                assignments[family] = chosen
        return assignments

    def release(self, assignments: dict):
        with self._lock:
            for family, key in assignments.items():
                loads = self._loads.get(family, {})
                if key in loads:
                    loads[key] = max(0, loads[key] - 1)

    def reset(self):
        with self._lock:
            self._loads = {family: {} for family in _CREDENTIAL_ENV}
            self._cursor = {family: 0 for family in _CREDENTIAL_ENV}


_SERVER_CREDENTIAL_POOL = _ServerCredentialPool()


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
    try:
        data = json.loads(getattr(job, "preset_data", "") or "{}")
    except Exception:
        data = {}
    return resolve_server_keys(
        getattr(job, "mode", "url") or "url",
        _provider_from_job(job),
        simple_mode=bool((data or {}).get("simple_mode", False)),
    )


def acquire_server_keys_for_job(job) -> ServerKeyLease:
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
    assignments = _SERVER_CREDENTIAL_POOL.acquire(families)
    try:
        optional_openai = configured_server_keys("openai")
        openai_key = assignments.get(
            "openai",
            optional_openai[0] if optional_openai else "",
        )
    except Exception:
        _SERVER_CREDENTIAL_POOL.release(assignments)
        raise
    return ServerKeyLease(
        _SERVER_CREDENTIAL_POOL,
        assignments,
        openai_key,
        assignments[generation_family],
    )


def ensure_server_keys_for_job(job) -> tuple:
    """HTTP admission-check перед enqueue/retry/resume; Job не мутирует."""
    try:
        return resolve_server_keys_for_job(job)
    except InvalidGenerationProvider as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ServerKeyConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def ensure_ai_keys_from_env(job) -> tuple:
    """Deprecated compatibility alias: проверяет server keys, но не пишет их в Job."""
    return ensure_server_keys_for_job(job)


def _reset_credential_pool_for_tests():
    _SERVER_CREDENTIAL_POOL.reset()
