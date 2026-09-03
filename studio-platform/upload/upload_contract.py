#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Versioned durable protocol shared by the upload worker and manager.

The process boundary is not the upload boundary.  A retry becomes unsafe only
after the worker has handed the file to Studio (``upload_started``), or after a
specific Studio ``video_id`` is known.  These phases are emitted as monotonic
markers so the parent can recover the correct decision even if the worker dies
before its terminal JSON result.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any


PHASE_MARKER = "__UPLOAD_PHASE__|"
PHASE_ACK_MARKER = "__UPLOAD_PHASE_ACK__|"
RESULT_MARKER = "__UPLOAD_RESULT__|"
PHASE_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 2

PHASES = (
    "preflight",
    "profile_opened",
    "upload_started",
    "video_id_known",
)
PHASE_RANK = {phase: index for index, phase in enumerate(PHASES)}
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
UC_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

RESULT_REQUIRED_KEYS = {
    "upload_result_schema_version",
    "phase",
    "success",
    "url",
    "error",
    "visibility",
    "video_id",
    "do_not_reupload",
    "studio_row_evidence",
    "exact_studio_evidence",
}
RESULT_OPTIONAL_BOOL_KEYS = {
    "skipped",
    "draft_uploaded",
    "monetized",
    "ab_test",
    "collaborator",
    "midrolls",
}
RESULT_OPTIONAL_KEYS = RESULT_OPTIONAL_BOOL_KEYS | {"reason", "altered_disclosed"}
RESULT_VISIBILITIES = {
    "unknown",
    "UNKNOWN",
    "DRAFT",
    "draft",
    "DRAFT_UPLOADED",
    "DRAFT_EXISTS",
    "BUSY",
    "NOT_FOUND",
    "PROCESSING",
    "PUBLIC_PENDING",
    "PUBLIC",
    "PRIVATE",
    "UNLISTED",
    "SCHEDULED",
    "ALREADY_EXISTS",
    "SCHEDULE_UNVERIFIED",
    "SCHEDULED_WRONG_TIME",
    "SCHEDULED_WRONG_DATE",
    "TITLE_MISMATCH",
    "RECEIPT_PERSIST_FAILED",
}
EXACT_EVIDENCE_KEYS = {
    "verified_channel_uc",
    "row_video_id",
    "studio_state",
    "visibility_user_set_visibility",
    "scheduled_time_seconds",
    "normalized_title_sha256",
}
ROW_EVIDENCE_KEYS = EXACT_EVIDENCE_KEYS | {
    "observed_normalized_title_sha256",
    "title_matches",
}


class UploadContractError(ValueError):
    """A stable fail-closed protocol validation error."""


def parse_absolute_schedule(value: Any) -> dt.datetime | None:
    """Parse one ISO-8601 absolute instant at Studio's minute precision."""
    if value in (None, ""):
        return None
    if not isinstance(value, str) or value != value.strip():
        raise UploadContractError("schedule must be an ISO8601 string with UTC offset")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UploadContractError("schedule must be ISO8601 with UTC offset") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UploadContractError("schedule must include a UTC offset")
    if parsed.second or parsed.microsecond:
        raise UploadContractError("schedule must use minute precision")
    return parsed


def phase_requires_no_reupload(phase: str) -> bool:
    return PHASE_RANK.get(phase, -1) >= PHASE_RANK["upload_started"]


def video_id_from_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    match = re.fullmatch(
        r"https://youtu\.be/([A-Za-z0-9_-]{11})(?:[/?#].*)?", value.strip()
    )
    return match.group(1) if match else ""


def initial_phase_event() -> dict[str, Any]:
    return {
        "upload_phase_schema_version": PHASE_SCHEMA_VERSION,
        "phase": "preflight",
        "video_id": "",
    }


def validate_phase_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "upload_phase_schema_version", "phase", "video_id"
    }:
        raise UploadContractError("upload phase fields are invalid")
    version = value.get("upload_phase_schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != PHASE_SCHEMA_VERSION:
        raise UploadContractError("unsupported upload phase schema")
    phase = value.get("phase")
    video_id = value.get("video_id")
    if phase not in PHASES:
        raise UploadContractError("upload phase is invalid")
    if not isinstance(video_id, str) or (video_id and VIDEO_ID_RE.fullmatch(video_id) is None):
        raise UploadContractError("upload phase video_id is invalid")
    if phase == "video_id_known" and not video_id:
        raise UploadContractError("video_id_known phase has no video_id")
    if phase != "video_id_known" and video_id:
        raise UploadContractError("pre-id upload phase carries video_id")
    return dict(value)


def advance_phase_event(current: Any, phase: str, video_id: str = "") -> dict[str, Any]:
    before = validate_phase_event(current)
    candidate = validate_phase_event(
        {
            "upload_phase_schema_version": PHASE_SCHEMA_VERSION,
            "phase": phase,
            "video_id": str(video_id or ""),
        }
    )
    if PHASE_RANK[candidate["phase"]] < PHASE_RANK[before["phase"]]:
        raise UploadContractError("upload phase regressed")
    if before["phase"] == "video_id_known" and candidate != before:
        raise UploadContractError("known upload video_id changed")
    return candidate


def encode_phase_event(event: Any) -> str:
    value = validate_phase_event(event)
    return PHASE_MARKER + json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def decode_phase_marker(line: str) -> dict[str, Any]:
    if not isinstance(line, str) or not line.startswith(PHASE_MARKER):
        raise UploadContractError("upload phase marker is absent")
    try:
        value = json.loads(line[len(PHASE_MARKER):])
    except json.JSONDecodeError as exc:
        raise UploadContractError("upload phase JSON is invalid") from exc
    return validate_phase_event(value)


def encode_phase_ack(event: Any) -> str:
    """Encode the manager ACK for one durably persisted phase event."""
    value = validate_phase_event(event)
    return PHASE_ACK_MARKER + json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def decode_phase_ack(line: str) -> dict[str, Any]:
    if not isinstance(line, str) or not line.startswith(PHASE_ACK_MARKER):
        raise UploadContractError("upload phase ACK is absent")
    try:
        value = json.loads(line[len(PHASE_ACK_MARKER):])
    except json.JSONDecodeError as exc:
        raise UploadContractError("upload phase ACK JSON is invalid") from exc
    return validate_phase_event(value)


def _validate_evidence(value: Any, *, row: bool) -> dict[str, Any] | None:
    if value is None:
        return None
    expected_keys = ROW_EVIDENCE_KEYS if row else EXACT_EVIDENCE_KEYS
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise UploadContractError("Studio evidence fields are invalid")
    if not isinstance(value.get("verified_channel_uc"), str) or UC_RE.fullmatch(
        value["verified_channel_uc"]
    ) is None:
        raise UploadContractError("Studio evidence UC is invalid")
    if not isinstance(value.get("row_video_id"), str) or VIDEO_ID_RE.fullmatch(
        value["row_video_id"]
    ) is None:
        raise UploadContractError("Studio evidence video_id is invalid")
    if value.get("studio_state") not in RESULT_VISIBILITIES:
        raise UploadContractError("Studio evidence state is invalid")
    if not isinstance(value.get("visibility_user_set_visibility"), str):
        raise UploadContractError("Studio evidence visibility is invalid")
    epoch = value.get("scheduled_time_seconds")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise UploadContractError("Studio evidence schedule epoch is invalid")
    title_sha = value.get("normalized_title_sha256")
    if not isinstance(title_sha, str) or SHA256_RE.fullmatch(title_sha) is None:
        raise UploadContractError("Studio evidence title hash is invalid")
    if row:
        observed_sha = value.get("observed_normalized_title_sha256")
        if not isinstance(observed_sha, str) or SHA256_RE.fullmatch(observed_sha) is None:
            raise UploadContractError("Studio row observed title hash is invalid")
        if type(value.get("title_matches")) is not bool:
            raise UploadContractError("Studio row title_matches is invalid")
    else:
        if value["studio_state"] != "SCHEDULED":
            raise UploadContractError("exact Studio evidence state is not SCHEDULED")
        if value["visibility_user_set_visibility"] != "VIDEO_USER_SET_VISIBILITY_SCHEDULED":
            raise UploadContractError("exact Studio evidence visibility is invalid")
        if epoch <= 0:
            raise UploadContractError("exact Studio evidence schedule epoch is invalid")
    return dict(value)


def validate_result(candidate: Any) -> dict[str, Any]:
    """Validate exactly one schema-v2 terminal worker result."""
    if not isinstance(candidate, dict):
        raise UploadContractError("upload result must be an object")
    if set(candidate) - (RESULT_REQUIRED_KEYS | RESULT_OPTIONAL_KEYS):
        raise UploadContractError("upload result has unknown fields")
    if not RESULT_REQUIRED_KEYS.issubset(candidate):
        raise UploadContractError("upload result fields are incomplete")
    version = candidate.get("upload_result_schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != RESULT_SCHEMA_VERSION:
        raise UploadContractError("unsupported upload result schema")
    phase = candidate.get("phase")
    if phase not in PHASES:
        raise UploadContractError("upload result phase is invalid")
    if type(candidate.get("success")) is not bool:
        raise UploadContractError("upload result success must be boolean")
    for key in ("url", "error", "visibility", "video_id"):
        if not isinstance(candidate.get(key), str):
            raise UploadContractError(f"upload result {key} must be a string")
    if candidate["visibility"] not in RESULT_VISIBILITIES:
        raise UploadContractError("upload result visibility is invalid")
    if type(candidate.get("do_not_reupload")) is not bool:
        raise UploadContractError("upload result do_not_reupload must be boolean")
    for key in RESULT_OPTIONAL_BOOL_KEYS:
        if key in candidate and type(candidate[key]) is not bool:
            raise UploadContractError(f"upload result {key} must be boolean")
    if "reason" in candidate and not isinstance(candidate["reason"], str):
        raise UploadContractError("upload result reason must be a string")
    if (
        "altered_disclosed" in candidate
        and candidate["altered_disclosed"] is not None
        and type(candidate["altered_disclosed"]) is not bool
    ):
        raise UploadContractError("upload result altered_disclosed must be boolean or null")

    video_id = candidate["video_id"]
    if video_id and VIDEO_ID_RE.fullmatch(video_id) is None:
        raise UploadContractError("upload result video_id is invalid")
    expected_no_reupload = phase_requires_no_reupload(phase) or bool(video_id)
    if candidate["do_not_reupload"] != expected_no_reupload:
        raise UploadContractError("upload result retry boundary is inconsistent")
    if video_id and phase != "video_id_known":
        raise UploadContractError("known video_id lacks video_id_known phase")

    url = candidate["url"]
    if url == "DRAFT_UPLOADED":
        if not phase_requires_no_reupload(phase):
            raise UploadContractError("draft sentinel is before upload boundary")
    elif url:
        url_video_id = video_id_from_url(url)
        if not url_video_id or not video_id or url_video_id != video_id:
            raise UploadContractError("upload result URL/video_id binding is invalid")
    elif candidate["success"]:
        raise UploadContractError("successful upload result has no URL")

    row_evidence = _validate_evidence(candidate["studio_row_evidence"], row=True)
    exact_evidence = _validate_evidence(candidate["exact_studio_evidence"], row=False)
    for evidence in (row_evidence, exact_evidence):
        if evidence is not None and evidence["row_video_id"] != video_id:
            raise UploadContractError("Studio evidence/video_id binding is invalid")
    if row_evidence is not None and exact_evidence is not None:
        for key in EXACT_EVIDENCE_KEYS:
            if row_evidence[key] != exact_evidence[key]:
                raise UploadContractError("exact and row Studio evidence disagree")
    if candidate["success"] and candidate["visibility"] == "SCHEDULED":
        if row_evidence is None or exact_evidence is None:
            raise UploadContractError("scheduled success lacks exact Studio evidence")
    if candidate["success"] and candidate["error"]:
        raise UploadContractError("successful upload result carries an error")
    return dict(candidate)


def build_result(raw: Any, phase_event: Any) -> dict[str, Any]:
    """Build the worker's stable result from trusted in-process values."""
    source = dict(raw) if isinstance(raw, dict) else {}
    event = validate_phase_event(phase_event)
    url = str(source.get("url") or "")
    video_id = str(source.get("video_id") or "")
    if VIDEO_ID_RE.fullmatch(video_id) is None:
        match = re.search(
            r"(?:youtu\.be/|[?&]v=|/video/)([A-Za-z0-9_-]{11})(?:[/?&#]|$)",
            url,
        )
        video_id = match.group(1) if match else ""
    if not video_id and event["phase"] == "video_id_known":
        video_id = event["video_id"]
    if event["phase"] == "video_id_known" and video_id and video_id != event["video_id"]:
        # Номер, выданный мастером НАШЕЙ загрузке, надёжнее любого, вычисленного из ссылки
        # или найденного в списке канала по заголовку: заголовки соседних выпусков похожи,
        # и поиск легко указывает на чужой ролик. Раньше расхождение валило весь заливщик
        # с голым кодом 1 — заливка при этом уже прошла, терялось только подтверждение.
        _log = logging.getLogger(__name__)
        _log.warning("Номер из результата (%s) разошёлся с номером загрузки (%s) — "
                     "оставляю номер загрузки", video_id, event["video_id"])
        video_id = event["video_id"]
    if video_id:
        event = advance_phase_event(event, "video_id_known", video_id)
    result = {
        "upload_result_schema_version": RESULT_SCHEMA_VERSION,
        "phase": event["phase"],
        "success": bool(source.get("success")),
        "url": url,
        "error": str(source.get("error") or ""),
        "visibility": str(source.get("visibility") or "unknown"),
        "video_id": video_id,
        "do_not_reupload": phase_requires_no_reupload(event["phase"]) or bool(video_id),
        "studio_row_evidence": (
            source.get("studio_row_evidence")
            if isinstance(source.get("studio_row_evidence"), dict)
            else None
        ),
        "exact_studio_evidence": (
            source.get("exact_studio_evidence")
            if isinstance(source.get("exact_studio_evidence"), dict)
            else None
        ),
    }
    for key in RESULT_OPTIONAL_KEYS:
        if key in source:
            result[key] = source[key]
    return result


def failure_result(message: str, phase_event: Any, prior: Any = None) -> dict[str, Any]:
    """Preserve exact known identity while turning a path into safe failure."""
    source = dict(prior) if isinstance(prior, dict) else {}
    source.update({"success": False, "error": str(message)})
    source.setdefault("url", "")
    source.setdefault("visibility", "unknown")
    return build_result(source, phase_event)
