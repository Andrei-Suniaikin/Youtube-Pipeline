#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-host profile lease client used around the dual-host gate and worker."""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import re
import secrets
import subprocess
import threading
from typing import Any, Iterator


LEASE_CONTEXT_SCHEMA_VERSION = 1
PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HOLDER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class ProfileLeaseError(RuntimeError):
    pass


def normalize_lease_context(value: Any) -> dict[str, Any] | None:
    if value in (None, {}):
        return None
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "coordinator_command",
        "holder",
        "ttl_seconds",
        "renew_interval_seconds",
        "command_timeout_seconds",
    }:
        raise ProfileLeaseError("profile lease context fields are invalid")
    version = value.get("schema_version")
    if isinstance(version, bool) or version != LEASE_CONTEXT_SCHEMA_VERSION:
        raise ProfileLeaseError("unsupported profile lease context schema")
    command = value.get("coordinator_command")
    if (
        not isinstance(command, list)
        or not 1 <= len(command) <= 24
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 1024
            or "\x00" in item
            for item in command
        )
    ):
        raise ProfileLeaseError("profile lease coordinator command is invalid")
    holder = value.get("holder")
    if not isinstance(holder, str) or HOLDER_RE.fullmatch(holder) is None:
        raise ProfileLeaseError("profile lease holder is invalid")

    def number(name: str, minimum: float, maximum: float) -> float:
        raw = value.get(name)
        if isinstance(raw, bool):
            raise ProfileLeaseError(f"profile lease {name} is invalid")
        try:
            result = float(raw)
        except (TypeError, ValueError) as exc:
            raise ProfileLeaseError(f"profile lease {name} is invalid") from exc
        if not math.isfinite(result) or not minimum <= result <= maximum:
            raise ProfileLeaseError(f"profile lease {name} is invalid")
        return result

    ttl = number("ttl_seconds", 60, 6 * 3600)
    renew = number("renew_interval_seconds", 10, ttl / 2)
    timeout = number("command_timeout_seconds", 1, 120)
    return {
        "schema_version": LEASE_CONTEXT_SCHEMA_VERSION,
        "coordinator_command": list(command),
        "holder": holder,
        "ttl_seconds": ttl,
        "renew_interval_seconds": renew,
        "command_timeout_seconds": timeout,
    }


class ProfileLease:
    """Renewed owner-token lease backed by one atomic coordinator command."""

    def __init__(self, profile: str, context: dict[str, Any]):
        profile = str(profile or "")
        if PROFILE_RE.fullmatch(profile) is None:
            raise ProfileLeaseError("profile lease profile is invalid")
        normalized = normalize_lease_context(context)
        if normalized is None:
            raise ProfileLeaseError("profile lease context is required")
        self.profile = profile
        self.context = normalized
        self.owner_token = secrets.token_hex(24)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._lost_error = ""
        self.release_error = ""
        self._retain_until_expiry_reason = ""

    def _invoke(self, action: str) -> dict[str, Any]:
        argv = list(self.context["coordinator_command"])
        argv += [
            action,
            "--profile", self.profile,
            "--owner-token", self.owner_token,
            "--holder", self.context["holder"],
        ]
        if action in {"acquire", "renew"}:
            argv += ["--ttl-seconds", str(self.context["ttl_seconds"])]
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.context["command_timeout_seconds"],
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            # Текст исключения subprocess содержит ВЕСЬ argv, а в нём — `--owner-token`
            # с живым токеном лизы. Он уезжал в лог задачи и в сообщение об ошибке,
            # которое видит пользователь. Оставляем только класс ошибки; `from None`
            # обрывает и цепочку, где тот же argv лежит в __cause__ (правка production
            # 6fcb8b5b, срез live_2026-09-18).
            raise ProfileLeaseError(
                f"profile lease {action} transport failed ({type(exc).__name__})") from None
        lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
        if len(lines) != 1:
            raise ProfileLeaseError(f"profile lease {action} returned invalid framing")
        try:
            result = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise ProfileLeaseError(f"profile lease {action} returned invalid JSON") from exc
        if (
            not isinstance(result, dict)
            or type(result.get("schema_version")) is not int
            or result.get("schema_version") != 1
        ):
            raise ProfileLeaseError(f"profile lease {action} returned invalid schema")
        status_key = {
            "acquire": "acquired",
            "renew": "renewed",
            "release": "released",
        }[action]
        if type(result.get(status_key)) is not bool:
            raise ProfileLeaseError(f"profile lease {action} returned invalid status")
        lease = result.get("lease")
        if action != "release" and lease is not None:
            if (
                not isinstance(lease, dict)
                or "owner_token" in lease
                or lease.get("profile") != self.profile
                or not isinstance(lease.get("holder"), str)
            ):
                raise ProfileLeaseError(
                    f"profile lease {action} returned invalid lease identity"
                )
            if result[status_key] and lease.get("holder") != self.context["holder"]:
                raise ProfileLeaseError(
                    f"profile lease {action} returned wrong holder"
                )
        result["_returncode"] = completed.returncode
        return result

    def acquire(self) -> None:
        result = self._invoke("acquire")
        if result.get("_returncode") != 0 or result.get("acquired") is not True:
            lease = result.get("lease") if isinstance(result.get("lease"), dict) else {}
            holder = str(lease.get("holder") or "unknown")[:64]
            raise ProfileLeaseError(
                f"AdsPower profile {self.profile} is reserved by {holder}"
            )
        self._thread = threading.Thread(
            target=self._renew_loop,
            name=f"upload-profile-lease-{self.profile}",
            daemon=True,
        )
        self._thread.start()

    def _renew_loop(self) -> None:
        while not self._stop.wait(self.context["renew_interval_seconds"]):
            try:
                result = self._invoke("renew")
                if result.get("_returncode") != 0 or result.get("renewed") is not True:
                    raise ProfileLeaseError("coordinator no longer recognizes owner token")
            except ProfileLeaseError as exc:
                self._lost_error = str(exc)
                self._lost.set()
                return

    def assert_held(self) -> None:
        if self._lost.is_set():
            raise ProfileLeaseError(
                f"profile lease lost for {self.profile}: {self._lost_error or 'unknown'}"
            )

    @property
    def lost_event(self) -> threading.Event:
        return self._lost

    @property
    def lost_error(self) -> str:
        return self._lost_error

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.context["command_timeout_seconds"] + 1)
        if self._retain_until_expiry_reason:
            self.release_error = (
                "release intentionally deferred until coordinator TTL: "
                + self._retain_until_expiry_reason
            )
            return
        try:
            result = self._invoke("release")
            if result.get("_returncode") != 0 or result.get("released") is not True:
                self.release_error = "coordinator rejected release"
        except ProfileLeaseError as exc:
            # Never pretend a failed release freed the profile.  The remote TTL
            # remains the fail-safe; callers surface this diagnostic separately.
            self.release_error = str(exc)

    def retain_until_expiry(self, reason: str) -> None:
        """Stop renewing but deliberately leave the coordinator lease in place."""
        value = str(reason or "post-worker profile state is uncertain").strip()
        self._retain_until_expiry_reason = value[:500]


@contextmanager
def profile_lease(profile: str, context: dict[str, Any] | None) -> Iterator[ProfileLease | None]:
    normalized = normalize_lease_context(context)
    if normalized is None:
        yield None
        return
    lease = ProfileLease(profile, normalized)
    lease.acquire()
    try:
        lease.assert_held()
        yield lease
    finally:
        lease.close()
