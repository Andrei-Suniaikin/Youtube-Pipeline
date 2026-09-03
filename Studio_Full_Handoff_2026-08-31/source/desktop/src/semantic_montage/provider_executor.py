"""Allowlisted HTTP execution, retries and resumable paid provider workflows."""
from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import re
import time
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urljoin, urlparse

from .common import atomic_write_json, canonical_hash, media_duration, sha256_file
from .providers import PreparedRequest, ProviderError
from .state import JobState


class ProviderExecutionError(ProviderError):
    pass


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderExecutionError("provider response is not valid JSON") from exc


class HTTPTransport(Protocol):
    def __call__(self, request: PreparedRequest, timeout: float) -> HTTPResponse: ...


def requests_transport(request: PreparedRequest, timeout: float) -> HTTPResponse:
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - dependency preflight
        raise ProviderExecutionError("requests is required for live provider HTTP") from exc
    response = requests.request(
        request.method,
        request.url,
        headers=dict(request.headers),
        params=dict(request.params),
        json=dict(request.json_body) if request.json_body is not None else None,
        timeout=timeout,
        allow_redirects=False,
    )
    return HTTPResponse(
        status=response.status_code,
        headers=dict(response.headers),
        body=response.content,
        url=response.url,
    )


def _redact(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, Mapping):
        output = {}
        for key, item in value.items():
            folded = str(key).casefold()
            if any(token in folded for token in ("authorization", "api_key", "api-key", "token", "secret")):
                output[str(key)] = "<redacted>"
            else:
                output[str(key)] = _redact(item, secrets)
        return output
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "<redacted>")
        redacted = re.sub(r"(?i)(bearer|token)\s+[A-Za-z0-9._~+/-]{8,}", r"\1 <redacted>", redacted)
        return redacted
    return value


@dataclass
class ProviderExecutor:
    allowed_hosts: frozenset[str]
    transport: HTTPTransport = requests_transport
    timeout_seconds: float = 30.0
    max_attempts: int = 4
    max_redirects: int = 3
    max_retry_after_seconds: float = 30.0
    sleeper: Callable[[float], None] = time.sleep
    secrets: tuple[str, ...] = ()
    audit: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.max_attempts < 1 or self.max_redirects < 0:
            raise ValueError("invalid provider executor limits")
        if not self.allowed_hosts:
            raise ValueError("provider host allowlist must not be empty")

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts:
            raise ProviderExecutionError(f"provider URL is outside allowlist: {parsed.hostname}")
        if parsed.username or parsed.password:
            raise ProviderExecutionError("provider URL must not embed credentials")

    @staticmethod
    def _retry_delay(response: HTTPResponse | None, attempt: int, cap: float) -> float:
        if response:
            raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
            if raw:
                try:
                    return min(cap, max(0.0, float(raw)))
                except ValueError:
                    pass
        return min(cap, 0.5 * (2 ** max(0, attempt - 1)))

    def execute(self, request: PreparedRequest, *, operation: str) -> HTTPResponse:
        self._validate_url(request.url)
        current = request
        redirects = 0
        retry_statuses = {429, 500, 502, 503, 504}
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            response: HTTPResponse | None = None
            try:
                response = self.transport(current, self.timeout_seconds)
                self._validate_url(response.url)
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location") or response.headers.get("location")
                    if not location or redirects >= self.max_redirects:
                        raise ProviderExecutionError("provider redirect limit or missing location")
                    redirected_url = urljoin(current.url, location)
                    self._validate_url(redirected_url)
                    redirected_method = "GET" if response.status == 303 else current.method
                    current = PreparedRequest(
                        redirected_method,
                        redirected_url,
                        headers=current.headers,
                        params={} if redirected_method == "GET" else current.params,
                        json_body=None if redirected_method == "GET" else current.json_body,
                    )
                    redirects += 1
                    continue
                if 200 <= response.status < 300:
                    self.audit.append(
                        _redact(
                            {
                                "operation": operation,
                                "attempt": attempt,
                                "request": current.as_dict(include_secrets=False),
                                "status": response.status,
                                "response_bytes": len(response.body),
                            },
                            self.secrets,
                        )
                    )
                    return response
                message = response.body[:500].decode("utf-8", "replace")
                if response.status not in retry_statuses:
                    raise ProviderExecutionError(
                        f"provider {operation} returned HTTP {response.status}: {message}"
                    )
                last_error = ProviderExecutionError(
                    f"provider {operation} temporary HTTP {response.status}"
                )
            except ProviderExecutionError:
                raise
            except Exception as error:
                last_error = error
            self.audit.append(
                _redact(
                    {
                        "operation": operation,
                        "attempt": attempt,
                        "request": current.as_dict(include_secrets=False),
                        "status": response.status if response else None,
                        "error": f"{type(last_error).__name__}: {last_error}",
                    },
                    self.secrets,
                )
            )
            if attempt < self.max_attempts:
                self.sleeper(self._retry_delay(response, attempt, self.max_retry_after_seconds))
        raise ProviderExecutionError(
            f"provider {operation} failed after {self.max_attempts} attempts: {last_error}"
        )

    def json(self, request: PreparedRequest, *, operation: str) -> Mapping[str, Any]:
        response = self.execute(request, operation=operation)
        content_type = (response.headers.get("Content-Type") or response.headers.get("content-type") or "").lower()
        if "json" not in content_type:
            raise ProviderExecutionError(f"provider {operation} returned non-JSON MIME: {content_type}")
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ProviderExecutionError(f"provider {operation} JSON must be an object")
        return dict(payload)

    def download(
        self,
        request: PreparedRequest,
        destination: str | os.PathLike[str],
        *,
        operation: str,
        allowed_mime_prefixes: tuple[str, ...] = ("audio/",),
        expected_sha256: str | None = None,
        expected_duration: tuple[float, float] | None = None,
    ) -> dict[str, Any]:
        response = self.execute(request, operation=operation)
        content_type = (response.headers.get("Content-Type") or response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if not any(content_type.startswith(prefix) for prefix in allowed_mime_prefixes):
            raise ProviderExecutionError(f"download has forbidden MIME: {content_type}")
        if not response.body:
            raise ProviderExecutionError("download is empty")
        digest = hashlib.sha256(response.body).hexdigest()
        if expected_sha256 and digest != expected_sha256.lower():
            raise ProviderExecutionError("download SHA-256 mismatch")
        target = Path(destination).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        with temporary.open("wb") as output:
            output.write(response.body)
            output.flush()
            os.fsync(output.fileno())
        if expected_duration is not None:
            duration = media_duration(temporary)
            low, high = expected_duration
            if not low <= duration <= high:
                temporary.unlink(missing_ok=True)
                raise ProviderExecutionError(f"download duration outside [{low}, {high}]")
        else:
            duration = None
        os.replace(temporary, target)
        return {
            "path": str(target),
            "sha256": digest,
            "bytes": target.stat().st_size,
            "content_type": content_type,
            "duration": duration,
        }


@dataclass(frozen=True)
class PaidWorkflowContract:
    provider: str
    operation_kind: str
    idempotency_key: str
    request_hash: str
    submit_request: Callable[[], PreparedRequest]
    parse_submission: Callable[[Mapping[str, Any]], tuple[str, Mapping[str, Any]]]
    status_request: Callable[[str], PreparedRequest]
    parse_status: Callable[[Mapping[str, Any]], tuple[str, Mapping[str, Any] | None]]
    max_polls: int = 120
    poll_seconds: float = 2.0


def run_paid_workflow(
    *,
    state: JobState,
    executor: ProviderExecutor,
    contract: PaidWorkflowContract,
) -> dict[str, Any]:
    """Submit once, then poll/resume without duplicating a paid provider job."""

    claim = state.claim_paid_operation(
        provider=contract.provider,
        idempotency_key=contract.idempotency_key,
        operation_kind=contract.operation_kind,
        request_hash=contract.request_hash,
    )
    if claim["action"] == "resume":
        row = claim["row"]
        if row.get("status") == "completed":
            return dict(row.get("result") or {})
        provider_job_id = str(row.get("provider_job_id") or "")
        if not provider_job_id:
            raise ProviderExecutionError("paid operation resume lacks provider_job_id")
    else:
        payload = executor.json(contract.submit_request(), operation=f"{contract.provider}.submit")
        provider_job_id, receipt = contract.parse_submission(payload)
        if not provider_job_id or not isinstance(receipt, Mapping):
            raise ProviderExecutionError("paid submit parser returned invalid job/receipt")
        state.record_paid_submission(
            contract.provider,
            contract.idempotency_key,
            str(claim["claim_token"]),
            provider_job_id,
            dict(receipt),
        )
    for poll in range(1, contract.max_polls + 1):
        payload = executor.json(
            contract.status_request(provider_job_id), operation=f"{contract.provider}.status"
        )
        status, result = contract.parse_status(payload)
        if status == "completed":
            if not isinstance(result, Mapping):
                raise ProviderExecutionError("completed provider status lacks result")
            state.complete_paid_operation(
                contract.provider, contract.idempotency_key, dict(result)
            )
            return dict(result)
        if status in {"failed", "cancelled", "rejected"}:
            raise ProviderExecutionError(f"paid provider job ended with status={status}")
        if status not in {"queued", "running", "processing"}:
            raise ProviderExecutionError(f"unknown paid provider status={status}")
        if poll < contract.max_polls:
            executor.sleeper(contract.poll_seconds)
    raise ProviderExecutionError("paid provider polling budget exhausted")


def validate_commercial_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    required = ("provider", "asset_id", "license_id", "licensed_at", "license_type")
    missing = [name for name in required if not str(receipt.get(name) or "").strip()]
    if missing or receipt.get("commercial_use_allowed") is not True:
        raise ProviderExecutionError(
            "commercial license receipt invalid: " + ",".join(missing or ["commercial_use_allowed"])
        )
    sanitized = _redact(dict(receipt), ())
    assert isinstance(sanitized, dict)
    sanitized["receipt_hash"] = canonical_hash(sanitized)
    return sanitized


__all__ = [
    "HTTPResponse",
    "PaidWorkflowContract",
    "ProviderExecutionError",
    "ProviderExecutor",
    "run_paid_workflow",
    "validate_commercial_receipt",
]
