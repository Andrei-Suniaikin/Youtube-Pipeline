"""Small production LLM client with portable, environment-only credentials."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Mapping

import requests


API_URL = "https://api.openai.com/v1/chat/completions"
API_KEY_ENV = "OPENAI_API_KEY"
_USAGE_LOCK = threading.Lock()
_USAGE: dict[str, dict[str, int]] = {}
_USAGE_FIELDS = (
    "calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
)


class LLMConfigurationError(RuntimeError):
    """The production runner has no externally supplied API credential."""


def api_key() -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise LLMConfigurationError(
            f"{API_KEY_ENV} is required; credentials are never read from project files"
        )
    return key


def reset_usage() -> None:
    with _USAGE_LOCK:
        _USAGE.clear()


def _record_usage(model: str, usage: Mapping[str, Any]) -> None:
    details = usage.get("prompt_tokens_details") or {}
    with _USAGE_LOCK:
        row = _USAGE.setdefault(model, {field: 0 for field in _USAGE_FIELDS})
        row["calls"] += 1
        row["input_tokens"] += int(usage.get("prompt_tokens") or 0)
        row["output_tokens"] += int(usage.get("completion_tokens") or 0)
        row["total_tokens"] += int(usage.get("total_tokens") or 0)
        if isinstance(details, Mapping):
            row["cached_input_tokens"] += int(details.get("cached_tokens") or 0)


def usage_snapshot() -> dict[str, dict[str, int]]:
    with _USAGE_LOCK:
        return {model: dict(row) for model, row in sorted(_USAGE.items())}


def merge_usage(*snapshots: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            continue
        for model, values in snapshot.items():
            if not isinstance(values, Mapping):
                continue
            row = merged.setdefault(str(model), {field: 0 for field in _USAGE_FIELDS})
            for field in _USAGE_FIELDS:
                row[field] += int(values.get(field) or 0)
    return {model: merged[model] for model in sorted(merged)}


def chat(
    model: str,
    messages: list[dict[str, Any]],
    reasoning_effort: str = "minimal",
    max_tokens: int = 4000,
    retries: int = 2,
    timeout: int = 180,
    response_format: dict[str, Any] | None = None,
) -> str:
    body: dict[str, Any] = {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "max_completion_tokens": int(max_tokens),
        "messages": messages,
    }
    if response_format is not None:
        body["response_format"] = response_format
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
    }
    last_error: Exception | None = None
    for attempt in range(max(0, int(retries)) + 1):
        try:
            response = requests.post(
                API_URL, json=body, headers=headers, timeout=int(timeout)
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise RuntimeError(
                    f"transient HTTP {response.status_code}: {response.text[:200]}"
                )
            response.raise_for_status()
            payload = response.json()
            usage = payload.get("usage") or {}
            if isinstance(usage, Mapping):
                _record_usage(model, usage)
            content = payload["choices"][0]["message"]["content"] or ""
            if not str(content).strip():
                raise RuntimeError("provider returned empty content")
            return str(content)
        except (requests.RequestException, KeyError, TypeError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < int(retries):
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(
        f"LLM did not return a valid response after {int(retries) + 1} attempts: {last_error}"
    )


def extract_json(text: str) -> Any:
    match = re.search(r"[\[{].*[\]}]", str(text), re.S)
    if not match:
        raise ValueError(f"response contains no JSON: {str(text)[:200]!r}")
    return json.loads(match.group(0))


__all__ = [
    "API_KEY_ENV",
    "LLMConfigurationError",
    "api_key",
    "chat",
    "extract_json",
    "merge_usage",
    "reset_usage",
    "usage_snapshot",
]
