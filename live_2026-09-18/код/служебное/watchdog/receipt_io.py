#!/usr/bin/env python3
"""Canonical upload receipts and a narrow atomic SSH transport.

The receipt is deliberately a proof emitted from the uploader's final Studio
row, not a copy of upload intent.  Integrity at rest is provided by a canonical
SHA-256; authenticity comes from the scoped SSH key/forced command used to put
the file in the VPS inbox.  There is intentionally no shared HMAC secret.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC = dt.timezone.utc
SCHEMA_VERSION = 2
PRODUCER_CONTEXT_SCHEMA_VERSION = 2
MAX_RECEIPT_BYTES = 64 * 1024
SCHEDULED_VISIBILITY = "VIDEO_USER_SET_VISIBILITY_SCHEDULED"
UC_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SSH_TARGET_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,255}$")
PERMANENT_REMOTE_REJECT_CODES = {
    "receipt_too_large",
    "receipt_json_invalid",
    "receipt_not_object",
    "receipt_fields_invalid",
    "schema_version_unsupported",
    "uc_invalid",
    "video_id_invalid",
    "manifest_sha256_invalid",
    "manifest_name_invalid",
    "expected_operational_date_invalid",
    "normalized_title_sha256_invalid",
    "source_host_invalid",
    "studio_state_invalid",
    "scheduled_epoch_invalid",
    "scheduled_at_utc_invalid",
    "studio_evidence_invalid",
    "receipt_sha256_invalid",
    "sha256_mismatch",
    "json_not_canonicalizable",
    "source_host_key_mismatch",
    "channel_not_enabled",
    "source_host_not_allowed",
    "operational_date_mismatch",
    "receipt_path_conflict",
    "stale",
    "verified_at_invalid",
    "timezone_or_epoch_invalid",
}
TOP_LEVEL_KEYS = {
    "schema_version",
    "uc",
    "video_id",
    "studio_state",
    "scheduled_epoch",
    "scheduled_at_utc",
    "verified_at",
    "manifest_sha256",
    "manifest_name",
    "expected_operational_date",
    "normalized_title_sha256",
    "source_host",
    "evidence",
    "receipt_sha256",
}
LEGACY_SCHEMA_VERSION = 1
TOP_LEVEL_KEYS_BY_VERSION = {
    # Схема 1 отличается ровно отсутствием ``scheduled_at_utc``: момент публикации в
    # ней задан только ``scheduled_epoch``, из которого ISO-строка выводится
    # однозначно. Читаем такие квитанции как валидные, потому что квитанция — это
    # доказательство УЖЕ СОСТОЯВШЕЙСЯ заливки, и подъём версии продюсера не вправе
    # обнулять его задним числом. Так 14.08 четыре реально опубликованных ролика от
    # 13.08 разом уехали в rejected и держали infra:receipt_inbox красным 175 проверок
    # подряд — единственные ⚠️-сообщения тех суток были про них, и они заглушили собой
    # обвал публикаций. Писать продюсер продолжает только SCHEMA_VERSION.
    LEGACY_SCHEMA_VERSION: TOP_LEVEL_KEYS - {"scheduled_at_utc"},
    SCHEMA_VERSION: TOP_LEVEL_KEYS,
}
EVIDENCE_KEYS = {
    "verified_channel_uc",
    "row_video_id",
    "studio_state",
    "visibility_user_set_visibility",
    "scheduled_time_seconds",
    "normalized_title_sha256",
}
PRODUCER_CONTEXT_KEYS = {
    "schema_version",
    "manifest_sha256",
    "manifest_name",
    "operational_date",
    "expected_scheduled_at",
    "source_host",
    "outbox_dir",
    "receipt_io_path",
    "existing_upload_policy",
    "push",
}


class ReceiptError(ValueError):
    """A safe stable validation code for an unusable receipt."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PermanentRemoteReject(ReceiptError):
    """A syntactically exact VPS NACK that must not be retried forever."""

    def __init__(self, remote_code: str):
        super().__init__("remote_rejected")
        self.remote_code = remote_code


def normalize_producer_context(value: Any) -> dict[str, Any]:
    """Validate the one versioned receipt-producer contract.

    ``existing_upload_policy`` is deliberately explicit.  ``reconcile_exact``
    means a local uploaded URL may be reconciled against the exact Studio row;
    it never means disabling dedup or the one-per-day decision silently.
    """
    if not isinstance(value, dict) or set(value) != PRODUCER_CONTEXT_KEYS:
        raise ReceiptError("producer_context_fields_invalid")
    version = value.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != PRODUCER_CONTEXT_SCHEMA_VERSION
    ):
        raise ReceiptError("producer_context_schema_unsupported")
    manifest_sha = value.get("manifest_sha256")
    manifest_name = value.get("manifest_name")
    operational_date = value.get("operational_date")
    expected_scheduled_at = value.get("expected_scheduled_at")
    source_host = value.get("source_host")
    outbox_dir = value.get("outbox_dir")
    receipt_io_path = value.get("receipt_io_path")
    existing_policy = value.get("existing_upload_policy")
    if not isinstance(manifest_sha, str) or SHA256_RE.fullmatch(manifest_sha) is None:
        raise ReceiptError("manifest_sha256_invalid")
    if (
        not isinstance(manifest_name, str)
        or not manifest_name
        or len(manifest_name) > 200
        or "/" in manifest_name
        or "\\" in manifest_name
        or any(ord(character) < 32 for character in manifest_name)
    ):
        raise ReceiptError("manifest_name_invalid")
    try:
        parsed_date = dt.date.fromisoformat(operational_date)
    except (TypeError, ValueError) as exc:
        raise ReceiptError("expected_operational_date_invalid") from exc
    if parsed_date.isoformat() != operational_date:
        raise ReceiptError("expected_operational_date_invalid")
    if not isinstance(source_host, str) or SOURCE_HOST_RE.fullmatch(source_host) is None:
        raise ReceiptError("source_host_invalid")
    if not isinstance(outbox_dir, str) or not os.path.isabs(outbox_dir):
        raise ReceiptError("producer_outbox_invalid")
    if not isinstance(receipt_io_path, str) or (
        receipt_io_path and not os.path.isabs(receipt_io_path)
    ):
        raise ReceiptError("producer_receipt_io_path_invalid")
    expected_instant = _parse_aware_iso(
        expected_scheduled_at, "expected_scheduled_at_invalid"
    )
    if expected_instant.second or expected_instant.microsecond:
        raise ReceiptError("expected_scheduled_at_invalid")
    if existing_policy not in {"skip", "reconcile_exact"}:
        raise ReceiptError("existing_upload_policy_invalid")

    push = value.get("push")
    if not isinstance(push, dict) or type(push.get("enabled")) is not bool:
        raise ReceiptError("producer_push_invalid")
    enabled = push["enabled"]
    expected_push_keys = (
        {"enabled", "ssh_target", "timeout_seconds"}
        if enabled
        else {"enabled"}
    )
    if set(push) != expected_push_keys:
        raise ReceiptError("producer_push_invalid")
    target = push.get("ssh_target", "")
    timeout_raw = push.get("timeout_seconds", 30.0)
    if enabled and (
        not isinstance(target, str)
        or SSH_TARGET_RE.fullmatch(target) is None
        or target.startswith("-")
    ):
        raise ReceiptError("ssh_target_invalid")
    if isinstance(timeout_raw, bool):
        raise ReceiptError("ssh_timeout_invalid")
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ReceiptError("ssh_timeout_invalid") from exc
    if not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise ReceiptError("ssh_timeout_invalid")
    return {
        "schema_version": PRODUCER_CONTEXT_SCHEMA_VERSION,
        "manifest_sha256": manifest_sha,
        "manifest_name": manifest_name,
        "operational_date": operational_date,
        "expected_scheduled_at": _utc_iso(expected_instant),
        "source_host": source_host,
        "outbox_dir": outbox_dir,
        "receipt_io_path": receipt_io_path,
        "existing_upload_policy": existing_policy,
        "push": (
            {
                "enabled": True,
                "ssh_target": target,
                "timeout_seconds": timeout,
            }
            if enabled
            else {"enabled": False}
        ),
    }


def _parse_hhmm(value: str) -> dt.time:
    try:
        return dt.datetime.strptime(value, "%H:%M").time()
    except (TypeError, ValueError) as exc:
        raise ReceiptError("publication_day_start_invalid") from exc


def _parse_utc(value: Any) -> dt.datetime:
    return _parse_aware_iso(value, "verified_at_invalid").astimezone(UTC)


def _parse_aware_iso(value: Any, error_code: str) -> dt.datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ReceiptError(error_code)
    raw = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ReceiptError(error_code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReceiptError(error_code)
    return parsed


def _utc_iso(value: dt.datetime | None = None) -> str:
    current = value or dt.datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ReceiptError("verified_at_invalid")
    return current.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_body(receipt: dict[str, Any]) -> bytes:
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    try:
        return json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReceiptError("json_not_canonicalizable") from exc


def receipt_sha256(receipt: dict[str, Any]) -> str:
    """Hash the canonical receipt body (the hash field itself is excluded)."""
    if not isinstance(receipt, dict):
        raise ReceiptError("receipt_not_object")
    return hashlib.sha256(_canonical_body(receipt)).hexdigest()


def _structure(receipt: dict[str, Any]) -> tuple[dt.datetime, int]:
    if not isinstance(receipt, dict):
        raise ReceiptError("receipt_not_object")
    # Версию проверяем ДО набора полей: иначе квитанция неподдерживаемой схемы
    # рапортует «receipt_fields_invalid», и по коду ошибки не отличить «продюсер
    # пишет мусор» от «квитанция старой версии».
    version = receipt.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in TOP_LEVEL_KEYS_BY_VERSION
    ):
        raise ReceiptError("schema_version_unsupported")
    if set(receipt) != TOP_LEVEL_KEYS_BY_VERSION[version]:
        raise ReceiptError("receipt_fields_invalid")

    uc = receipt.get("uc")
    video_id = receipt.get("video_id")
    manifest_sha = receipt.get("manifest_sha256")
    manifest_name = receipt.get("manifest_name")
    expected_operational_date = receipt.get("expected_operational_date")
    title_sha = receipt.get("normalized_title_sha256")
    source_host = receipt.get("source_host")
    if not isinstance(uc, str) or UC_RE.fullmatch(uc) is None:
        raise ReceiptError("uc_invalid")
    if not isinstance(video_id, str) or VIDEO_ID_RE.fullmatch(video_id) is None:
        raise ReceiptError("video_id_invalid")
    if not isinstance(manifest_sha, str) or SHA256_RE.fullmatch(manifest_sha) is None:
        raise ReceiptError("manifest_sha256_invalid")
    if (
        not isinstance(manifest_name, str)
        or not manifest_name
        or len(manifest_name) > 200
        or "/" in manifest_name
        or "\\" in manifest_name
        or any(ord(character) < 32 for character in manifest_name)
    ):
        raise ReceiptError("manifest_name_invalid")
    try:
        parsed_operational_date = dt.date.fromisoformat(expected_operational_date)
    except (TypeError, ValueError) as exc:
        raise ReceiptError("expected_operational_date_invalid") from exc
    if parsed_operational_date.isoformat() != expected_operational_date:
        raise ReceiptError("expected_operational_date_invalid")
    if not isinstance(title_sha, str) or SHA256_RE.fullmatch(title_sha) is None:
        raise ReceiptError("normalized_title_sha256_invalid")
    if not isinstance(source_host, str) or SOURCE_HOST_RE.fullmatch(source_host) is None:
        raise ReceiptError("source_host_invalid")
    if receipt.get("studio_state") != "SCHEDULED":
        raise ReceiptError("studio_state_invalid")
    scheduled_epoch = receipt.get("scheduled_epoch")
    if isinstance(scheduled_epoch, bool) or not isinstance(scheduled_epoch, int) or scheduled_epoch <= 0:
        raise ReceiptError("scheduled_epoch_invalid")
    if version >= SCHEMA_VERSION:
        # В схеме 1 этого поля нет: сверять нечего, момент публикации несёт
        # scheduled_epoch, и он уже проверен выше и перекрёстно сверяется с
        # evidence.scheduled_time_seconds ниже.
        scheduled_at_utc = receipt.get("scheduled_at_utc")
        scheduled_instant = _parse_aware_iso(
            scheduled_at_utc, "scheduled_at_utc_invalid"
        ).astimezone(UTC)
        if (
            scheduled_at_utc != _utc_iso(scheduled_instant)
            or int(scheduled_instant.timestamp()) != scheduled_epoch
        ):
            raise ReceiptError("scheduled_at_utc_invalid")

    evidence = receipt.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS:
        raise ReceiptError("studio_evidence_invalid")
    if (
        evidence.get("verified_channel_uc") != uc
        or evidence.get("row_video_id") != video_id
        or evidence.get("studio_state") != "SCHEDULED"
        or evidence.get("visibility_user_set_visibility") != SCHEDULED_VISIBILITY
        or evidence.get("scheduled_time_seconds") != scheduled_epoch
        or evidence.get("normalized_title_sha256") != title_sha
    ):
        raise ReceiptError("studio_evidence_invalid")

    digest = receipt.get("receipt_sha256")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise ReceiptError("receipt_sha256_invalid")
    if digest != receipt_sha256(receipt):
        raise ReceiptError("sha256_mismatch")
    return _parse_utc(receipt.get("verified_at")), scheduled_epoch


def validate_receipt_structure(receipt: dict[str, Any]) -> dict[str, Any]:
    """Public structural/hash validator shared by producers and migrations."""
    _structure(receipt)
    return dict(receipt)


def build_scheduled_receipt(
    *,
    evidence: dict[str, Any],
    manifest_sha256: str,
    manifest_name: str,
    expected_operational_date: str,
    source_host: str,
    expected_scheduled_at: str,
    verified_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """Seal exact evidence from one verified ``ytcp-video-row``.

    ``evidence`` must be built only after exact channel routing and the final
    row read.  In particular, schedule intent and the old global upload log do
    not have the required fields and cannot produce a receipt.
    """
    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS:
        raise ReceiptError("studio_evidence_invalid")
    expected_instant = _parse_aware_iso(
        expected_scheduled_at, "expected_scheduled_at_invalid"
    ).astimezone(UTC)
    if (
        expected_instant.second
        or expected_instant.microsecond
        or isinstance(evidence.get("scheduled_time_seconds"), bool)
        or not isinstance(evidence.get("scheduled_time_seconds"), int)
        or int(expected_instant.timestamp()) != evidence.get("scheduled_time_seconds")
    ):
        raise ReceiptError("expected_scheduled_at_invalid")
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "uc": evidence.get("verified_channel_uc"),
        "video_id": evidence.get("row_video_id"),
        "studio_state": evidence.get("studio_state"),
        "scheduled_epoch": evidence.get("scheduled_time_seconds"),
        "scheduled_at_utc": _utc_iso(expected_instant),
        "verified_at": _utc_iso(verified_at),
        "manifest_sha256": manifest_sha256,
        "manifest_name": manifest_name,
        "expected_operational_date": expected_operational_date,
        "normalized_title_sha256": evidence.get("normalized_title_sha256"),
        "source_host": source_host,
        "evidence": dict(evidence),
    }
    receipt["receipt_sha256"] = receipt_sha256(receipt)
    _structure(receipt)
    return receipt


def operational_date_for_epoch(
    epoch: int,
    timezone: str | dt.tzinfo,
    publication_day_start: str,
) -> dt.date:
    """Derive the operational date from Studio's actual scheduled epoch."""
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
        raise ReceiptError("scheduled_epoch_invalid")
    try:
        zone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
        local = dt.datetime.fromtimestamp(epoch, tz=UTC).astimezone(zone)
    except (OSError, OverflowError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ReceiptError("timezone_or_epoch_invalid") from exc
    boundary = _parse_hhmm(publication_day_start)
    if boundary == dt.time(0, 0):
        return local.date()
    if local.time().replace(tzinfo=None) >= boundary:
        return local.date() + dt.timedelta(days=1)
    return local.date()


def validate_receipt(
    receipt: dict[str, Any],
    *,
    enabled_channels: dict[str, int] | set[str] | Iterable[str] | None = None,
    timezone: str = "UTC",
    publication_day_start: str = "00:00",
    allowed_source_hosts: set[str] | Iterable[str] | None = None,
    max_age_hours: float | None = None,
    future_skew_minutes: float = 5.0,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Validate canonical proof and add its derived operational date."""
    verified_at, scheduled_epoch = _structure(receipt)
    current = now or dt.datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ReceiptError("now_invalid")
    current = current.astimezone(UTC)
    try:
        if isinstance(future_skew_minutes, bool):
            raise ValueError("boolean future skew")
        future_skew = float(future_skew_minutes)
    except (TypeError, ValueError) as exc:
        raise ReceiptError("future_skew_invalid") from exc
    if not math.isfinite(future_skew) or future_skew < 0:
        raise ReceiptError("future_skew_invalid")
    if verified_at > current + dt.timedelta(minutes=future_skew):
        raise ReceiptError("verified_at_future")
    if max_age_hours is not None:
        try:
            if isinstance(max_age_hours, bool):
                raise ValueError("boolean max age")
            max_age = float(max_age_hours)
        except (TypeError, ValueError) as exc:
            raise ReceiptError("max_age_invalid") from exc
        if not math.isfinite(max_age) or max_age <= 0:
            raise ReceiptError("max_age_invalid")
        if current - verified_at > dt.timedelta(hours=max_age):
            raise ReceiptError("stale")

    if enabled_channels is not None:
        known = set(enabled_channels)
        if receipt["uc"] not in known:
            raise ReceiptError("channel_not_enabled")
    if allowed_source_hosts is not None and receipt["source_host"] not in set(allowed_source_hosts):
        raise ReceiptError("source_host_not_allowed")

    normalized = dict(receipt)
    normalized["operational_date"] = operational_date_for_epoch(
        scheduled_epoch, timezone, publication_day_start
    ).isoformat()
    if normalized["operational_date"] != receipt["expected_operational_date"]:
        raise ReceiptError("operational_date_mismatch")
    normalized["verified_at_epoch"] = int(verified_at.timestamp())
    normalized["conflict"] = False
    return normalized


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextmanager
def _directory_lock(directory: Path, name: str = ".receipt.lock"):
    """Serialize local RMW decisions while keeping atomic hard-link publication."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = directory / name
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except ImportError as exc:  # receipt producers are supported on macOS/Linux only
            raise ReceiptError("file_lock_unavailable") from exc
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        os.close(descriptor)


def _encoded(receipt: dict[str, Any]) -> bytes:
    data = json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    if len(data) > MAX_RECEIPT_BYTES:
        raise ReceiptError("receipt_too_large")
    return data


def _read_file(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReceiptError("receipt_file_unreadable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ReceiptError("receipt_file_not_regular")
        if info.st_size > MAX_RECEIPT_BYTES:
            raise ReceiptError("receipt_too_large")
        chunks: list[bytes] = []
        remaining = MAX_RECEIPT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(16 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_RECEIPT_BYTES:
            raise ReceiptError("receipt_too_large")
        value = json.loads(raw.decode("utf-8"))
    except ReceiptError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiptError("receipt_json_invalid") from exc
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise ReceiptError("receipt_not_object")
    return value


def _store_no_replace(
    receipt: dict[str, Any], directory: Path, *, create_directory: bool = True
) -> Path:
    _structure(receipt)
    if create_directory:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    elif not directory.exists():
        raise ReceiptError("inbox_missing")
    try:
        directory_info = directory.lstat()
    except OSError as exc:
        raise ReceiptError("inbox_unavailable") from exc
    if not stat.S_ISDIR(directory_info.st_mode) or stat.S_ISLNK(directory_info.st_mode):
        raise ReceiptError("inbox_not_directory")
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    destination = directory / f"{receipt['receipt_sha256']}.json"
    data = _encoded(receipt)
    with _directory_lock(directory):
        if destination.exists():
            existing = _read_file(destination)
            if existing != receipt:
                raise ReceiptError("receipt_path_conflict")
            return destination

        fd, temp_name = tempfile.mkstemp(prefix=".receipt.", suffix=".tmp", dir=directory)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            try:
                os.link(temp_path, destination)
            except FileExistsError:
                existing = _read_file(destination)
                if existing != receipt:
                    raise ReceiptError("receipt_path_conflict")
            _fsync_directory(directory)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    return destination


def write_outbox(receipt: dict[str, Any], outbox_dir: str | os.PathLike[str]) -> Path:
    """Atomically persist a receipt before any network delivery attempt."""
    return _store_no_replace(receipt, Path(outbox_dir))


def store_inbox(receipt: dict[str, Any], inbox_dir: str | os.PathLike[str]) -> Path:
    """Atomically and idempotently store one already sealed receipt on the VPS."""
    return _store_no_replace(receipt, Path(inbox_dir), create_directory=False)


def load_policy(config_path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiptError("config_invalid") from exc
    if not isinstance(raw, dict):
        raise ReceiptError("config_invalid")
    receipt_config = raw.get("receipts", {})
    channels = raw.get("channels", [])
    if not isinstance(receipt_config, dict) or not isinstance(channels, list):
        raise ReceiptError("config_invalid")
    if receipt_config.get("enabled") is not True:
        raise ReceiptError("receipts_disabled")
    enabled: dict[str, int] = {}
    for channel in channels:
        if not isinstance(channel, dict) or not channel.get("enabled", True):
            continue
        uc = channel.get("uc")
        expected = channel.get("expected_publications", 1)
        if (
            isinstance(uc, str)
            and UC_RE.fullmatch(uc) is not None
            and isinstance(expected, int)
            and not isinstance(expected, bool)
            and expected > 0
        ):
            enabled[uc] = expected
    hosts = receipt_config.get("allowed_source_hosts", ["mac", "minsk"])
    if (
        not isinstance(hosts, list)
        or not hosts
        or any(
            not isinstance(item, str) or SOURCE_HOST_RE.fullmatch(item) is None
            for item in hosts
        )
    ):
        raise ReceiptError("config_invalid")
    timezone_name = raw.get("timezone")
    publication_day_start = raw.get("publication_day_start", "00:00")
    try:
        ZoneInfo(timezone_name)
        _parse_hhmm(publication_day_start)
        raw_max_age = receipt_config.get("max_age_hours", 72)
        raw_future_skew = receipt_config.get("future_skew_minutes", 5)
        if isinstance(raw_max_age, bool) or isinstance(raw_future_skew, bool):
            raise ValueError("boolean receipt numeric")
        max_age_hours = float(raw_max_age)
        future_skew_minutes = float(raw_future_skew)
    except (TypeError, ValueError, ZoneInfoNotFoundError, ReceiptError) as exc:
        raise ReceiptError("config_invalid") from exc
    if (
        not math.isfinite(max_age_hours)
        or not math.isfinite(future_skew_minutes)
        or max_age_hours <= 0
        or future_skew_minutes < 0
    ):
        raise ReceiptError("config_invalid")
    return {
        "enabled_channels": enabled,
        "timezone": timezone_name,
        "publication_day_start": publication_day_start,
        "allowed_source_hosts": set(hosts),
        "max_age_hours": max_age_hours,
        "future_skew_minutes": future_skew_minutes,
    }


def _semantic_key(receipt: dict[str, Any]) -> tuple[Any, ...]:
    return (
        receipt["uc"],
        receipt["video_id"],
        receipt["scheduled_epoch"],
        receipt["manifest_sha256"],
        receipt["expected_operational_date"],
        receipt["normalized_title_sha256"],
    )


def _bounded_file_digest(path: Path) -> str:
    """Hash bounded content plus metadata without reading an oversized reject into RAM."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReceiptError("receipt_archive_failed") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ReceiptError("receipt_archive_failed")
        hasher = hashlib.sha256()
        hasher.update(path.name.encode("utf-8", "surrogateescape"))
        hasher.update(f"\0{info.st_size}\0{info.st_mtime_ns}".encode("ascii"))
        remaining = MAX_RECEIPT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(16 * 1024, remaining))
            if not chunk:
                break
            hasher.update(chunk)
            remaining -= len(chunk)
        return hasher.hexdigest()
    finally:
        os.close(descriptor)


def _archive_to_directory(path: Path, archive_dir: Path) -> Path:
    token = _bounded_file_digest(path)[:16]
    archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _directory_lock(archive_dir, ".archive.lock"):
        destination = archive_dir / f"{path.stem}.{token}{path.suffix}"
        counter = 0
        while destination.exists():
            counter += 1
            destination = archive_dir / f"{path.stem}.{token}.{counter}{path.suffix}"
        try:
            os.link(path, destination, follow_symlinks=False)
            _fsync_directory(archive_dir)
            path.unlink()
        except OSError as exc:
            try:
                destination.unlink()
            except OSError:
                pass
            raise ReceiptError("receipt_archive_failed") from exc
        _fsync_directory(path.parent)
    return destination


def _archive_receipt(path: Path, category: str) -> Path:
    return _archive_to_directory(path, path.parent.parent / "archive" / category)


def _prune_archive(archive_root: Path, cutoff_epoch: float) -> int:
    removed = 0
    if not archive_root.exists():
        return removed
    for path in archive_root.glob("*/*.json"):
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff_epoch:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def scan_inbox(
    inbox_dir: str | os.PathLike[str],
    *,
    enabled_channels: dict[str, int] | set[str] | Iterable[str],
    timezone: str,
    publication_day_start: str,
    allowed_source_hosts: set[str] | Iterable[str],
    max_age_hours: float,
    future_skew_minutes: float,
    now: dt.datetime | None = None,
    rotate: bool = False,
    archive_retention_days: float = 30.0,
    diagnostic_cap: int = 25,
    rejected_alert_hours: float = 24.0,
) -> dict[str, Any]:
    """Read valid receipts, collapse re-verifications and expose conflicts.

    ``rejected_alert_hours`` разделяет ДВА разных факта, которые раньше считались
    одним числом: сколько отклонённых квитанций лежит в архиве всего (это бэклог,
    он живёт до ``archive_retention_days`` = 30 суток) и сколько появилось недавно
    (это здоровье: значит, продюсер прямо сейчас пишет то, что не проходит
    проверку). Здоровьем считается второе. Пока считалось первое, четыре файла от
    13.08 держали ``infra:receipt_inbox`` красным 175 проверок подряд и приучали
    не читать тревоги вообще.
    """
    if not isinstance(diagnostic_cap, int) or isinstance(diagnostic_cap, bool) or diagnostic_cap < 1:
        raise ReceiptError("diagnostic_cap_invalid")
    try:
        retention_days = float(archive_retention_days)
    except (TypeError, ValueError) as exc:
        raise ReceiptError("archive_retention_invalid") from exc
    if not math.isfinite(retention_days) or retention_days <= 0:
        raise ReceiptError("archive_retention_invalid")
    directory = Path(inbox_dir)
    if not directory.exists():
        raise ReceiptError("inbox_missing")
    try:
        directory_info = directory.lstat()
    except OSError as exc:
        raise ReceiptError("inbox_unavailable") from exc
    if not stat.S_ISDIR(directory_info.st_mode) or stat.S_ISLNK(directory_info.st_mode):
        raise ReceiptError("inbox_not_directory")
    valid_files = 0
    invalid: list[dict[str, str]] = []
    expired: list[dict[str, str]] = []
    audit_records: list[dict[str, Any]] = []
    rotated_expired = 0
    rotated_rejected = 0
    by_semantic: dict[tuple[Any, ...], dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        receipt = None
        try:
            receipt = _read_file(path)
            normalized = validate_receipt(
                receipt,
                enabled_channels=enabled_channels,
                timezone=timezone,
                publication_day_start=publication_day_start,
                allowed_source_hosts=allowed_source_hosts,
                max_age_hours=max_age_hours,
                future_skew_minutes=future_skew_minutes,
                now=now,
            )
            if path.name != f"{normalized['receipt_sha256']}.json":
                raise ReceiptError("receipt_filename_mismatch")
            valid_files += 1
            key = _semantic_key(normalized)
            previous = by_semantic.get(key)
            if previous is None or normalized["verified_at_epoch"] > previous["verified_at_epoch"]:
                by_semantic[key] = normalized
        except ReceiptError as exc:
            target = expired if exc.code == "stale" else invalid
            target.append({"file": path.name[:96], "error": exc.code})
            audit_sha = path.stem if SHA256_RE.fullmatch(path.stem) else ""
            if not audit_sha:
                try:
                    audit_sha = _bounded_file_digest(path)
                except ReceiptError:
                    audit_sha = hashlib.sha256(path.name.encode("utf-8")).hexdigest()
            audit_uc = ""
            audit_video_id = ""
            audit_operational_date = ""
            audit_source_host = ""
            if isinstance(receipt, dict):
                audit_uc = str(receipt.get("uc") or "")[:64]
                audit_video_id = str(receipt.get("video_id") or "")[:32]
                audit_source_host = str(receipt.get("source_host") or "")[:64]
                try:
                    audit_operational_date = operational_date_for_epoch(
                        int(receipt.get("scheduled_epoch") or 0),
                        timezone,
                        publication_day_start,
                    ).isoformat()
                except (ReceiptError, TypeError, ValueError):
                    audit_operational_date = ""
            audit_records.append(
                {
                    "receipt_sha256": audit_sha,
                    "status": "expired" if exc.code == "stale" else "rejected",
                    "reason": exc.code,
                    "uc": audit_uc,
                    "video_id": audit_video_id,
                    "operational_date": audit_operational_date,
                    "source_host": audit_source_host,
                }
            )
            if rotate:
                try:
                    _archive_receipt(path, "expired" if exc.code == "stale" else "rejected")
                    if exc.code == "stale":
                        rotated_expired += 1
                    else:
                        rotated_rejected += 1
                except ReceiptError as archive_error:
                    invalid.append({"file": path.name[:96], "error": archive_error.code})

    receipts = list(by_semantic.values())
    semantic_duplicates = valid_files - len(receipts)
    conflict_groups: list[dict[str, Any]] = []
    conflicting_hashes: set[str] = set()

    by_video: dict[str, list[dict[str, Any]]] = {}
    by_channel_day: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_channel_video: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for receipt in receipts:
        by_video.setdefault(receipt["video_id"], []).append(receipt)
        by_channel_day.setdefault(
            (receipt["uc"], receipt["operational_date"]), []
        ).append(receipt)
        by_channel_video.setdefault((receipt["uc"], receipt["video_id"]), []).append(receipt)

    def note_conflict(code: str, group: Iterable[dict[str, Any]]) -> None:
        hashes = sorted({item["receipt_sha256"] for item in group})
        marker = (code, tuple(hashes))
        if any(item.get("_marker") == marker for item in conflict_groups):
            return
        conflict_groups.append({"code": code, "receipts": hashes, "_marker": marker})
        conflicting_hashes.update(hashes)

    for group in by_video.values():
        identities = {
            (
                item["uc"], item["scheduled_epoch"], item["manifest_sha256"],
                item["normalized_title_sha256"],
            )
            for item in group
        }
        if len(identities) > 1:
            note_conflict("video_identity_conflict", group)
    for group in by_channel_video.values():
        identities = {
            (
                item["scheduled_epoch"], item["manifest_sha256"],
                item["normalized_title_sha256"],
            )
            for item in group
        }
        if len(identities) > 1:
            note_conflict("schedule_or_manifest_conflict", group)
    expected_map = enabled_channels if isinstance(enabled_channels, dict) else {}
    for (uc, _date), group in by_channel_day.items():
        expected = int(expected_map.get(uc, 1)) if isinstance(expected_map, dict) else 1
        if len({item["video_id"] for item in group}) > expected:
            note_conflict("channel_day_overbooked", group)

    for receipt in receipts:
        receipt["conflict"] = receipt["receipt_sha256"] in conflicting_hashes
        audit_records.append(
            {
                "receipt_sha256": receipt["receipt_sha256"],
                "status": "conflict" if receipt["conflict"] else "valid",
                "reason": "receipt_conflict" if receipt["conflict"] else "",
                "uc": receipt["uc"],
                "video_id": receipt["video_id"],
                "operational_date": receipt["operational_date"],
                "source_host": receipt["source_host"],
            }
        )
    for item in conflict_groups:
        item.pop("_marker", None)
    receipts.sort(key=lambda item: (item["operational_date"], item["uc"], item["video_id"]))
    conflict_count = len(conflict_groups)
    conflict_summary = [
        {
            "code": item["code"],
            "receipt_count": len(item["receipts"]),
            "receipts": item["receipts"][:diagnostic_cap],
        }
        for item in conflict_groups[:diagnostic_cap]
    ]
    archive_pruned = 0
    if rotate:
        current = now or dt.datetime.now(UTC)
        archive_pruned = _prune_archive(
            directory.parent / "archive",
            current.timestamp() - retention_days * 86400,
        )
    try:
        alert_hours = float(rejected_alert_hours)
    except (TypeError, ValueError) as exc:
        raise ReceiptError("rejected_alert_hours_invalid") from exc
    if not math.isfinite(alert_hours) or alert_hours <= 0:
        raise ReceiptError("rejected_alert_hours_invalid")
    rejected_cutoff = (now or dt.datetime.now(UTC)).timestamp() - alert_hours * 3600
    rejected_archive: list[dict[str, str]] = []
    rejected_recent: list[dict[str, str]] = []
    rejected_archive_dir = directory.parent / "archive" / "rejected"
    if rejected_archive_dir.exists():
        for archived_path in sorted(rejected_archive_dir.glob("*.json")):
            try:
                info = archived_path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                entry = {"file": archived_path.name[:96], "error": "archived_rejected"}
                rejected_archive.append(entry)
                if info.st_mtime >= rejected_cutoff:
                    rejected_recent.append(entry)
    return {
        "receipts": receipts,
        "valid_files": valid_files,
        "semantic_duplicates": semantic_duplicates,
        "invalid_count": len(invalid),
        "expired_count": len(expired),
        "conflict_count": conflict_count,
        "invalid": invalid[:diagnostic_cap],
        "expired": expired[:diagnostic_cap],
        "conflicts": conflict_summary,
        "rotated_expired": rotated_expired,
        "rotated_rejected": rotated_rejected,
        "archive_pruned": archive_pruned,
        "unresolved_rejected_count": len(rejected_archive),
        "rejected_archive": rejected_archive[:diagnostic_cap],
        "rejected_recent_count": len(rejected_recent),
        "rejected_recent": rejected_recent[:diagnostic_cap],
        "audit_records": audit_records,
    }


def ingest_bytes(
    data: bytes,
    inbox_dir: str | os.PathLike[str],
    *,
    policy: dict[str, Any] | None = None,
    expected_source_host: str | None = None,
    now: dt.datetime | None = None,
) -> tuple[Path, str]:
    if len(data) > MAX_RECEIPT_BYTES:
        raise ReceiptError("receipt_too_large")
    try:
        receipt = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiptError("receipt_json_invalid") from exc
    _structure(receipt)
    if expected_source_host is not None and receipt.get("source_host") != expected_source_host:
        raise ReceiptError("source_host_key_mismatch")
    if policy is not None:
        validate_receipt(receipt, now=now, **policy)
    destination = Path(inbox_dir) / f"{receipt['receipt_sha256']}.json"
    existed = destination.exists()
    path = store_inbox(receipt, inbox_dir)
    disposition = "exists" if existed else "stored"
    if policy is not None:
        scan = scan_inbox(inbox_dir, now=now, **policy)
        if any(
            item["receipt_sha256"] == receipt["receipt_sha256"] and item["conflict"]
            for item in scan["receipts"]
        ):
            disposition = "conflict"
    return path, disposition


def _archive_sent(source: Path) -> Path:
    sent_dir = source.parent / "sent"
    sent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = sent_dir / source.name
    if destination.exists():
        if _read_file(destination) != _read_file(source):
            raise ReceiptError("sent_path_conflict")
        source.unlink()
    else:
        os.replace(source, destination)
    _fsync_directory(source.parent)
    _fsync_directory(sent_dir)
    return destination


def _deliver_one(
    path: Path,
    ssh_target: str,
    *,
    ssh_binary: str,
    timeout_seconds: float,
) -> None:
    receipt = _read_file(path)
    _structure(receipt)
    completed = subprocess.run(
        [ssh_binary, "--", ssh_target],
        input=_encoded(receipt),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )
    stdout = completed.stdout.decode("utf-8", "replace") if isinstance(completed.stdout, bytes) else str(completed.stdout or "")
    expected_ack = re.compile(
        rf"^OK {re.escape(receipt['receipt_sha256'])} (?:stored|exists|conflict)$"
    )
    if completed.returncode == 0 and expected_ack.fullmatch(stdout.strip()) is not None:
        _archive_sent(path)
        return
    stderr = (
        completed.stderr.decode("utf-8", "replace")
        if isinstance(completed.stderr, bytes)
        else str(completed.stderr or "")
    )
    remote_error = re.fullmatch(r"ERROR ([a-z0-9_]{1,64})", stderr.strip())
    if completed.returncode == 2 and remote_error is not None:
        remote_code = remote_error.group(1)
        if remote_code in PERMANENT_REMOTE_REJECT_CODES:
            raise PermanentRemoteReject(remote_code)
        # Fail safe: server/config/inbox/lock/future-skew and unknown new codes
        # are repairable infrastructure failures, so retain the canonical proof.
        raise ReceiptError(f"remote_transient:{remote_code}")
    raise ReceiptError("ssh_delivery_failed")


def _validate_push_options(
    ssh_target: str,
    timeout_seconds: float,
    retention_days: float,
    diagnostic_cap: int,
) -> tuple[str, float, float, int]:
    target = str(ssh_target or "")
    if SSH_TARGET_RE.fullmatch(target) is None or target.startswith("-"):
        raise ReceiptError("ssh_target_invalid")
    if isinstance(timeout_seconds, bool):
        raise ReceiptError("ssh_timeout_invalid")
    try:
        timeout = float(timeout_seconds)
        retention = float(retention_days)
    except (TypeError, ValueError) as exc:
        raise ReceiptError("push_options_invalid") from exc
    if not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise ReceiptError("ssh_timeout_invalid")
    if not math.isfinite(retention) or retention <= 0:
        raise ReceiptError("archive_retention_invalid")
    if not isinstance(diagnostic_cap, int) or isinstance(diagnostic_cap, bool) or diagnostic_cap < 1:
        raise ReceiptError("diagnostic_cap_invalid")
    return target, timeout, retention, diagnostic_cap


def _new_push_result() -> dict[str, Any]:
    return {
        "pending": 0,
        "sent": 0,
        "failed": 0,
        "rejected": 0,
        "deadlettered": 0,
        "archive_pruned": 0,
        "errors": [],
    }


def _record_push_error(
    result: dict[str, Any], path: Path, code: str, diagnostic_cap: int
) -> None:
    if len(result["errors"]) < diagnostic_cap:
        result["errors"].append({"file": path.name[:96], "error": code})


def _attempt_delivery(
    path: Path,
    ssh_target: str,
    *,
    ssh_binary: str,
    timeout_seconds: float,
    result: dict[str, Any],
    diagnostic_cap: int,
) -> None:
    try:
        _deliver_one(
            path,
            ssh_target,
            ssh_binary=ssh_binary,
            timeout_seconds=timeout_seconds,
        )
        result["sent"] += 1
    except PermanentRemoteReject as exc:
        result["rejected"] += 1
        try:
            _archive_to_directory(path, path.parent / "deadletter")
        except ReceiptError as archive_error:
            result["failed"] += 1
            _record_push_error(result, path, archive_error.code, diagnostic_cap)
        else:
            result["deadlettered"] += 1
            _record_push_error(
                result, path, f"remote_rejected:{exc.remote_code}", diagnostic_cap
            )
    except (OSError, subprocess.SubprocessError, ReceiptError) as exc:
        result["failed"] += 1
        code = exc.code if isinstance(exc, ReceiptError) else "ssh_delivery_failed"
        _record_push_error(result, path, code, diagnostic_cap)


def push_receipt(
    receipt_path: str | os.PathLike[str],
    ssh_target: str,
    *,
    ssh_binary: str = "ssh",
    timeout_seconds: float = 30.0,
    archive_retention_days: float = 30.0,
    diagnostic_cap: int = 25,
) -> dict[str, Any]:
    """Attempt only the just-created receipt; backlog retry belongs to the timer."""
    ssh_target, timeout_seconds, retention_days, diagnostic_cap = _validate_push_options(
        ssh_target, timeout_seconds, archive_retention_days, diagnostic_cap
    )
    path = Path(receipt_path)
    result = _new_push_result()
    if not path.exists():
        return result
    with _directory_lock(path.parent, ".push.lock"):
        if not path.exists():
            return result
        result["pending"] = 1
        _attempt_delivery(
            path,
            ssh_target,
            ssh_binary=ssh_binary,
            timeout_seconds=timeout_seconds,
            result=result,
            diagnostic_cap=diagnostic_cap,
        )
        result["archive_pruned"] = _prune_archive(
            path.parent,
            dt.datetime.now(UTC).timestamp() - retention_days * 86400,
        )
    return result


def push_outbox(
    outbox_dir: str | os.PathLike[str],
    ssh_target: str,
    *,
    ssh_binary: str = "ssh",
    timeout_seconds: float = 30.0,
    archive_retention_days: float = 30.0,
    diagnostic_cap: int = 25,
) -> dict[str, Any]:
    """Push pending receipts; only transient failures remain pending."""
    ssh_target, timeout_seconds, retention_days, diagnostic_cap = _validate_push_options(
        ssh_target, timeout_seconds, archive_retention_days, diagnostic_cap
    )
    directory = Path(outbox_dir)
    result = _new_push_result()
    if not directory.exists():
        return result
    with _directory_lock(directory, ".push.lock"):
        for path in sorted(directory.glob("*.json")):
            result["pending"] += 1
            _attempt_delivery(
                path,
                ssh_target,
                ssh_binary=ssh_binary,
                timeout_seconds=timeout_seconds,
                result=result,
                diagnostic_cap=diagnostic_cap,
            )
        result["archive_pruned"] = _prune_archive(
            directory,
            dt.datetime.now(UTC).timestamp() - retention_days * 86400,
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VideoStudio canonical upload receipts")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest = subparsers.add_parser("ingest", help="forced-command: read one receipt from stdin")
    ingest.add_argument("--inbox", required=True)
    ingest.add_argument("--config", required=True)
    ingest.add_argument(
        "--source-host", required=True,
        help="producer identity pinned by this SSH key's forced command",
    )
    push = subparsers.add_parser("push", help="retry all locally pending receipts over SSH")
    push.add_argument("--outbox", required=True)
    push.add_argument("--target", required=True)
    push.add_argument("--ssh-binary", default="ssh")
    push.add_argument("--timeout-seconds", type=float, default=30.0)
    push.add_argument("--archive-retention-days", type=float, default=30.0)
    push.add_argument("--diagnostic-cap", type=int, default=25)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "ingest":
            data = sys.stdin.buffer.read(MAX_RECEIPT_BYTES + 1)
            policy = load_policy(args.config)
            path, disposition = ingest_bytes(
                data,
                args.inbox,
                policy=policy,
                expected_source_host=args.source_host,
            )
            print(f"OK {path.stem} {disposition}")
            return 0
        result = push_outbox(
            args.outbox,
            args.target,
            ssh_binary=args.ssh_binary,
            timeout_seconds=args.timeout_seconds,
            archive_retention_days=args.archive_retention_days,
            diagnostic_cap=args.diagnostic_cap,
        )
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return 0 if result["failed"] == 0 else 1
    except ReceiptError as exc:
        print(f"ERROR {exc.code}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
