#!/usr/bin/env python3
"""Sanitized, offline-only reference for a frozen upload wave.

This script intentionally omits browser, AdsPower, SSH, host, and credential
integration.  It demonstrates the identity and retry-boundary checks that must
surround the real upload runtime.  All input is supplied explicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any


UC_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PHASES = ("preflight", "profile_opened", "upload_started", "video_id_known")
REQUIRED_ROW_FIELDS = {
    "key",
    "channel",
    "lang",
    "uc",
    "profile",
    "device",
    "pid",
    "rewrite_model",
    "video_rel",
    "cover_rel",
    "text_rel",
    "bytes",
    "video_sha256",
    "cover_sha256",
    "text_sha256",
    "duration_seconds",
    "width",
    "height",
    "video_codec",
    "audio_codec",
    "title",
    "description",
    "schedule",
    "cutoff",
    "schedule_timezone",
}
ATTEMPT_KEYS = {
    "schema_version",
    "attempt_id",
    "manifest_sha256",
    "key",
    "uc",
    "profile",
    "pid",
    "normalized_title_sha256",
    "video_sha256",
    "worker_log",
    "phase",
    "video_id",
    "outcome",
    "manager_entered",
    "result_sha256",
    "created_at",
    "updated_at",
}


class ReferenceError(ValueError):
    """Fail-closed validation error."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReferenceError(f"{field} is empty")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ReferenceError(f"{field} is not a safe relative path")
    return path.as_posix()


def load_frozen_manifest(path: Path, expected_sha256: str) -> tuple[list[dict[str, Any]], str]:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise ReferenceError("expected manifest SHA-256 is invalid")
    actual_sha256 = file_sha256(path)
    if actual_sha256 != expected_sha256:
        raise ReferenceError("frozen manifest SHA-256 mismatch")
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceError(f"frozen manifest is unreadable: {exc}") from exc
    if not isinstance(rows, list) or not rows:
        raise ReferenceError("frozen manifest root must be a non-empty list")

    seen: dict[str, set[str]] = {name: set() for name in ("key", "uc", "pid")}
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ReferenceError(f"row {index} is not an object")
        missing = REQUIRED_ROW_FIELDS - set(row)
        if missing:
            raise ReferenceError(f"row {index} misses fields: {sorted(missing)}")
        if UC_RE.fullmatch(str(row["uc"])) is None:
            raise ReferenceError(f"row {index} has invalid UC")
        for field in ("key", "profile", "pid", "device"):
            if SAFE_ID_RE.fullmatch(str(row[field])) is None:
                raise ReferenceError(f"row {index} has invalid {field}")
        for field in ("video_rel", "cover_rel", "text_rel"):
            safe_relative_path(row[field], field)
        for field in ("video_sha256", "cover_sha256", "text_sha256"):
            if SHA256_RE.fullmatch(str(row[field])) is None:
                raise ReferenceError(f"row {index} has invalid {field}")
        for field, values in seen.items():
            value = str(row[field])
            if value in values:
                raise ReferenceError(f"duplicate manifest {field}: {value}")
            values.add(value)
        normalized.append(dict(row))
    return normalized, actual_sha256


def classify_attempt(
    journal: Any,
    *,
    manifest_sha256: str,
    row: dict[str, Any],
) -> tuple[str, str]:
    """Return `(retry_safe|blocked|reconcile, video_id)` for one attempt."""
    if not isinstance(journal, dict) or set(journal) != ATTEMPT_KEYS:
        raise ReferenceError("attempt journal fields are invalid")
    expected_identity = (
        manifest_sha256,
        row["key"],
        row["uc"],
        str(row["profile"]),
        row["pid"],
        row["video_sha256"],
    )
    actual_identity = (
        journal.get("manifest_sha256"),
        journal.get("key"),
        journal.get("uc"),
        journal.get("profile"),
        journal.get("pid"),
        journal.get("video_sha256"),
    )
    if actual_identity != expected_identity:
        raise ReferenceError("attempt journal identity mismatch")
    if journal.get("schema_version") != 2:
        raise ReferenceError("attempt journal schema is unsupported")
    if journal.get("phase") not in PHASES:
        raise ReferenceError("attempt phase is invalid")
    if type(journal.get("manager_entered")) is not bool:
        raise ReferenceError("manager_entered must be boolean")

    phase = str(journal["phase"])
    video_id = str(journal.get("video_id") or "")
    if phase == "video_id_known":
        if VIDEO_ID_RE.fullmatch(video_id) is None:
            raise ReferenceError("known upload has invalid video_id")
        return "reconcile", video_id
    if video_id:
        raise ReferenceError("pre-id attempt carries a video_id")
    if phase == "upload_started":
        return "blocked", ""
    return "retry_safe", ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--attempt-journal", type=Path)
    args = parser.parse_args()

    rows, manifest_sha256 = load_frozen_manifest(args.manifest, args.manifest_sha256)
    result: dict[str, Any] = {
        "manifest_sha256": manifest_sha256,
        "rows": len(rows),
        "mode": "offline-reference",
    }
    if args.attempt_journal:
        journal = json.loads(args.attempt_journal.read_text(encoding="utf-8"))
        disposition, video_id = classify_attempt(
            journal,
            manifest_sha256=manifest_sha256,
            row=rows[0],
        )
        result.update({"attempt_disposition": disposition, "video_id": video_id})
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
