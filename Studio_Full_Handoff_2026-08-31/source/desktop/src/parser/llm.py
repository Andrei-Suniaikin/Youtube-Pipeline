"""Мини-клиент OpenAI chat completions (текст и vision) для классификатора и судьи.

Гочча gpt-5 семейства: без reasoning_effort="minimal"/"low" внутренний reasoning
съедает max_completion_tokens и content приходит пустым (проверено 15.07.2026).
"""
import base64
import json
import re
import threading
import time

import requests

from .config import openai_key

API_URL = "https://api.openai.com/v1/chat/completions"
_USAGE_LOCK = threading.Lock()
_USAGE = {}
_USAGE_FIELDS = ("calls", "input_tokens", "output_tokens", "total_tokens",
                 "cached_input_tokens")


def reset_usage():
    with _USAGE_LOCK:
        _USAGE.clear()


def _record_usage(model: str, usage: dict):
    if not isinstance(usage, dict):
        return
    details = usage.get("prompt_tokens_details") or {}
    with _USAGE_LOCK:
        row = _USAGE.setdefault(model, {"calls": 0, "input_tokens": 0,
                                        "output_tokens": 0, "total_tokens": 0,
                                        "cached_input_tokens": 0})
        row["calls"] += 1
        row["input_tokens"] += int(usage.get("prompt_tokens") or 0)
        row["output_tokens"] += int(usage.get("completion_tokens") or 0)
        row["total_tokens"] += int(usage.get("total_tokens") or 0)
        row["cached_input_tokens"] += int(details.get("cached_tokens") or 0)


def usage_snapshot() -> dict:
    with _USAGE_LOCK:
        return {model: dict(row) for model, row in sorted(_USAGE.items())}


def merge_usage(*snapshots: dict) -> dict:
    """Pure per-model sum used for report resume checkpoints."""
    merged = {}
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        for model, values in snapshot.items():
            if not isinstance(values, dict):
                continue
            row = merged.setdefault(str(model), {field: 0 for field in _USAGE_FIELDS})
            for field in _USAGE_FIELDS:
                try:
                    row[field] += int(values.get(field) or 0)
                except (TypeError, ValueError):
                    pass
    return {model: merged[model] for model in sorted(merged)}


def chat(model: str, messages: list, reasoning_effort: str = "minimal",
         max_tokens: int = 4000, retries: int = 2, timeout: int = 180,
         response_format: dict | None = None) -> str:
    body = {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "max_completion_tokens": max_tokens,
        "messages": messages,
    }
    if response_format is not None:
        body["response_format"] = response_format
    headers = {"Authorization": f"Bearer {openai_key()}",
               "Content-Type": "application/json"}
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(API_URL, json=body, headers=headers, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            payload = r.json()
            _record_usage(model, payload.get("usage") or {})
            content = payload["choices"][0]["message"]["content"] or ""
            if not content.strip():
                raise RuntimeError("пустой content (reasoning съел лимит?)")
            return content
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"LLM не ответил после {retries + 1} попыток: {last}")


def image_part(path: str) -> dict:
    b64 = base64.standard_b64encode(open(path, "rb").read()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def extract_json(text: str):
    """Вытащить первый JSON-объект/массив из ответа модели (терпимо к обвязке)."""
    m = re.search(r"[\[{].*[\]}]", text, re.S)
    if not m:
        raise ValueError(f"нет JSON в ответе: {text[:200]!r}")
    return json.loads(m.group(0))
