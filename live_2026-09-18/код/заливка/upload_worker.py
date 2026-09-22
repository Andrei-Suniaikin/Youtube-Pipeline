#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube Upload Worker — загрузка видео через AdsPower + Selenium.

Standalone-воркер для VideoStudio. Запускается как подпроцесс (см. CLI внизу) ИЛИ
вызывается напрямую через upload_to_youtube(...).

Маршрутизация на нужный канал — по URL канала (channel_url), а НЕ через меню
"Switch account". Уборщик навигирует прямо в Studio нужного канала и ПРОВЕРЯЕТ,
что мы реально попали на него (а не на accounts.google.com и не на чужой канал).

Поддерживает schedule_date для планирования публикации.
UI YouTube Studio следует языку Google-аккаунта профиля, поэтому определение
факта публикации устойчиво к 8 языкам (ru/de/fr/it/pl/pt/ro/cs).
"""
import os
import math
import re
import sys
import json
import time
import logging
import hashlib
import unicodedata
import requests
try:
    import adspower_rate_limit
except ImportError:
    import importlib.util as _rate_importlib
    _rate_spec = _rate_importlib.spec_from_file_location(
        "adspower_rate_limit", os.path.join(os.path.dirname(os.path.abspath(__file__)), "adspower_rate_limit.py")
    )
    if _rate_spec is None or _rate_spec.loader is None:
        raise
    adspower_rate_limit = _rate_importlib.module_from_spec(_rate_spec)
    _rate_spec.loader.exec_module(adspower_rate_limit)
from datetime import datetime, timezone
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
ADSPOWER_API = os.getenv("ADSPOWER_API", "http://localhost:50325")

try:
    import upload_contract
except ImportError:
    import importlib.util as _importlib_util

    _contract_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upload_contract.py")
    _contract_spec = _importlib_util.spec_from_file_location("upload_contract", _contract_path)
    if _contract_spec is None or _contract_spec.loader is None:
        raise
    upload_contract = _importlib_util.module_from_spec(_contract_spec)
    _contract_spec.loader.exec_module(upload_contract)

_UPLOAD_PHASE_EVENT = upload_contract.initial_phase_event()
_FINALIZE_ONLY_VIDEO_ID = ""
_UPLOAD_AUTH_CONTEXT = {}
_UPLOAD_AUTH_ATTEMPTED = False
_UPLOAD_PROGRESS_LAST_AT = float("-inf")
_UPLOAD_PROGRESS_FRONTEND_ID = None
_FRESH_UPLOAD_BINDING = None


def _reset_upload_phase(*, emit=False):
    global _UPLOAD_PHASE_EVENT, _FINALIZE_ONLY_VIDEO_ID, _UPLOAD_AUTH_CONTEXT, _UPLOAD_AUTH_ATTEMPTED
    global _UPLOAD_PROGRESS_LAST_AT, _UPLOAD_PROGRESS_FRONTEND_ID, _FRESH_UPLOAD_BINDING
    _FRESH_UPLOAD_BINDING = None
    _UPLOAD_PHASE_EVENT = upload_contract.initial_phase_event()
    _FINALIZE_ONLY_VIDEO_ID = ""
    _UPLOAD_AUTH_CONTEXT, _UPLOAD_AUTH_ATTEMPTED = {}, False
    _UPLOAD_PROGRESS_LAST_AT = float("-inf")
    _UPLOAD_PROGRESS_FRONTEND_ID = None
    if emit:
        print(upload_contract.encode_phase_event(_UPLOAD_PHASE_EVENT), flush=True)
    return dict(_UPLOAD_PHASE_EVENT)


def _advance_upload_phase(phase: str, video_id: str = ""):
    global _UPLOAD_PHASE_EVENT
    candidate = upload_contract.advance_phase_event(
        _UPLOAD_PHASE_EVENT, phase, video_id
    )
    if candidate != _UPLOAD_PHASE_EVENT:
        _UPLOAD_PHASE_EVENT = candidate
        print(upload_contract.encode_phase_event(candidate), flush=True)
    return dict(_UPLOAD_PHASE_EVENT)


def _current_upload_phase():
    return dict(_UPLOAD_PHASE_EVENT)


def _mark_known_video_id(video_id: str):
    """Record recovered/new Studio identity without allowing a phase regression.

    Номер, единожды выданный НАШЕЙ загрузке, здесь уже не меняется. Сюда приходят и
    находки из списка канала — а список мы ищем по заголовку, и у соседних выпусков он
    похож, так что находка вполне может указывать на чужой ролик. Раньше расхождение
    поднимало исключение и валило заливщик с голым кодом 1, хотя ролик был уже залит:
    терялось только подтверждение. Теперь чужой номер отбрасывается с предупреждением.
    """
    if upload_contract.VIDEO_ID_RE.fullmatch(str(video_id or "")) is None:
        return _current_upload_phase()
    if _FRESH_UPLOAD_BINDING is not None and (
            not _FRESH_UPLOAD_BINDING.get("frontend_id") or
            _FRESH_UPLOAD_BINDING.get("video_id") != str(video_id)):
        raise RuntimeError("strict_fresh_video_id_not_bound; identity adoption forbidden")
    current = _current_upload_phase()
    if current["phase"] == "video_id_known" and current["video_id"] != str(video_id):
        logger.warning(
            "Попытка сменить номер ролика %s → %s: оставляю номер, выданный нашей "
            "загрузке (совпадение заголовков не доказывает принадлежность строки)",
            current["video_id"], video_id)
        return current
    return _advance_upload_phase("video_id_known", str(video_id))


def _require_retained_finalize_endpoint(address, expected_port, *, strict, finalize_video_id):
    """Opt-in retained recovery must attach; it may never start a browser."""
    if not expected_port:
        return
    if (not strict or upload_contract.VIDEO_ID_RE.fullmatch(str(finalize_video_id or "")) is None
            or not str(expected_port).isdigit() or not 1 <= int(expected_port) <= 65535):
        raise RuntimeError("retained_finalize_context_invalid")
    parsed = urlparse("http://" + str(address or ""))
    if (parsed.hostname not in ("127.0.0.1", "localhost") or parsed.port != int(expected_port)
            or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise RuntimeError("retained_finalize_endpoint_changed; browser start forbidden")


def _mark_upload_started():
    current = _current_upload_phase()
    if not upload_contract.phase_requires_no_reupload(current["phase"]):
        return _advance_upload_phase("upload_started")
    return current



def _persist_fresh_identity(stage, binding, state):
    proof_path=os.getenv("VIDEO_UPLOAD_THUMBNAIL_PROOF_PATH", "")
    if not proof_path:
        raise RuntimeError("strict_fresh_identity_proof_path_required; file handoff forbidden")
    path=os.path.splitext(proof_path)[0]+".fresh_"+stage+".json"
    with open(path,"x",encoding="utf-8") as handle:
        json.dump({"schema_version":1,"event":"fresh_upload_identity_"+stage,
                   "observed_at":datetime.now(timezone.utc).isoformat(),
                   "binding":binding,"studio_state":state},handle,sort_keys=True,allow_nan=False)
        handle.flush();os.fsync(handle.fileno())
    fd=os.open(os.path.dirname(os.path.abspath(path)),os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def _read_wizard_recording_identity(driver, *, required=False):
    """Persist a late Studio ID before acting on the upload's thumbnail."""
    state = YS.read_wizard(driver) or {}
    if _FRESH_UPLOAD_BINDING is not None:
        raw = YS.read_fresh_upload_state(driver, _FRESH_UPLOAD_BINDING["channel_uc"])
        identity = YS.fresh_upload_identity(raw, _FRESH_UPLOAD_BINDING)
        state["videoId"] = identity["videoId"] if identity else ""
        if identity and not _FRESH_UPLOAD_BINDING["frontend_id"]:
            bound=dict(_FRESH_UPLOAD_BINDING,frontend_id=identity["frontendId"],video_id=identity["videoId"])
            _persist_fresh_identity("bound",bound,raw)  # fsync precedes durable video-ID ACK
            _FRESH_UPLOAD_BINDING.update(bound)
    video_id = state.get("videoId") or ""
    if video_id:
        if not state.get("present") or not upload_contract.VIDEO_ID_RE.fullmatch(video_id):
            raise RuntimeError("strict_wizard_video_id_invalid")
        known = _current_upload_phase().get("video_id") or ""
        if known and known != video_id:
            raise RuntimeError("strict_wizard_video_id_mismatch; publication forbidden")
        _mark_known_video_id(video_id)
        _observe_upload_progress(driver, video_id)
    elif required:
        raise RuntimeError("strict_fresh_video_id_unproven; metadata forbidden" if _FRESH_UPLOAD_BINDING is not None
                           else "strict_thumbnail_video_id_unknown; publication forbidden")
    return state


def _observe_upload_progress(driver, video_id):
    """Emit bounded, identity-bound observations without changing upload state."""
    global _UPLOAD_PROGRESS_LAST_AT, _UPLOAD_PROGRESS_FRONTEND_ID
    if os.getenv("VIDEO_UPLOAD_PROGRESS_OBSERVATIONS") != "1":
        return
    observed = time.monotonic()
    if observed - _UPLOAD_PROGRESS_LAST_AT < 2.0:
        return
    _UPLOAD_PROGRESS_LAST_AT = observed
    try:
        uc = _UPLOAD_AUTH_CONTEXT.get("channel_uc", "")
        read_started = datetime.now(timezone.utc)
        sample = YS.read_bound_upload_progress(driver, uc, video_id)
        read_finished = datetime.now(timezone.utc)
        if (not isinstance(sample, dict)
                or set(sample) != {"channel_uc", "video_id", "frontend_id", "fraction", "observed_at"}
                or sample["channel_uc"] != uc or sample["video_id"] != video_id
                or not isinstance(sample["frontend_id"], str) or not 0 < len(sample["frontend_id"]) <= 256
                or type(sample["fraction"]) not in (int, float) or not 0 <= sample["fraction"] <= 1):
            return
        sampled = datetime.fromisoformat(sample["observed_at"].replace("Z", "+00:00"))
        if (read_finished < read_started or sampled.tzinfo is None or sampled.timestamp() < read_started.timestamp() - 1
                or sampled.timestamp() > read_finished.timestamp() + 1):
            return
        if _UPLOAD_PROGRESS_FRONTEND_ID is None:
            _UPLOAD_PROGRESS_FRONTEND_ID = sample["frontend_id"]
        elif _UPLOAD_PROGRESS_FRONTEND_ID != sample["frontend_id"]:
            return
        event = dict(sample, schema_version=1, read_started_at=read_started.isoformat(),
                     read_finished_at=read_finished.isoformat(), monotonic_seconds=observed)
        print("__UPLOAD_PROGRESS__|" + json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)
    except Exception:
        # Instrumentation cannot change the durable phase or retry a file.
        logger.debug("Bound upload progress observation unavailable")


def _wait_wizard_ready_recording_identity(driver, timeout=90):
    """Use wait_wizard_ready's state predicates, recording IDs even while LOADING.

    Kept in the opt-in worker: the shared Studio reader and legacy waits retain
    their contract. Identity/read failures must not be swallowed by wait_until.
    """
    deadline = time.monotonic() + timeout
    while True:
        if os.getenv("VIDEO_UPLOAD_STRICT") == "1":
            _require_auth_clear(driver, stage="wizard_before_metadata")
        state = _read_wizard_recording_identity(driver)
        if ((_FRESH_UPLOAD_BINDING is None or state.get("videoId"))
                and state.get("present") and state.get("errorSectionStatus") != "LOADING"
                and not (state.get("isVideoUploaded") is None
                         and not state.get("workflowStep"))):
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(2.0, remaining))


def _wait_mp4_uploaded(driver, timeout=1800, *, stage="before_thumbnail", check_auth=True):
    """A ready DETAILS form is not proof that the MP4 transfer has completed."""
    start = time.monotonic()
    deadline, last_log = start + timeout, float("-inf")
    while True:
        if check_auth:
            _require_auth_clear(driver, stage=stage)
        else:
            # A long-video phone gate cannot be drained by waiting for transfer.
            # Ignore account Verify during failure drain, but retain on eligibility.
            auth_gate_present(driver, raise_on_error=True, check_upload_eligibility=True)
        state = _read_wizard_recording_identity(driver)
        video_id = state.get("videoId") or ""
        if (state.get("present") and state.get("isVideoUploaded") is True
                and upload_contract.VIDEO_ID_RE.fullmatch(video_id)
                and _current_upload_phase().get("video_id") == video_id):
            logger.info("MP4 полностью передан по состоянию Studio: id=%s (%s)", video_id, stage)
            return state
        now = time.monotonic()
        if now - last_log >= 30:
            progress = YS.read_upload_progress(driver) or {}
            logger.info("Ожидаю завершения MP4 (%s): %dс, progress=%s%%, uploaded=%s, id=%s",
                        stage, int(now - start), progress.get("pct", "?"),
                        state.get("isVideoUploaded"), _current_upload_phase().get("video_id") or "ещё нет")
            last_log = now
        if now >= deadline:
            return None
        time.sleep(min(3.0, deadline - now))


def _strict_transfer_may_close(driver, *, completion_proven=False, timeout=120):
    """Do not terminate an unproven MP4 transfer just because a later step failed.

    A bounded read-only drain may establish completion. Otherwise leave Chrome
    running: the manager detects the open profile and retains its shared lease.
    """
    if completion_proven:
        return True
    try:
        if _wait_mp4_uploaded(driver, timeout=timeout, stage="failure_drain", check_auth=False):
            return True
    except Exception as exc:
        logger.error("Завершение MP4 перед закрытием не доказано: %s", exc)
    logger.error("UPLOAD_IN_PROGRESS_PROFILE_RETAINED: завершение MP4 не доказано; "
                 "браузер оставлен открытым, свежую загрузку не повторять")
    try:
        driver.service.stop()  # Disconnect Chromedriver without closing the AdsPower browser.
    except Exception:
        pass
    return False


def _arm_upload_boundary(ack_stream=None):
    """Persist ``upload_started`` in the manager before Studio sees the file.

    Receipt-enabled manager runs set ``VIDEO_UPLOAD_PHASE_ACK=1`` and provide a
    stdin pipe.  The manager ACK is written only after its phase callback has
    fsynced the attempt log and journal.  Missing/mismatched ACK fails before
    ``send_keys`` and remains conservatively no-reupload.
    """
    event = _mark_upload_started()
    if os.getenv("VIDEO_UPLOAD_PHASE_ACK") != "1":
        return event
    stream = sys.stdin if ack_stream is None else ack_stream
    line = stream.readline()
    if not line:
        raise RuntimeError("durable upload boundary ACK missing before file handoff")
    acknowledged = upload_contract.decode_phase_ack(line.rstrip("\r\n"))
    if acknowledged != event or acknowledged.get("phase") != "upload_started":
        raise RuntimeError("durable upload boundary ACK identity mismatch")
    return event


def _send_video_file_at_upload_boundary(file_input, video_path: str, ack_stream=None, *, driver=None):
    """Arm and ACK the no-reupload boundary, then hand the file to Studio."""
    if _FINALIZE_ONLY_VIDEO_ID:
        raise RuntimeError("finalize_video_id forbids every video file handoff")
    if os.getenv("VIDEO_UPLOAD_STRICT") == "1":
        _require_auth_clear(driver, stage="before_file_handoff")
    event = _arm_upload_boundary(ack_stream=ack_stream)
    file_input.send_keys(os.path.abspath(video_path))
    return event


def _normalize_exact_title(value) -> str:
    """Canonical title used only for exact final-row equality and its proof hash."""
    normalized = unicodedata.normalize("NFC", str(value or ""))
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def _exact_title_proof(requested, observed) -> dict:
    requested_normalized = _normalize_exact_title(requested)
    observed_normalized = _normalize_exact_title(observed)
    return {
        "matches": bool(requested_normalized) and requested_normalized == observed_normalized,
        "normalized_title_sha256": hashlib.sha256(
            requested_normalized.encode("utf-8")
        ).hexdigest(),
        "observed_normalized_sha256": hashlib.sha256(
            observed_normalized.encode("utf-8")
        ).hexdigest(),
    }


def _strict_thumbnail_preflight(thumbnail_path: str, *, required: bool = True):
    """Validate the explicitly selected cover before any AdsPower request.

    Enabled only by VIDEO_UPLOAD_STRICT=1; reading the file proves local
    availability, not that Studio has accepted or saved the custom thumbnail.
    """
    if not thumbnail_path:
        if required:
            raise ValueError("strict_thumbnail_missing: an explicit thumbnail is required")
        return
    if not os.path.isfile(thumbnail_path):
        raise ValueError("strict_thumbnail_unreadable: thumbnail is not a regular file")
    try:
        with open(thumbnail_path, "rb") as handle:
            if not handle.read(1):
                raise ValueError("strict_thumbnail_empty: thumbnail file is empty")
    except OSError as exc:
        raise ValueError("strict_thumbnail_unreadable: cannot read thumbnail file") from exc


def _thumbnail_upload_inputs(driver, by, *, strict=False):
    # Live 2026-09-14: evidence/canary_thumbnail_input_scope.json shows one
    # background-editor input before the wizard input. Never send to that first
    # page-level match when finalizing an existing draft.
    selector = 'ytcp-uploads-dialog input[type="file"]' if strict else 'input[type="file"]'
    inputs = driver.find_elements(by.CSS_SELECTOR, selector)
    if not strict:
        return inputs
    images = [item for item in inputs if 'image' in (item.get_attribute('accept') or '')]
    if len(images) != 1:
        raise RuntimeError(
            f"strict_thumbnail_input_ambiguous: expected one wizard image input, found {len(images)}"
        )
    return images


def _strict_existing_blocker(row: dict, requested_title: str,
                             studio_state: str, expected_state: str) -> str:
    """A fuzzy match is a reason to stop, never permission to upload again."""
    if not isinstance(row, dict) or not _exact_title_proof(
        requested_title, row.get("title")
    )["matches"]:
        return "strict_existing_title_mismatch: candidate title is not exact; fresh upload forbidden"
    if upload_contract.VIDEO_ID_RE.fullmatch(str(row.get("videoId") or "")) is None:
        return "strict_existing_id_invalid: candidate video ID is invalid; fresh upload forbidden"
    if studio_state != expected_state:
        return (
            f"strict_existing_visibility_mismatch: found {studio_state}, "
            f"expected {expected_state}; fresh upload forbidden"
        )
    return ""


def _strict_existing_failure(message: str, row=None) -> dict:
    """Keep a discovered candidate ID durable even when its identity is disputed."""
    video_id = str((row or {}).get("videoId") or "")
    if upload_contract.VIDEO_ID_RE.fullmatch(video_id):
        _mark_known_video_id(video_id)
    else:
        # An existing/interrupted upload without a readable ID is still unsafe
        # to repeat. Persist that uncertainty in the same durable protocol.
        _mark_upload_started()
    return upload_contract.failure_result(
        message, _current_upload_phase(), {"skipped": True, "visibility": "UNKNOWN"}
    )


def _strict_finalize_row(rows, video_id: str, requested_title: str) -> dict:
    matches = [row for row in (rows or [])
               if isinstance(row, dict) and row.get("videoId") == video_id]
    if len(matches) != 1:
        raise ValueError("strict_finalize_not_found: expected exactly one known video ID; no file upload")
    row = matches[0]
    blocker = _strict_existing_blocker(row, requested_title, YS.classify(row), "DRAFT")
    if blocker:
        raise ValueError(blocker)
    return row


# Live proof: research/ww2_night_20260914/thumbnail_evidence/, profile 45,
# KMOv7Np2zY0, 2026-09-14. startTransfer sets SUCCESS/fileName/server resource
# only after its upload Promise resolves. Reloaded custom thumbnails are
# NOT_STARTED: their mere presence must not pass a fresh file-handoff gate.
_STRICT_THUMBNAIL_STATE_JS = r"""
var wizard = document.querySelector('ytcp-uploads-dialog');
if (!wizard) return {present:false};
var editor = wizard.querySelector('ytcp-video-thumbnail-editor');
var custom = editor && editor.querySelector('ytcp-video-custom-still-editor');
var uploader = custom && custom.querySelector('ytcp-thumbnail-uploader');
var form = editor && editor.thumbnailsEditorForm;
if (!editor || !custom || !uploader || !form) return {present:false};
var value = (form.customStillFormField || {}).value || {};
var preview = value.previewDataUri || '';
var images = Array.from(uploader.querySelectorAll('img'));
return {
  present:true,
  wizard_video_id:wizard.videoId || '',
  video_id:(editor.video || {}).videoId || '',
  channel_uc:(editor.video || {}).channelId || '',
  thumbnail_type:(form.thumbnailType || {}).value || '',
  thumbnail_type_valid:(form.thumbnailType || {}).isValid === true,
  custom_valid:(form.customStillFormField || {}).isValid === true,
  upload_state:value.state || '',
  filename:value.fileName || '',
  has_server_resource:!!(value.encryptedScottyResourceId || value.encryptedBlobId),
  custom_selected:custom.selected === true,
  uploader_selected:uploader.selected === true,
  uploader_state:uploader.state || '',
  upload_error:editor.uploadErrorMessage || custom.errorMessage || custom.uploadErrorMessage || '',
  preview_ready:!!preview && images.some(function(img){
    return img.src === preview && img.complete && img.naturalWidth > 0 && img.naturalHeight > 0;
  })
};
"""


def _strict_thumbnail_ready(state, *, video_id, channel_uc, filename):
    if not isinstance(state, dict) or state.get("present") is not True:
        return False
    for key, expected in (("wizard_video_id", video_id), ("video_id", video_id),
                          ("channel_uc", channel_uc)):
        observed = state.get(key)
        if observed and observed != expected:
            raise RuntimeError(f"strict_thumbnail_identity_mismatch: {key}")
        if not observed:
            return False
    if state.get("upload_error"):
        raise RuntimeError(f"strict_thumbnail_upload_error: {state['upload_error']}")
    if state.get("upload_state") != "SUCCESS":
        return False
    if state.get("filename") != filename:
        raise RuntimeError("strict_thumbnail_filename_mismatch")
    return (
        state.get("thumbnail_type") == "CUSTOM_STILL"
        and state.get("uploader_state") == "preview"
        and all(state.get(key) is True for key in (
            "thumbnail_type_valid", "custom_valid", "has_server_resource",
            "custom_selected", "uploader_selected", "preview_ready",
        ))
    )


def _thumbnail_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_thumbnail_confirmation(driver, thumbnail_path: str, *, channel_uc: str,
                                    video_id: str, expected_sha256: str, timeout=120):
    if not upload_contract.VIDEO_ID_RE.fullmatch(video_id or ""):
        raise RuntimeError("strict_thumbnail_video_id_unknown; publication forbidden")
    if not upload_contract.UC_RE.fullmatch(channel_uc or ""):
        raise RuntimeError("strict_thumbnail_channel_unknown; publication forbidden")
    deadline = time.monotonic() + timeout
    state = {}
    while True:
        state = driver.execute_script(_STRICT_THUMBNAIL_STATE_JS)
        if _strict_thumbnail_ready(state, video_id=video_id, channel_uc=channel_uc,
                                   filename=os.path.basename(thumbnail_path)):
            actual_sha256 = _thumbnail_sha256(thumbnail_path)
            if not expected_sha256 or actual_sha256 != expected_sha256:
                raise RuntimeError("strict_thumbnail_local_file_changed")
            proof = {
                "schema_version": 1, "event": "thumbnail_handoff_verified",
                "video_id": video_id, "channel_uc": channel_uc,
                "filename": os.path.basename(thumbnail_path),
                "thumbnail_sha256": actual_sha256,
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "studio_state": state,
            }
            proof_path = os.getenv("VIDEO_UPLOAD_THUMBNAIL_PROOF_PATH", "")
            if proof_path:
                with open(proof_path, "x", encoding="utf-8") as handle:
                    json.dump(proof, handle, ensure_ascii=False, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                directory_fd = os.open(os.path.dirname(os.path.abspath(proof_path)), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            logger.info("Обложка подтверждена Studio: video_id=%s sha256=%s", video_id, actual_sha256)
            return proof
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "strict_thumbnail_unverified: "
                f"state={(state or {}).get('upload_state', 'unreadable')}; publication forbidden"
            )
        time.sleep(1)


def _verified_existing_scheduled_result(
    verified_channel_uc: str,
    row: dict,
    requested_title: str,
    expected_epoch,
    *,
    studio_state: str,
    schedule_mode: str,
    picker_local: datetime,
    browser_timezone: str,
) -> dict:
    """Turn one exact existing Studio row into a retry-safe result/receipt proof."""
    video_id = str((row or {}).get("videoId") or "")
    proof = _exact_title_proof(requested_title, (row or {}).get("title"))
    try:
        observed_epoch = int((row or {}).get("scheduledTime") or 0)
    except (TypeError, ValueError, OverflowError):
        observed_epoch = 0
    altered_disclosed = (row or {}).get("alteredDisclosed") is True
    picker_text = (
        picker_local.strftime("%Y-%m-%d %H:%M")
        if isinstance(picker_local, datetime)
        else ""
    )
    browser_timezone = str(browser_timezone or "").strip()
    row_evidence = {
        "verified_channel_uc": verified_channel_uc,
        "row_video_id": video_id,
        "studio_state": str(studio_state or "UNKNOWN"),
        "visibility_user_set_visibility": str(
            (row or {}).get("userSetVisibility") or ""
        ),
        "scheduled_time_seconds": observed_epoch,
        "normalized_title_sha256": proof["normalized_title_sha256"],
        "observed_normalized_title_sha256": proof["observed_normalized_sha256"],
        "title_matches": bool(proof["matches"]),
    }
    base = {
        "url": f"https://youtu.be/{video_id}" if video_id else "",
        "visibility": str(studio_state or "UNKNOWN"),
        "video_id": video_id,
        "do_not_reupload": bool(video_id),
        "skipped": True,
        "altered_disclosed": altered_disclosed,
        "studio_row_evidence": row_evidence,
        "exact_studio_evidence": None,
    }
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or "") is None:
        return {
            **base,
            "success": False,
            "error": "scheduled_existing_video_id_invalid: do not reupload until inspected",
        }
    logger.info(
        "Состояние строки: id=%s → %s (draft=%s privacy=%s userSet=%s)",
        video_id,
        studio_state or "UNKNOWN",
        (row or {}).get("draftStatus"),
        (row or {}).get("privacy"),
        (row or {}).get("userSetVisibility"),
    )
    if studio_state != "SCHEDULED":
        return {
            **base,
            "success": False,
            "error": (
                f"scheduled_existing_state_unverified: actual={studio_state or 'UNKNOWN'}; "
                f"known video_id={video_id}; do not reupload"
            ),
        }
    if not proof["matches"]:
        return {
            **base,
            "success": False,
            "visibility": "TITLE_MISMATCH",
            "error": (
                "title_mismatch: Studio row title differs after NFC/whitespace/casefold; "
                f"known video_id={video_id}; do not reupload"
            ),
        }
    logger.info(
        "Заголовок строки подтверждён точно: id=%s normalized_sha256=%s",
        video_id, proof["normalized_title_sha256"],
    )
    if (row or {}).get("userSetVisibility") != "VIDEO_USER_SET_VISIBILITY_SCHEDULED":
        return {
            **base,
            "success": False,
            "visibility": "SCHEDULE_UNVERIFIED",
            "error": (
                "scheduled_existing_visibility_unverified: known video_id="
                f"{video_id}; do not reupload"
            ),
        }
    if not _scheduled_epoch_matches(observed_epoch, expected_epoch):
        return {
            **base,
            "success": False,
            "visibility": "SCHEDULED_WRONG_TIME",
            "error": (
                f"scheduled_existing_wrong_time: epoch {observed_epoch} instead of "
                f"{int(expected_epoch)}; known video_id={video_id}; do not reupload"
            ),
        }
    if schedule_mode != "absolute" or not picker_text or not browser_timezone:
        return {
            **base,
            "success": False,
            "visibility": "SCHEDULE_UNVERIFIED",
            "error": (
                "scheduled_existing_absolute_context_unverified: "
                f"known video_id={video_id}; do not reupload"
            ),
        }
    logger.info(
        "Отложка подтверждена машинно: scheduledTime=%s, ожидали %s "
        "(режим=absolute, picker=%s, browser_tz=%s)",
        observed_epoch,
        int(expected_epoch),
        picker_text,
        browser_timezone,
    )
    if not altered_disclosed:
        logger.error(
            "AI-дисклоуз НЕ проставлен: строка ролика отдаёт alteredDisclosed=false"
        )
        return {
            **base,
            "success": False,
            "error": (
                "scheduled_existing_altered_content_unverified: "
                f"known video_id={video_id}; do not reupload"
            ),
        }
    logger.info("AI-дисклоуз подтверждён состоянием строки")
    exact_evidence = {
        "verified_channel_uc": verified_channel_uc,
        "row_video_id": video_id,
        "studio_state": "SCHEDULED",
        "visibility_user_set_visibility": "VIDEO_USER_SET_VISIBILITY_SCHEDULED",
        "scheduled_time_seconds": observed_epoch,
        "normalized_title_sha256": proof["normalized_title_sha256"],
    }
    return {
        **base,
        "success": True,
        "error": "",
        "exact_studio_evidence": exact_evidence,
    }


def _is_aware_datetime(value) -> bool:
    """True, если datetime задаёт абсолютный момент, а не время пикера."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    try:
        return value.utcoffset() is not None
    except Exception:
        return False


def _browser_timezone(driver) -> str:
    """Прочитать IANA-таймзону именно AdsPower-браузера, не хост-машины."""
    try:
        value = driver.execute_script(
            "return (Intl.DateTimeFormat().resolvedOptions().timeZone || '').trim();"
        )
    except Exception:
        return ""
    return str(value or "").strip()


def _schedule_for_browser_picker(schedule_date: datetime, browser_timezone: str):
    """Вернуть ``(picker_local, exact_epoch, mode)`` для расписания.

    Aware datetime — абсолютный момент. Его переводим в локальные дату/время
    браузерного профиля; ``exact_epoch`` затем позволяет проверить ровно тот же
    Unix timestamp в ``scheduledPublishingDetails``. Naive datetime запрещён:
    без offset невозможно доказать абсолютный момент публикации.
    """
    if not _is_aware_datetime(schedule_date):
        raise ValueError("schedule must include a UTC offset")
    name = str(browser_timezone or "").strip()
    if not name:
        raise ValueError("AdsPower browser timezone is empty")
    try:
        from zoneinfo import ZoneInfo
        browser_zone = ZoneInfo(name)
    except Exception as exc:
        raise ValueError(f"invalid AdsPower browser timezone: {name!r}") from exc
    if schedule_date.second or schedule_date.microsecond:
        raise ValueError(
            "aware schedule must use minute precision because Studio picker has no seconds"
        )
    exact_epoch = int(schedule_date.timestamp())
    zoned = schedule_date.astimezone(browser_zone)
    picker_local = zoned.replace(tzinfo=None)
    # Во время осеннего отката один wall-clock час встречается дважды. Пикер не
    # позволяет выбрать fold=0/1, поэтому до финализации нельзя честно обещать
    # точный absolute instant — безопаснее остановиться до загрузки.
    first = picker_local.replace(tzinfo=browser_zone, fold=0).utcoffset()
    second = picker_local.replace(tzinfo=browser_zone, fold=1).utcoffset()
    if first != second:
        raise ValueError(
            f"ambiguous browser-local schedule during DST fold: {picker_local} {name}"
        )
    return picker_local, exact_epoch, "absolute"


def _scheduled_epoch_matches(actual_epoch, expected_epoch) -> bool:
    """Сверить итог Studio строго по тому же Unix timestamp."""
    try:
        actual = int(actual_epoch)
        expected = int(expected_epoch)
    except (TypeError, ValueError, OverflowError):
        return False
    if actual <= 0:
        return False
    return actual == expected

# state-driven слой: читает РЕАЛЬНОЕ состояние компонентов Studio (workflowStep,
# draftStatus, privacy, availabilityRadioSelection) вместо угадывания по тексту
# страницы. Селекторы там подтверждены живым дампом DOM — см. yt_studio.SEL.
# Если модуль недоступен, воркер работает по старому текстовому пути (деградация
# заметна в логе, а не молча).
try:
    import yt_studio as YS
except ImportError:
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import yt_studio as YS
    except ImportError:
        YS = None
        logger.warning("yt_studio не найден — работаю по СТАРОМУ текстовому пути "
                       "(менее надёжно: зависит от языка интерфейса)")


_UID_CACHE = {}


def _resolve_user_id(profile_id):
    """serial_number → user_id. Часть эндпоинтов AdsPower (browser/active и
    периодически browser/start) с serial_number отвечает AggregateError, а с
    user_id работает стабильно — поэтому номер профиля резолвим один раз."""
    pid = str(profile_id).strip()
    if not pid.isdigit():
        return pid
    if pid in _UID_CACHE:
        return _UID_CACHE[pid]
    # AdsPower под нагрузкой отвечает AggregateError на любой эндпоинт — лечится
    # паузой, поэтому резолв делаем с ретраями (результат кэшируется на процесс).
    for att in range(1, 4):
        try:
            for page in (1, 2, 3):
                r = adspower_rate_limit.get(f"{ADSPOWER_API}/api/v1/user/list",
                                 params={"page": page, "page_size": 100}, timeout=30).json()
                if r.get("code") != 0:
                    raise RuntimeError(str(r.get("msg"))[:40])
                lst = (r.get("data") or {}).get("list", [])
                for x in lst:
                    if str(x.get("serial_number")) == pid:
                        _UID_CACHE[pid] = x.get("user_id")
                        logger.info("Профиль %s → user_id %s", pid, _UID_CACHE[pid])
                        return _UID_CACHE[pid]
                if len(lst) < 100:
                    break
            return None       # список получен, но профиля в нём нет
        except Exception as e:
            logger.debug("_resolve_user_id (попытка %d): %s", att, e)
            time.sleep(4 * att)
    return None


def _ads_param(profile_id) -> dict:
    """Параметры AdsPower API. Цифровой номер резолвим в user_id (стабильнее),
    при неудаче остаёмся на serial_number — прежнее поведение."""
    pid = str(profile_id).strip()
    if not pid.isdigit():
        return {"user_id": pid}
    uid = _resolve_user_id(pid)
    return {"user_id": uid} if uid else {"serial_number": pid}


# ---------------------------------------------------------------------------
# Локализованные слова для финальной кнопки (#done-button).
# Используются ТОЛЬКО как вторичный сигнал — основной сигнал это id/состояние DOM.
# Языки: en, ru, de, fr, it, pl, pt, ro, cs (8 целевых + английский).
# ---------------------------------------------------------------------------
_PUBLISH_WORDS = [
    # en
    "publish",
    # ru
    "опубликовать", "публиковать",
    # de
    "veröffentlichen",
    # fr
    "publier",
    # it
    "pubblica", "pubblicare",
    # pl
    "opublikuj", "publikuj",
    # pt
    "publicar",
    # ro
    "publică", "publica",
    # cs
    "publikovat", "zveřejnit",
]

_SCHEDULE_WORDS = [
    # en
    "schedule",
    # ru
    "запланировать", "запланируй",
    # de
    "planen", "zeitplan",
    # fr
    "programmer", "planifier",
    # it
    "pianifica", "programma",
    # pl
    "zaplanuj",
    # pt
    "agendar", "programar",
    # ro
    "programează", "programare",
    # cs
    "naplánovat", "naplanovat",
]

_SAVE_WORDS = [
    # en
    "save",
    # ru
    "сохранить",
    # de
    "speichern",
    # fr
    "enregistrer", "sauvegarder",
    # it
    "salva", "salvare",
    # pl
    "zapisz",
    # pt
    "salvar", "guardar",
    # ro
    "salvează", "salvare",
    # cs
    "uložit",
]


def _classify_done_button(driver) -> str:
    """Классифицирует финальную кнопку #done-button -> 'publish'|'schedule'|'save'|'unknown'.

    Стратегия (от устойчивого к хрупкому):
      1. СОСТОЯНИЕ мастера: ytcp-video-visibility-select.availabilityRadioSelection и
         имена радио с aria-checked=true (дамп 2026-08-03) — то намерение, которое
         Studio реально приняла.
      2. publishingType / атрибуты компонента (язык-независимо).
      3. Текст кнопки против мультиязычного списка слов — только фолбэк.

    Подпись кнопки первичным признаком быть не может: она локализована, а списков
    слов нет для es/nl/tr, и от этого вердикта зависит решение «жать или не жать».
    """
    # 1) Намерение — из состояния компонента видимости. availability проверяем ПЕРВЫМ:
    #    у запланированного ролика радио остаётся на PRIVATE, признак отложки — только
    #    availabilityRadioSelection == SCHEDULE.
    if YS is not None:
        try:
            _w = YS.read_wizard(driver)
            if _w.get("present"):
                if _w.get("availability") == "SCHEDULE":
                    return "schedule"
                _checked = [str(c).upper() for c in (_w.get("visibilityChecked") or [])]
                if "PUBLIC" in _checked:
                    return "publish"
                if "PRIVATE" in _checked or "UNLISTED" in _checked:
                    # для приватного/по ссылке штатная финальная кнопка — «Save»
                    return "save"
        except Exception as _ce:
            logger.debug("Классификация кнопки: состояние не прочитано (%s) — текстовый путь", _ce)

    info = driver.execute_script("""
        var b = document.querySelector('#done-button');
        if (!b) return null;
        // Текст кнопки (видимый)
        var txt = (b.textContent || '').trim();
        // Возможные язык-независимые подсказки на самой кнопке / контейнере
        var attrs = [];
        try {
            var host = b.closest('ytcp-button') || b;
            for (var a of ['publishing-type','data-publishing-type','aria-label','label']) {
                var v = host.getAttribute && host.getAttribute(a);
                if (v) attrs.push(a + '=' + v);
            }
        } catch(e) {}
        // Видна ли вообще панель Schedule (date picker) — косвенный сигнал расписания
        var hasSchedulePicker = !!document.querySelector('ytcp-datetime-picker');
        return {text: txt, attrs: attrs.join('|').toLowerCase(), hasSchedulePicker: hasSchedulePicker};
    """)
    if not info:
        return "unknown"

    attrs = info.get("attrs") or ""
    text = (info.get("text") or "").strip().lower()

    # 1) Язык-независимо: publishing-type в атрибутах компонента (YouTube кладёт сюда
    #    PUBLISH / SCHEDULE / PRIVATE и т.п.).
    if "schedule" in attrs:
        return "schedule"
    if "publish" in attrs or "public" in attrs:
        return "publish"

    # 2) Текст кнопки против мультиязычных списков.
    if any(w in text for w in _SCHEDULE_WORDS):
        return "schedule"
    if any(w in text for w in _PUBLISH_WORDS):
        return "publish"
    if any(w in text for w in _SAVE_WORDS):
        return "save"

    # 3) Косвенно: открыт date picker — почти наверняка Schedule.
    if info.get("hasSchedulePicker"):
        return "schedule"

    return "unknown"


def _intent_confirmed(driver, schedule_date, visibility):
    """Подтверждает ли СОСТОЯНИЕ то намерение, с которым мы собираемся жать #done-button.

    True — подтверждено, False — состояние прочитано и намерение НЕ применено,
    None — состояния нет (мастер не на экране / модуль yt_studio не загружен).
    None не равно True: решает вызывающий, и молча за успех это не сходит.
    Признаки машинные (дамп 2026-08-03): ytcp-video-visibility-select.availabilityRadioSelection
    и имена видимых радио с aria-checked=true.
    """
    if YS is None:
        return None
    try:
        w = YS.read_wizard(driver)
    except Exception as e:
        logger.warning("Намерение: состояние мастера не прочитано (%s)", e)
        return None
    if not w.get("present"):
        return None
    checked = [str(c).upper() for c in (w.get("visibilityChecked") or [])]
    want = "SCHEDULE" if schedule_date else (visibility or "PUBLIC").upper()
    ok = (w.get("availability") == "SCHEDULE") if schedule_date else (want in checked)
    logger.info("Намерение %s: availability=%r aria-checked=%s → %s", want,
                w.get("availability"), checked, "подтверждено" if ok else "НЕ подтверждено")
    return bool(ok)


def _rearm_intent(driver, schedule_date, visibility, attempts=2):
    """Переармировать отложку/видимость и вернуть, подтвердило ли это состояние.

    False = жать #done-button нельзя: намерение не применено, клик даст черновик
    (или эфир не в тот день). Старый путь (_set_schedule/_set_public) используется
    только когда состояния нет — сам по себе его возврат вердиктом не считается.
    """
    for i in range(1, attempts + 1):
        try:
            if schedule_date:
                ok = YS.set_schedule(driver, schedule_date) if YS is not None \
                    else _set_schedule(driver, schedule_date)
            else:
                ok = YS.set_visibility(driver, visibility) if YS is not None \
                    else _set_public(driver, visibility)
        except Exception as e:
            logger.warning("Переармирование (%d/%d) упало: %s", i, attempts, e)
            ok = False
        conf = _intent_confirmed(driver, schedule_date, visibility)
        if conf:
            return True
        if conf is None and ok:
            logger.warning("Переармирование: состояние недоступно, верю подтверждению самого "
                           "действия (aria-checked радио) — гейт по состоянию не выполнен")
            return True
        logger.warning("Переармирование (%d/%d) состоянием не подтверждено", i, attempts)
        time.sleep(2)
    return False


class StrictAuthGateError(RuntimeError):
    """A strict pre-upload auth check must reach the outer failure handler."""


class StrictUploadEligibilityError(StrictAuthGateError):
    """Channel feature eligibility, distinct from Google account identity Verify."""


# Live 2026-09-14: trust host has zero height; its open paper dialog is 512x313.
# research/ww2_night_20260914/evidence/long_video_eligibility_descendants_0bc6c88b0faa4f51800596428d833446.json
_LONG_VIDEO_PHONE_GATE_JS = r"""(function(){
    function deep(root,out){
        for(const el of root.querySelectorAll('*')){
            out.push(el);if(el.shadowRoot)deep(el.shadowRoot,out);
        }return out;
    }
    const phrase='To upload videos longer than 15 minutes, verify your phone number. Refresh the page after verifying.';
    const hosts=deep(document,[]).filter(e=>e.tagName==='YT-TRUST-TIERS-WIZARD-DIALOG');
    for(const host of hosts){
        const hs=getComputedStyle(host);
        if(host.hidden||hs.display==='none'||hs.visibility==='hidden')continue;
        const children=deep(host,[]).concat(host.shadowRoot?deep(host.shadowRoot,[]):[]);
        for(const dialog of children){
            if(dialog.tagName!=='TP-YT-PAPER-DIALOG'||dialog.opened!==true)continue;
            const r=dialog.getBoundingClientRect(),s=getComputedStyle(dialog);
            if(dialog.hidden||r.width<=0||r.height<=0||s.display==='none'||s.visibility==='hidden')continue;
            const text=(dialog.innerText||'').replace(/\s+/g,' ').trim();
            if(text.includes(phrase))return true;
        }
    }return false;
})()"""


def auth_gate_present(driver, *, raise_on_error=False, check_upload_eligibility=False):
    """Google требует подтверждение личности («Verify it's you») поверх мастера.

    Это НЕ транзиентный сбой. Модалка перехватывает кнопку публикации: файл
    заливается, мастер доходит до Visibility, а Schedule/Publish уходит в никуда.
    Никакие переклики её не пробьют — нужен человек с телефоном аккаунта.

    Отличать обязательно, иначе воркер (а за ним очередь) ретраит то, что ретраем
    не лечится: в прогоне 11.08 на профиле 46 он так пять раз «переармировал
    visibility» и потерял 12 минут, а в логе осталось только недоумение.

    Признак — контейнер `ytcp-auth-confirmation-dialog` с раскрытым
    `tp-yt-paper-dialog` внутри либо снаружи. По тексту не ищем.
    """
    try:
        script = """
            function deep(root, out){
              for (const el of root.querySelectorAll('*')) {
                out.push(el);
                if (el.shadowRoot) deep(el.shadowRoot, out);
              }
              return out;
            }
            var all = deep(document, []);
            var host = all.filter(function(e){ return e.tagName === 'YTCP-AUTH-CONFIRMATION-DIALOG'; });
            if (!host.length) return false;
            // элемент может быть предрендерен скрытым — засчитываем только раскрытый диалог
            for (var i = 0; i < host.length; i++) {
              // Live 2026-09-14, profiles 21/55: auth host -> confirmation-dialog
              // -> ytcp-dialog -> tp-yt-paper-dialog (opened=true, rect 576x200).
              // evidence/early_auth_gate_descendants_20260914.json
              var children = deep(host[i], []);
              if (host[i].shadowRoot) deep(host[i].shadowRoot, children);
              for (var j = 0; j < children.length; j++) {
                var child = children[j];
                if (child.tagName === 'TP-YT-PAPER-DIALOG' && child.opened === true) {
                  var cr = child.getBoundingClientRect();
                  var cs = getComputedStyle(child);
                  if (!child.hidden && cr.width > 0 && cr.height > 0 &&
                      cs.display !== 'none' && cs.visibility !== 'hidden') return true;
                }
              }
              var e = host[i];
              for (var k = 0; k < 10 && e; k++) {
                if (e.tagName === 'TP-YT-PAPER-DIALOG') {
                  var r = e.getBoundingClientRect();
                  if (r.width > 0 && r.height > 0) return true;
                }
                e = e.parentElement || (e.getRootNode() && e.getRootNode().host);
              }
            }
            return false;
        """
        if check_upload_eligibility:
            script = ("if (" + _LONG_VIDEO_PHONE_GATE_JS + ") "
                      "return 'PHONE_VERIFICATION_REQUIRED_LONG_VIDEO';\n" + script)
        result = driver.execute_script(script)
        if check_upload_eligibility and result == "PHONE_VERIFICATION_REQUIRED_LONG_VIDEO":
            raise StrictUploadEligibilityError(
                "PHONE_VERIFICATION_REQUIRED_LONG_VIDEO: YouTube требует подтвердить номер "
                "телефона канала для видео длиннее 15 минут. Это ограничение функции "
                "канала, а не Google Verify it's you. Подтверждение выполняет владелец; "
                "текущую попытку и запрет повторной отправки сохранить."
            )
        if check_upload_eligibility and type(result) is not bool:
            raise RuntimeError("upload eligibility/auth DOM result unavailable")
        return bool(result)
    except StrictUploadEligibilityError:
        raise
    except Exception as exc:
        if raise_on_error:
            raise StrictAuthGateError("AUTH_GATE_CHECK_UNREADABLE: auth dialog state unavailable") from exc
        return False


def _managed_auth_request(driver, *, stage):
    """Pause this exact wizard; the owning manager performs auth under its lease."""
    global _UPLOAD_AUTH_ATTEMPTED
    if _UPLOAD_AUTH_ATTEMPTED:
        raise StrictAuthGateError("AUTH_GATE: automatic confirmation already attempted; no retry")
    _UPLOAD_AUTH_ATTEMPTED = True
    try:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upload_auth.py")
        spec = importlib.util.spec_from_file_location("worker_upload_auth", path)
        auth = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(auth)
        state = driver.execute_script("return " + auth.ORIGINAL_JS)
        target = driver.execute_cdp_cmd("Target.getTargetInfo", {})["targetInfo"]["targetId"]
        phase = _current_upload_phase()
        ids, frontends = state.get("video_ids", []), state.get("frontend_ids", [])
        if len(ids) > 1 or len(frontends) > 1:
            raise auth.AuthBlocked("Auth upload identity is ambiguous")
        if ids:
            if phase["phase"] not in ("upload_started", "video_id_known"):
                raise auth.AuthBlocked("Auth cannot adopt an unrelated existing upload")
            if phase["video_id"] and phase["video_id"] != ids[0]:
                raise auth.AuthBlocked("Auth upload identity changed")
            if _FRESH_UPLOAD_BINDING is not None:
                _read_wizard_recording_identity(driver, required=True)
            _mark_known_video_id(ids[0])
            phase = _current_upload_phase()
        if phase["phase"] == "upload_started" and len(frontends) != 1:
            raise auth.AuthBlocked("Auth needs the original upload frontend identity")
        request = dict(schema_version=1, request_id=auth.uuid.uuid4().hex,
                       profile=_UPLOAD_AUTH_CONTEXT.get("profile", ""),
                       channel_uc=_UPLOAD_AUTH_CONTEXT.get("channel_uc", ""), target_id=target,
                       phase=phase["phase"], video_id=phase["video_id"], stage=stage,
                       frontend_id=frontends[0] if frontends else "")
        auth.validate_request(request)
        auth.validate_original(state, request)
        print(auth.encode_request(request), flush=True)
        ack = auth.wait_ack(request, sys.stdin)
        if driver.execute_cdp_cmd("Target.getTargetInfo", {})["targetInfo"]["targetId"] != target:
            raise auth.AuthBlocked("Auth continuation target changed")
        after = driver.execute_script("return " + auth.ORIGINAL_JS)
        observed_id = auth.validate_original(after, dict(request, video_id=ack["video_id"]), auth_clear=True)
        if observed_id != ack["video_id"]:
            raise auth.AuthBlocked("Auth continuation video identity differs from ACK")
        if ack["video_id"]:
            if _FRESH_UPLOAD_BINDING is not None:
                fresh = _read_wizard_recording_identity(driver, required=True)
                if fresh.get("videoId") != ack["video_id"]:
                    raise auth.AuthBlocked("Fresh auth identity differs from ACK")
            _mark_known_video_id(ack["video_id"])
    except Exception:
        raise StrictAuthGateError("AUTH_GATE: managed confirmation blocked; same upload phase retained") from None


def _require_auth_clear(driver, *, stage):
    if driver is None:
        raise StrictAuthGateError("AUTH_GATE_CHECK_UNREADABLE: browser driver missing")
    if auth_gate_present(driver, raise_on_error=True, check_upload_eligibility=True):
        if (os.getenv("VIDEO_UPLOAD_AUTO_AUTH") == "1"
                and os.getenv("VIDEO_UPLOAD_AUTH_PROTOCOL") == "1"):
            _managed_auth_request(driver, stage=stage)
            if not auth_gate_present(driver, raise_on_error=True, check_upload_eligibility=True):
                return
        phase = _current_upload_phase()
        raise StrictAuthGateError(
            f"AUTH_GATE: Google requires identity confirmation ({stage}); "
            f"upload phase={phase['phase']}; preserve this attempt and its known ID"
        )


def save_blackbox(driver, tag="fail"):
    """Снимок страницы в момент проблемы: скриншот + состояние + DOM + текст.

    Без этого разбор сводится к «оно не сработало» и следующей заливке вслепую.
    Пишет в research/dom/blackbox/<tag>_<ts>/ и возвращает путь (или "").
    """
    if driver is None:
        return ""
    try:
        ts = time.strftime("%Y%m%d-%H%M%S")
        base = os.path.join(os.path.expanduser("~/VideoStudio/research/dom/blackbox"),
                            f"{tag}_{ts}")
        os.makedirs(base, exist_ok=True)
        try:
            driver.save_screenshot(os.path.join(base, "page.png"))
        except Exception:
            pass
        snap = {"url": ""}
        try:
            snap["url"] = driver.current_url
        except Exception:
            pass
        if YS is not None:
            try:
                snap["wizard"] = YS.read_wizard(driver)
                snap["videos"] = YS.read_videos(driver)[:15]
                snap["uploadProgress"] = YS.read_upload_progress(driver)
            except Exception as e:
                snap["state_error"] = str(e)
        with open(os.path.join(base, "state.json"), "w") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        try:
            with open(os.path.join(base, "body.txt"), "w") as f:
                f.write(driver.execute_script("return document.body.innerText") or "")
        except Exception:
            pass
        logger.warning("Чёрный ящик сохранён: %s", base)
        return base
    except Exception as e:
        logger.debug("save_blackbox: %s", e)
        return ""


def _extract_uc_from_url(url: str):
    """Достаёт UC-id из URL вида .../channel/UCxxxx (studio или youtube). Иначе None."""
    if not url:
        return None
    m = re.search(r"/channel/(UC[\w-]+)", url)
    return m.group(1) if m else None


def _resolve_channel_uc(driver, channel_url: str):
    """Резолвит channel_url -> UC-id.

    1. Если в URL уже есть /channel/UCxxxx — берём напрямую.
    2. Иначе (@handle / custom url) — driver.get(url), читаем канонический id со страницы
       (meta itemprop=channelId / link canonical / og:url / итоговый current_url).

    Возвращает (uc_id|None, err|None).
    """
    direct = _extract_uc_from_url(channel_url)
    if direct:
        return direct, None

    if "youtube.com" not in (channel_url or ""):
        return None, f"unsupported channel url (no youtube.com, no /channel/UC): {channel_url}"

    logger.info("Резолвлю UC-id со страницы канала: %s", channel_url)
    try:
        driver.get(channel_url)
        time.sleep(6)
    except Exception as e:
        return None, f"failed to open channel url {channel_url}: {e}"

    # Не залогинен / нет доступа
    if "accounts.google.com" in (driver.current_url or ""):
        return None, f"not logged in / no access to channel: {channel_url}"

    uc = driver.execute_script("""
        function find() {
            // meta itemprop=channelId (самый надёжный)
            var m = document.querySelector('meta[itemprop="channelId"]');
            if (m && m.content && m.content.indexOf('UC') === 0) return m.content;
            // canonical link
            var c = document.querySelector('link[rel="canonical"]');
            if (c && c.href) { var mm = c.href.match(/\\/channel\\/(UC[\\w-]+)/); if (mm) return mm[1]; }
            // og:url
            var og = document.querySelector('meta[property="og:url"]');
            if (og && og.content) { var m2 = og.content.match(/\\/channel\\/(UC[\\w-]+)/); if (m2) return m2[1]; }
            // любой channel-ссылочный href на странице
            var a = document.querySelector('a[href*="/channel/UC"]');
            if (a && a.href) { var m3 = a.href.match(/\\/channel\\/(UC[\\w-]+)/); if (m3) return m3[1]; }
            // прямо в HTML (ytInitialData)
            var html = document.documentElement.innerHTML;
            var m4 = html.match(/"channelId":"(UC[\\w-]+)"/);
            if (m4) return m4[1];
            return null;
        }
        return find();
    """)
    if uc:
        return uc, None

    # Последняя попытка — из итогового URL (редирект @handle -> /channel/UC...)
    uc = _extract_uc_from_url(driver.current_url or "")
    if uc:
        return uc, None

    return None, f"could not resolve channel id from url: {channel_url}"


def _fast_ui_enabled():
    return os.getenv("VIDEO_UPLOAD_FAST_UI") == "1" and YS is not None


# Existing selectors/properties: account_switcher_2026-08-07.json and
# c3_identity45_details.json. Navigation reuse requires one atomic live snapshot,
# not a remembered channel name or a UC occurring somewhere in a query string.
_FAST_CHANNEL_READY_JS = r"""
var uc=arguments[0],app=document.querySelector('ytcp-app');
var u=new URL(location.href),path='/channel/'+uc;
if(u.origin!=='https://studio.youtube.com'||!(u.pathname===path||u.pathname.startsWith(path+'/'))||
   document.readyState==='loading'||!app||!app.channel||app.channel.channelId!==uc||
   !app.currentEntity||app.currentEntity.type!=='CHANNEL'||app.currentEntity.id!==uc)return null;
var controls=document.querySelectorAll('ytcp-icon-button#upload-icon,#create-icon,ytcp-button#create-icon,tp-yt-iron-icon#create-icon');
for(var e of controls){var r=e.getBoundingClientRect(),s=getComputedStyle(e);
 if(!e.hidden&&r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&
    e.disabled!==true&&e.getAttribute('aria-disabled')!=='true')return {url:location.href,channel_uc:uc};}
return null;
"""


def _fast_channel_ready(driver, uc_id):
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", uc_id or "") is None:
        return False
    try:
        state = driver.execute_script(_FAST_CHANNEL_READY_JS, uc_id)
        return bool(isinstance(state, dict) and state.get("channel_uc") == uc_id
                    and state.get("url") == driver.current_url)
    except Exception:
        return False


def _wait_channel_ready(driver, uc_id, timeout):
    return bool(YS.wait_until(driver, lambda d: _fast_channel_ready(d, uc_id),
                             timeout=timeout, poll=0.25, desc="точный канал Studio готов"))


def _wait_account_avatar(driver, timeout=7):
    # S2 live diagnostic: a visible avatar preceded the fully booted Studio and
    # its click did not open a menu. Require the current channel shell as well.
    def ready(d):
        current_uc = _extract_uc_from_url(d.current_url or "")
        return (_fast_channel_ready(d, current_uc)
                and YS._account_menu_control(d, "avatar"))
    return bool(YS.wait_until(driver, ready,
                             timeout=timeout, poll=0.25, desc="аватар Studio готов"))


# Live 2026-09-14: s2_diagnostics/result.json, exact PL main-document 403.
# Only navigation timing is read: a failed fetch/XHR or an older document must
# never shorten the direct route's normal 25-second readiness budget.
_DIRECT_NAVIGATION_STATE_JS = r"""
var entries=performance.getEntriesByType('navigation'),n=entries.length===1?entries[0]:null;
return {url:location.href,time_origin_ms:performance.timeOrigin,ready:document.readyState,
 navigation_count:entries.length,navigation_name:n?n.name:null,
 response_status_supported:!!n&&('responseStatus' in n),
 response_status:n&&typeof n.responseStatus==='number'?n.responseStatus:null,
 response_end:n?n.responseEnd:null,app_present:document.querySelector('ytcp-app')!==null,
 wizard_count:document.querySelectorAll('ytcp-uploads-dialog').length};
"""


def _direct_navigation_rejected(state, uc_id, previous_time_origin, current_url):
    if (not isinstance(state, dict) or re.fullmatch(r"UC[A-Za-z0-9_-]{22}", str(uc_id or "")) is None
            or type(previous_time_origin) not in (int, float) or not math.isfinite(previous_time_origin)
            or previous_time_origin <= 0):
        return False
    try:
        expected_path = f"/channel/{uc_id}/videos/upload"
        for raw in (state["url"], state["navigation_name"], current_url):
            parsed = urlparse(raw)
            if (parsed.scheme != "https" or parsed.netloc != "studio.youtube.com"
                    or parsed.path != expected_path):
                return False
        return (state["url"] == current_url
                and type(state["time_origin_ms"]) in (int, float) and math.isfinite(state["time_origin_ms"])
                and state["time_origin_ms"] > previous_time_origin
                and state["ready"] in ("interactive", "complete")
                and type(state["navigation_count"]) is int and state["navigation_count"] == 1
                and state["response_status_supported"] is True
                and type(state["response_status"]) in (int, float) and state["response_status"] == 403
                and type(state["response_end"]) in (int, float) and math.isfinite(state["response_end"])
                and state["response_end"] > 0 and state["app_present"] is False
                and type(state["wizard_count"]) is int and state["wizard_count"] == 0)
    except (KeyError, TypeError, ValueError):
        return False


def _wait_direct_channel_ready(driver, uc_id, previous_time_origin, timeout=25):
    try:
        state = driver.execute_script(_DIRECT_NAVIGATION_STATE_JS)
        if _direct_navigation_rejected(state, uc_id, previous_time_origin, driver.current_url):
            logger.info("FAST_UI: exact main-document HTTP 403 для %s — сразу к переключателю", uc_id)
            return False
    except Exception:
        pass  # Missing timing evidence retains the original bounded wait.
    return _wait_channel_ready(driver, uc_id, timeout)


def _wait_upload_dialog(driver, uc_id, timeout=6):
    # This is only a readiness hint. The existing fresh_upload_baseline and
    # durable upload boundary still authorise the eventual MP4 handoff.
    def ready(d):
        if not _fast_channel_ready(d, uc_id):
            return None
        state = YS.read_fresh_upload_state(d, uc_id)
        YS._fresh_scope(state, uc_id)
        return state
    return bool(YS.wait_until(driver, ready, timeout=timeout, poll=0.25,
                             desc="диалог новой загрузки готов"))


# Existing generic paper-dialog selectors/properties, also recorded live in
# s5_resume_readonly_20260914T1348.json. Read-only: no global Cancel or Escape.
# Ancestor geometry is deliberately not a visibility criterion: Studio hosts
# can be height=0 while their opened inner paper dialog is visibly rendered.
_VISIBLE_RESUME_STATE_JS = r"""
function deep(root){var out=[];for(var e of root.querySelectorAll('*')){
 out.push(e);if(e.shadowRoot)out.push(...deep(e.shadowRoot));}return out;}
function visible(e){if(!e)return false;var r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;
 for(var p=e;p;p=p.parentElement||(p.getRootNode()&&p.getRootNode().host)){
  var s=getComputedStyle(p);if(p.hidden||s.display==='none'||s.visibility==='hidden'||
   s.visibility==='collapse'||s.opacity==='0')return false;}return true;}
var app=document.querySelector('ytcp-app'), dialogs=deep(document).filter(e=>e.tagName==='TP-YT-PAPER-DIALOG'&&visible(e));
var words=/resume upload|wrong file|возобнов|продолжить загруз|fortsetzen|reprendre|reanudar/i;
return {url:location.href,ready:document.readyState,body_visible:visible(document.body),
 app_uc:app&&app.channel?app.channel.channelId:null,
 entity_id:app&&app.currentEntity?app.currentEntity.id:null,
 entity_type:app&&app.currentEntity?app.currentEntity.type:null,
 visible_dialog_count:dialogs.length,
 resume_dialog_count:dialogs.filter(e=>words.test(e.innerText||'')).length};
"""


def _visible_resume_disposition(driver, uc_id):
    """CLEAR/BUSY/UNREADABLE; hidden strings never authorise a click or reload."""
    try:
        if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", str(uc_id or "")) is None:
            return "resume-unreadable"
        state = driver.execute_script(_VISIBLE_RESUME_STATE_JS)
        if not isinstance(state, dict):
            return "resume-unreadable"
        parsed = urlparse(state["url"])
        if (parsed.scheme != "https" or parsed.netloc != "studio.youtube.com"
                or parsed.path != f"/channel/{uc_id}/videos/upload"
                or state["url"] != driver.current_url
                or state["ready"] not in ("interactive", "complete")
                or state["body_visible"] is not True
                or state["app_uc"] != uc_id or state["entity_id"] != uc_id
                or state["entity_type"] != "CHANNEL"
                or type(state["visible_dialog_count"]) is not int
                or type(state["resume_dialog_count"]) is not int
                or not 0 <= state["resume_dialog_count"] <= state["visible_dialog_count"]):
            return "resume-unreadable"
        return "resume-visible-busy" if state["resume_dialog_count"] else "none"
    except Exception:
        return "resume-unreadable"


def _wait_visible_resume_disposition(driver, uc_id, *, timeout=10.0):
    """Allow a loading Content page to boot; known visible resume stops at once."""
    deadline = time.monotonic() + timeout
    while True:
        disposition = _visible_resume_disposition(driver, uc_id)
        if disposition != "resume-unreadable":
            return disposition
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return disposition
        time.sleep(min(0.25, remaining))


def _channel_name_from_page(driver, channel_url):
    """Открывает публичную страницу канала и читает его НАЗВАНИЕ (для матча в переключателе аккаунтов)."""
    try:
        driver.get(channel_url)
        def read_name(d):
            return d.execute_script("""
            var og = document.querySelector('meta[property="og:title"]');
            if (og && og.content) return og.content;
            var t = document.querySelector('meta[name="title"]');
            if (t && t.content) return t.content;
            return (document.title || '').replace(/ - YouTube$/, '');
            """)
        if _fast_ui_enabled():
            nm = YS.wait_until(driver, lambda d: (lambda name: name if name and name != "YouTube" else None)(read_name(d)),
                               timeout=5, poll=0.25, desc="название публичного канала готово")
        else:
            time.sleep(5)
            nm = read_name(driver)
        return (nm or "").strip()
    except Exception:
        return ""


def _studio_loaded(driver):
    """Studio отрисовала рабочую шапку канала (кнопки Create/Upload).

    Это признак ЗАГРУЗКИ, а НЕ прав доступа: его отсутствие значит «рабочего состояния
    нет» и ничего не говорит о причине — так же выглядят недогруженная страница,
    транзиент переключателя и чужой authuser. Вывод «не тот Google-аккаунт» отсюда
    делать нельзя (страницу отказа отличает отдельный признак, которого в SEL пока нет).

    Селекторы — id-шные, из дампа шапки Studio (research/dom/account_switcher_2026-08-07.json:
    `ytcp-icon-button#upload-icon`, `button#avatar-btn`).
    """
    from selenium.webdriver.common.by import By
    for sel in ("ytcp-icon-button#upload-icon", "#create-icon",
                "ytcp-button#create-icon", "tp-yt-iron-icon#create-icon"):
        try:
            if driver.find_elements(By.CSS_SELECTOR, sel):
                return True
        except Exception:
            continue
    # ФОЛБЭК по надписи: aria-label язык-зависим (на локализованном Studio это
    # «Créer»/«Utwórz»), поэтому только после машинных признаков и с записью в лог.
    try:
        if driver.find_elements(By.CSS_SELECTOR, "[aria-label='Create']"):
            logger.info("_studio_loaded: состояние опознано по англ. надписи Create (фолбэк)")
            return True
    except Exception:
        pass
    return False


def _studio_ready(driver, uc_id, au=None, timeout=30):
    """Открыть Studio канала и ДОЖДАТЬСЯ рабочего состояния (вместо слепой паузы).

    Вердикт выносится по первому истинному замеру, а не одним снимком через N секунд:
    раньше «не успела прогрузиться за 11с» приравнивалось к «нет доступа», и профиль,
    который каналом ВЛАДЕЕТ, получал ABORT.

    URL без слэша перед query: '/channel/UC/?authuser=N' ломает роутер Studio
    («Oops, something went wrong»), '/channel/UC?authuser=N' — работает.
    """
    url = f"https://studio.youtube.com/channel/{uc_id}" + (f"?authuser={au}" if au is not None else "")
    try:
        if _fast_ui_enabled() and au is None and _fast_channel_ready(driver, uc_id):
            logger.info("FAST_UI: точный канал уже готов, повторный переход пропущен: %s", uc_id)
            return True
        driver.get(url)
    except Exception as e:
        logger.warning("Переход в Studio канала не удался (%s): %s", url, e)
        return False
    if YS is not None:
        if _fast_ui_enabled():
            return _wait_channel_ready(driver, uc_id, timeout)
        ok = YS.wait_until(driver,
                           lambda d: (uc_id in (d.current_url or "")) and _studio_loaded(d),
                           timeout=timeout, poll=1.5, desc="рабочее состояние Studio канала")
        return bool(ok)
    time.sleep(11)      # без yt_studio ждать нечем — прежнее поведение
    return uc_id in (driver.current_url or "") and _studio_loaded(driver)


def _switch_to_channel(driver, channel_name, uc_id):
    """Переключает активный аккаунт/канал через меню Studio. На ОДНОМ AdsPower-профиле может быть
    несколько Google-аккаунтов → без переключения прямой заход даёт 'You don't have permission'.
    Матчит пункт по UC (если у пункта есть ссылка /channel/UC...) ИЛИ по имени (диакритика-нечувствительно)."""
    from selenium.webdriver.common.by import By
    import unicodedata
    def norm(s):
        s = s or ""
        nfkd = unicodedata.normalize('NFKD', s)
        return ''.join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()
    target_norm = norm(channel_name)
    try:
        driver.get("https://studio.youtube.com")
        if _fast_ui_enabled():
            if not _wait_account_avatar(driver, timeout=25):
                logger.warning("FAST_UI: Studio ещё не готова к переключателю; аватар не нажимаю")
                return False
            if _fast_channel_ready(driver, uc_id):
                logger.info("FAST_UI: после root-перехода нужный канал уже готов; меню не открываю: %s", uc_id)
                return True
        else:
            time.sleep(7)
        # Переключатель открываем МАШИННО: пункт «Switch account» опознаётся по
        # data.icon.iconType == SWITCH_ACCOUNTS (yt_studio.SEL_ACCOUNTS, дамп 2026-08-07),
        # а не по надписи. Список слов ниже знает 9 языков — на es/nl/tr переключатель
        # по нему не находился, и код молча читал каналы текущего аккаунта.
        _opened = None
        if YS is not None:
            try:
                _opened = YS.open_account_menu(driver)
            except Exception as _oe:
                logger.warning("Машинное открытие переключателя аккаунтов упало: %s", _oe)
        if not (_opened or {}).get("accounts"):
            logger.info("Переключатель машинно не прочитан — открываю по надписи (фолбэк, "
                        "список языков ограничен)")
            # с чистой страницы: машинная попытка могла оставить меню в неизвестном
            # состоянии, а повторный клик по аватару его просто закроет
            driver.get("https://studio.youtube.com")
            if _fast_ui_enabled():
                if not _wait_account_avatar(driver, timeout=25):
                    return False
                if _fast_channel_ready(driver, uc_id):
                    return True
            else:
                time.sleep(5)
            avatar = None
            for sel in ("#avatar-btn", "button#avatar-btn", "ytcp-button#account-button", "#account-button"):
                try:
                    els = driver.find_elements(By.CSS_SELECTOR, sel)
                    if els:
                        avatar = els[0]; break
                except Exception:
                    continue
            if not avatar:
                logger.warning("avatar-btn не найден — меню аккаунта не открыть")
                return False
            driver.execute_script("arguments[0].click();", avatar)
            time.sleep(3)
            # "Switch account" — мультиязычно
            SWITCH_WORDS = ("switch account", "сменить аккаунт", "konto wechseln", "changer de compte",
                            "cambia account", "przełącz konto", "mudar de conta", "comută", "přepnout účet")
            _sw_hit = ""
            for it in driver.find_elements(By.CSS_SELECTOR, "tp-yt-paper-item, ytd-compact-link-renderer, [role=menuitem]"):
                t = (it.text or "").strip().lower()
                if t and any(w in t for w in SWITCH_WORDS):
                    driver.execute_script("arguments[0].click();", it)
                    _sw_hit = t[:40]
                    time.sleep(5)
                    break
            if not _sw_hit:
                # Молчать тут нельзя: без этого клика ниже читается список каналов
                # ТЕКУЩЕГО аккаунта, чужой аккаунт в него не попадёт, и итогом будет
                # «не найден в переключателе. Доступно: » с пустым перечнем.
                logger.warning("Пункт «Switch account» не найден ни машинно, ни по надписи — "
                               "дальше видны только каналы ТЕКУЩЕГО аккаунта")
        accounts = driver.find_elements(By.CSS_SELECTOR,
                                        "ytd-account-item-renderer, [role=option], ytmusic-account-item-renderer")
        all_texts = []
        for acc in accounts:
            txt = (acc.text or "").strip()
            if txt:
                all_texts.append(txt.split(chr(10))[0][:50])
            uc_match = False
            try:
                for a in acc.find_elements(By.CSS_SELECTOR, "a[href*='/channel/UC']"):
                    if uc_id and uc_id in (a.get_attribute("href") or ""):
                        uc_match = True; break
            except Exception:
                pass
            tn = norm(txt)
            # Матч по имени — ТОЛЬКО полное вхождение названия канала. Ветка «совпали
            # первые два слова» убрана: она кликала по чужому пункту-однофамильцу
            # (тот же класс ошибки, что приглашение соавтора чужому каналу), а промах
            # ловился лишь последующей сверкой UC ценой ~15с на попытку.
            name_match = bool(target_norm) and target_norm in tn
            if uc_match or name_match:
                driver.execute_script("arguments[0].click();", acc)
                # чем именно опознали — в лог: имя это догадка, UC — идентификатор
                logger.info("Переключился на канал: %s (совпадение по %s)",
                            txt.replace(chr(10), ' ')[:50], "UC" if uc_match else "названию")
                if _fast_ui_enabled():
                    return _wait_channel_ready(driver, uc_id, timeout=8)
                time.sleep(8)
                return True
        if not accounts:
            logger.warning("Переключатель не отдал ни одного пункта аккаунта — список НЕ прочитан "
                           "(это не значит «канала нет»): цель '%s' (UC %s)", channel_name, uc_id)
            return False
        logger.warning("Канал '%s' (UC %s) не найден в переключателе. Доступно: %s",
                       channel_name, uc_id, " | ".join(all_texts))
        return False
    except Exception as e:
        logger.warning("Ошибка переключения аккаунта: %s", e)
        return False


# --- Восстановление вышедшего Google-аккаунта (опционально) --------------------
# Выключено, пока нет VIDEO_UPLOAD_ACCOUNT_RECOVERY=1. Запускается ТОЛЬКО здесь,
# в самом конце маршрутизации: файл ещё не передан (фаза ниже upload_started),
# поэтому повторная отправка MP4 по этому пути невозможна.
try:
    import upload_account_recovery as ACCOUNT_RECOVERY
except ImportError:
    ACCOUNT_RECOVERY = None
try:
    import upload_coordination as _RECOVERY_COORDINATION
except ImportError:
    _RECOVERY_COORDINATION = None

# Кто нас запустил — фиксируем В МОМЕНТ СТАРТА воркера. Если брать getppid()
# позже, осиротевший воркер запомнит init как «своего менеджера» и проверка
# станет тавтологией ровно тогда, когда менеджер уже умер.
_RECOVERY_PARENT_PID_AT_START = os.getppid()


def _recovery_manager_supervised():
    """Менеджер держит cross-host лизу и живой durable-канал.

    Обе переменные выставляет только upload_manager._invoke_worker, и только
    когда у него есть лиза профиля. Puller не ставит ни одной.
    """
    return (os.getenv("VIDEO_UPLOAD_AUTO_AUTH") == "1"
            and os.getenv("VIDEO_UPLOAD_PHASE_ACK") == "1")


def _recovery_manager_alive():
    parent = os.getppid()
    return parent == _RECOVERY_PARENT_PID_AT_START and parent > 1


def _recovery_phase_allows_login():
    return _current_upload_phase()["phase"] in ("preflight", "profile_opened")


def _recovery_lease_context():
    """Настройки общего координатора лиз (env перекрывает значения по умолчанию)."""
    raw = os.getenv("VIDEO_UPLOAD_LEASE_CONTEXT")
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            logger.warning("VIDEO_UPLOAD_LEASE_CONTEXT не разобран — лизу не беру")
            return None
    holder = "minsk" if sys.platform.startswith("linux") else "mac"
    return {"schema_version": 1,
            "coordinator_command": ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                    "--", "video-ops-profile-leases-vps",
                                    "/opt/video-ops/upload_profile_lease.py"],
            "holder": holder, "ttl_seconds": 900,
            "renew_interval_seconds": 120, "command_timeout_seconds": 40}


def _recovery_ownership_check(profile_id):
    """Профиль открыт РОВНО на этой машине — переспрашиваем перед каждым вводом.

    Профили общие для Mac и Минска. Ввод пароля длится минуты, за это время
    вторая машина может открыть тот же профиль и порвать сессию. Недоступный
    сосед — это «не доказано», а не «свободно».
    """
    peer = os.getenv("VIDEO_UPLOAD_PEER_ADSPOWER", "").strip()
    if not peer:
        raise RuntimeError("не задан VIDEO_UPLOAD_PEER_ADSPOWER — владение профилем не доказать")
    uid = _resolve_user_id(profile_id)

    def active(api):
        response = adspower_rate_limit.get(f"{api}/api/v1/browser/local-active", timeout=15)
        value = response.json()
        entries = (value.get("data") or {}).get("list")
        if response.status_code != 200 or value.get("code") != 0 or not isinstance(entries, list):
            raise RuntimeError(f"AdsPower {api} не ответил списком активных профилей")
        return [e for e in entries if isinstance(e, dict) and e.get("user_id") == uid]

    # AdsPower пропускает примерно один управляющий запрос в секунду, и лимит
    # общий со всеми заливками. Переспрашиваем не чаще раза в 15 секунд: этого
    # достаточно, чтобы ни один ввод не ушёл в профиль, открытый на второй
    # машине, и при этом мы не занимаем чужую очередь опросами в циклах ожидания.
    last = {"at": 0.0}

    def check():
        now = time.monotonic()
        if now - last["at"] < 15.0:
            return
        if not active(ADSPOWER_API):
            raise RuntimeError("профиль больше не открыт на этой машине — ввод прекращён")
        if active(peer):
            raise RuntimeError("профиль открыт на второй машине — ввод прекращён")
        last["at"] = now
    return check


def _try_account_recovery(driver, profile_id, uc_id, last_err, channel_label=""):
    """Вернуть (uc_id, None) при восстановленном входе, иначе (None, ошибка).

    Исходную ошибку маршрутизации не теряем: если восстановление не применимо
    или упало не по вине аккаунта, оператор должен увидеть первоначальную
    причину, а не «проблема с аккаунтом».
    """
    if ACCOUNT_RECOVERY is None or not ACCOUNT_RECOVERY.enabled():
        return None, last_err
    if not _recovery_phase_allows_login():
        return None, last_err

    def run(lease_check):
        return ACCOUNT_RECOVERY.maybe_recover(
            driver, str(profile_id), uc_id, lease_check,
            ownership_check=_recovery_ownership_check(profile_id),
            channel_label=channel_label,
            evidence_dir=os.getenv("VIDEO_UPLOAD_RECOVERY_EVIDENCE_DIR") or None,
            on_status=lambda value: logger.warning("Вход в аккаунт: %s", value),
            phase_before_upload=True,
        )

    try:
        if _recovery_manager_supervised():
            # Лиза уже у менеджера: наш ключ к ней — то, что он жив и он же наш
            # родитель. Умер менеджер — лиза больше ничем не подтверждена.
            def supervised_check():
                if not _recovery_manager_alive():
                    raise RuntimeError("менеджер с лизой профиля умер — ввод прекращён")
                if not _recovery_phase_allows_login():
                    raise RuntimeError("файл уже передаётся — вход запрещён")
            result = run(supervised_check)
        else:
            # Без менеджера (боевой puller) берём такую же лизу сами, иначе
            # два процесса могут вводить пароль в один профиль.
            if _RECOVERY_COORDINATION is None:
                logger.info("Восстановление входа пропущено: нет модуля лиз профиля")
                return None, last_err
            context = _recovery_lease_context()
            if not context:
                return None, last_err
            try:
                with _RECOVERY_COORDINATION.profile_lease(str(profile_id), context) as lease:
                    def own_check():
                        lease.assert_held()
                        if not _recovery_phase_allows_login():
                            raise RuntimeError("файл уже передаётся — вход запрещён")
                    result = run(own_check)
            except _RECOVERY_COORDINATION.ProfileLeaseError as exc:
                logger.warning("Лиза профиля не получена — вход не запускаю: %s", exc)
                return None, last_err
    except ACCOUNT_RECOVERY.OwnerHelpNeeded as exc:
        return None, f"account recovery needs the owner: {exc}"
    except Exception as exc:
        return None, f"{last_err} | account recovery blocked: {type(exc).__name__}"
    if not result:
        return None, last_err
    # Живое доказательство, а не догадка: номер аккаунта берём из подтверждённой
    # сессии Studio того самого канала.
    proven = str(result.get("session_index") or "")
    if proven.isdigit():
        _ACTIVE_AU[uc_id] = proven
        logger.info("Канал подтверждён после входа: %s (authuser=%s)", uc_id, proven)
    else:
        logger.info("Канал подтверждён после входа: %s", uc_id)
    return uc_id, None


def _navigate_to_channel_studio(driver, channel_url: str):
    """Резолвит канал → ПЕРЕКЛЮЧАЕТ активный аккаунт/канал через меню (на профиле несколько Google-аккаунтов)
    → верифицирует доступ. Возвращает (uc_id|None, err|None); err != None → ABORT без загрузки."""
    if not channel_url:
        return None, "channel_url is empty"
    # На одном профиле несколько Google-аккаунтов, а меню Studio перечисляет каналы только
    # ТЕКУЩЕГО аккаунта — через границу аккаунтов оно не ходит и отдаёт пустой список
    # («не найден в переключателе. Доступно: »). Явный ?authuser=N в channel_url переключает
    # активный аккаунт и ЗАЛИПАЕТ на сессию (проверено live), поэтому пробуем его ПЕРВЫМ,
    # а меню оставляем запасным путём.
    m_au = re.search(r"[?&]authuser=(\d+)", channel_url)
    authuser = m_au.group(1) if m_au else None
    if authuser is not None:
        channel_url = re.sub(r"[?&]authuser=\d+", "", channel_url)
    uc_id, err = _resolve_channel_uc(driver, channel_url)
    if err or not uc_id:
        return None, err or f"could not resolve channel id from url: {channel_url}"
    # Рабочий номер прошлого прогона по этому каналу мог протухнуть (номер дрейфует
    # между сессиями) — верифицируем заново, а не наследуем.
    _ACTIVE_AU.pop(uc_id, None)
    if _fast_ui_enabled() and authuser is None and _fast_channel_ready(driver, uc_id):
        logger.info("FAST_UI: переиспользую готовый точный канал: %s", uc_id)
        return uc_id, None

    def _dbg_page():
        """Что реально на экране — в лог, чтобы отказ не был безымянным."""
        try:
            return driver.execute_script(
                "return {u: location.href.slice(0,110),"
                " t: (document.body.innerText||'').replace(/\\s+/g,' ').slice(0,120)};") or {}
        except Exception:
            return {}

    if authuser is not None:
        # ВАЖНО: проверяем ПРЯМО на authuser-URL и никуда не уходим — переход на «чистый»
        # URL сразу после может вернуть аккаунт по умолчанию и дать ложный отказ.
        # Номер authuser НЕ закреплён за каналом: он дрейфует между сессиями, и
        # вчерашний рабочий номер сегодня отдаёт «you don't have permission».
        if _studio_ready(driver, uc_id, authuser):
            logger.info("Канал верифицирован через authuser=%s: %s", authuser, uc_id)
            _ACTIVE_AU[uc_id] = authuser
            return uc_id, None
        _d = _dbg_page()
        logger.warning("authuser=%s не дал рабочего состояния Studio · url=%s · текст=%s",
                       authuser, _d.get("u"), _d.get("t"))

        # Номер из URL не подошёл → ЧИТАЕМ номер аккаунта из меню (машинные данные
        # ytd-account-item-renderer → serviceEndpoint...authuser=N; yt_studio.SEL_ACCOUNTS,
        # дамп 2026-08-07), а не перебираем 0..3: у профиля бывает 6 аккаунтов, и перебор
        # объявлял «нет доступа» на канале, которым профиль ВЛАДЕЕТ (плюс 44с слепых пауз).
        # Имя канала читаем с публичной страницы — заодно оказываемся на youtube.com,
        # где живёт свитчер с этими данными.
        _name = _channel_name_from_page(driver, f"https://www.youtube.com/channel/{uc_id}")
        _au_read, _list_read = None, False
        if YS is not None:
            try:
                # authuser_for_channel сам открывает меню; read_accounts после него ничего
                # не кликает и говорит, БЫЛ ли список вообще прочитан (иначе «номер не
                # найден» неотличимо от «меню не отдало данные»). Повторно открывать меню
                # нельзя — второй клик по аватару его закрывает.
                _au_read = YS.authuser_for_channel(driver, _name)
                _list_read = bool(_au_read) or bool((YS.read_accounts(driver) or {}).get("accounts"))
            except Exception as _ae:
                logger.warning("Список аккаунтов профиля не прочитан: %s", _ae)
        else:
            logger.warning("yt_studio не загружен — список аккаунтов профиля прочитать нечем")

        if _au_read and str(_au_read) != str(authuser):
            logger.info("Номер аккаунта прочитан из меню: authuser=%s (в ссылке был %s)",
                        _au_read, authuser)
            if _studio_ready(driver, uc_id, _au_read):
                logger.info("Канал верифицирован через прочитанный authuser=%s: %s", _au_read, uc_id)
                _ACTIVE_AU[uc_id] = str(_au_read)
                return uc_id, None
            _d = _dbg_page()
            logger.warning("Прочитанный authuser=%s тоже не дал Studio · url=%s · текст=%s",
                           _au_read, _d.get("u"), _d.get("t"))
        elif _list_read and not _au_read:
            # Список аккаунтов прочитан, канала в нём нет — это факт, а не повод гадать
            # номерами: перебор 0..3 тут только жёг бы время.
            logger.warning("Канал '%s' (UC %s) не найден среди аккаунтов профиля — "
                           "перебором номеров не подменяю, иду через переключатель меню",
                           _name, uc_id)
        elif not _list_read:
            # Список НЕ прочитан (меню не отдало данные) — только тогда старый перебор,
            # и с явной пометкой, что это гадание.
            logger.warning("Список аккаунтов не прочитан — падаю на ПЕРЕБОР номеров 0..3 (гадание)")
            for _au in [str(x) for x in (0, 1, 2, 3) if str(x) != str(authuser)]:
                if _studio_ready(driver, uc_id, _au, timeout=20):
                    logger.info("Канал верифицирован перебором: authuser=%s → %s", _au, uc_id)
                    _ACTIVE_AU[uc_id] = _au
                    return uc_id, None
        logger.info("authuser не дал доступа — падаю на переключатель меню")

    # БЫСТРЫЙ ПУТЬ: если активный аккаунт профиля уже владеет нужным каналом (обычный
    # случай — один аккаунт на профиль), прямой переход экономит ~60с на заливку против
    # прохода через меню-переключатель. Безопасно: принимаем результат ТОЛЬКО при
    # совпадении UC в current_url и загруженной Studio — иначе идём прежним путём.
    if authuser is None:
        try:
            _previous_time_origin = None
            if _fast_ui_enabled():
                try:
                    _previous_time_origin = driver.execute_script("return performance.timeOrigin;")
                except Exception:
                    pass
            driver.get(f"https://studio.youtube.com/channel/{uc_id}/videos/upload")
            # ждём готовности, а не спим 7с: не успела прогрузиться ≠ «не тот канал»
            if YS is not None:
                if _fast_ui_enabled():
                    _wait_direct_channel_ready(driver, uc_id, _previous_time_origin, timeout=25)
                else:
                    YS.wait_until(driver,
                                  lambda d: (uc_id in (d.current_url or "")) and _studio_loaded(d),
                                  timeout=25, poll=1.5, desc="Studio канала (прямой переход)")
            else:
                time.sleep(7)
            if (_fast_channel_ready(driver, uc_id) if _fast_ui_enabled()
                    else uc_id in (driver.current_url or "") and _studio_loaded(driver)):
                logger.info("Канал верифицирован прямым переходом (без меню): %s", uc_id)
                return uc_id, None
            logger.info("Прямой переход не дал нужный канал — иду через переключатель меню")
        except Exception as _fe:
            logger.info("Прямой переход упал (%s) — иду через переключатель меню", str(_fe)[:60])

    # ВАЖНО: имя читаем с ПУБЛИЧНОЙ страницы канала (youtube.com/channel/UC), а НЕ со студийного
    # URL — иначе og:title = "Channel dashboard - YouTube Studio" и матч в переключателе ломается.
    name = _channel_name_from_page(driver, f"https://www.youtube.com/channel/{uc_id}")
    logger.info("Цель: канал '%s' (UC %s) — переключаю активный аккаунт через меню", name, uc_id)
    # RETRY: сразу после старта браузера переключатель аккаунтов часто пуст (warmup-транзиент) →
    # _switch_to_channel не находит канал → permission denied. Лечится повтором с прогревом сессии.
    last_err = None
    for attempt in range(1, 4):
        _switch_to_channel(driver, name, uc_id)  # мульти-аккаунт: ставим нужную сессию
        if _fast_ui_enabled() and _fast_channel_ready(driver, uc_id):
            logger.info("FAST_UI: переключатель подтвердил точный канал без повторного перехода: %s", uc_id)
            return uc_id, None
        driver.get(f"https://studio.youtube.com/channel/{uc_id}/")
        # ждём рабочего состояния опросом, а не одним замером через 8с: медленная
        # загрузка раньше засчитывалась как отказ доступа
        if YS is not None:
            if _fast_ui_enabled():
                _wait_channel_ready(driver, uc_id, timeout=25)
            else:
                YS.wait_until(driver,
                              lambda d: (uc_id in (d.current_url or "")) and _studio_loaded(d),
                              timeout=25, poll=1.5, desc="Studio канала после переключения")
        else:
            time.sleep(8)
        cur = driver.current_url or ""
        if "accounts.google.com" in cur:
            last_err = f"not logged in / no access: {channel_url}"
        elif uc_id not in cur:
            landed = _extract_uc_from_url(cur)
            last_err = f"wrong/inaccessible channel — переключение не удалось: {channel_url}"
            logger.error("Не на нужном канале (попытка %d): ожидали %s, попали %s", attempt, uc_id, landed)
        elif not (_fast_channel_ready(driver, uc_id) if _fast_ui_enabled() else _studio_loaded(driver)):
            # Причину НЕ называем: тот же признак дают недогруз, транзиент переключателя
            # и чужой authuser. Отдельного признака страницы отказа в SEL пока нет —
            # утверждать «не тот Google-аккаунт» значит выдавать догадку за факт.
            last_err = (f"Studio не отдала рабочее состояние канала (доступ не подтверждён): {channel_url}")
            _d = _dbg_page()
            logger.warning("Рабочее состояние Studio не получено (попытка %d) · url=%s · текст=%s",
                           attempt, _d.get("u"), _d.get("t"))
        else:
            logger.info("Канал верифицирован + Studio загружена: %s (попытка %d)", uc_id, attempt)
            return uc_id, None
        if attempt < 3:
            logger.info("Повтор переключения с прогревом сессии (попытка %d→%d)...", attempt, attempt + 1)
            try:
                driver.get("https://www.youtube.com"); time.sleep(7)
            except Exception:
                pass
    recovered_uc, last_err = _try_account_recovery(driver, _UPLOAD_AUTH_CONTEXT.get("profile", ""),
                                                   uc_id, last_err, channel_label=name)
    if recovered_uc:
        return recovered_uc, None
    return None, last_err or f"switch failed after retries: {channel_url}"


def stop_profile(profile_id: str):
    try:
        adspower_rate_limit.get(f"{ADSPOWER_API}/api/v1/browser/stop", params=_ads_param(profile_id), timeout=30)
        logger.info("Профиль %s остановлен", profile_id)
    except Exception as e:
        logger.warning("Ошибка остановки: %s", e)


def _probe_duration(video_path):
    """Длительность локального файла через ffprobe.

    Сразу после публикации Studio отдаёт lengthSeconds = 0 (идёт обработка),
    поэтому для плана слотов берём длительность у самого файла.
    """
    if not video_path or not os.path.exists(video_path):
        return 0.0
    try:
        import subprocess
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out) if out else 0.0
    except Exception as e:
        logger.debug("_probe_duration: %s", e)
        return 0.0


def _place_midrolls(driver, video_id, chapters=None, step_sec=None, duration_sec=0):
    """Расставить рекламные слоты сразу после публикации.

    chapters — секунды начала глав (тогда слот ставится на +1 с от главы: попадает
    на смену кадра, YouTube засчитывает как High Quality Ad Break).
    Без глав — равномерная сетка каждые step_sec.

    Возвращает (успех, описание). Никогда не бросает: заливка уже состоялась.
    """
    if YS is None:
        return False, "нет yt_studio"
    try:
        if not duration_sec:
            duration_sec = _probe_duration(globals().get("_CURRENT_VIDEO_PATH", ""))
        if not duration_sec:
            # длительность берём из состояния строки видео (уже открытый список канала)
            try:
                row = next((v for v in YS.read_videos(driver)
                            if v.get("videoId") == video_id), None)
                duration_sec = float((row or {}).get("lengthSeconds") or 0)
            except Exception:
                duration_sec = 0
        plan = YS.plan_midrolls(duration_sec, chapters=chapters,
                                step_sec=step_sec or YS.MIDROLL_STEP_SEC)
        if not plan:
            return False, f"нечего ставить (длительность {duration_sec}с)"

        ok, why = YS.open_ad_breaks_editor(driver, video_id)
        if not ok:
            return False, why
        # сначала быстрый путь (запись всего набора в модель, доли секунды);
        # если модель не приняла — поштучная вставка кликами как раньше
        fast_ok, msg = YS.set_ad_slots_fast(driver, plan)
        if not fast_ok:
            logger.info("Мидроллы: быстрый путь не сработал (%s) — вставляю поштучно", msg)
            added, msg = YS.add_ad_slots(driver, plan)
            if not added:
                YS.discard_ad_breaks(driver)
                return False, msg
        saved, smsg = YS.save_ad_breaks(driver)
        return bool(saved), f"{msg}; {smsg}"
    except Exception as e:
        return False, f"мидроллы упали: {str(e)[:80]}"


def _post_publish_boost(driver, uc_id, video_id, do_monetize=False, ab_thumbs=None,
                        collaborator="", midrolls=False, chapters=None,
                        midroll_step=None):
    """Best-effort пост-публикация в ТОЙ ЖЕ selenium-сессии: монетизация (monet_cdp.run) +
    A/B-обложки (studio_ab_monet.set_ab_thumbnails). Один проход, без отдельного старта AdsPower.

    БЕЗОПАСНОСТЬ: вызывать ТОЛЬКО когда visibility уже подтверждён PUBLIC/SCHEDULED (не PENDING,
    не DRAFT). Обе операции правят страницы настроек (/monetization, /edit) — их Save НЕ трогает
    visibility-радио, поэтому запланированный ролик НЕ может уйти в Public раньше срока.
    Никогда не бросает (заливка уже успешна — это доп. шаги). Работает в отдельной вкладке.

    Ограничение A/B для SCHEDULE: обложки грузятся и тест АРМИТСЯ, но YouTube держит его
    Ineligible до выхода в эфир и активирует сам при публикации — дожимать не нужно."""
    out = {"monetized": False, "ab_test": False, "collaborator": False, "midrolls": False}
    if not video_id or (not do_monetize and not ab_thumbs and not collaborator and not midrolls):
        return out
    _tools = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools")
    if _tools not in sys.path:
        sys.path.insert(0, _tools)
    opened = False
    try:
        driver.switch_to.new_window("tab")
        opened = True
    except Exception:
        pass
    try:
        if do_monetize:
            try:
                import monet_cdp  # ленивый импорт: monet_cdp сам делает import upload_worker
                out["monetized"] = bool(monet_cdp.run(driver, uc_id, video_id))
                logger.info("Пост-публикация: монетизация=%s", out["monetized"])
            except Exception as e:
                logger.warning("Пост-публикация: монетизация упала: %s", e)
        if ab_thumbs:
            try:
                import studio_ab_monet
                out["ab_test"] = bool(studio_ab_monet.set_ab_thumbnails(driver, video_id, ab_thumbs))
                logger.info("Пост-публикация: A/B-тест=%s", out["ab_test"])
            except Exception as e:
                logger.warning("Пост-публикация: A/B упал: %s", e)
        if collaborator:
            # Приглашение соавтору. Уходит НАРУЖУ и необратимо, поэтому канал ищется
            # строго по точному @handle — при отсутствии совпадения функция откажет,
            # а не пригласит похожий канал (так однажды приглашение ушло чужому).
            # Соавтор увидит его как «Pending acceptance», пока не примет.
            try:
                if YS is None:
                    logger.warning("Пост-публикация: коллаборация пропущена — нет yt_studio")
                else:
                    ok, msg = YS.set_collaborator(driver, video_id, collaborator, save=True)
                    out["collaborator"] = bool(ok)
                    logger.info("Пост-публикация: коллаборация=%s · %s", ok, str(msg)[:110])
            except Exception as e:
                logger.warning("Пост-публикация: коллаборация упала: %s", e)
        if midrolls:
            # Реклама ставится сразу при заливке: по главам (если есть таймкоды)
            # либо равномерной сеткой. Плотная сетка ловит показы при перемотке
            # и не даёт алгоритму простаивать после frequency capping.
            try:
                ok_m, msg_m = _place_midrolls(driver, video_id, chapters=chapters,
                                              step_sec=midroll_step)
                out["midrolls"] = bool(ok_m)
                logger.info("Пост-публикация: мидроллы=%s · %s", ok_m, str(msg_m)[:110])
            except Exception as e:
                logger.warning("Пост-публикация: мидроллы упали: %s", e)
    finally:
        if opened:
            try:
                driver.close()
                driver.switch_to.window(driver.window_handles[0])
            except Exception:
                pass
    return out


# Рабочий номер authuser ПО КАНАЛУ: {uc_id: "N"} — тот, под которым Studio этого канала
# реально открылась при верификации. Раньше номер был один на процесс, и подобранный для
# канала A применялся к каналу B (раннеры гоняют список каналов в ОДНОМ процессе):
# список видео канала B открывался под чужим аккаунтом → ноль строк → dedup «не нашёл»
# → повторная заливка и дубль. Заполняется только после верификации того же UC.
_ACTIVE_AU = {}


def _au_suffix(channel_url: str, uc_id: str = "") -> str:
    """`?authuser=N` для СЛУЖЕБНЫХ страниц Studio (список видео) — по КАНАЛУ.

    Считалось, что заход через ?authuser=N залипает на сессию и дальше можно ходить по
    обычным URL. Это верно НЕ всегда: 02.08.2026 список видео, открытый без параметра,
    отдавал «Oops, you don't have permission to view this page» и ноль строк, а с
    параметром — 30 строк на том же канале в том же окне. Из-за этого finalize честно
    рапортовал «черновик не найден (no-row)» по существующему черновику (Stín, Tajemnice).

    Верифицированный номер отдаётся ТОЛЬКО для своего uc_id; иначе — номер из URL канала.
    Пусто, если ни того, ни другого нет — тогда поведение прежнее.
    """
    au = _ACTIVE_AU.get(uc_id) if uc_id else None
    if au is not None:
        return f"?authuser={au}"
    m = re.search(r"[?&]authuser=(\d+)", channel_url or "")
    return "?authuser=%s" % m.group(1) if m else ""


def _videos_url(uc_id, channel_url):
    return f"https://studio.youtube.com/channel/{uc_id}/videos/upload{_au_suffix(channel_url, uc_id)}"


def _row_by_state(driver, uc_id, channel_url, needle, video_id="", timeout=180):
    """Строка залитого ролика в списке канала + её статус. (row, kind) либо (None, None).

    ЕДИНСТВЕННЫЙ честный источник вердикта заливки: свойство `.video` у
    ytcp-video-row (draftStatus / privacy / visibility.userSetVisibility) — машинные
    константы, одинаковые на любом языке интерфейса. Ни закрытие мастера, ни текст
    финального диалога, ни слова в строке статусом не являются: 06–07.08 именно
    текстовые признаки объявили провальными успешные заливки.

    Ждём появления КОНКРЕТНОЙ строки, а не спим фиксированные 6с: свежий ролик
    попадает в список с задержкой, а пустой список — это «не прочитал», а не «нет
    ролика», и принимать его за факт нельзя (иначе следующий прогон зальёт дубль).
    """
    if YS is None:
        logger.error("Вердикт по состоянию невозможен: модуль yt_studio не загружен "
                     "(он обязателен — текстовые признаки статуса врут)")
        return None, None
    try:
        driver.get(_videos_url(uc_id, channel_url))
    except Exception as e:
        logger.warning("Переход к списку видео канала не удался: %s", e)

    _tick = {"n": 0}

    def _look(d):
        # раз в ~30с перечитываем страницу: только что залитая строка приходит в список
        # при загрузке страницы, и без перезахода ожидание могло крутиться вхолостую
        _tick["n"] += 1
        if _tick["n"] > 1 and _tick["n"] % 6 == 1:
            try:
                d.get(_videos_url(uc_id, channel_url))
            except Exception as e:
                logger.debug("_row_by_state: перезагрузка списка не удалась: %s", e)
        rows = YS.read_videos(d) or []
        if not rows:
            return None                      # список ещё не отрисован
        if video_id:
            v = next((r for r in rows if r.get("videoId") == video_id), None)
            if v:
                # Сверяем заголовок: id мог прийти устаревшим из мастера (профиль
                # долго жил открытым) и указать на ЧУЖОЙ ролик канала. 12.08 так и
                # вышло — вердикт вынесли по свежеопубликованному чужому видео,
                # хотя нашей записи на канале не было вовсе. При удачном совпадении
                # статуса это дало бы ЛОЖНЫЙ УСПЕХ: очередь закрыла бы задачу и
                # удалила исходники ролика, которого нет.
                if YS.find_video(d, needle, videos=[v])[0] is not None:
                    return v
                logger.warning(
                    "id=%s ведёт на ЧУЖОЙ ролик («%s») — не наш, вердикт по нему не выношу",
                    video_id, str(v.get("title"))[:60])
        v, _ = YS.find_video(d, needle, videos=rows)
        return v

    row = YS.wait_until(driver, _look, timeout=timeout, poll=5,
                        desc="строка ролика в списке канала")
    if not row:
        return None, None
    kind = YS.classify(row)
    logger.info("Состояние строки: id=%s → %s (draft=%s privacy=%s userSet=%s)",
                row.get("videoId"), kind, row.get("draftStatus"),
                row.get("privacy"), row.get("userSetVisibility"))
    return row, kind


def _processing_public_pending_allowed(row, *, visibility, schedule_date, video_id):
    """PUBLIC_PENDING requires the requested PUBLIC intent on the exact row.

    The result contract has no PRIVATE/UNLISTED/SCHEDULE pending success states;
    those requests remain PROCESSING failures with their known no-reupload ID.
    """
    return bool(
        visibility == "PUBLIC" and schedule_date is None
        and isinstance(video_id, str)
        and upload_contract.VIDEO_ID_RE.fullmatch(video_id)
        and isinstance(row, dict) and row.get("videoId") == video_id
        and row.get("draftStatus") == "DRAFT_STATUS_NONE"
        and row.get("privacy") == "VIDEO_PRIVACY_PUBLIC"
        and row.get("userSetVisibility") == "VIDEO_USER_SET_VISIBILITY_PUBLIC"
        and row.get("effectiveStatus") == "VIDEO_VISIBILITY_STATUS_UPLOADING_OR_PROCESSING"
    )


def _early_public_pending_allowed(
    row, *, enabled, strict, visibility, schedule_date, video_id,
    verified_channel_uc, current_url, requested_title, upload_complete_proven,
    thumbnail_proof, thumbnail_sha256, thumbnail_filename, public_intent_confirmed,
):
    """Skip only the settle wait; existing final row/title/result gates still run."""
    if (enabled is not True or strict is not True or visibility != "PUBLIC"
            or schedule_date is not None or upload_complete_proven is not True
            or public_intent_confirmed is not True or not isinstance(row, dict)
            or upload_contract.VIDEO_ID_RE.fullmatch(video_id or "") is None
            or upload_contract.UC_RE.fullmatch(verified_channel_uc or "") is None):
        return False
    location = urlparse(current_url or "")
    if (location.scheme != "https" or location.hostname != "studio.youtube.com"
            or location.path != f"/channel/{verified_channel_uc}/videos/upload"
            or row.get("videoId") != video_id
            or row.get("draftStatus") != "DRAFT_STATUS_NONE"
            or row.get("privacy") != "VIDEO_PRIVACY_PUBLIC"
            or row.get("userSetVisibility") != "VIDEO_USER_SET_VISIBILITY_PUBLIC"
            or row.get("effectiveStatus") != "VIDEO_VISIBILITY_STATUS_UPLOADING_OR_PROCESSING"
            or not _exact_title_proof(requested_title, row.get("title"))["matches"]
            or YS is None or YS.classify(row) != "PROCESSING"):
        return False
    proof = thumbnail_proof if isinstance(thumbnail_proof, dict) else {}
    if (proof.get("event") != "thumbnail_handoff_verified"
            or proof.get("video_id") != video_id or proof.get("channel_uc") != verified_channel_uc
            or proof.get("filename") != thumbnail_filename
            or not thumbnail_filename or re.fullmatch(r"[a-f0-9]{64}", thumbnail_sha256 or "") is None
            or proof.get("thumbnail_sha256") != thumbnail_sha256):
        return False
    try:
        return _strict_thumbnail_ready(proof.get("studio_state"), video_id=video_id,
                                       channel_uc=verified_channel_uc, filename=thumbnail_filename)
    except RuntimeError:
        return False  # uncertain evidence keeps the original settle/final verdict path


def _settle_row(driver, uc_id, channel_url, video_id, want, timeout=120):
    """Дождаться, пока строка ролика придёт в одно из ожидаемых состояний `want`.

    Studio обновляет список НЕ мгновенно: сразу после финализации ролик ещё
    показывается черновиком, хотя расписание уже принято. Раньше это ждали тремя
    sleep(20) и сравнением заголовков сырыми строками — Studio отдаёт заголовок в
    NFD, наш в NFC, поэтому на любой диакритике равенство не выполнялось НИКОГДА, и
    запланированный ролик рапортовался провалом. Здесь ждём смену состояния
    КОНКРЕТНОЙ строки по videoId. Возвращает (row, kind) либо (None, None).
    """
    if YS is None or not video_id:
        return None, None

    def _check(d):
        try:
            d.get(_videos_url(uc_id, channel_url))
        except Exception as e:
            logger.debug("_settle_row: перезагрузка списка не удалась: %s", e)
            return None
        v = next((r for r in (YS.read_videos(d) or []) if r.get("videoId") == video_id), None)
        return v if (v and YS.classify(v) in want) else None

    row = YS.wait_until(driver, _check, timeout=timeout, poll=8,
                        desc="ожидаемое состояние строки (%s)" % "/".join(sorted(want)))
    return (row, YS.classify(row)) if row else (None, None)


def upload_to_youtube(
    video_path: str, title: str, description: str, profile_id: str,
    thumbnail_path: str = "",
    # Полчаса на проверки YouTube хватает мелким роликам, а наши идут по 300 МБ и
    # час длиной: на них Studio доходит до шага видимости дольше, и заливщик сдавался,
    # оставляя на канале черновик. Срок вынесен в настройку, чтобы его можно было
    # поднять под свои ролики, не трогая код.
    headless: bool = False, timeout: int = int(os.environ.get("UPLOAD_VISIBILITY_TIMEOUT", "1800")),
    schedule_date: datetime = None,
    channel_url: str = "",
    finalize_draft: bool = False,
    upload_only: bool = False,
    search_title: str = "",
    monetize: bool = False,
    ab_thumbnails: list = None,
    collaborator: str = "",
    midrolls: bool = False,
    chapters: list = None,
    midroll_step: int = 0,
    reconcile_video_id: str = "",
    finalize_video_id: str = "",
) -> dict:
    """finalize_draft=True: НЕ заливать файл, а найти уже залитый ЧЕРНОВИК с таким title,
    открыть его мастер заново («Edit draft» = чистый рендер, лечит пустой диалог) и
    до-планировать (schedule/обложка/altered). Если уже Scheduled/Public — идемпотентный skip.

    upload_only=True (Фаза A): только ЗАЛИТЬ файл (resume прерванного / fresh отсутствующего) +
    обложка/altered, оставить ЧЕРНОВИКОМ (не планировать). Проверки YouTube (copyright ~10мин для
    длинных роликов) бегут в фоне; планирование — отдельной Фазой B, когда проверки уже прошли."""
    """Загружает видео на YouTube через AdsPower-профиль и навигацию по URL канала.

    channel_url — URL канала (https://www.youtube.com/channel/UC... или /@handle).
    Воркер навигирует ПРЯМО в Studio этого канала и верифицирует доступ; при провале
    верификации — ABORT без загрузки.

    Returns {success, url, error, visibility}.
    """

    _reset_upload_phase(emit=True)
    globals()["_UPLOAD_AUTH_CONTEXT"] = {"profile": str(profile_id),
                                       "channel_uc": _extract_uc_from_url(channel_url) or ""}
    _strict = os.getenv("VIDEO_UPLOAD_STRICT") == "1"
    _strict_private = _strict and VISIBILITY == "PRIVATE"
    if _strict_private and schedule_date is not None:
        return upload_contract.failure_result("strict_private_schedule_prohibited", _current_upload_phase())
    _early_pending_enabled = _strict and os.getenv("VIDEO_UPLOAD_EARLY_PUBLIC_PENDING") == "1"
    if finalize_video_id:
        if upload_contract.VIDEO_ID_RE.fullmatch(str(finalize_video_id)) is None:
            return upload_contract.failure_result(
                "finalize_video_id_invalid", _current_upload_phase()
            )
        globals()["_FINALIZE_ONLY_VIDEO_ID"] = finalize_video_id
        _mark_known_video_id(finalize_video_id)
        if not _strict or not finalize_draft or upload_only or reconcile_video_id:
            return upload_contract.failure_result(
                "finalize_video_id requires strict mode and --finalize-draft only",
                _current_upload_phase(),
            )
    if _strict:
        try:
            _strict_thumbnail_preflight(
                thumbnail_path, required=not bool(reconcile_video_id)
            )
        except ValueError as exc:
            return upload_contract.failure_result(str(exc), _current_upload_phase())
        if YS is None:
            return upload_contract.failure_result(
                "strict_state_reader_unavailable: yt_studio is required",
                _current_upload_phase(),
            )

    # YouTube заголовок ≤100 символов. Длиннее → шаг Details в ошибке («Title too long»),
    # кнопка Next заблокирована → заливка молча застревает и видео остаётся черновиком.
    # Обрезаем по границе слова ОДИН раз тут — защищает все пути (fresh/finalize/resume/dedup).
    if title and len(title) > 100:
        _cut = title[:100]
        _sp = _cut.rfind(" ")
        title = (_cut[:_sp] if _sp > 40 else _cut).rstrip(" ,;:—-")
        logger.warning("Заголовок >100 символов — обрезан до %d: %s", len(title), title)

    # search_title: по чему ИСКАТЬ черновик/дубль (если заголовок черновика ≠ желаемому,
    # напр. черновик сохранён под именем файла, а публикуем с локализованным title).
    _st = search_title if search_title else title
    _wizard_midrolls = 0     # пауз уже поставлено в мастере (повтор не нужен)
    _wizard_vid = ""         # videoId нашего ролика, прочитанный из мастера [дамп 2026-08-07]
    _schedule_source = schedule_date
    _schedule_exact_epoch = None
    _schedule_mode = "none"
    _schedule_browser_tz = ""

    globals()["_CURRENT_VIDEO_PATH"] = video_path or ""
    if schedule_date is not None and not _is_aware_datetime(schedule_date):
        return {
            "success": False,
            "url": "",
            "error": "schedule must be timezone-aware absolute datetime",
            "visibility": "unknown",
        }
    if reconcile_video_id and upload_contract.VIDEO_ID_RE.fullmatch(
        str(reconcile_video_id)
    ) is None:
        return {
            "success": False,
            "url": "",
            "error": "reconcile_video_id_invalid",
            "visibility": "unknown",
        }
    if reconcile_video_id and schedule_date is None:
        return {
            "success": False,
            "url": "",
            "error": "reconcile_exact requires absolute schedule; fresh upload forbidden",
            "visibility": "unknown",
        }
    if not finalize_draft and not reconcile_video_id and not os.path.exists(video_path):
        return {"success": False, "url": "", "error": f"Видео не найдено: {video_path}", "visibility": "unknown"}

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.common.action_chains import ActionChains
    except ImportError:
        return {"success": False, "url": "", "error": "pip install selenium", "visibility": "unknown"}

    # 1. Запускаем профиль AdsPower с retry-backoff: локальный API лимитирован (~1 req/sec) →
    #    "Too many request per second" / AggregateError при частых стартах. Ждём и повторяем.
    selenium_addr = None
    chromedriver_path = ""
    _start_err = None

    # Профиль мог остаться запущенным с прошлого раза — тогда повторный start отдаёт
    # AggregateError, хотя браузер живой и к нему можно просто присоединиться.
    try:
        _act = adspower_rate_limit.get(f"{ADSPOWER_API}/api/v1/browser/active",
                            params=_ads_param(profile_id), timeout=20).json()
        if _act.get("code") == 0 and (_act.get("data") or {}).get("status") == "Active":
            selenium_addr = ((_act["data"].get("ws") or {}).get("selenium")) or ""
            chromedriver_path = _act["data"].get("webdriver", "") or ""
            if selenium_addr:
                logger.info("Профиль %s уже запущен — присоединяюсь без рестарта", profile_id)
    except Exception as _ae:
        logger.debug("browser/active: %s", _ae)

    try:
        _require_retained_finalize_endpoint(
            selenium_addr, os.getenv("VIDEO_UPLOAD_FINALIZE_EXISTING_PORT", ""),
            strict=_strict, finalize_video_id=finalize_video_id)
    except RuntimeError as exc:
        return upload_contract.failure_result(str(exc), _current_upload_phase())

    for _att in range(1, 9 if not selenium_addr else 0):
        try:
            # AggregateError у AdsPower — почти всегда «дёрнули слишком рано»: профиль
            # ещё останавливается после прошлого запуска. Лечится ожиданием, не спешкой.
            time.sleep(3)
            resp = adspower_rate_limit.get(f"{ADSPOWER_API}/api/v1/browser/start", params={
                **_ads_param(profile_id), "open_tabs": 1, "ip_tab": 0,
                "headless": 1 if headless else 0,
            }, timeout=120)
            data = resp.json()
            if data.get("code") == 0:
                selenium_addr = data["data"]["ws"]["selenium"]
                chromedriver_path = data["data"].get("webdriver", "")
                logger.info("Профиль %s запущен (попытка %d)", profile_id, _att)
                break
            _start_err = f"AdsPower: {data.get('msg')}"
        except Exception as e:
            _start_err = str(e)
        # rate-limit / транзиент → backoff и повтор (кап 25с: дальше расти незачем,
        # профиль либо освободится, либо проблема не во времени)
        _wait = min(25, 4 * _att)
        logger.warning("browser/start неуспех (попытка %d/8): %s — жду %dс", _att, str(_start_err)[:60], _wait)
        time.sleep(_wait)
    if not selenium_addr:
        return {"success": False, "url": "", "error": _start_err or "browser/start failed", "visibility": "unknown"}
    if _current_upload_phase()["phase"] == "preflight":
        _advance_upload_phase("profile_opened")

    driver = None
    _file_handoff_attempted = False
    _upload_complete_proven = False
    _thumbnail_proof = None
    _terminal_failure = {}
    try:
        # 2. Подключаемся через Selenium
        opts = Options()
        if _strict:
            opts.page_load_strategy = "eager"
        opts.add_experimental_option("debuggerAddress", selenium_addr)
        driver = webdriver.Chrome(service=Service(chromedriver_path), options=opts) if chromedriver_path else webdriver.Chrome(options=opts)
        # Таймауты, чтобы не виснуть вечно на driver.get при «живом-но-замёрзшем» Chrome
        # (закрытая крышка Mac / half-open сокет на большом аплоаде) — иначе батч стоит без диагностики.
        try:
            driver.set_page_load_timeout(60 if _strict else 180)
            driver.set_script_timeout(120)
        except Exception:
            pass

        # ISO с offset задаёт абсолютный момент. Поля Studio, напротив, принимают
        # локальные wall-clock дату/время браузера. Поэтому после подключения к
        # конкретному AdsPower-профилю читаем ЕГО IANA timezone и лишь затем
        # вычисляем значения пикера. Offset нельзя просто снять через replace():
        # это и давало сдвиг эфира на несколько часов между Mac/Минском/профилем.
        if schedule_date:
            _schedule_browser_tz = _browser_timezone(driver)
            try:
                schedule_date, _schedule_exact_epoch, _schedule_mode = \
                    _schedule_for_browser_picker(schedule_date, _schedule_browser_tz)
            except ValueError as _tz_error:
                logger.error("Отложка: %s — aware-время не планирую", _tz_error)
                return {
                    "success": False,
                    "url": "",
                    "error": f"schedule_timezone_failed: {_tz_error}",
                    "visibility": "unknown",
                }
            logger.info(
                "Отложка: absolute %s (epoch=%s) → браузер %s → пикер %s",
                _schedule_source.isoformat(), _schedule_exact_epoch,
                _schedule_browser_tz, schedule_date.strftime("%Y-%m-%d %H:%M"),
            )
        # ФОНОВОЕ ОКНО. Chrome для неактивного окна глушит рендеринг и лейаут: элементы
        # существуют в DOM, но getBoundingClientRect отдаёт нули. В мастере это выглядит
        # как «поля 0×0» и «Название НЕ применилось (readback='no visible box')» — ролик
        # уезжает на канал с именем файла вместо заголовка. Именно это ломало заливку,
        # когда профили шли параллельно.
        # Лечится штатными командами CDP: страница считает себя видимой и в фокусе,
        # даже когда её окно перекрыто другим. Фокус ОС при этом не отбирается —
        # значит несколько профилей могут работать одновременно, не мешая друг другу.
        # Окно уже вьюпорта по умолчанию (у профилей AdsPower 1291 px): строки Studio
        # и диалоги вылезают за край — кнопка «Delete video» встаёт на x≈1307, «Done»
        # в диалоге видимости на x≈1247, и CDP-клик по координате уходит мимо экрана.
        # launch_args AdsPower размер НЕ меняет (берётся из настроек профиля),
        # а set_window_size из драйвера — меняет.
        try:
            driver.set_window_size(1600, 1000)
        except Exception as _we:            # noqa: BLE001
            logger.debug("не удалось расширить окно: %s", _we)

        for _cmd, _args in (
            ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            ("Page.setWebLifecycleState", {"state": "active"}),
            ("Emulation.setAutoDarkModeOverride", {"enabled": False}),
        ):
            try:
                driver.execute_cdp_cmd(_cmd, _args)
            except Exception as _ce:      # noqa: BLE001
                logger.debug("CDP %s не принят: %s", _cmd, _ce)

        wait = WebDriverWait(driver, 30)
        _dump_page(driver, "старт")

        # 3. РОУТИНГ ПО URL КАНАЛА + ВЕРИФИКАЦИЯ (заменяет switch_channel)
        logger.info("Навигирую на канал по URL: %s", channel_url)
        uc_id, route_err = _navigate_to_channel_studio(driver, channel_url)
        if route_err or not uc_id:
            msg = route_err or f"wrong or inaccessible channel: {channel_url}"
            logger.error("ABORT (роутинг): %s — загрузка НЕ выполняется", msg)
            return {"success": False, "url": "", "error": msg, "visibility": "unknown"}

        # 3.5. Единая умная логика: RESUME прерванной загрузки / SKIP готового / открыть черновик /
        #      FRESH upload отсутствующего. Источник истины — список контента канала.
        _finalizing = False   # открыт реальный черновик (файл уже залит) → schedule-only, без re-upload
        _resumed = False      # дослали файл в ПРЕРВАННУЮ загрузку (resume)
        # videoId — единственный ключ, которым ролик можно вести без ошибок: заголовок
        # Studio отдаёт в NFD (наш в NFC), а подстрока матчит соседний ролик серии.
        _draft_vid = ""
        # состояние канала РЕАЛЬНО прочитано. False = дедуп не отработал, и свежую
        # заливку начинать нельзя: «список пуст» ≠ «ролика нет» (см. гейт ниже).
        _dedup_done = False
        # JS клика по «Edit draft» нужного ролика — переоткрывает мастер (чистый рендер).
        _EDIT_DRAFT_JS = r"""
            var needle = arguments[0].toLowerCase().substring(0,50);
            var dw=['edit draft','изменить черновик','редактировать черновик','entwurf bearbeiten','modifier le brouillon','modifica bozza','edytuj wersję roboczą','editar rascunho','editează schița','editează schita','upravit koncept'];
            var rows=document.querySelectorAll('ytcp-video-row, tr[id*=row]');
            for(var r of rows){
                var te=r.querySelector('#video-title,[id*=title-text]');
                var t=(te?te.textContent:'').trim().toLowerCase();
                if(!t||!t.includes(needle)) continue;
                var cands=r.querySelectorAll('a,button,ytcp-button,[role=button]');
                for(var c of cands){ var ct=(c.textContent||'').trim().toLowerCase();
                    for(var k=0;k<dw.length;k++){ if(ct.indexOf(dw[k])>=0){ try{c.click();}catch(e){} return 'edit:'+dw[k]; } } }
                var a=r.querySelector('a[href*="/video/"]'); if(a){ try{a.click();}catch(e){} return 'link'; }
                return 'row-no-edit';
            }
            return 'no-row';
        """

        def _open_draft(video_id, needle, tag="черновик"):
            """Открыть мастер черновика. Основное — по videoId, текст — только фолбэк.

            _EDIT_DRAFT_JS ищет строку подстрокой заголовка и кликает по НАДПИСИ
            «Edit draft» из списка на 11 языков. Две беды сразу: заголовок Studio
            отдаёт в NFD (наш в NFC) → на любой диакритике совпадения нет и свой
            черновик «не находится»; а при совпадении подстроки открывается ЧУЖОЙ
            ролик серии. videoId у нас уже прочитан из состояния — ведём по нему.
            Возвращает (успех, чем_открыли).
            """
            if YS is not None and video_id:
                try:
                    if YS.open_draft_wizard(driver, video_id):
                        if finalize_video_id:
                            observed_id = YS.wait_until(
                                driver, lambda d: (YS.read_wizard(d) or {}).get("videoId"),
                                timeout=20, poll=1.0, desc="exact draft video ID",
                            )
                            if observed_id != finalize_video_id:
                                return False, "exact-draft-video-id-mismatch"
                        return True, "state:%s" % video_id
                    logger.warning("%s: мастер по videoId=%s не открылся — текстовый фолбэк",
                                   tag, video_id)
                except Exception as _oe:
                    logger.warning("%s: open_draft_wizard(%s) упал: %s — текстовый фолбэк",
                                   tag, video_id, _oe)
            else:
                logger.warning("%s: videoId неизвестен (YS=%s) — открываю текстовым путём, "
                               "он может промахнуться мимо своей строки",
                               tag, "нет" if YS is None else "есть")
            if finalize_video_id:
                return False, "exact-id-open-failed; text fallback forbidden"
            # текстовому пути нужна именно страница со списком роликов (он ищет строку в ней)
            try:
                driver.get(_videos_url(uc_id, channel_url))
                if YS is not None:
                    YS.wait_videos(driver, timeout=45)
                else:
                    time.sleep(6)
            except Exception as _ne:
                logger.warning("%s: переход к списку роликов не удался: %s", tag, _ne)
            _r = driver.execute_script(_EDIT_DRAFT_JS, needle)
            _ok = bool(_r) and _r not in ("no-row", "row-no-edit")
            if _ok:
                time.sleep(6)
            return _ok, "text:%s" % _r

        try:
            if reconcile_video_id or finalize_video_id:
                driver.get(_videos_url(uc_id, channel_url))
            else:
                driver.get(f"https://studio.youtube.com/channel/{uc_id}/videos/upload{_au_suffix(channel_url, uc_id)}")
            if _strict:
                _require_auth_clear(driver, stage="content_before_wait")
            # Ждём ОТРИСОВКИ строк канала (ytcp-video-row.video), а не фиксированных 6с:
            # по нерасрисованной странице resume-проба и dedup читают пустоту, а пустой
            # список — это «не прочитал», а не «ролика нет» → второй заливкой лёг бы дубль.
            # Кап 30с: полное ожидание всё равно делает dedup ниже (wait_videos 60с),
            # а на пустом канале строк не будет никогда.
            if YS is not None:
                _initial_rows = YS.wait_videos(driver, timeout=30, expected_uc=uc_id)
            else:
                _initial_rows = None
                time.sleep(6)
            if reconcile_video_id:
                _mark_known_video_id(reconcile_video_id)
                if YS is None:
                    return {
                        "success": False,
                        "url": f"https://youtu.be/{reconcile_video_id}",
                        "video_id": reconcile_video_id,
                        "error": "reconcile_exact requires yt_studio state reader; fresh upload forbidden",
                        "visibility": "NOT_FOUND",
                    }
                _rows = _initial_rows
                if not _rows:
                    _rows = YS.wait_videos(driver, timeout=60)
                _matches = [
                    item for item in (_rows or [])
                    if isinstance(item, dict) and item.get("videoId") == reconcile_video_id
                ]
                if len(_matches) != 1:
                    return {
                        "success": False,
                        "url": f"https://youtu.be/{reconcile_video_id}",
                        "video_id": reconcile_video_id,
                        "error": (
                            "reconcile_exact did not find exactly one requested Studio row; "
                            "fresh upload forbidden"
                        ),
                        "visibility": "NOT_FOUND",
                    }
                return _verified_existing_scheduled_result(
                    uc_id,
                    _matches[0],
                    title,
                    _schedule_exact_epoch,
                    studio_state=YS.classify(_matches[0]),
                    schedule_mode=_schedule_mode,
                    picker_local=schedule_date,
                    browser_timezone=_schedule_browser_tz,
                )
            if finalize_draft:
                # дать ТРАНЗИЕНТНОЙ resume-модалке (от прерванных аплоадов) уйти — она мелькает
                # ~первые секунды и ломает resume-check, после чего dedup не видит готовые черновики.
                # Проба показала: к ~12с страница чистая, «Edit draft» доступен.
                # Ждать её исчезновения по состоянию нечем: подтверждённого дампом селектора
                # этой модалки в SEL/SELECTOR_MAP нет — отсюда пауза.
                time.sleep(8)

            # 3.6. RESUME ПРЕРВАННОЙ ЗАГРУЗКИ (сон мака/обрыв убил аплоад на середине): на /videos/upload
            #      авто-появляется resume-промпт с file input. Досылаем ТОТ ЖЕ файл → грузится в
            #      СУЩЕСТВУЮЩИЙ черновик (без дубля, live-проверено). ГЕЙТ: на странице есть НАШ тайтл —
            #      иначе прерван ДРУГОЙ ролик, и мы НЕ привязываем наш файл к чужому черновику.
            try:
                # Признак прерванной загрузки ищем по ВИДИМОЙ кнопке «Resume upload»
                # в строке ролика, а не по тексту страницы. На /videos/upload всегда
                # висит статичная подсказка «upload interrupted — click "Resume upload"…»,
                # и скрытый input[type=file] присутствует всегда — старое условие
                # (текст + любой file input) срабатывало ВХОЛОСТУЮ при каждой заливке
                # на такой канал и давало ложное «channel busy».
                rs = driver.execute_script(r"""
                    function deepAll(root){
                      var out=[], st=[root];
                      while(st.length){ var n=st.pop();
                        if(!n) continue;
                        if(n.nodeType===1) out.push(n);
                        var kids=n.children||[];
                        for(var i=0;i<kids.length;i++) st.push(kids[i]);
                        if(n.shadowRoot) st.push(n.shadowRoot);
                      }
                      return out;
                    }
                    var all=deepAll(document);
                    var resumeBtn=null, resumeRow=null;
                    for(var i=0;i<all.length;i++){
                      var e=all[i];
                      var tag=(e.tagName||'').toLowerCase();
                      if(tag!=='button' && tag!=='ytcp-button') continue;
                      var tx=((e.innerText||'')+' '+((e.getAttribute&&e.getAttribute('aria-label'))||'')).toLowerCase();
                      if(!/resume upload|возобнов|wznów|wznow|fortsetzen|reprendre|riprendi|retomar|reluare|pokra/.test(tx)) continue;
                      var r=e.getBoundingClientRect();
                      if(r.width<5||r.height<5) continue;      // скрытая кнопка-шаблон
                      resumeBtn=e;
                      var p=e;
                      while(p && (p.tagName||'').toLowerCase()!=='ytcp-video-row') p=p.parentElement||p.host;
                      resumeRow=p;
                      break;
                    }
                    var has=false;
                    var fis=document.querySelectorAll('input[type=file]');
                    for(var f of fis){ var a=(f.getAttribute('accept')||''); if(a.indexOf('image')<0){has=true;break;} }
                    // Ключ строки берём из САМОГО КОМПОНЕНТА (ytcp-video-row.video):
                    // videoId/title тут машинные и от языка интерфейса не зависят.
                    // Текстовые поля ниже остались только для фолбэка и лога.
                    var rvid='', rtitle='';
                    try{ var rvv = resumeRow ? resumeRow.video : null;
                         if(rvv){ rvid = rvv.videoId||''; rtitle = rvv.title||''; } }catch(e){}
                    var needle=arguments[0].toLowerCase().substring(0,40);
                    var rowText=resumeRow?(resumeRow.innerText||'').toLowerCase():'';
                    var pageText=(document.body.innerText||'').toLowerCase();
                    return {resume: !!resumeBtn, hasFileInput: has,
                            resumeVideoId: rvid, resumeTitle: rtitle,
                            hasTitle: resumeBtn ? (rowText.indexOf(needle)>=0)
                                                : (pageText.indexOf(needle)>=0)};
                """, title)
            except Exception:
                rs = {}

            # ЧЕЙ это прерванный аплоад — решаем сверкой videoId, а не вхождением сырого
            # текста. Заголовок в строке приходит в NFD, наш — в NFC, поэтому indexOf по
            # первым 40 символам промахивался на любой диакритике: СВОЙ недолитый ролик
            # получал hasTitle=false и уходил в «channel busy» навсегда (повтор давал тот
            # же результат). Текстовый признак остаётся только фолбэком — когда строка
            # прерванной загрузки не отдала своё состояние, и об этом пишем в лог.
            _resume_vid = (rs or {}).get("resumeVideoId") or ""
            _resume_mine = False
            if _strict and rs and rs.get("resume"):
                return _strict_existing_failure(
                    "strict_resume_requires_reconcile: interrupted upload exists; "
                    "automatic file handoff forbidden",
                    {"videoId": _resume_vid},
                )
            if rs and rs.get("resume"):
                if YS is not None and _resume_vid:
                    try:
                        _rrows = YS.wait_videos(driver, timeout=45)
                        _mine, _mine_kind = YS.find_video(driver, _st, videos=_rrows) if _rrows else (None, None)
                        _resume_mine = bool(_mine and _mine.get("videoId") == _resume_vid)
                        logger.info("RESUME: прерванная строка id=%s (%r); наш ролик по состоянию: id=%s → %s",
                                    _resume_vid, (rs.get("resumeTitle") or "")[:40],
                                    (_mine or {}).get("videoId") or "не найден", _mine_kind)
                    except Exception as _re_err:
                        logger.warning("RESUME: состояние списка не прочитано (%s) — принадлежность "
                                       "прерванной загрузки по тексту (фолбэк)", _re_err)
                        _resume_mine = bool(rs.get("hasTitle"))
                else:
                    logger.warning("RESUME: videoId прерванной строки не прочитан (YS=%s) — "
                                   "принадлежность по тексту (фолбэк, возможен промах на диакритике)",
                                   "нет" if YS is None else "есть")
                    _resume_mine = bool(rs.get("hasTitle"))

            if (rs and rs.get("resume") and rs.get("hasFileInput") and _resume_mine
                    and video_path and os.path.exists(video_path)):
                _draft_vid = _resume_vid or _draft_vid
                _mark_known_video_id(_draft_vid)
                logger.info("RESUME: прерванная загрузка нашего ролика — досылаю файл %s (%dМБ)",
                            os.path.basename(video_path), os.path.getsize(video_path)//1024//1024)
                for fi in driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]'):
                    if 'image' in (fi.get_attribute('accept') or ''):
                        continue
                    try:
                        _send_video_file_at_upload_boundary(fi, video_path, driver=driver)
                        break
                    except Exception:
                        pass
                # ФАКТ «мастер принял файл» — состояние аплоада (uploadDetailsByFrontendId /
                # isVideoUploaded, SELECTOR_MAP §ytcp-uploads-dialog), а не ОДИН снимок текста
                # страницы ровно через 10с: на медленной досылке тайтл ещё не отрисован, и
                # исправный черновик получал ложный ABORT.
                _att = False
                if YS is not None:
                    def _accepted(d):
                        if YS.read_upload_progress(d):        # запись в uploadDetailsByFrontendId
                            return True
                        return YS.read_wizard(d).get("isVideoUploaded") is True
                    _att = bool(YS.wait_until(driver, _accepted, timeout=min(timeout, 180),
                                              poll=3.0, desc="мастер принял досланный файл"))
                    if not _att:
                        # состояние аплоада молчит → принадлежность проверяем по СТРОКЕ ролика
                        # (find_video нормализует NFD→NFC), а не по document.body.innerText
                        try:
                            _row_mine, _ = YS.find_video(driver, _st)
                            _att = bool(_row_mine and (not _draft_vid
                                                       or _row_mine.get("videoId") == _draft_vid))
                            logger.warning("RESUME: мастер прогресс не отдал — принадлежность по "
                                           "строке ролика: %s", "наш" if _att else "не подтверждена")
                        except Exception as _fe:
                            logger.warning("RESUME: строку ролика не прочитал (%s)", _fe)
                else:
                    logger.warning("RESUME: yt_studio нет — факт досылки проверяю по тексту "
                                   "страницы (фолбэк, промахивается на диакритике)")
                    time.sleep(10)
                    _att = driver.execute_script("""
                        var needle=arguments[0].toLowerCase().substring(0,40);
                        return ((document.body.innerText||'').toLowerCase().indexOf(needle)>=0);
                    """, title)
                if not _att:
                    logger.error("RESUME: досылка файла не подтверждена состоянием — ABORT (защита от чужого черновика)")
                    save_blackbox(driver, "resume-send-not-accepted")
                    return {"success": False, "url": "", "error": "resume: file not accepted by wizard", "visibility": "DRAFT"}
                if not _wait_file_processing(driver, timeout, video_id=_draft_vid, needle=_st):
                    # Недолитый файл финализировать нельзя: Publish/Schedule по нему уходит
                    # в Draft. Черновик оставляем как есть — следующий проход дошлёт остаток.
                    logger.error("RESUME: завершение дозагрузки НЕ подтверждено состоянием — "
                                 "финализацию не начинаю, черновик остаётся")
                    save_blackbox(driver, "resume-upload-not-finished")
                    return {"success": False, "url": "",
                            "error": "resume: дозагрузка не подтверждена (isVideoUploaded/prechecks) — финализация отменена",
                            "visibility": "DRAFT"}
                _resumed = True
                if upload_only:
                    logger.info("UPLOAD_ONLY: файл дозагружен (resume) — оставляю ЧЕРНОВИКОМ (проверки в фоне)")
                    return {"success": True, "url": "DRAFT_UPLOADED", "error": "", "visibility": "DRAFT_UPLOADED"}
                logger.info("RESUME: файл дозагружен — планирую через «Edit draft»")
                time.sleep(4)
                driver.get(f"https://studio.youtube.com/channel/{uc_id}/videos/upload{_au_suffix(channel_url, uc_id)}")
                time.sleep(6)

            if _resumed:
                # После resume черновик ТОЧНО есть → переоткрыть мастер по videoId той же
                # строки, в которую мы дослали файл (текстовый клик мог бы открыть соседний).
                # needle для фолбэка — _st (по чему ищем черновик), как во всём дедупе:
                # у прерванной загрузки заголовок часто = имя файла, а не наш title
                _ok_ed, _ed = _open_draft(_draft_vid, _st, "RESUME→FINALIZE")
                logger.info("RESUME→FINALIZE: черновик открыт = %s", _ed)
                if not _ok_ed:
                    return {"success": False, "url": "", "error": f"resume done, но Edit draft не открылся ({_ed})", "visibility": "DRAFT"}
                _finalizing = True
            else:
                # SAFETY: на канале висит ЧУЖАЯ прерванная загрузка (resume-промпт есть, но НЕ наш тайтл)?
                # Тогда file input на странице — от ЧУЖОГО черновика; Create/file-send может дослать наш
                # файл туда → порча чужого черновика. Откладываем: на повторном проходе (после resume того
                # ролика) этот зайдёт чисто.
                if rs and rs.get("resume") and rs.get("hasFileInput") and not _resume_mine:
                    if os.getenv("IGNORE_INTERRUPTED") == "1":
                        logger.warning("IGNORE_INTERRUPTED=1: чужой прерванный аплоад ЕСТЬ — лью СВЕЖАК поверх (старый черновик НЕ трогаю)")
                    else:
                        logger.warning("На канале ДРУГАЯ прерванная загрузка (не наш тайтл) — откладываю ролик (retry позже)")
                        return {"success": False, "url": "", "error": "channel busy: foreign interrupted upload — retry later", "visibility": "BUSY"}
                # 3.65. (НЕ resume) Закрыть чужую resume/Wrong-file модалку, чтобы не блокировала.
                try:
                    # для finalize resume-dismiss НЕ нужен (черновик открываем через dedup→Edit draft);
                    # его deepAll-детектор ложно срабатывает на скрытом shadow-тексте и кликает не ту кнопку.
                    _fast_resume_guard = _strict and _fast_ui_enabled()
                    _rd = "none" if finalize_draft else (_wait_visible_resume_disposition(driver, uc_id)
                        if _fast_resume_guard else driver.execute_script(r"""
                        function deepAll(root){var o=[];var e=root.querySelectorAll('*');for(var i=0;i<e.length;i++){o.push(e[i]);if(e[i].shadowRoot)o=o.concat(deepAll(e[i].shadowRoot));}return o;}
                        var all=deepAll(document);
                        var hit=false;
                        for(var k=0;k<all.length;k++){var tx=(all[k].textContent||'').toLowerCase();
                            if(tx.indexOf('resume upload')>=0||tx.indexOf('wrong file')>=0||tx.indexOf('возобнов')>=0||tx.indexOf('продолжить загруз')>=0||tx.indexOf('fortsetzen')>=0||tx.indexOf('reprendre')>=0||tx.indexOf('reanudar')>=0){hit=true;break;}}
                        if(!hit) return 'none';
                        var CANC=['cancel','отмена','abbrechen','annuler','annulla','anuluj','cancelar','anulează','zrušit','zrusit'];
                        for(var j=0;j<all.length;j++){var b=all[j];var t=(b.innerText||b.textContent||'').trim().toLowerCase();
                            if(t.length<=14 && CANC.indexOf(t)>=0){var r=b.getBoundingClientRect(); if(r.width>0&&r.height>0){try{b.click();}catch(x){} return 'cancel:'+t;}}}
                        return 'resume-no-cancel';
                    """))
                    if _fast_resume_guard and _rd != "none":
                        logger.warning("FAST_UI: %s — файл не отправляю, resume-диалог не изменяю", _rd)
                        return {"success": False, "url": "", "visibility": "BUSY",
                                "error": "channel busy: " + _rd + "; no Cancel/Escape/reload or file handoff"}
                    if _rd and _rd != 'none' and not finalize_draft:
                        logger.warning("Закрыл чужую resume/Wrong-file модалку (%s) — перезагружаю upload-страницу", _rd)
                        try:
                            driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
                        except Exception:
                            pass
                        time.sleep(2)
                        driver.get(f"https://studio.youtube.com/channel/{uc_id}/videos/upload{_au_suffix(channel_url, uc_id)}")
                        if _strict:
                            _require_auth_clear(driver, stage="content_after_resume_dismiss")
                        time.sleep(8)
                except StrictAuthGateError:
                    raise
                except Exception as _rde:
                    logger.debug("resume-dismiss: %s", _rde)

                # 3.7. DEDUP: видео с таким title уже на канале?
                # (отладочный дамп всех строк канала убран: он печатал простыню на
                #  каждую заливку, а решение теперь принимается по состоянию компонента —
                #  при промахе dedup улики собирает save_blackbox)
                # --- DEDUP по СОСТОЯНИЮ компонента (основной путь) --------------------
                # Studio отдаёт статус машинными константами (DRAFT_STATUS_*, VIDEO_PRIVACY_*),
                # одинаковыми на всех языках. Старый текстовый путь ниже держит regex по 9
                # языкам и, например, НЕ знает испанского «Borrador» / нидерландского
                # «Concept» → на таком канале черновик не распознавался и ролик заливался
                # заново. Плюс сравнение заголовков тут NFC-нормализовано (Studio отдаёт
                # диакритику в NFD — прямое сравнение промахивалось).
                existing = None
                if YS is not None:
                    try:
                        # Пустой список — это «канал не прочитан», а НЕ «ролика нет».
                        # Принять пустоту за факт = залить второй раз поверх своего же ролика.
                        _rows = YS.wait_videos(driver, timeout=60, expected_uc=uc_id)
                        if not _rows:
                            # один честный повтор с перезаходом: строки приходят при загрузке
                            # страницы, и подвисшая отрисовка лечится именно перезаходом
                            logger.warning("DEDUP: список канала пуст — перезахожу и читаю ещё раз")
                            driver.get(_videos_url(uc_id, channel_url))
                            if _strict:
                                _require_auth_clear(driver, stage="content_dedup_retry")
                            _rows = YS.wait_videos(driver, timeout=60, expected_uc=uc_id)
                        if not _rows:
                            # Пустой список бывает двух разных природ, и раньше обе считались
                            # сбоем: «строки не отрисовались» и «канал новый, роликов нет».
                            # У второй Studio показывает свой пустой экран («No content
                            # available» + кнопка загрузки) — это ПРОЧИТАННЫЙ ноль, и без
                            # него первый ролик на свежий канал залить нельзя в принципе.
                            if hasattr(YS, "список_пуст_подтверждён") and YS.список_пуст_подтверждён(driver):
                                logger.info("DEDUP: Studio показывает пустой список — "
                                            "на канале роликов нет, свежая заливка")
                                _rows = []
                            else:
                                raise RuntimeError("список видео канала пуст — состояние не прочитано")
                        if _rows is None:
                            raise RuntimeError("список видео канала пуст — состояние не прочитано")
                        if finalize_video_id:
                            try:
                                _v = _strict_finalize_row(_rows, finalize_video_id, title)
                            except ValueError as exc:
                                return _strict_existing_failure(
                                    str(exc), {"videoId": finalize_video_id}
                                )
                            _st_kind = "DRAFT"
                        else:
                            _v, _st_kind = YS.find_video(driver, _st, videos=_rows)
                        if _v:
                            if _strict:
                                _blocker = _strict_existing_blocker(
                                    _v, title, _st_kind,
                                    "DRAFT" if finalize_video_id else (
                                        "SCHEDULED" if schedule_date is not None else VISIBILITY
                                    ),
                                )
                                if _blocker:
                                    return _strict_existing_failure(_blocker, _v)
                                if thumbnail_path and not finalize_video_id:
                                    return _strict_existing_failure(
                                        "strict_existing_thumbnail_unverified: exact video exists; "
                                        "custom thumbnail readback required; fresh upload forbidden",
                                        _v,
                                    )
                            _draft_vid = _v.get("videoId", "")
                            _mark_known_video_id(_draft_vid)
                            logger.info("DEDUP (состояние): %r → %s (id=%s, prechecks=%s)",
                                        _v.get("title", "")[:45], _st_kind, _draft_vid,
                                        _v.get("prechecksDone"))
                            if _st_kind == "DRAFT":
                                existing = "DRAFT_EXISTS"
                            elif _st_kind in ("PUBLIC", "SCHEDULED", "UNLISTED", "PRIVATE"):
                                existing = f"https://youtu.be/{_draft_vid}" if _draft_vid else "yes"
                            elif _st_kind == "PROCESSING":
                                # файл уже залит и обрабатывается — повторная заливка = дубль
                                existing = "DRAFT_EXISTS"
                        else:
                            logger.info("DEDUP (состояние): %r на канале не найден — свежая заливка",
                                        (_st or "")[:45])
                        # состояние канала прочитано — только теперь решение «лить/не лить» опирается на факт
                        _dedup_done = True
                    except StrictAuthGateError:
                        raise
                    except Exception as _se:
                        # Раньше здесь писалось «падаю на текстовый путь», но текстовая ветка
                        # выполняется только при YS is None — то есть дедуп просто отключался,
                        # и код молча уходил в свежую заливку, кладя на канал ДУБЛЬ.
                        logger.error("DEDUP по состоянию не выполнен (%s) — проверка дублей НЕ сделана", _se)
                        existing = None

                if existing is None and YS is None:
                    existing = driver.execute_script("""
                    var needle = arguments[0].toLowerCase().substring(0, 50);
                    var rows = document.querySelectorAll('ytcp-video-row, tr[id*=row]');
                    for (var r of rows) {
                        var titleEl = r.querySelector('#video-title, [id*=title-text]');
                        var t = (titleEl?.textContent || '').trim().toLowerCase();
                        if (!t || !t.includes(needle)) continue;
                        var rowText = r.textContent.trim();
                        var isScheduled = /Scheduled|Запланир|naplánov|geplant|programmé|programmat|zaplanow|agendad|programat/i.test(rowText);
                        var isPublic = /Public|Открыт|Veřejn|Öffentlich|Publique|Pubblic|Publiczn|Públic/i.test(rowText);
                        var isUnlisted = /Unlisted|Доступ по ссылке|Nezveřejn|Nicht gelistet|Non répertor|Non in elenco|Niepubliczn|Não listad|Nelistat/i.test(rowText);
                        var isDraft = /Draft|Черновик|koncept|Entwurf|brouillon|bozza|robocza|rascunho|schiț/i.test(rowText);
                        if (isDraft && !isScheduled && !isPublic && !isUnlisted) return 'DRAFT_EXISTS';
                        if (!isScheduled && !isPublic && !isUnlisted) continue;
                        var allLinks = r.querySelectorAll('a[href*="/video/"]');
                        for (var a of allLinks) {
                            var m = a.href.match(/\\/video\\/([\\w-]{11})/);
                            if (m) return 'https://youtu.be/' + m[1];
                        }
                        var html = r.outerHTML;
                        var m2 = html.match(/\\/video\\/([\\w-]{11})/);
                        if (m2) return 'https://youtu.be/' + m2[1];
                        return 'yes';
                    }
                    return null;
                """, _st)
                    # текстовый дедуп — осознанная деградация без yt_studio, но проверка ВЫПОЛНЕНА
                    _dedup_done = True
                    logger.warning("DEDUP выполнен ТЕКСТОВЫМ путём (yt_studio не загружен): "
                                   "статус читается словами и не знает es/nl/tr")
                if existing and existing != "DRAFT_EXISTS":
                    m = re.search(r"/video/([\w-]{11})", str(existing))
                    url_found = f"https://youtu.be/{m.group(1)}" if m else existing
                    logger.info("DEDUP: видео с таким title уже на канале (Scheduled/Public): %s — пропускаю", url_found)
                    if (
                        YS is not None
                        and _st_kind == "SCHEDULED"
                        and isinstance(_v, dict)
                        and schedule_date is not None
                    ):
                        _existing_want_epoch = _schedule_exact_epoch
                        _existing_result = _verified_existing_scheduled_result(
                            uc_id,
                            _v,
                            title,
                            _existing_want_epoch,
                            studio_state=_st_kind,
                            schedule_mode=_schedule_mode,
                            picker_local=schedule_date,
                            browser_timezone=_schedule_browser_tz,
                        )
                        logger.info(
                            "DEDUP: exact scheduled row повторно проверена: id=%s success=%s",
                            _existing_result.get("video_id") or "unknown",
                            _existing_result.get("success"),
                        )
                        return _existing_result
                    return {"success": True, "skipped": True, "url": url_found, "error": "", "visibility": "ALREADY_EXISTS"}
                if existing == "DRAFT_EXISTS":
                    if upload_only:
                        logger.info("UPLOAD_ONLY: уже полноценный черновик (файл залит) — пропускаю (заливать нечего)")
                        return {"success": True, "skipped": True, "url": "DRAFT_UPLOADED", "error": "", "visibility": "DRAFT_EXISTS"}
                    # реальный черновик с файлом → мастер этого же videoId → schedule
                    logger.info("DEDUP: найден полноценный черновик (id=%s) — открываю мастер...", _draft_vid)
                    _ok_ed, _ed = _open_draft(_draft_vid, _st, "FINALIZE")
                    logger.info("FINALIZE: черновик открыт = %s", _ed)
                    if not _ok_ed:
                        return {"success": False, "url": "", "error": f"finalize: черновик не открылся ({_ed})", "visibility": "DRAFT"}
                    _finalizing = True
                elif finalize_draft:
                    # Дедуп мог не признать строку черновиком (напр. состояние прочиталось, но
                    # заголовок в списке отличается от нашего) — пробуем ещё раз тем же ключом:
                    # если videoId известен, идём по нему, иначе остаётся текстовый фолбэк.
                    _ok_ed, _ed = _open_draft(_draft_vid, _st, "FINALIZE (прямой)")
                    logger.info("FINALIZE: прямое открытие черновика = %s", _ed)
                    if _ok_ed:
                        _finalizing = True
                    else:
                        return {"success": False, "url": "", "error": f"finalize: черновик не найден (edit={_ed})", "visibility": "NOT_FOUND"}
                # existing is None и не finalize_draft → пойдём в FRESH upload ниже
        except StrictAuthGateError:
            raise
        except Exception as _de:
            # Не debug: любое исключение здесь означает, что проверка дублей не сделана,
            # а это решение о том, лить или нет. Гейт ниже такую заливку не пропустит.
            logger.error("Resume/Dedup: проверка не завершена (%s)", _de)

        # Сбой дедупа — не разрешение лить. «Список пуст / состояние не прочитано» ≠
        # «ролика на канале нет»: раньше и пустой список, и любое исключение молча
        # приводили к FRESH upload, то есть к ДУБЛЮ поверх собственного ролика — самый
        # дорогой исход (второй ролик на канале, его уже не откатить).
        if not _finalizing and not _resumed and not _dedup_done:
            logger.error("Свежую заливку НЕ начинаю: состояние канала не прочитано, "
                         "дубль-проверка не выполнена")
            save_blackbox(driver, "dedup-not-done")
            return {"success": False, "url": "",
                    "error": "dedup не выполнен: состояние канала не прочитано — заливку не начинаю",
                    "visibility": "UNKNOWN"}
        if _resumed and not _finalizing:
            # Сюда попадаем, только если после досылки файла что-то упало до открытия
            # мастера. Файл уже влит в СУЩЕСТВУЮЩИЙ черновик, поэтому уход в FRESH upload
            # означал бы второй ролик тем же файлом.
            logger.error("Resume прошёл, но мастер черновика не открыт — свежую заливку НЕ начинаю")
            save_blackbox(driver, "resume-no-wizard")
            return {"success": False, "url": "",
                    "error": "resume выполнен, мастер черновика не открылся — заливку не повторяю",
                    "visibility": "DRAFT"}

        # 4-5. Открыть загрузку + отправить файл. В finalize-режиме ПРОПУСКАЕМ —
        #      мастер уже открыт через «Edit draft», файл давно залит (черновик).
        if finalize_video_id and not _finalizing:
            return _strict_existing_failure(
                "strict_finalize_wizard_not_open: fresh upload forbidden",
                {"videoId": finalize_video_id},
            )
        if not _finalizing:
            # 4. Открыть загрузку: «Create» → «Upload videos» (кнопка теперь Create, не Upload; UI зависит
            #    от языка аккаунта). Файловый <input type=file> — стабильный якорь: появляется как диалог открыт.
            logger.info("Открываю загрузку (Create → Upload)...")
            # слова «Upload videos» на 9+ языках (en/ru/de/fr/it/pl/pt/ro/cs/es); «încarcă» — рум. форма без ă в корне
            _UP = ("upload", "загруз", "hochladen", "importer", "mettre en ligne", "carica",
                   "prześlij", "wgraj", "carregar", "enviar", "încărc", "încarc", "nahrát", "subir")

            def _file_input_present():
                """Диалог загрузки открыт = присутствует САМ мастер (SEL['wizard'] =
                ytcp-uploads-dialog, дамп 2026-08-03).

                Наличие input[type=file] признаком быть не может: на /videos/upload скрытый
                file input висит ВСЕГДА (см. комментарий к resume выше) — цикл выходил, НЕ
                открыв мастер, после чего файл уходил в первый попавшийся input страницы,
                то есть в resume-input ЧУЖОГО черновика.
                """
                if YS is not None:
                    try:
                        return bool(YS.read_wizard(driver).get("present"))
                    except Exception as _we:
                        logger.warning("Состояние мастера не прочитано (%s) — признак диалога "
                                       "беру по file input (фолбэк, менее надёжно)", _we)
                try:
                    return any('image' not in (f.get_attribute('accept') or '')
                               for f in driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]'))
                except Exception:
                    return False

            # Ретрай: холодный Chrome на свежем профиле рисует попап меню дольше 2с; плюс на пустой
            # странице контента есть отдельная кнопка «Upload videos» — пробуем и её.
            for _try in range(1, 4):
                if _file_input_present():
                    break
                # 0) прямое открытие диалога по URL (?d=ud): JS-клик по пункту меню «Upload videos»
                #    на части профилей молча не срабатывает (та же грабля, что _click_done) —
                #    deep-link стабильнее кликов. Live-проверено на профиле 59 (fr).
                try:
                    _au_q = _au_suffix(channel_url, uc_id)
                    driver.get(f"https://studio.youtube.com/channel/{uc_id}/videos/upload"
                               + (f"{_au_q}&d=ud" if _au_q else "?d=ud"))
                    if _fast_ui_enabled():
                        _wait_upload_dialog(driver, uc_id, timeout=6)
                    else:
                        time.sleep(6)
                    if _file_input_present():
                        logger.info("Диалог загрузки открыт напрямую (?d=ud, попытка %d)", _try)
                        break
                except Exception:
                    pass
                # a) прямая видимая кнопка «Upload videos» (пустой канал, /videos/upload)
                try:
                    _hit = driver.execute_script("""
                        var UP = arguments[0];
                        var els = document.querySelectorAll('ytcp-button, button');
                        for (var e of els) {
                            var t = (e.innerText || '').trim().toLowerCase();
                            if (!t || t.length > 40) continue;
                            for (var w of UP) {
                                if (t.indexOf(w) >= 0) {
                                    var r = e.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0) { e.click(); return t; }
                                }
                            }
                        }
                        return null;
                    """, list(_UP))
                    if _hit:
                        logger.info("Клик по кнопке загрузки: %r (попытка %d)", _hit, _try)
                        time.sleep(4)
                        if _file_input_present():
                            break
                except Exception:
                    pass
                # b) Create → пункт меню
                _opened = False
                for _sel in ("#create-icon", "ytcp-button#create-icon", "[aria-label='Create']",
                             "[aria-label='Создать']", "[aria-label='Erstellen']", "#upload-icon"):
                    try:
                        _btns = driver.find_elements(By.CSS_SELECTOR, _sel)
                        if _btns:
                            driver.execute_script("arguments[0].click()", _btns[0])
                            time.sleep(4)
                            _opened = True
                            break
                    except Exception:
                        continue
                if _opened:
                    try:
                        for _it in driver.find_elements(By.CSS_SELECTOR,
                                "tp-yt-paper-item, ytcp-text-menu-item, ytcp-menu-item, [role=menuitem], #text-item-0, #text-item-1"):
                            _t = (_it.text or "").strip().lower()
                            if _t and any(_w in _t for _w in _UP):
                                driver.execute_script("arguments[0].click()", _it)
                                time.sleep(3)
                                break
                    except Exception:
                        pass
                else:
                    logger.warning("Кнопка Create не найдена (попытка %d)", _try)
                if _file_input_present():
                    break
                logger.info("Диалог загрузки ещё не открыт (попытка %d/3) — жду и повторяю", _try)
                time.sleep(4)

            # 5. Загружаем видео
            size_mb = os.path.getsize(video_path) // 1024 // 1024
            logger.info("Загружаю видео: %s (%d МБ)", os.path.basename(video_path), size_mb)
            # Файл шлём в input ИМЕННО нового диалога загрузки (ytcp-uploads-dialog), а не в
            # resume-input чужого прерванного черновика на странице — иначе испортим тот черновик.
            file_input = None
            for _fsel in ("ytcp-uploads-dialog input[type='file']",
                          "ytcp-uploads-file-picker input[type='file']",
                          "input[type='file']"):
                try:
                    _fis = [f for f in driver.find_elements(By.CSS_SELECTOR, _fsel)
                            if 'image' not in (f.get_attribute('accept') or '')]
                    if _fis:
                        file_input = _fis[-1]
                        break
                except Exception:
                    continue
            if file_input is None:
                file_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="file"]')))
            if _strict:
                global _FRESH_UPLOAD_BINDING
                # The file input can exist before the inner dialog is opened.
                # Wait only for that readiness transition; authorisation below
                # still uses the unchanged complete fresh-file identity check.
                _fresh_scope_deadline = time.monotonic() + 12
                while True:
                    _before_fresh = YS.read_fresh_upload_state(driver, uc_id, file_input)
                    try:
                        YS._fresh_scope(_before_fresh, uc_id)
                        break
                    except YS.FreshUploadIdentityError as _scope_error:
                        if (str(_scope_error) != 'strict_fresh_identity_scope_invalid'
                                or _before_fresh.get('route_ok') is not True
                                or _before_fresh.get('channel_uc') not in (None, uc_id)
                                or _before_fresh.get('entity_id') not in (None, uc_id)
                                or _before_fresh.get('entity_type') not in (None, 'CHANNEL')
                                or type(_before_fresh.get('wizard_count')) is not int
                                or type(_before_fresh.get('opened_count')) is not int
                                or _before_fresh.get('wizard_count') not in (0, 1)
                                or _before_fresh.get('opened_count') not in (0, 1)
                                or not isinstance(_before_fresh.get('entries'), list)
                                or any(not isinstance(e, dict) for e in _before_fresh['entries'])
                                or any(e.get('local') is not False for e in _before_fresh.get('entries', []))
                                or time.monotonic() >= _fresh_scope_deadline):
                            logger.error("Fresh dialog scope not ready: %s", json.dumps(_before_fresh, ensure_ascii=False))
                            raise
                        time.sleep(0.25)
                _FRESH_UPLOAD_BINDING = YS.fresh_upload_baseline(
                    _before_fresh, uc_id, os.path.basename(video_path), os.path.getsize(video_path))
                _FRESH_UPLOAD_BINDING["video_sha256"] = _thumbnail_sha256(video_path)
                _persist_fresh_identity("baseline", _FRESH_UPLOAD_BINDING, _before_fresh)
            _file_handoff_attempted = True
            _send_video_file_at_upload_boundary(file_input, video_path, driver=driver)
            logger.info("Файл отправлен, жду готовности мастера...")
            # [дамп 2026-08-07] Мастер отдаёт videoId нашего ролика через ~8с после отправки,
            # на шаге DETAILS — задолго до конца загрузки. Забираем его СРАЗУ: дальше весь
            # вердикт идёт по id, а не по заголовку. Именно поиск по заголовку 07.08 объявил
            # провальными 18 заливок, у которых файл был долит и отложка выставлена.
            if YS is not None and not _wizard_vid:
                try:
                    # короткая попытка: на мелких файлах id приходит за ~8с. На больших
                    # он появится позже — его подхватит цикл ожидания загрузки ниже.
                    _wizard_vid = YS.wait_until(
                        driver, lambda d: ((_read_wizard_recording_identity(d) if _strict else YS.read_wizard(d)) or {}).get("videoId") or None,
                        timeout=25, poll=4, desc="videoId ролика в мастере") or ""
                    if _wizard_vid:
                        _mark_known_video_id(_wizard_vid)
                        logger.info("videoId ролика получен из мастера: %s", _wizard_vid)
                    else:
                        logger.info("videoId пока не выдан (файл ещё принимается) — доберу по ходу загрузки")
                except Exception as _ve:
                    logger.warning("videoId из мастера не прочитан: %s", str(_ve)[:80])
            # Ждём состояние, а не часы: сразу после открытия мастер отдаёт
            # errorSectionStatus=LOADING и isVideoUploaded=null — действовать в этот
            # момент бесполезно, а фиксированные 10с то не хватали, то тратились впустую.
            if YS is not None:
                try:
                    _rd = (_wait_wizard_ready_recording_identity(driver, timeout=min(timeout, 180))
                           if _strict else YS.wait_wizard_ready(driver, timeout=min(timeout, 180)))
                    if _strict:
                        if _FRESH_UPLOAD_BINDING is not None:
                            _read_wizard_recording_identity(driver, required=True)
                        _wizard_vid = _current_upload_phase().get("video_id") or _wizard_vid
                    if _rd:
                        logger.info("Мастер готов: шаг=%s errorSection=%s",
                                    _rd.get("workflowStep"), _rd.get("errorSectionStatus"))
                    else:
                        logger.info("Ожидание готовности мастера истекло; продолжаю подготовку метаданных")
                except Exception as _we:
                    if _strict:
                        raise
                    logger.warning("Ожидание готовности мастера не сработало (%s) — пауза 10с", _we)
                    time.sleep(10)
            else:
                time.sleep(10)
        else:
            logger.info("FINALIZE: мастер открыт через «Edit draft», файл уже залит — пропускаю Create/upload")
            # Дождаться, что мастер РЕАЛЬНО отрисовался (Details: #textbox), иначе обложка/altered/тайтл
            # уйдут впустую (пустой серый диалог). Пусто ~25с → ПЕРЕОТКРЫТЬ «Edit draft» (ручной приём юзера).
            _reopens = 0
            _det_start = time.time()
            # ГОТОВНОСТЬ мастера = его СОСТОЯНИЕ (present + errorSectionStatus != LOADING,
            # см. yt_studio.wait_wizard_ready). Видимый #textbox — только косвенный признак:
            # поле рисуется РАНЬШЕ, чем мастер отдаёт состояние (LOADING/isVideoUploaded=null,
            # SELECTOR_MAP §ytcp-uploads-dialog), а на шаге без заголовка его нет вовсе.
            # _READY_JS оставлен фолбэком на случай YS is None (о нём пишем в лог).
            _READY_JS = """
                var b=document.querySelectorAll('#textbox[aria-label]');
                for (var i=0;i<b.length;i++){
                    var r=b[i].getBoundingClientRect();
                    if (r.width>0 && r.height>0) return true;
                }
                return false;
            """
            while time.time() - _det_start < min(timeout, 300):
                _rd = None
                if YS is not None:
                    # None здесь = мастера нет ИЛИ он залип в errorSectionStatus=LOADING,
                    # то есть ровно то состояние, ради которого делается переоткрытие.
                    _rd = YS.wait_wizard_ready(driver, timeout=45)
                    _ready = bool(_rd)
                else:
                    logger.warning("FINALIZE: yt_studio нет — готовность мастера по видимому "
                                   "#textbox (фолбэк, косвенный признак)")
                    # Мастеру нужно ~10-15с на отрисовку — опрашиваем, а не судим по одному замеру
                    # через 6с (из-за этого он объявлялся пустым, хотя просто не успел).
                    _ready = False
                    for _ in range(14):
                        _ready = driver.execute_script(_READY_JS)
                        if _ready:
                            break
                        time.sleep(3)
                if _ready:
                    logger.info("FINALIZE: мастер готов (шаг=%s errorSection=%s uploaded=%s)",
                                (_rd or {}).get("workflowStep") or "?",
                                (_rd or {}).get("errorSectionStatus") or "?",
                                (_rd or {}).get("isVideoUploaded"))
                    break
                if _reopens < 5:
                    _reopens += 1
                    logger.warning("FINALIZE: мастер не готов по состоянию (нет в DOM / залип LOADING) "
                                   "— ПЕРЕОТКРЫВАЮ «Edit draft» (#%d)", _reopens)
                    try:
                        # Свёрнутое/перекрытое окно Chrome приостанавливает раскладку, и лениво
                        # отрисовываемый диалог отдаёт rect 0×0 — ровно это ловится при параллельной
                        # работе нескольких профилей AdsPower. Разворачиваем окно и поднимаем наверх.
                        driver.set_window_position(0, 0)
                        driver.set_window_size(1440, 900)
                        driver.execute_script("window.focus();")
                        time.sleep(1)
                    except Exception:
                        pass
                    try:
                        # переоткрываем ТОТ ЖЕ ролик по videoId — текстовый клик на
                        # переоткрытии мог увести мастер на соседнюю строку серии
                        _ok_re, _re = _open_draft(_draft_vid, _st, "FINALIZE (переоткрытие)")
                        logger.info("FINALIZE: переоткрытие = %s", _re)
                    except Exception:
                        pass
                else:
                    # улики нужны здесь, а не «оно опять не сработало» на следующей заливке
                    logger.warning("FINALIZE: мастер так и не готов после %d переоткрытий — продолжаю осторожно", _reopens)
                    save_blackbox(driver, "finalize-wizard-not-ready")
                    break

        if _strict:
            _require_auth_clear(driver, stage="before_title")
        # 6. Название — через JS установка textContent и правильные события
        _dump_page(driver, "перед-названием")
        logger.info("Заполняю название: %s", title[:50])
        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#textbox[aria-label]")))
            _TITLE_JS = """
                var boxes = document.querySelectorAll('#textbox[aria-label]');
                if (!boxes.length) return 'no boxes';
                // ПЕРВОЕ ВИДИМОЕ, а не boxes[0]: скрытый первый элемент = название уходит в никуда
                var t = null;
                for (var i=0;i<boxes.length;i++){
                    var r=boxes[i].getBoundingClientRect();
                    if (r.width>0 && r.height>0) { t = boxes[i]; break; }
                }
                if (!t) return 'no visible box';
                t.focus();
                var range = document.createRange();
                range.selectNodeContents(t);
                var sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
                document.execCommand('delete', false, null);
                document.execCommand('insertText', false, arguments[0]);
                return t.textContent.trim().substring(0, 80);
            """
            # ВЕРИФИКАЦИЯ: insertText молча не срабатывает, если textbox ещё не интерактивен
            # (свежий профиль/медленный рендер) — YouTube оставляет тайтл = имя файла ("v1_...").
            # Поэтому: до 5 попыток, сверяем readback с ожидаемым началом.
            import unicodedata as _ud
            def _tnorm(s):
                return _ud.normalize("NFC", s or "").lower().replace(" ", "")[:25]
            _want = _tnorm(title)
            set_ok = ""
            for _ta in range(1, 6):
                set_ok = driver.execute_script(_TITLE_JS, title)
                if _tnorm(str(set_ok)).startswith(_want[:20]):
                    logger.info("Название установлено (попытка %d): %s", _ta, str(set_ok)[:50])
                    break
                logger.warning("Название НЕ применилось (попытка %d, readback=%r) — жду и повторяю", _ta, str(set_ok)[:40])
                time.sleep(3)
            else:
                # ЗАПАСНОЙ ПУТЬ (01.08): YouTube поменял редактор — document.execCommand('insertText')
                # молча не меняет содержимое, хотя readback поля читается. Печатаем нативно:
                # реальные события клавиатуры проходят там, где execCommand уже нет.
                logger.warning("JS-ввод не сработал — печатаю название нативно")
                try:
                    from selenium.webdriver.common.keys import Keys as _K
                    _el = driver.find_element(By.CSS_SELECTOR, "#textbox[aria-label]")
                    _el.click(); time.sleep(0.4)
                    _mod = _K.COMMAND if sys.platform == "darwin" else _K.CONTROL
                    _el.send_keys(_mod, "a"); time.sleep(0.2)
                    _el.send_keys(_K.DELETE); time.sleep(0.3)
                    _el.send_keys(title); time.sleep(0.8)
                    _rb = (_el.text or "").strip()
                    if _tnorm(_rb).startswith(_want[:20]):
                        logger.info("Название установлено нативным вводом: %s", _rb[:50])
                    else:
                        logger.error("Название не применилось и нативно: %r", _rb[:50])
                except Exception as _ne:
                    logger.error("Нативный ввод названия упал: %s", _ne)
            time.sleep(1)
        except Exception as e:
            logger.warning("Название: %s", e)

        # 7. Описание — аналогично
        logger.info("Заполняю описание...")
        try:
            set_ok = driver.execute_script("""
                var boxes = document.querySelectorAll('#textbox[aria-label]');
                if (boxes.length < 2) return 'no desc box';
                var t = boxes[1];
                t.focus();
                var range = document.createRange();
                range.selectNodeContents(t);
                var sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
                document.execCommand('delete', false, null);
                document.execCommand('insertText', false, arguments[0]);
                return 'ok: ' + t.textContent.length + ' chars';
            """, description)
            logger.info("Описание установлено: %s", set_ok)
            time.sleep(1)
        except Exception as e:
            logger.warning("Описание: %s", e)

        # 8. Обложка
        if _strict:
            _uploaded = _wait_mp4_uploaded(driver, timeout=timeout)
            if not _uploaded:
                raise RuntimeError("strict_mp4_upload_incomplete: thumbnail handoff forbidden")
            _upload_complete_proven = True
            _wizard_vid = _uploaded["videoId"]
        _thumbnail_handed_off = False
        _thumbnail_expected_sha256 = _thumbnail_sha256(thumbnail_path) if _strict else ""
        if thumbnail_path and os.path.exists(thumbnail_path):
            logger.info("Загружаю обложку: %s", os.path.basename(thumbnail_path))
            try:
                file_inputs = _thumbnail_upload_inputs(driver, By, strict=_strict)
                for fi in file_inputs:
                    accept = fi.get_attribute("accept") or ""
                    if "image" in accept:
                        fi.send_keys(os.path.abspath(thumbnail_path))
                        _thumbnail_handed_off = True
                        logger.info("Файл обложки передан Studio")
                        # A/B прямо в мастере (01.08): у YouTube в шаге обложки есть
                        # «Тест и сравнение» — до трёх вариантов. Пост-проход после публикации
                        # для этого не нужен, черновику/приватному он тоже ставится.
                        _extra = [p for p in (ab_thumbnails or [])
                                  if p and os.path.exists(p)
                                  and os.path.abspath(p) != os.path.abspath(thumbnail_path)]
                        # A/B в мастере выключен по умолчанию (01.08): подтверждение не срабатывает,
                        # а файл варианта рискует уйти в поле ОСНОВНОЙ обложки. Ставим отдельным
                        # проходом через studio_ab_monet. Включить: AB_IN_WIZARD=1
                        if _extra and os.environ.get("AB_IN_WIZARD") == "1":
                            try:
                                time.sleep(2)
                                # Кнопка «A/B Testing» на шаге деталей открывает МОДАЛКУ,
                                # которая по умолчанию стоит на вкладке «Title only» и требует
                                # второй заголовок. Если её не переключить и не закрыть —
                                # мастер заливки встаёт намертво (инцидент 01.08).
                                _opened = driver.execute_script(r"""
                                    var pat = /a\s*[\/]?\s*b\s*test/i;
                                    var all = document.querySelectorAll('ytcp-button, button, [role=button]');
                                    for (var b of all) {
                                        var lbl = ((b.getAttribute('aria-label')||'') + ' ' +
                                                   (b.innerText||b.textContent||'')).trim();
                                        var rc = b.getBoundingClientRect();
                                        if (pat.test(lbl) && rc.width > 0) {
                                            b.scrollIntoView({block:'center'}); b.click();
                                            return lbl.slice(0,30);
                                        }
                                    }
                                    return '';
                                """)
                                logger.info("A/B: кнопка = %r", _opened)
                                time.sleep(2.5)
                                _ab_done = False
                                try:
                                    if _opened:
                                        # 1) вкладка «Thumbnail only» — иначе форма просит 2-й заголовок
                                        _dump = driver.execute_script(r"""
                                            var out = [];
                                            for (var e of document.querySelectorAll(
                                                    'button,[role=tab],[role=button],ytcp-button,tp-yt-paper-tab,tp-yt-paper-item')) {
                                                var rc = e.getBoundingClientRect();
                                                var t = (e.innerText||e.textContent||'').trim().replace(/\s+/g,' ');
                                                if (t && rc.width>0 && rc.height>0 && t.length<40) out.push(t);
                                            }
                                            return out.slice(0,45);
                                        """)
                                        logger.info("A/B: элементы модалки = %s", _dump)
                                        # вкладки нарисованы не кнопками — ищем ЛЮБОЙ элемент
                                        # с точным текстом и кликаем его кликабельного предка
                                        _tab = driver.execute_script(r"""
                                            var pat = /^(thumbnail only|только значок|miniatura)$/i;
                                            var all = document.querySelectorAll('*');
                                            for (var e of all) {
                                                if (e.children.length) continue;
                                                var t = (e.textContent||'').trim();
                                                if (!pat.test(t)) continue;
                                                var rc = e.getBoundingClientRect();
                                                if (!rc.width || !rc.height) continue;
                                                var n = e;
                                                for (var i=0; i<5 && n; i++) {
                                                    n.click();
                                                    n = n.parentElement;
                                                }
                                                return t.slice(0,30);
                                            }
                                            return '';
                                        """)
                                        logger.info("A/B: вкладка = %r", _tab)
                                        time.sleep(2)
                                        # 2) вторая обложка в слот модалки
                                        _ins = [x for x in driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
                                                if "image" in (x.get_attribute("accept") or "")]
                                        if len(_ins) > 1:
                                            _ins[-1].send_keys(os.path.abspath(_extra[0]))
                                            logger.info("A/B: добавлен вариант %s", os.path.basename(_extra[0]))
                                            time.sleep(3)
                                        # 3) подтвердить тест
                                        _set = driver.execute_script(r"""
                                            var pat = /set\s*test|запустить\s*тест|начать\s*тест/i;
                                            for (var b of document.querySelectorAll('ytcp-button, button')) {
                                                var t = (b.innerText||b.textContent||'').trim();
                                                var dis = b.getAttribute('disabled') !== null ||
                                                          b.getAttribute('aria-disabled') === 'true';
                                                if (t && pat.test(t) && !dis) { b.click(); return t.slice(0,20); }
                                            }
                                            return '';
                                        """)
                                        logger.info("A/B: подтверждение = %r", _set)
                                        _ab_done = bool(_set)
                                        time.sleep(2)
                                finally:
                                    # ВСЕГДА закрываем модалку: открытая форма блокирует мастер
                                    # признак открытой модалки — видимая кнопка «Set test»
                                    _still = driver.execute_script(r"""
                                        var open = false;
                                        for (var b of document.querySelectorAll('ytcp-button, button')) {
                                            var t = (b.innerText||b.textContent||'').trim();
                                            var rc = b.getBoundingClientRect();
                                            if (/set\s*test|запустить\s*тест/i.test(t) && rc.width>0) { open = true; break; }
                                        }
                                        if (!open) return '';
                                        for (var b of document.querySelectorAll('ytcp-button, button, [role=button]')) {
                                            var t = ((b.getAttribute('aria-label')||'') + ' ' +
                                                     (b.innerText||b.textContent||'')).trim();
                                            var rc = b.getBoundingClientRect();
                                            if (/^(cancel|отмена|close|закрыть|zrušit|anuluj)$/i.test(t) && rc.width>0) {
                                                b.click(); return t.slice(0,20);
                                            }
                                        }
                                        return 'open';
                                    """)
                                    if _still:
                                        logger.warning("A/B: модалка была открыта — закрыл (%r, тест=%s)", _still, _ab_done)
                                        try:
                                            from selenium.webdriver.common.keys import Keys as _K2
                                            driver.switch_to.active_element.send_keys(_K2.ESCAPE)
                                        except Exception:
                                            pass
                                        time.sleep(1.5)
                            except Exception as _abe:
                                logger.warning("A/B в мастере не вышел: %s", _abe)
                        time.sleep(3)
                        break
            except Exception as e:
                logger.warning("Обложка: %s", e)
                if _strict:
                    return upload_contract.failure_result(
                        f"strict_thumbnail_handoff_failed: {e}", _current_upload_phase(),
                        {"visibility": "DRAFT"},
                    )
        if _strict:
            try:
                if not _thumbnail_handed_off:
                    raise RuntimeError("strict_thumbnail_handoff_failed: image input not found")
                # The early 25s probe can expire before Studio assigns an ID.
                # Re-read and emit its durable phase BEFORE checking the cover,
                # even if readiness timed out or a known draft was reopened.
                _wizard_vid = _read_wizard_recording_identity(driver, required=True)["videoId"]
                _thumbnail_proof = _strict_thumbnail_confirmation(
                    driver, thumbnail_path, channel_uc=uc_id,
                    video_id=_current_upload_phase().get("video_id") or "",
                    expected_sha256=_thumbnail_expected_sha256,
                )
            except Exception as exc:
                save_blackbox(driver, "strict-thumbnail-unverified")
                return upload_contract.failure_result(
                    str(exc), _current_upload_phase(), {"visibility": "DRAFT"}
                )

        # 9. "Not made for kids"
        logger.info("Выбираю 'Not made for kids'...")
        _kids_ok = False
        if YS is not None:
            try:
                _kids_ok = YS.set_not_made_for_kids(driver)
            except Exception as _ke:
                # уровень не debug: причина отказа нужна в боевом логе — без «для детей»
                # шаг DETAILS невалиден и Next заблокирован
                logger.warning("«Не для детей» (по состоянию) упало: %s", _ke)
        if not _kids_ok and YS is None:
            # Фолбэк ТОЛЬКО когда нет yt_studio. Селектор тот же (SELECTOR_MAP «Шаг Details»),
            # но клик — настоящей последовательностью MouseEvent: Polymer штатно глотает
            # el.click(), поэтому «клик прошёл» состоянием НЕ является. Прежний фолбэк по
            # английским словам ('No,' / 'not made') убран: на pl/ro/cs/de профилях он не
            # находил ничего и молчал.
            try:
                driver.execute_script(r"""
                    var el=document.querySelector('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]');
                    if(!el) return false;
                    var ov=document.querySelector('ytcp-uploads-review'), od=null;
                    if(ov){od=ov.style.display; ov.style.display='none';}
                    el.scrollIntoView({block:'center'});
                    var r=el.getBoundingClientRect();
                    var o={bubbles:true,cancelable:true,view:window,clientX:r.left+r.width/2,clientY:r.top+r.height/2,button:0};
                    el.dispatchEvent(new MouseEvent('mousedown',o));
                    el.dispatchEvent(new MouseEvent('mouseup',o));
                    el.dispatchEvent(new MouseEvent('click',o));
                    if(ov) ov.style.display=od||'';
                    return true;
                """)
                time.sleep(0.8)   # Polymer применяет выбор асинхронно — читаем после паузы
                _chk_kids = driver.execute_script(
                    "var r=document.querySelector('tp-yt-paper-radio-button[name=VIDEO_MADE_FOR_KIDS_NOT_MFK]');"
                    " return r? String(r.getAttribute('aria-checked')):null;")
                _kids_ok = (_chk_kids == "true")
                logger.warning("«Не для детей» без yt_studio (фолбэк): aria-checked=%s", _chk_kids)
            except Exception as _ke2:
                logger.warning("«Не для детей» (фолбэк без yt_studio) упал: %s", _ke2)
        if not _kids_ok:
            # Невыставленный ответ «для детей» делает шаг DETAILS невалидным
            # (isCurrentStepInvalid=true) и намертво блокирует Next → мастер не дойдёт до
            # Visibility, ролик останется черновиком. Поэтому здесь громкая ошибка и улики,
            # а не тихое продолжение: ниже сквозной проход ещё раз чинит DETAILS по
            # состоянию, а если и он не справится — гейт Visibility завершит заливку ошибкой.
            logger.error("«Не для детей» НЕ подтверждено по aria-checked — шаг DETAILS "
                         "останется невалидным (Next будет заблокирован)")
            save_blackbox(driver, "kids-radio-not-set")

        # 9.5. Altered/AI content disclosure — радио name=VIDEO_HAS_ALTERED_CONTENT_YES/NO
        # (реальная структура подтверждена дампом DOM: aria "Yes, AI was used" / "No, AI wasn’t used").
        # Раскрываем = "Yes" (контент AI-генерируемый → дисклоуз). Клик через dispatchEvent (как _set_public),
        # т.к. overlay/обычный click может перехватываться.
        logger.info("Отмечаю 'Altered content' = Yes (AI disclosed)...")
        _ai_ok = False
        if YS is not None:
            try:
                # раскрывает секцию при необходимости и подтверждает по aria-checked
                _ai_ok = YS.set_altered_content_yes(driver)
            except Exception as _ae:
                logger.debug("altered (состояние): %s", _ae)
        try:
            if not _ai_ok:
                for _sel in ("#toggle-button", "ytcp-button#toggle-button"):
                    try:
                        _tb = driver.find_element(By.CSS_SELECTOR, _sel)
                        if _tb.is_displayed():
                            driver.execute_script("arguments[0].click()", _tb)
                            time.sleep(1.0)
                            break
                    except Exception:
                        pass

            _r = driver.execute_script(r"""
                var radios = document.querySelectorAll('tp-yt-paper-radio-button,[role=radio]');
                var yes=null;
                for (var r of radios){ if((r.getAttribute('name')||'').toUpperCase()==='VIDEO_HAS_ALTERED_CONTENT_YES'){yes=r;break;} }
                if(!yes){ for (var r of radios){ var a=(r.getAttribute('aria-label')||'').toLowerCase(); if(a.indexOf('ai was used')>=0){yes=r;break;} } }
                if(!yes) return {ok:false};
                var overlay=document.querySelector('ytcp-uploads-review'), od=null;
                if(overlay){od=overlay.style.display; overlay.style.display='none';}
                yes.scrollIntoView({block:'center'});
                var rect=yes.getBoundingClientRect();
                var o={bubbles:true,cancelable:true,view:window,clientX:rect.left+rect.width/2,clientY:rect.top+rect.height/2,button:0};
                yes.dispatchEvent(new MouseEvent('mousedown',o));
                yes.dispatchEvent(new MouseEvent('mouseup',o));
                yes.dispatchEvent(new MouseEvent('click',o));
                if(overlay) overlay.style.display=od||'';
                return {ok:true};
            """)
            if _r and _r.get('ok'):
                time.sleep(0.8)
                _chk = driver.execute_script("var r=document.querySelector('tp-yt-paper-radio-button[name=VIDEO_HAS_ALTERED_CONTENT_YES]'); return r? String(r.getAttribute('aria-checked')||r.checked):null;")
                if _chk in ("true", "True", "1"):
                    # результат фолбэка теперь ЗАПОМИНАЕТСЯ: ниже по нему решается,
                    # ругаться ли на отсутствие AI-метки (раньше он никуда не шёл)
                    _ai_ok = True
                    logger.info("Altered content = Yes отмечено (aria-checked=%s)", _chk)
                else:
                    logger.warning("Altered content: клик прошёл, но aria-checked=%s (не подтвердилось)", _chk)
            else:
                logger.warning("Altered content радио (VIDEO_HAS_ALTERED_CONTENT_YES) не найдено — UI мог измениться")
        except Exception as e:
            logger.warning("Altered content обработка упала: %s", e)
        if not _ai_ok:
            # Ролик уедет на канал БЕЗ AI-дисклоуза — это не мелочь и не повод молчать.
            # Итог всё равно перепроверяется по состоянию строки (alteredDisclosed) в
            # разделе 15, но след в логе должен быть здесь, у места отказа.
            logger.error("AI-дисклоуз (altered content) НЕ подтверждён в мастере — "
                         "проверю по состоянию строки после публикации")

        # 10+11. Дойти до шага Visibility и ДОЖДАТЬСЯ его радио-кнопок.
        # Раньше гейтом был текст body ("upload complete"/"обработка") — он матчился слишком рано,
        # schedule/public запускались до отрисовки радио → видео уходило в Draft. Теперь гейт —
        # реальное наличие ВИДИМЫХ радио PUBLIC/PRIVATE/UNLISTED (радио SCHEDULE не существует),
        # а продвижение — строго шаг за шагом по #next-button (см. _advance_step_by_step).
        if upload_only:
            logger.info("UPLOAD_ONLY (Фаза A): пропускаю ожидание Visibility — нам нужен только черновик")
        else:
            logger.info("Дохожу до шага Visibility (жду радио-кнопки, до %d мин)...", timeout // 60)
        vis_ready = False
        start_time = time.time()
        last_advance = 0.0
        last_refresh = time.time()
        refreshes = 0
        _bb_state_unreadable = False   # чёрный ящик по нечитаемому состоянию — один раз за заливку
        _adv_state = {"warned": False}  # предупреждение о деградации — один раз за заливку

        def _advance_step_by_step(_tmo=45):
            """Продвинуть мастер к Visibility ЧЕСТНО — шаг за шагом по #next-button.

            Прыжок по [id^=step-badge-] у СВЕЖЕЙ заливки (uploadMode=NEW_UPLOAD) запрещён:
            перепрыгнуть непройденный шаг нельзя (SELECTOR_MAP §2b), а обход MONETIZATION
            и CONTENT_RATINGS как раз и оставлял ролик черновиком. У ЧЕРНОВИКА
            (uploadMode=EXISTING_DRAFT) шаги уже completed — там прыжок законен.
            Переход подтверждается реальным workflowStep, а не фактом клика.
            """
            if YS is None:
                if not _adv_state["warned"]:
                    _adv_state["warned"] = True
                    logger.warning("yt_studio нет — продвигаю мастер только кнопкой Next, "
                                   "без чтения шага и без починки MONETIZATION/CONTENT_RATINGS "
                                   "(деградированный путь)")
                try:
                    # только Next и только когда он разблокирован; бейджи не трогаем —
                    # вслепую прыгать по шагам хуже, чем не двигаться вовсе
                    driver.execute_script(
                        "var nb=document.querySelector('ytcp-button#next-button');"
                        "if(nb && nb.getAttribute('aria-disabled')!=='true'){"
                        " var r=nb.getBoundingClientRect();"
                        " var o={bubbles:true,cancelable:true,view:window,"
                        "        clientX:r.left+r.width/2,clientY:r.top+r.height/2,button:0};"
                        " nb.dispatchEvent(new MouseEvent('mousedown',o));"
                        " nb.dispatchEvent(new MouseEvent('mouseup',o));"
                        " nb.dispatchEvent(new MouseEvent('click',o));}")
                except Exception as _ne2:
                    logger.warning("Next (фолбэк без yt_studio) не нажался: %s", _ne2)
                return False
            try:
                _w0 = YS.read_wizard(driver)
                if _w0.get("uploadMode") == "EXISTING_DRAFT":
                    _w1 = YS.goto_step_and_wait(driver, YS.STEP_REVIEW,
                                                timeout=_tmo, attempts=2)
                    return bool(_w1)
                _a2, _w2 = YS.advance_to_review(driver, monetize=True, timeout=_tmo)
                if not _a2:
                    logger.info("Продвижение по шагам пока не дошло до Visibility: %s", _w2)
                return bool(_a2)
            except Exception as _pe2:
                logger.warning("Продвижение по шагам упало: %s", _pe2)
                return False

        # СКВОЗНОЙ ПРОХОД (основной путь): ведём мастер шаг за шагом, как руками.
        # На монетизированных каналах свежая заливка проходит через шаги, о которых
        # старый код не знал: MONETIZATION (радио ON/OFF + «Done») и CONTENT_RATINGS
        # (анкета самосертификации). Пока они не пройдены, Next заблокирован и мастер
        # НИКОГДА не доходит до Visibility — именно поэтому ролики оставались черновиками.
        if YS is not None and not upload_only:
            try:
                # on=True: шаг Monetization в мастере — это «показывать рекламу на
                # ролике», и на YPP-канале это штатный выбор (старый _fill_monetization_step
                # тоже ставил On). Параметр monetize в сигнатуре — про ПОСТ-публикационную
                # настройку через studio_ab_monet, это другое; передавать его сюда нельзя,
                # иначе при monetize=False (значение по умолчанию) реклама выключается.
                _midroll_plan = None
                if midrolls:
                    _dur = _probe_duration(video_path)
                    _midroll_plan = YS.plan_midrolls(_dur, chapters=chapters,
                                                     step_sec=midroll_step or YS.MIDROLL_STEP_SEC)
                    if _midroll_plan:
                        logger.info("Мидроллы: план на %d слотов (ролик %.0fс) — ставлю прямо в мастере",
                                    len(_midroll_plan), _dur)
                _adv, _why = YS.advance_to_review(driver, monetize=True,
                                                  timeout=min(timeout, 300),
                                                  midroll_plan=_midroll_plan)
                if _adv:
                    vis_ready = True
                    if _adv.get("midrollsPlaced"):
                        _wizard_midrolls = int(_adv["midrollsPlaced"])
                    logger.info("Сквозной проход до Visibility выполнен (шаг=%s, радио=%s)",
                                _adv.get("workflowStep"), _adv.get("visibilityRadiosVisible"))
                else:
                    logger.warning("Сквозной проход не дошёл до Visibility: %s — "
                                   "продолжаю прежним циклом ожидания", _why)
            except Exception as _ae:
                logger.warning("Сквозной проход упал: %s — прежний цикл ожидания", _ae)

        while (not upload_only) and not vis_ready and time.time() - start_time < timeout:
            # Готовность шага = мастер реально на последнем шаге (workflowStep=REVIEW)
            # И радио видимости отрисованы. Старый детектор считал ЛЮБЫЕ радио с
            # name PUBLIC/PRIVATE/UNLISTED/SCHEDULE по всему документу (радио SCHEDULE
            # не существует вовсе) и не знал, на каком шаге мастер находится.
            _wz = None
            try:
                if YS is not None:
                    _wz = YS.read_wizard(driver)
                    n_vis = (_wz.get("visibilityRadiosVisible") or 0) if _wz.get("workflowStep") == YS.STEP_REVIEW else 0
                else:
                    n_vis = driver.execute_script("""
                        var radios = document.querySelectorAll('tp-yt-paper-radio-button');
                        var c = 0;
                        for (var r of radios) {
                            var n = (r.getAttribute('name')||'').toUpperCase();
                            if (n==='PUBLIC'||n==='PRIVATE'||n==='UNLISTED') {
                                var b = r.getBoundingClientRect();
                                if (b.width>0 && b.height>0) c++;
                            }
                        }
                        return c;
                    """)
            except Exception:
                n_vis = 0
            if n_vis and n_vis >= 2:
                vis_ready = True
                logger.info("Шаг Visibility готов (видимых радио: %s)", n_vis)
                break
            # КОРНЕВАЯ ПРИЧИНА пустого мастера: YouTube ещё гоняет проверки/обработку
            # («Checking for copyright… X min left» ~10–15 мин на 2-часовых роликах) → backend не отдал
            # состояние → шаг Visibility ФИЗИЧЕСКИ пуст (радио нет в DOM). Переоткрытие/refresh тут
            # бесполезны (лечат не ту причину). Пока проверки идут — просто ЖДЁМ (и мягко продвигаемся).
            try:
                if _wz is not None and _wz.get("present"):
                    # проверки/заливка идут = мастер сам сообщает об этом состоянием
                    checks_busy = bool(_wz.get("isPrechecksLoading")) or not _wz.get("isVideoUploaded")
                elif YS is not None:
                    # Мастера на экране нет → судим по СТРОКЕ ролика: videoPrechecks
                    # (copyright/brandSafety) и effectiveStatus — машинные и одинаковые на всех
                    # языках (SELECTOR_MAP §ytcp-video-row). Текст страницы признаком быть не
                    # может: слова «processing/обработка/checking» висят и в ЧУЖИХ строках
                    # списка, а ложное «проверки идут» ниже сбрасывало last_refresh и
                    # блокировало recovery до самого таймаута (до 30 мин молчаливого ожидания).
                    _row = None
                    try:
                        _rows = YS.read_videos(driver) or []
                        if _draft_vid:
                            _row = next((r for r in _rows if r.get("videoId") == _draft_vid), None)
                        if _row is None and _rows:
                            _row, _ = YS.find_video(driver, _st, videos=_rows)
                    except Exception as _re2:
                        logger.debug("checks_busy: строка ролика не прочитана: %s", _re2)
                    if _row is None:
                        # Состояние не прочитано ≠ «проверки идут»: это повод дать сработать
                        # recovery (переоткрытие/refresh), а не ждать молча.
                        checks_busy = False
                        if not _bb_state_unreadable:      # раз за заливку, чтобы не залить лог
                            _bb_state_unreadable = True
                            logger.warning("Ни мастер, ни строка ролика не отдают состояние — "
                                           "НЕ считаю это «идут проверки», отдаю решение recovery")
                            save_blackbox(driver, "checks-state-unreadable")
                    else:
                        checks_busy = (not _row.get("prechecksDone")) or \
                                      _row.get("effectiveStatus") == YS.VIS_PROCESSING
                else:
                    # без модуля состояния судить не по чему — пусть работает recovery
                    checks_busy = False
                    if not _bb_state_unreadable:
                        _bb_state_unreadable = True
                        logger.warning("yt_studio нет — состояние проверок YouTube недоступно, "
                                       "жду через recovery (фолбэк)")
            except Exception:
                checks_busy = False
            if n_vis == 0 and checks_busy:
                if time.time() - last_advance > 6:
                    last_advance = time.time()
                    # мягко продвигаемся ровно тем же честным путём, что и основной проход
                    # (шаги MONETIZATION/CONTENT_RATINGS чинятся внутри), коротким окном —
                    # чтобы не залипнуть на одном вызове, пока идут проверки YouTube
                    if _advance_step_by_step(20):
                        vis_ready = True
                        logger.info("Шаг Visibility достигнут во время ожидания проверок")
                        break
                    # Пишем не «жду», а что показывают сами селекторы: файл долит или нет,
                    # сколько процентов, на каком шаге мастер и идут ли проверки. По такому
                    # логу видно, стоим мы по вине YouTube или у нас что-то не нажалось.
                    logger.info(
                        "...проверки YouTube идут (%dс): файл залит=%s, %s%%, шаг=%s, "
                        "проверки=%s — переключателей видимости в DOM ещё нет",
                        int(time.time() - start_time),
                        (_wz or {}).get("isVideoUploaded"),
                        (_wz or {}).get("pct"),
                        (_wz or {}).get("workflowStep"),
                        (_wz or {}).get("isPrechecksLoading"))
                # Сдвигаем таймер recovery, чтобы он не сработал сразу после завершения
                # проверок. Делать это можно ТОЛЬКО когда «проверки идут» подтверждено
                # состоянием (ветка выше): на текстовом признаке это глушило recovery
                # насмерть и воркер молча досиживал до полного таймаута.
                last_refresh = time.time()
                time.sleep(5)
                continue
            # blank-render recovery (FINALIZE/«Edit draft»): мастер черновика иногда открывается ПУСТЫМ
            # (серый, только Back/Save). Простой refresh тут НЕ годится (закроет wizard) — лечится
            # ПЕРЕОТКРЫТИЕМ: вернуться на список → снова клик «Edit draft» = чистый ре-рендер (ручной приём юзера).
            if n_vis == 0 and _finalizing and time.time() - last_refresh > 25 and refreshes < 5:
                refreshes += 1
                logger.warning("FINALIZE: мастер пуст %dс — ПЕРЕОТКРЫВАЮ «Edit draft» (#%d)", int(time.time() - start_time), refreshes)
                try:
                    # переоткрываем СВОЙ ролик по videoId (искать строку подстрокой заголовка
                    # на середине финализации — способ уехать в чужой черновик)
                    _ok_re, _re = _open_draft(_draft_vid, _st, "FINALIZE (пустой мастер)")
                    logger.info("FINALIZE: переоткрытие черновика = %s", _re)
                except Exception:
                    pass
                last_refresh = time.time()
                last_advance = 0.0
                continue
            # blank-render recovery (FRESH upload): шаг финализации иногда грузится ПУСТЫМ →
            # лечится refresh страницы (подсказка юзера). Если пусто ~25с — обновляем и повторяем.
            if n_vis == 0 and not _finalizing and time.time() - last_refresh > 25 and refreshes < 6:
                refreshes += 1
                logger.warning("Шаг Visibility пуст %dс — refresh страницы (#%d)", int(time.time() - start_time), refreshes)
                try:
                    driver.refresh()
                except Exception:
                    pass
                time.sleep(8)
                last_refresh = time.time()
                last_advance = 0.0
                continue
            # каждые ~6с пробуем продвинуться к Visibility — тем же честным путём
            if time.time() - last_advance > 6:
                last_advance = time.time()
                if _advance_step_by_step(30):
                    vis_ready = True
                    logger.info("Шаг Visibility достигнут продвижением по шагам")
                    break
                elapsed = int(time.time() - start_time)
                if elapsed > 0:
                    logger.info("...жду Visibility (%dс, видимых радио пока %s)", elapsed, n_vis)
            time.sleep(2)

        if not vis_ready and not upload_only:
            # Последняя честная сверка по состоянию: цикл мог выйти по сбою чтения, а
            # мастер реально стоять на REVIEW.
            _wz_fail = YS.read_wizard(driver) if YS is not None else {}
            if (YS is not None and _wz_fail.get("workflowStep") == YS.STEP_REVIEW
                    and (_wz_fail.get("visibilityRadiosVisible") or 0) >= 2):
                vis_ready = True
                logger.info("Visibility всё же готов по состоянию (шаг=%s, радио=%s) — продолжаю",
                            _wz_fail.get("workflowStep"), _wz_fail.get("visibilityRadiosVisible"))
            else:
                # Радио видимости нет = мастер НЕ на шаге REVIEW. Применять видимость не к
                # чему, а #done-button в этом состоянии — это «Save», то есть ЧЕРНОВИК.
                # Раньше здесь стоял warning и выполнение шло дальше: получались «успешные»
                # прогоны, после которых на канале лежал черновик. Файл уже залит, поэтому
                # доводить его будет отдельный проход FINALIZE — заново лить не надо.
                logger.error("Шаг Visibility не достигнут за %d мин (шаг=%s, радио=%s, "
                             "невалидный шаг=%s) — финализацию НЕ выполняю",
                             timeout // 60, _wz_fail.get("workflowStep"),
                             _wz_fail.get("visibilityRadiosVisible"),
                             _wz_fail.get("isCurrentStepInvalid"))
                save_blackbox(driver, "visibility-not-reached")
                return {"success": False, "url": "",
                        "error": "мастер не дошёл до шага Visibility (шаг=%s) — ролик остался "
                                 "черновиком, нужен проход FINALIZE"
                                 % (_wz_fail.get("workflowStep") or "состояние недоступно"),
                        "visibility": "DRAFT"}

        # === 11.5. ЖДЁМ ЗАВЕРШЕНИЯ ЗАГРУЗКИ ФАЙЛА перед финализацией ===
        # Иначе для больших файлов Done жмётся во время аплоада → видео уходит в Draft
        # (расписание/публикацию нельзя применить к незавершённой загрузке).
        # Мелкие файлы грузятся мгновенно и проходили, большие (сотни МБ) — нет.
        if _finalizing:
            logger.info("FINALIZE: файл уже загружен (черновик) — пропускаю ожидание загрузки")
        else:
            logger.info("Жду завершения загрузки файла перед финализацией (до %d мин)...", timeout // 60)
        up_done = bool(_finalizing)
        up_start = time.time()
        seen_pct = False
        last_log = 0
        while not _finalizing and time.time() - up_start < timeout:
            # Факт «файл долит» берём у самого мастера (isVideoUploaded), а не из
            # текста «upload complete / загрузка завершена / …» на 9 языках: текстовый
            # гейт срабатывал РАНЬШЕ реального завершения, и финализация уходила
            # в незалитое видео → черновик.
            if YS is not None:
                _w = YS.read_wizard(driver)
                if _w.get("present"):
                    _pr = YS.read_upload_progress(driver) or {}
                    _p = _pr.get("pct")
                    st = {"pct": int(_p) if isinstance(_p, (int, float)) else -1,
                          "done": _w.get("isVideoUploaded") is True, "by_state": True}
                else:
                    st = {"pct": -1, "done": False, "by_state": True}
            else:
                st = driver.execute_script(r"""
                var d = document.querySelector('ytcp-uploads-dialog') || document.body;
                var t = (d.innerText || d.textContent || '').toLowerCase();
                var m = t.match(/(\d{1,3})\s*%/);
                var pct = m ? parseInt(m[1]) : -1;
                var done = /processing|upload complete|uploaded|checks complete|finished|обработ|загрузка заверш|hochgeladen|wird verarbeitet|en cours de traitement|traitement terminé|elaborazione|completato|przetwarz|przesyłanie zakończ|a processar|concluíd|se procesează|încărcare finaliz|zpracov|nahrávání dokonč/.test(t);
                return {pct: pct, done: done};
            """)
            pct = st.get("pct", -1)
            done = st.get("done", False)
            if pct is not None and pct >= 0:
                seen_pct = True
            if st.get("by_state"):
                # На state-пути признак ровно ОДИН: сам мастер сказал isVideoUploaded=true.
                # Процент из uploadDetailsByFrontendId — только для лога: read_upload_progress
                # отдаёт None при любом промахе deepFind (мастер перерисовался, ключи сменились),
                # и старое «процент исчез после того как был виден» переигрывало честный
                # isVideoUploaded=False → Publish/Schedule жались во время аплоада → Draft.
                _finished = bool(done)
            else:
                # текстовый фолбэк (YS is None) — оставлен как был, но он неточен
                _finished = bool(done or pct >= 100 or (seen_pct and pct < 0))
            if _finished:
                up_done = True
                logger.info("Загрузка файла завершена (pct=%s, isVideoUploaded=%s, по состоянию=%s)",
                            pct, done, bool(st.get("by_state")))
                break
            # videoId забираем ПО ХОДУ загрузки, а не одним ожиданием после отправки:
            # [замер 07.08] на клипе 300 КБ он приходит за ~8с, а на боевых 300 МБ через
            # прокси Studio заводит ролик только приняв файл — фиксированное ожидание в
            # 90с истекало впустую, и вердикт снова оставался без id.
            if YS is not None and not _wizard_vid:
                try:
                    _wizard_vid = (YS.read_wizard(driver) or {}).get("videoId") or ""
                    if _wizard_vid:
                        _mark_known_video_id(_wizard_vid)
                        logger.info("videoId ролика получен из мастера: %s", _wizard_vid)
                except Exception:
                    pass
            now = int(time.time() - up_start)
            if now - last_log >= 30:
                last_log = now
                logger.info("...загрузка идёт (%dс, %s%%)", now, pct if pct >= 0 else "?")
            time.sleep(5)
        if not up_done:
            # Продолжаем (файл мог долиться, а состояние — не прочитаться), но улики
            # снимаем здесь: иначе разбор очередного Draft снова будет вслепую.
            logger.warning("Не дождался подтверждения isVideoUploaded за %d мин — продолжаю (риск Draft)", timeout // 60)
            save_blackbox(driver, "upload-not-confirmed")

        # === 11.9. UPLOAD_ONLY (Фаза A): сохранить ЧЕРНОВИКОМ и выйти (не планировать). ===
        if upload_only:
            logger.info("UPLOAD_ONLY: сохраняю черновиком (Save and close) — без планирования")
            _saved = False
            for _ in range(3):
                try:
                    driver.execute_script(r"""
                        var ids=['#close-button','ytcp-button#close-button','#exit-button'];
                        for(var s of ids){var b=document.querySelector(s);
                          if(b){var t=b.querySelector('button,tp-yt-paper-button')||b;
                            var r=t.getBoundingClientRect();var o={bubbles:true,cancelable:true,view:window,clientX:r.left+r.width/2,clientY:r.top+r.height/2,button:0};
                            t.dispatchEvent(new MouseEvent('mousedown',o));t.dispatchEvent(new MouseEvent('mouseup',o));t.dispatchEvent(new MouseEvent('click',o));return;}}
                    """)
                except Exception:
                    pass
                time.sleep(3)
                if not driver.execute_script("var d=document.querySelector('ytcp-uploads-dialog'); return !!(d && d.offsetParent!==null);"):
                    _saved = True
                    break
            logger.info("UPLOAD_ONLY: диалог закрыт=%s — черновик сохранён, проверки в фоне", _saved)
            return {"success": True, "url": "DRAFT_UPLOADED", "error": "", "visibility": "DRAFT_UPLOADED"}

        # === 12. VISIBILITY — Schedule или Public ===
        time.sleep(2)

        # Страховка YPP не нужна, если сюда дошли по состоянию: шаг Monetization
        # заполняется и ПОДТВЕРЖДАЕТСЯ внутри сквозного прохода (advance_to_review →
        # set_monetization → #save-button → isCurrentStepInvalid=false).
        if vis_ready and YS is not None:
            logger.debug("Monetization-страховка не нужна — шаги пройдены сквозным проходом")
        else:
            # Прежняя страховка прыгала на #step-badge-1 и обратно на последний бейдж.
            # Без состояния мастера это гадание вслепую: у свежей заливки прыжок по
            # бейджам не работает вовсе (SELECTOR_MAP §2b), а на подготовленном мастере
            # сбивает уже выставленный шаг. Поэтому ничего не жмём — честно пишем, что
            # монетизация в мастере не подтверждена; её добирает пост-публикационный шаг.
            logger.warning("Monetization-страховка пропущена: состояние мастера недоступно "
                           "(yt_studio=%s) — шаг Monetization в мастере НЕ подтверждён",
                           YS is not None)

        if schedule_date:
            _dump_page(driver, "перед-отложкой")
            logger.info("Планирую публикацию на %s...", schedule_date.strftime("%Y-%m-%d %H:%M"))
            # Ретрай ×3 по образцу Public-петли ниже: _set_schedule само-гейтится (kind≠schedule→False,
            # date_mismatch→False), поэтому повтор Public утечь не даёт — чистый плюс к надёжности
            # (schedule_failed был осознанным safe-fail, но БЕЗ ретрая — 4 ролика легли зря).
            ok = False
            # ОСНОВНОЙ путь — по реальной структуре отложки (подтверждена дампом DOM):
            # это НЕ радио name=SCHEDULE (такого элемента в Studio нет), а
            # раскрывающаяся секция #second-container-expand-button + ytcp-datetime-picker,
            # причём поле даты живёт в ОТДЕЛЬНОМ диалоге ytcp-date-picker на уровне body
            # и принимает дату ТЕКСТОМ. Старый код кликал по числу в календаре без
            # перелистывания месяцев — промах на любую дату вне текущего месяца.
            if YS is not None:
                try:
                    ok = YS.set_schedule(driver, schedule_date)
                except Exception as _se:
                    logger.warning("Отложка (состояние) упала: %s — пробую старый путь", _se)
            if not ok:
                if YS is not None:
                    logger.warning("Отложка по состоянию не подтвердилась — фолбэк на старый путь")
                for _sched_attempt in range(3):
                    if _set_schedule(driver, schedule_date):
                        ok = True
                        break
                    logger.warning("Попытка %d: Schedule не применился, повтор через 2с", _sched_attempt + 1)
                    time.sleep(2)
            if not ok:
                # НЕ публиковать в Public при заданном расписании — иначе ролик уйдёт в эфир
                # на недели раньше срока, без сигнала об ошибке. Безопасный провал:
                # оставляем как есть (Private/Draft) и возвращаем ошибку.
                logger.error("Schedule НЕ удался (3 попытки) — отказываюсь публиковать в Public (оставляю Private/Draft, чтобы не выпустить раньше времени)")
                return {"success": False, "url": "", "error": "schedule_failed: не публикую в Public во избежание раннего эфира", "visibility": "draft"}
        else:
            logger.info("Выбираю публичный доступ...")
            _vis_ok = False
            if YS is not None:
                try:
                    # верификация по aria-checked самого радио + состоянию компонента,
                    # а не по тексту кнопки Publish/Опубликовать/… на 9 языках
                    _vis_ok = YS.set_visibility(driver, VISIBILITY)
                except Exception as _ve:
                    logger.warning("Видимость (состояние) упала: %s — старый путь", _ve)
            if not _vis_ok:
                for attempt in range(3):
                    if _set_public(driver, VISIBILITY):
                        _vis_ok = True
                        break
                    logger.warning("Попытка %d: Public не применился, повтор через 2с", attempt + 1)
                    time.sleep(2)
            # Ветка отложки при неудаче честно отказывается публиковать (safe-fail ниже),
            # а здесь три провалившиеся попытки просто заканчивались и код шёл жать
            # финальную кнопку с невыставленной видимостью → «Save» → черновик, о
            # котором прогон не сообщал. Сверяем намерение с состоянием.
            _conf_vis = _intent_confirmed(driver, None, VISIBILITY)
            if _conf_vis:
                _vis_ok = True                      # состояние — старший источник
            elif _conf_vis is False:
                _vis_ok = _rearm_intent(driver, None, VISIBILITY)
            else:
                logger.warning("Видимость %s состоянием не проверена (мастер не отдал состояние) — "
                               "полагаюсь на подтверждение самого клика (aria-checked радио)", VISIBILITY)
            if not _vis_ok:
                logger.error("Видимость %s НЕ применилась — не жму финальную кнопку, "
                             "иначе ролик молча уйдёт в черновик", VISIBILITY)
                save_blackbox(driver, "visibility-not-set")
                return {"success": False, "url": "",
                        "error": "visibility_failed: %s не подтверждён состоянием мастера" % VISIBILITY,
                        "visibility": "draft"}

        # === 13. SAVE / PUBLISH / SCHEDULE ===
        # ФИНАЛИЗАЦИЯ (основной путь): жмём Schedule/Publish и ждём ПОДТВЕРЖДЕНИЯ
        # от Studio. Два места, где заливка молча оставалась приватной:
        #   • поверх мастера всплывает диалог «We're still checking your content»
        #     с кнопкой #primary-action-button («Got it») и съедает клики по done;
        #   • ytcp-uploads-dialog после успеха НЕ закрывается, поэтому ожидание его
        #     закрытия истекало и заливка считалась неудачной, хотя всё прошло.
        _fin_ok = False
        _fin_txt = ""
        _pending_public_intent = (
            _intent_confirmed(driver, None, VISIBILITY)
            if _early_pending_enabled and schedule_date is None and VISIBILITY == "PUBLIC" else False
        )
        if YS is not None:
            try:
                # Окно ожидания подтверждения считаем от длины ролика: у часовых+
                # видео Studio отвечает медленнее, а 150с хватало только коротким.
                if _strict_private:
                    _fin_ok, _fin_txt = YS.finalize(
                        driver, timeout=min(timeout, 420),
                        private_video_id=_current_upload_phase().get("video_id") or "",
                        private_channel_uc=uc_id,
                    )
                else:
                    _fin_ok, _fin_txt = YS.finalize(driver, timeout=min(timeout, 420))
                if _fin_ok:
                    logger.info("Финализация: %s", str(_fin_txt)[:120])
                elif _fin_ok is None:
                    # подтверждения не увидели — это НЕ провал, решает состояние строки видео
                    logger.info("Финализация без подтверждения в диалоге (%s) — проверю по состоянию канала",
                                str(_fin_txt)[:60])
            except Exception as _fe:
                if _strict_private:
                    raise
                logger.warning("Финализация (состояние) упала: %s — старый путь", _fe)
        if _fin_ok:
            done_kind = "schedule" if schedule_date else VISIBILITY.lower()
        else:
            done_kind = _classify_done_button(driver)
            logger.info("Финальная кнопка классифицирована как: '%s' (ожидали publish/schedule)", done_kind)
        if _fin_ok:
            pass
        elif done_kind == "save" and VISIBILITY in ("PRIVATE", "UNLISTED"):
            # для приватного/по ссылке «Save» — ШТАТНАЯ кнопка, а не признак черновика:
            # публиковать нечего, YouTube сохраняет с выбранной видимостью
            logger.info("Кнопка 'Save' при visibility=%s — так и должно быть", VISIBILITY)
        elif done_kind == "save":
            logger.error("Критично: кнопка = 'Save' — видео будет Draft! Visibility не применился.")
        # ГЕЙТ перед финальной кнопкой. Раньше при кнопке «Save» печаталась ошибка и клик
        # всё равно делался — ролик молча уходил в черновик, а прогон шёл дальше как при
        # нормальной публикации. Жмём только когда намерение подтверждено СОСТОЯНИЕМ
        # (availabilityRadioSelection / aria-checked радио), а не подписью кнопки.
        _do_click = not (_strict_private and _fin_ok)
        if not _fin_ok:
            _conf_done = _intent_confirmed(driver, schedule_date, VISIBILITY)
            if _conf_done is False and not _rearm_intent(driver, schedule_date, VISIBILITY):
                logger.error("Намерение (%s) не подтверждено состоянием — НЕ жму финальную кнопку: "
                             "клик в этот момент = гарантированный черновик",
                             "schedule" if schedule_date else VISIBILITY)
                save_blackbox(driver, "intent-not-confirmed")
                _do_click = False
            elif _conf_done is None:
                logger.warning("Намерение состоянием не проверено (мастер не отдал состояние) — "
                               "жму финальную кнопку по классификации '%s'", done_kind)
        if _do_click:
            if _strict_private:
                YS.click_private_save(driver, _current_upload_phase().get("video_id") or "", uc_id)
            else:
                _click_done(driver)
        else:
            # Выходить прямо тут нельзя: YS.finalize мог уже нажать кнопку и заливка
            # состояться. Итог поэтому выносит строка ролика (раздел 15) — она же
            # честно покажет DRAFT, если публикация действительно не прошла.
            logger.error("Финальная кнопка НЕ нажата — итог определяет состояние строки ролика")

        # Handle any popup/dialog after save (copyright warning etc.)
        try:
            time.sleep(2)
            for btn in ([] if _strict_private else driver.find_elements(By.CSS_SELECTOR, "ytcp-button, tp-yt-paper-button, button")):
                txt = (btn.text or "").strip().lower()
                if txt in ("ok", "got it", "close", "dismiss", "continue", "хорошо", "продолжить"):
                    driver.execute_script("arguments[0].click()", btn)
                    time.sleep(1)
                    break
        except Exception:
            pass

        # === 13.4. "Publish anyway" — при незавершённых проверках/обработке YouTube показывает диалог
        # "We're still checking your content" с кнопками Publish anyway / Go back. КЛИКАЕМ "Publish anyway":
        # видео опубликуется ПО ГОТОВНОСТИ. Без этого клика остаётся Private/Draft (главная причина черновиков).
        _pa_clicked = ""
        if not schedule_date and not _strict_private:
            # Опрос в цикле: диалог "still checking → Publish anyway" может всплыть с задержкой
            # (после клика Publish). Жмём его как только появится → публикация в ОДИН проход, без добивок.
            # Окно ловли РАСШИРЕНО с ~12с (8×1.5) до времени-производного (гонка: на медленных/
            # больных прокси диалог «Publish anyway» всплывает ПОЗЖЕ 12с → раньше уходило в черновик,
            # 7/11 заливок 15.07). Бюджет ≈10% от timeout, клампы 30..150с. Ранний выход ТОЛЬКО когда
            # и upload-диалог закрыт, И нет промпта «still checking/publish anyway» (закрытый диалог сам
            # по себе ≠ опубликовано — было ложное draft). Обычный «processing» НЕ повод выходить.
            _pa_budget = min(150, max(30, int((timeout or 1800) * 0.1)))
            _pa_start = time.time()
            while time.time() - _pa_start < _pa_budget:
                try:
                    _pa_clicked = driver.execute_script(r"""
                        var re=/publish anyway|опубликовать всё равно|опубликовать все равно|trotzdem ver|publier quand m|pubblica comunque|opublikuj mimo to|publikovat i tak|přesto zve|publica oricum|publicar mesmo/i;
                        var btns=document.querySelectorAll('ytcp-button, tp-yt-paper-button, button, [role=button], yt-button-shape');
                        for(var i=0;i<btns.length;i++){var b=btns[i];var t=((b.innerText||b.textContent)||'').trim();
                            if(t && re.test(t)){var r=b.getBoundingClientRect(); if(r.width>0&&r.height>0){
                                var o={bubbles:true,cancelable:true,view:window,clientX:r.left+r.width/2,clientY:r.top+r.height/2,button:0};
                                b.dispatchEvent(new MouseEvent('mousedown',o));b.dispatchEvent(new MouseEvent('mouseup',o));b.dispatchEvent(new MouseEvent('click',o));
                                return t;}}}
                        return "";
                    """)
                except Exception:
                    _pa_clicked = ""
                if _pa_clicked:
                    logger.info("Кликнул 'Publish anyway' (%s, %.0fс) — публикация по готовности (один проход)", _pa_clicked, time.time() - _pa_start)
                    time.sleep(3)
                    break
                # ранний выход: главный upload-диалог закрыт И нет промпта «still checking / publish anyway»
                _done = driver.execute_script(r"""
                    var d=document.querySelector('ytcp-uploads-dialog');
                    var dlgOpen = d && d.offsetParent!==null;
                    var t=((document.body&&document.body.innerText)||'').toLowerCase();
                    var stillChecking=/still checking|we're still|we are still|publish anyway|опубликовать всё равно|опубликовать все равно|проверяем ваш|мы (всё ещё |все еще )?проверя|noch (immer )?überprüf|kontrolujeme|přesto zve|sprawdzamy|weryfik|verificăm|verificamos/.test(t);
                    return (!dlgOpen && !stillChecking);
                """)
                if _done:
                    break
                time.sleep(2)

        # === 13.5. ДОБИВКА, если подтверждения финализации не было ===
        # Закрытие ytcp-uploads-dialog признаком публикации НЕ является ни в одну сторону:
        # после успеха мастер остаётся открытым (SELECTOR_MAP §2c), а закрыться он может и
        # при Save-as-draft, и по крестику, и при перерисовке Polymer. Прежнее «ждём 20с,
        # что диалог исчезнет» давало ложное «диалог не закрылся» на успешных заливках
        # (21 из 24 в батче 06.08) и запускало слепые переклики поверх принятой публикации,
        # а обратная надпись «публикация завершена» печаталась там, где публикации не было.
        # Настоящий вердикт выносит строка ролика — раздел 15.
        if _fin_ok:
            logger.info("Финализация подтверждена Studio — добивка не нужна")
        else:
            _wz_now = YS.read_wizard(driver) if YS is not None else {}
            if _wz_now.get("present") is False:
                logger.info("Мастер не на экране — добивать нечего, вердикт вынесет строка видео")
            else:
                # Прежде чем добивать — проверить, не Google ли держит кнопку. Модалку
                # «Verify it's you» переклики не пробивают, и каждая попытка стоит минут.
                if auth_gate_present(driver):
                    save_blackbox(driver, "auth-gate")
                    raise RuntimeError(
                        "AUTH_GATE: Google требует подтверждение личности (Verify it's you) "
                        f"на профиле {profile_id} — файл залит, но опубликовать нельзя. "
                        "Ретраи бесполезны: нужен человек с телефоном аккаунта, "
                        "либо залить этот канал с другой машины")
                logger.warning("Подтверждения финализации не было (шаг=%s) — переармирую visibility и добиваю done",
                               _wz_now.get("workflowStep"))
                # Ретрай ×5. КЛЮЧЕВОЕ: перед каждым перекликом проверяем тип кнопки. Если она стала
                # 'save' — значит visibility слетел (Polymer перерисовал шаг), и клик в этот момент =
                # ГАРАНТИРОВАННЫЙ Draft (именно так родились тихие черновики). Переармируем ТЕМ ЖЕ
                # намерением (schedule→schedule, public→public — НИКОГДА не подменяем Schedule на Public),
                # и только потом кликаем #done-button.
                for _ in range(5):
                    # Раньше здесь звался СТАРЫЙ путь отложки (_set_schedule ищет
                    # несуществующее радио name=SCHEDULE и кликает по числу календаря без
                    # перелистывания месяцев), а его возврат выбрасывался — #done-button
                    # жался в любом случае. Теперь: перекликаем только по подтверждённому
                    # состоянию, иначе прекращаем добивку — вердикт вынесет строка ролика.
                    if _intent_confirmed(driver, schedule_date, VISIBILITY) is False:
                        logger.warning("Намерение (%s) слетело перед перекликом — переармирую",
                                       "schedule" if schedule_date else VISIBILITY.lower())
                        if not _rearm_intent(driver, schedule_date, VISIBILITY):
                            logger.error("Переармирование состоянием не подтверждено — #done-button "
                                         "НЕ жму (клик дал бы черновик/не тот день)")
                            save_blackbox(driver, "rearm-not-confirmed")
                            break
                        time.sleep(1)
                    if _strict_private:
                        YS.click_private_save(driver, _current_upload_phase().get("video_id") or "", uc_id)
                    else:
                        driver.execute_script("""
                            var b = document.querySelector('#done-button');
                            if (b) {
                                var t = b.querySelector('button, tp-yt-paper-button') || b;
                                var r = t.getBoundingClientRect();
                                var o = {bubbles:true,cancelable:true,view:window,clientX:r.left+r.width/2,clientY:r.top+r.height/2,button:0};
                                t.dispatchEvent(new MouseEvent('mousedown',o));
                                t.dispatchEvent(new MouseEvent('mouseup',o));
                                t.dispatchEvent(new MouseEvent('click',o));
                            }
                        """)
                    time.sleep(3)
                    # Выход из добивки — по ПОДТВЕРЖДЕНИЮ Studio (финальный диалог со ссылкой
                    # на ролик), а не по исчезновению мастера.
                    _fs = YS.read_final_state(driver) if YS is not None else {}
                    if _fs.get("finalDialog"):
                        _fin_ok, _fin_txt = True, _fs["finalDialog"]
                        logger.info("Подтверждение получено после добивки: %s", str(_fin_txt)[:110])
                        break

        # 14. Получаем URL
        video_url = _extract_url(driver)

        if video_url:
            logger.info("Видео загружено: %s", video_url)
        else:
            logger.info("Загрузка завершена (URL не получен)")

        # === 14.5. ДИАГНОСТИКА: файл ещё заливается/обрабатывается ===
        # Раньше отсюда выходили с готовым вердиктом: слово «processing» в тексте ВСЕЙ
        # страницы (куда попадает список чужих роликов) + факт клика «Publish anyway»
        # решали, успех это или черновик, и реальное состояние нашего ролика не читалось
        # ни разу. Теперь это только запись в лог: принятую публикацию от черновика
        # различает строка ролика (PROCESSING против DRAFT) — раздел 15.
        if not schedule_date:
            try:
                # Факт «ещё обрабатывается» берём из состояния мастера, а не из слов на
                # странице: список видео канала на том же экране содержит слово Processing
                # для ЧУЖИХ роликов, и текстовый детектор срабатывал не на том. Список из
                # 14 языков всё равно не покрывал интерфейсы вроде испанского.
                _wz = YS.read_wizard(driver) if YS is not None else {}
                if _wz.get("present"):
                    _pr = YS.read_upload_progress(driver) or {}
                    _pct = _pr.get("pct")
                    still_proc = (not bool(_wz.get("isVideoUploaded"))
                                  or (_pct is not None and _pct < 100))
                    logger.info("Обработка (состояние мастера): isVideoUploaded=%s pct=%s status=%s",
                                _wz.get("isVideoUploaded"), _pct, _pr.get("status"))
                else:
                    still_proc = False
            except Exception as _pe:
                # Состояние мастера не прочиталось — гадать по словам на странице нельзя
                # (там же список чужих роликов со своими «Processing»). Оставляем факт
                # неизвестным и идём к вердикту по строке ролика.
                logger.warning("Состояние мастера не прочитано (%s) — «обрабатывается ли» не знаю", _pe)
                still_proc = False
            if still_proc:
                logger.info("Файл ещё заливается/обрабатывается (publish_anyway=%s) — статус решит строка ролика",
                            bool(_pa_clicked))

        # === 15. ВЕРДИКТ — ТОЛЬКО по состоянию строки ролика ===
        # Строка ytcp-video-row отдаёт draftStatus / privacy / visibility.userSetVisibility —
        # машинные константы, одинаковые на любом языке интерфейса. Всё остальное
        # (закрытие мастера, текст финального диалога, слова в строке) признаком не
        # является: Studio пишет «Video published» и для запланированного ролика, а
        # слова Draft/Public лежат в скрытом дропдауне видимости ВСЕГДА.
        # На /video/{id}/edit не ходим: эта навигация во время обработки роняет
        # ожидающую публикацию в Draft.
        # ожидаем ТЕРМИНАЛЬНОЕ состояние (PROCESSING сюда не входит — это ещё не итог)
        _want_states = {"SCHEDULED"} if schedule_date else {VISIBILITY.upper()}
        # id из мастера — первичный источник: он известен ещё до конца загрузки и не
        # зависит ни от ссылки в диалоге (её при отложке нет), ни от заголовка.
        _vid_id = _wizard_vid or (video_url.rstrip('/').split('/')[-1].split('?')[0] if video_url else "")
        _row, _kind = _row_by_state(driver, uc_id, channel_url, _st,
                                    video_id=_vid_id, timeout=180)
        _early_pending = _early_pending_enabled and _early_public_pending_allowed(
            _row, enabled=_early_pending_enabled, strict=_strict, visibility=VISIBILITY,
            schedule_date=schedule_date, video_id=_current_upload_phase().get("video_id") or "",
            verified_channel_uc=uc_id, current_url=driver.current_url, requested_title=title,
            upload_complete_proven=_upload_complete_proven, thumbnail_proof=_thumbnail_proof,
            thumbnail_sha256=_thumbnail_expected_sha256, thumbnail_filename=os.path.basename(thumbnail_path or ""),
            public_intent_confirmed=_pending_public_intent is True and _do_click is True,
        )
        if _early_pending:
            logger.info("PUBLIC request подтверждён для exact id=%s; файл и обложка приняты. "
                        "Пропускаю 120с PROCESSING; итог PUBLIC_PENDING требует отдельного readback.",
                        _row.get("videoId"))
        if _row is not None and _kind not in _want_states and not _early_pending:
            # Studio обновляет строку с задержкой: сразу после финализации ролик ещё
            # показывается черновиком, хотя расписание уже принято. Даём состоянию
            # устояться, прежде чем выносить приговор.
            logger.info("Строка пока %s — жду ожидаемого состояния (%s)",
                        _kind, "/".join(sorted(_want_states)))
            _row2, _kind2 = _settle_row(driver, uc_id, channel_url,
                                        _row.get("videoId"), _want_states, timeout=120)
            if _row2 is not None:
                _row, _kind = _row2, _kind2
        if _row is None:
            actual_visibility = "unknown"
            logger.error("Строка ролика в списке канала не найдена — заливка НЕ подтверждена "
                         "(подтверждение мастера: %s)",
                         str(_fin_txt)[:80] if _fin_txt else "не было")
            save_blackbox(driver, "not-verified")
        else:
            actual_visibility = _kind
            if not video_url and _row.get("videoId"):
                # У запланированного ролика финальный диалог ссылки не даёт — берём id из
                # строки. Но строку мы нашли по заголовку, а он у соседних выпусков похож:
                # если мастер уже назвал номер нашей загрузки, чужой сюда попасть не должен,
                # иначе итоговый результат разойдётся с зафиксированным номером и заливка,
                # которая на деле прошла, будет объявлена сбоем.
                if not _wizard_vid or _row["videoId"] == _wizard_vid:
                    video_url = f"https://youtu.be/{_row['videoId']}"
                else:
                    video_url = f"https://youtu.be/{_wizard_vid}"
                    logger.warning(
                        "Строка канала указывает на другой ролик (%s) — ссылку строю по id "
                        "мастера (%s)", _row["videoId"], _wizard_vid)

        # Exact title proof is anchored to the same final row/videoId as the
        # visibility verdict.  Substring/NFD-tolerant lookup remains useful for
        # finding the row, but it is not proof that Studio stored the requested
        # title.  The receipt contains only the normalized hash, never title text.
        _row_video_id = str((_row or {}).get("videoId") or "")
        _row_video_id_valid = re.fullmatch(
            r"[A-Za-z0-9_-]{11}", _row_video_id or ""
        ) is not None
        # Строку в списке канала мы ищем по заголовку, а заголовки у соседних выпусков
        # похожи — найденная строка вполне может оказаться чужим роликом. Номер, выданный
        # мастером ИМЕННО нашей загрузке, надёжнее: подменять его найденным нельзя, иначе
        # контракт видит смену номера и валит заливку, которая на деле прошла.
        if _row_video_id_valid and (not _wizard_vid or _row_video_id == _wizard_vid):
            _mark_known_video_id(_row_video_id)
        elif _row_video_id_valid and _wizard_vid and _row_video_id != _wizard_vid:
            logger.warning(
                "В списке канала нашлась строка с другим id (%s) — веду свой ролик по id "
                "мастера (%s); совпадение заголовков не доказывает, что это наш ролик",
                _row_video_id, _wizard_vid)
        _title_proof = _exact_title_proof(title, (_row or {}).get("title"))
        if (
            _row is not None
            and not _title_proof["matches"]
            and YS is not None
            and _row_video_id_valid
        ):
            # Studio can publish the terminal visibility a few seconds before
            # its list row receives the final title.  Re-read only this exact
            # videoId; never fall back to another similar title.
            _fresh_title_row = YS.wait_until(
                driver,
                lambda d, _vid=_row_video_id: next(
                    (
                        item for item in (YS.read_videos(d) or [])
                        if item.get("videoId") == _vid
                        and _exact_title_proof(title, item.get("title"))["matches"]
                    ),
                    None,
                ),
                timeout=30,
                poll=5,
                desc="точный заголовок строки ролика",
            )
            if _fresh_title_row is not None:
                _row = _fresh_title_row
                _kind = YS.classify(_row)
                actual_visibility = _kind
                _title_proof = _exact_title_proof(title, _row.get("title"))
        if _row is not None and _row_video_id_valid and _title_proof["matches"]:
            logger.info(
                "Заголовок строки подтверждён точно: id=%s normalized_sha256=%s",
                _row_video_id, _title_proof["normalized_title_sha256"],
            )
        elif _row is not None and not _title_proof["matches"]:
            logger.error(
                "Заголовок строки НЕ совпал точно: id=%s requested_sha256=%s "
                "observed_sha256=%s — videoId уже известен, перезаливать НЕЛЬЗЯ",
                _row_video_id or "unknown",
                _title_proof["normalized_title_sha256"],
                _title_proof["observed_normalized_sha256"],
            )
        elif _row is not None:
            logger.error(
                "Заголовок совпал, но exact videoId строки невалиден: %r — "
                "квитанцию не выпускаю",
                _row_video_id,
            )

        # Трёх прежних подстраховок здесь больше НЕТ — каждая выносила вердикт словами:
        #   • статус с /video/{id}/edit по [id*=visibility] (8 языков, без es/nl/tr) — слово
        #     «Public» лежит в скрытом дропдауне всегда, черновик мог стать «публичным»,
        #     а сам заход на /edit во время обработки роняет ожидающую публикацию в Draft;
        #   • отдельный поиск строки по заголовку — раздел 15 ищет одинаково (videoId, иначе
        #     YS.find_video с NFC-нормализацией), а отсутствие ссылки в диалоге для
        #     запланированного ролика норма и о статусе ничего не говорит;
        #   • regex isDraft по textContent строки и regex по тексту финального диалога —
        #     первый объявлял черновиком запланированные ролики, второй считал успехом
        #     предупреждение «Video processing».

        # Успех = состояние строки совпало с намерением заливки. «unknown» — это ПРОВАЛ
        # верификации, а не успех: раньше такой ролик уезжал в __UPLOAD_SUCCESS__,
        # upload_manager записывал uploaded_url, и dedup к нему больше не возвращался.
        success = actual_visibility in _want_states
        error = ""
        if (not success and actual_visibility == "PROCESSING"
                and _processing_public_pending_allowed(
                    _row, visibility=VISIBILITY, schedule_date=schedule_date,
                    video_id=_current_upload_phase().get("video_id") or "")):
            # PROCESSING — это НЕ черновик: у черновика строка отдаёт
            # effectiveStatus=…_DRAFT / draftStatus≠DRAFT_STATUS_NONE, а тут файл принят и
            # ещё обрабатывается, итоговая видимость применится по готовности. Терминального
            # состояния мы не дождались. PUBLIC_PENDING разрешён только для точного
            # PUBLIC-намерения и той же строки; остальные PROCESSING не являются успехом.
            success = True
            actual_visibility = "PUBLIC_PENDING"
            logger.info("Ролик принят и обрабатывается — видимость применится по готовности: %s",
                        video_url or "(ссылки нет)")
        if not success:
            error = ("состояние ролика %s, ожидали %s"
                     % (actual_visibility, "/".join(sorted(_want_states))))
            logger.error("Вердикт по состоянию: %s (url=%s)", error, video_url or "нет")
            if actual_visibility == "DRAFT":
                # главный симптом, ради которого всё это: снимаем улики сразу,
                # чтобы причина разбиралась по состоянию, а не по пересказу
                save_blackbox(driver, "draft")

        # SCHEDULED говорит только «отложка принята», но НЕ «в нужный момент». Итоговая
        # сверка — по машинному scheduledPublishingDetails...scheduledTimeSeconds.
        # schedule всегда absolute ISO с offset: сверяем ровно исходный Unix epoch.
        _verified_schedule_epoch = 0
        _observed_schedule_epoch = 0
        if success and schedule_date and _row is not None:
            try:
                _sched_ts = int(_row.get("scheduledTime") or 0)
            except (TypeError, ValueError):
                _sched_ts = 0
            _want_ts = _schedule_exact_epoch
            if _sched_ts <= 0 and YS is not None and _row.get("videoId"):
                # строка приходит в список раньше, чем Studio дописывает в неё
                # scheduledPublishingDetails — перечитываем ТУ ЖЕ строку по videoId,
                # чтобы не объявить провалом лаг обновления списка
                _fresh = YS.wait_until(
                    driver,
                    lambda d, _vid=_row.get("videoId"): next(
                        (r for r in (YS.read_videos(d) or [])
                         if r.get("videoId") == _vid and int(r.get("scheduledTime") or 0) > 0), None),
                    timeout=30, poll=5, desc="scheduledTime строки ролика")
                if _fresh:
                    _sched_ts = int(_fresh.get("scheduledTime") or 0)
            _observed_schedule_epoch = _sched_ts
            if _sched_ts <= 0:
                logger.error("Отложка НЕ подтверждена машинно: у строки ролика нет "
                             "scheduledPublishingDetails (просили %s)",
                             schedule_date.strftime("%Y-%m-%d %H:%M"))
                success, actual_visibility = False, "SCHEDULE_UNVERIFIED"
                error = "scheduled_time_missing: расписание не подтверждено состоянием строки"
                save_blackbox(driver, "schedule-unverified")
            elif not _scheduled_epoch_matches(_sched_ts, _want_ts):
                _got = datetime.fromtimestamp(_sched_ts, timezone.utc).isoformat()
                _wanted = datetime.fromtimestamp(_want_ts, timezone.utc).isoformat()
                logger.error(
                    "Отложка на ЧУЖОЙ absolute instant: Studio epoch=%s (%s), "
                    "просили epoch=%s (%s). Ролик уже на канале — перезаливать "
                    "НЕЛЬЗЯ, расписание нужно исправить у этого videoId.",
                    _sched_ts, _got, _want_ts, _wanted,
                )
                success, actual_visibility = False, "SCHEDULED_WRONG_TIME"
                error = ("scheduled_wrong_time: epoch %s вместо %s"
                         % (_sched_ts, _want_ts))
                save_blackbox(driver, "schedule-wrong-date")
            else:
                _verified_schedule_epoch = _sched_ts
                logger.info(
                    "Отложка подтверждена машинно: scheduledTime=%s, ожидали %s "
                    "(режим=%s, picker=%s, browser_tz=%s)",
                    _sched_ts, _want_ts, _schedule_mode,
                    schedule_date.strftime("%Y-%m-%d %H:%M"),
                    _schedule_browser_tz or "unknown",
                )

        # A known final videoId is durable evidence that a re-upload would make
        # a duplicate.  Return a failure on exact title mismatch, but keep that
        # ID and row evidence structured so orchestration fixes this same video.
        _title_mismatch = bool(_row is not None and not _title_proof["matches"])
        if _title_mismatch and success:
            success = False
            actual_visibility = "TITLE_MISMATCH"
            error = (
                "title_mismatch: Studio row title differs after NFC/whitespace/casefold; "
                f"known video_id={_row_video_id}; do not reupload"
            )

        _studio_row_evidence = None
        if _row is not None:
            _studio_row_evidence = {
                "verified_channel_uc": uc_id,
                "row_video_id": _row_video_id,
                "studio_state": _kind,
                "visibility_user_set_visibility": str(_row.get("userSetVisibility") or ""),
                "scheduled_time_seconds": int(_observed_schedule_epoch or 0),
                "normalized_title_sha256": _title_proof["normalized_title_sha256"],
                "observed_normalized_title_sha256": _title_proof["observed_normalized_sha256"],
                "title_matches": bool(_title_proof["matches"]),
            }
        _exact_studio_evidence = None
        if (
            success
            and _kind == "SCHEDULED"
            and _row is not None
            and _row.get("userSetVisibility") == "VIDEO_USER_SET_VISIBILITY_SCHEDULED"
            and _verified_schedule_epoch > 0
            and _title_proof["matches"]
            and _row_video_id_valid
        ):
            _exact_studio_evidence = {
                "verified_channel_uc": uc_id,
                "row_video_id": _row_video_id,
                "studio_state": "SCHEDULED",
                "visibility_user_set_visibility": "VIDEO_USER_SET_VISIBILITY_SCHEDULED",
                "scheduled_time_seconds": _verified_schedule_epoch,
                "normalized_title_sha256": _title_proof["normalized_title_sha256"],
            }

        # AI-дисклоуз проверяем ТЕМ ЖЕ способом, что и видимость — по состоянию строки
        # (alteredContentSettings.creatorDisclosedHasAlteredContent, SELECTOR_MAP §1).
        # Раньше результат простановки метки нигде не проверялся: ролик мог уехать на
        # канал без AI-метки, а прогон рапортовал чистый успех. Успех тут не отменяем
        # (ролик опубликован, повторная заливка сделала бы дубль), но признак отдаём
        # наверх явно, а расхождение пишем ошибкой.
        _ai_disclosed = None if _row is None else bool(_row.get("alteredDisclosed"))
        if _ai_disclosed is False:
            logger.error("AI-дисклоуз НЕ проставлен: строка ролика отдаёт alteredDisclosed=false "
                         "(в мастере отметка %s) — метку нужно доставить вручную",
                         "подтверждалась" if _ai_ok else "не подтвердилась")
        elif _ai_disclosed:
            logger.info("AI-дисклоуз подтверждён состоянием строки")

        # Пост-публикация монета+A/B — ТОЛЬКО при подтверждённом PUBLIC/SCHEDULED (не Draft/Private/Unlisted):
        # выпустить раньше времени невозможно (правим страницы настроек, не visibility).
        _boost = {"monetized": False, "ab_test": False}
        if success and actual_visibility in ("PUBLIC", "SCHEDULED") and video_url:
            _vid = video_url.rstrip('/').split('/')[-1].split('?')[0]
            _boost = _post_publish_boost(driver, uc_id, _vid, monetize, ab_thumbnails, collaborator,
                                             midrolls and not _wizard_midrolls,
                                             chapters, midroll_step or None)

        return {"success": success, "url": video_url, "error": error, "visibility": actual_visibility,
                "monetized": _boost["monetized"], "ab_test": _boost["ab_test"],
                                "collaborator": _boost.get("collaborator", False),
                                "midrolls": _boost.get("midrolls", False),
                                # None = состояние строки не прочитано (сама заливка тогда
                                # тоже не подтверждена), False = метки на ролике НЕТ
                                "altered_disclosed": _ai_disclosed,
                                "video_id": _row_video_id,
                                "do_not_reupload": bool(_row_video_id),
                                "studio_row_evidence": _studio_row_evidence,
                                "exact_studio_evidence": _exact_studio_evidence}

    except Exception as e:
        logger.error("Ошибка: %s", e)
        if not isinstance(e, StrictAuthGateError) or isinstance(e, StrictUploadEligibilityError):
            save_blackbox(driver, "exception")
        if isinstance(e, StrictAuthGateError):
            _terminal_failure.update(upload_contract.failure_result(str(e), _current_upload_phase()))
        else:
            _terminal_failure.update(success=False, url="", error=str(e), visibility="unknown")
        return _terminal_failure

    finally:
        if (_strict and _file_handoff_attempted and driver is not None
                and upload_contract.phase_requires_no_reupload(_current_upload_phase()["phase"])
                and not _strict_transfer_may_close(
                    driver, completion_proven=_upload_complete_proven, timeout=min(timeout, 120))):
            _terminal_failure.update(upload_contract.failure_result(
                "UPLOAD_IN_PROGRESS_PROFILE_RETAINED: MP4 completion unproven; "
                "browser left open, shared lease must be retained; do not reupload; "
                + _terminal_failure.get("error", ""),
                _current_upload_phase(), {"visibility": "UNKNOWN"},
            ))
        else:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            stop_profile(profile_id)


def _dump_page(driver, tag, logger_=None):
    """Снимок того, что РЕАЛЬНО на странице: шаг мастера, поля, видимые кнопки.

    Пишется в лог заливки на ключевых шагах, чтобы разбор потом шёл по фактам,
    а не по догадкам: видно, какие кнопки были доступны и какого они размера
    (нулевой размер = элемент есть в DOM, но не отрисован — частая причина
    «кнопка не нажалась» и «поле не заполнилось»).
    Включается переменной WORKER_PAGE_DUMP=1, чтобы не раздувать обычные прогоны.
    """
    if os.getenv("WORKER_PAGE_DUMP", "1") != "1":
        return
    lg = logger_ or logger
    try:
        info = driver.execute_script(r"""
            function deepAll(root){
              var out=[], st=[root];
              while(st.length){ var n=st.pop();
                if(!n) continue;
                if(n.nodeType===1) out.push(n);
                var k=n.children||[];
                for(var i=0;i<k.length;i++) st.push(k[i]);
                if(n.shadowRoot) st.push(n.shadowRoot);
              }
              return out;
            }
            var all=deepAll(document), btns=[], flds=[], step='';
            for(var i=0;i<all.length;i++){
              var e=all[i], tg=(e.tagName||'').toLowerCase(), r=e.getBoundingClientRect();
              if(tg==='ytcp-uploads-dialog'){ try{ step=e.workflowStep||''; }catch(err){} }
              var isBtn=(tg==='button'||tg==='ytcp-button');
              var isFld=((e.id||'')==='textbox'||tg==='input'||tg==='textarea');
              if(!isBtn && !isFld) continue;
              var tx=((e.innerText||'')+' '+((e.getAttribute&&e.getAttribute('aria-label'))||'')).trim().replace(/\s+/g,' ');
              var rec={t:tx.slice(0,34), w:Math.round(r.width), h:Math.round(r.height),
                       dis:!!(e.disabled||e.getAttribute('aria-disabled')==='true')};
              if(isBtn){ if(tx) btns.push(rec); } else { flds.push(rec); }
            }
            return {url:location.href.slice(0,110), step:step,
                    title:(document.title||'').slice(0,60),
                    buttons:btns.slice(0,18), fields:flds.slice(0,10)};
        """) or {}
    except Exception as e:                      # noqa: BLE001
        lg.warning("PAGE_DUMP[%s]: снять не удалось: %s", tag, str(e)[:80])
        return
    lg.info("PAGE_DUMP[%s] url=%s шаг=%s", tag, info.get("url"), info.get("step") or "—")
    for b in info.get("buttons") or []:
        lg.info("PAGE_DUMP[%s]   кнопка %-34s %sx%s%s", tag, b.get("t"),
                b.get("w"), b.get("h"), " DISABLED" if b.get("dis") else "")
    for f in info.get("fields") or []:
        lg.info("PAGE_DUMP[%s]   поле   %-34s %sx%s", tag, f.get("t"),
                f.get("w"), f.get("h"))


def _wait_file_processing(driver, timeout, video_id="", needle=""):
    """После resume-дозагрузки ждём, пока файл РЕАЛЬНО догрузится. True если дождались.

    Признак берём у состояния, а не у текста страницы:
      * мастер на экране → `ytcp-uploads-dialog.isVideoUploaded is True`
        (SELECTOR_MAP §ytcp-uploads-dialog; проценты из uploadDetailsByFrontendId — в лог);
      * мастера нет (список видео) → строка ролика, `videoPrechecks.*` = prechecksDone:
        проверки копирайта/brand safety не завершаются на недолитом файле.
    Старый текстовый детектор («processing|обработ|…» по document.body.innerText + regex
    процента по всей странице) ловил слова из ЧУЖИХ строк списка и объявлял готовность
    раньше времени — ровно так финализация уходила в незалитое видео и давала черновик.
    Текстовый путь остался фолбэком на случай, когда модуля состояния нет.
    """
    if YS is not None:
        state = {"logged": 0}

        def _done(d):
            w = YS.read_wizard(d)
            if w.get("present"):
                if w.get("isVideoUploaded") is True:
                    return True
                pr = YS.read_upload_progress(d) or {}
                state["pct"] = pr.get("pct")
                return False
            # мастер закрылся/не открывался — судим по строке нашего ролика
            rows = YS.read_videos(d) or []
            row = None
            if video_id:
                row = next((r for r in rows if r.get("videoId") == video_id), None)
            if row is None and rows and needle:
                row, _ = YS.find_video(d, needle, videos=rows)
            return bool(row and row.get("prechecksDone"))

        def _tick(elapsed, _last):
            if elapsed - state["logged"] >= 30:
                state["logged"] = elapsed
                _p = state.get("pct")
                logger.info("RESUME: дозагрузка идёт (%dс, %s%%)", int(elapsed),
                            _p if isinstance(_p, (int, float)) else "?")

        return bool(YS.wait_until(driver, _done, timeout=timeout, poll=5.0,
                                  desc="дозагрузка файла (isVideoUploaded/prechecks)",
                                  on_tick=_tick))

    logger.warning("RESUME: yt_studio нет — завершение дозагрузки определяю по тексту "
                   "страницы (фолбэк, срабатывает раньше реального завершения)")
    start = time.time(); seen = False; last = 0
    while time.time() - start < timeout:
        try:
            st = driver.execute_script(r"""
                var t=(document.body.innerText||document.body.textContent||'').toLowerCase();
                var m=t.match(/(\d{1,3})\s*%/);
                var pct=m?parseInt(m[1]):-1;
                var done=/processing|checks complete|finished|обработ|wird verarbeitet|en cours de traitement|elaborazione|przetwarz|a processar|se procesează|zpracov|upload complete|загрузка заверш|hochgeladen/.test(t);
                return {pct:pct, done:done};
            """)
        except Exception:
            st = {"pct": -1, "done": False}
        pct = st.get("pct", -1); done = st.get("done", False)
        if pct is not None and pct >= 0:
            seen = True
        if done or pct >= 100 or (seen and pct < 0):
            return True
        n = int(time.time() - start)
        if n - last >= 30:
            last = n
            try:
                logger.info("RESUME: дозагрузка идёт (%dс, %s%%)", n, pct if pct >= 0 else "?")
            except Exception:
                pass
        time.sleep(5)
    return False


def _fill_monetization_step(driver) -> bool:
    """Шаг Monetization мастера (YPP): включить рекламу — ТОЛЬКО по состоянию компонента.

    Прежняя реализация судила о монетизации по английскому слову в innerText
    (`\\bOn\\b`), выбирала опцию по точному тексту 'On' и им же подтверждала результат.
    На любой другой локали («Вкл», «Ein», «Wł.») это не срабатывало никогда, а сама
    механика была неверной: дампом 2026-08-03 установлено, что контрол — не дропдаун,
    а внутренняя `ytcp-icon-button` + радио `#radio-on/#radio-off`, и без
    `ytcp-button#save-button` («Done») шаг остаётся невалидным.

    Вся эта работа уже сделана в `yt_studio.set_monetization` (клики realClick сквозь
    shadow DOM, подтверждение радио по aria-checked, «Done», проверка
    `isCurrentStepInvalid === false`) — здесь только делегирование, чтобы у внешних
    вызовов был один честный путь.
    """
    if YS is None:
        logger.error("Monetization: нет yt_studio — шаг по состоянию не заполнить "
                     "(текстовый путь по слову 'On' убран как язык-зависимый и неверный)")
        return False
    try:
        return bool(YS.set_monetization(driver, on=True))
    except Exception as e:
        logger.warning("Monetization: заполнение шага упало: %s", e)
        return False


VISIBILITY = "PUBLIC"   # переопределяется из --visibility в main()


def _set_public(driver, target="PUBLIC"):
    """Выбирает видимость на странице Visibility (PUBLIC / PRIVATE / UNLISTED).
    Hack: ytcp-uploads-review перекрывает radio — временно скрываем overlay,
    dispatch real MouseEvent на radio, восстанавливаем overlay.
    Верифицирует что #done-button стал 'Publish' (язык-независимо через _classify_done_button).
    """
    result = driver.execute_script("""
        var overlay = document.querySelector('ytcp-uploads-review');
        var originalDisplay = null;
        if (overlay) {
            originalDisplay = overlay.style.display;
            overlay.style.display = 'none';
        }

        var want = (arguments[0] || 'PUBLIC').toUpperCase();
        var radios = document.querySelectorAll('tp-yt-paper-radio-button');
        var publicRadio = null;
        for (var r of radios) {
            var n = (r.getAttribute('name') || '').toUpperCase();
            if (n === want) { publicRadio = r; break; }
        }

        if (!publicRadio) {
            if (overlay) overlay.style.display = originalDisplay || '';
            return {ok: false, error: 'Public radio not found', count: radios.length};
        }

        publicRadio.scrollIntoView({block: 'center'});

        var rect = publicRadio.getBoundingClientRect();
        var cx = rect.left + rect.width / 2;
        var cy = rect.top + rect.height / 2;
        var evOpts = {bubbles:true, cancelable:true, view:window, clientX:cx, clientY:cy, button:0};
        publicRadio.dispatchEvent(new MouseEvent('mousedown', evOpts));
        publicRadio.dispatchEvent(new MouseEvent('mouseup', evOpts));
        publicRadio.dispatchEvent(new MouseEvent('click', evOpts));

        if (overlay) overlay.style.display = originalDisplay || '';

        return {
            ok: true,
            checked: publicRadio.checked,
            aria_checked: publicRadio.getAttribute('aria-checked')
        };
    """, target)

    if not result.get("ok"):
        logger.warning("Public radio: %s (total radios: %s)", result.get("error"), result.get("count"))
        return False

    time.sleep(1.5)
    # Re-verify checked state (язык-независимо через name=PUBLIC).
    final = driver.execute_script("""
        var want = (arguments[0] || 'PUBLIC').toUpperCase();
        var radios = document.querySelectorAll('tp-yt-paper-radio-button');
        var pub = null;
        for (var r of radios) {
            if ((r.getAttribute('name') || '').toUpperCase() === want) { pub = r; break; }
        }
        return {
            checked: pub ? pub.checked : false,
            aria: pub ? pub.getAttribute('aria-checked') : null
        };
    """, target)

    done_kind = _classify_done_button(driver)
    logger.info("Видимость %s: checked=%s done_button_kind='%s'", target, final.get("checked"), done_kind)

    # для PUBLIC финальная кнопка обязана стать 'Publish', а для PRIVATE/UNLISTED
    # штатная кнопка — 'Save': публиковать нечего, YouTube просто сохраняет видимость
    want_kind = "publish" if target == "PUBLIC" else "save"
    if done_kind != want_kind:
        logger.warning("#done-button = '%s', ожидали '%s' — повторный click", done_kind, want_kind)
        return False
    if not (final.get("checked") or str(final.get("aria")) == "true"):
        logger.warning("Радио %s не отмечено — повторный click", target)
        return False
    return True


def _schedule_time_matches(value, schedule_date: datetime) -> bool:
    """Сопоставить 12/24-часовой read-back Studio с нужным временем."""
    shown = str(value or "").upper().replace("\u202f", " ").replace("\u00a0", " ")
    shown = re.sub(r"(?<=\d)\.(?=\d{2}(?:\D|$))", ":", shown)
    match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?:\s*([AP])\.?\s*M\.?)?", shown)
    if not match:
        return False
    hour, minute = int(match.group(1)), int(match.group(2))
    meridiem = match.group(3)
    if meridiem:
        if not 1 <= hour <= 12:
            return False
        hour %= 12
        if meridiem == "P":
            hour += 12
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return False
    return (hour, minute) == (schedule_date.hour, schedule_date.minute)


def _set_schedule(driver, schedule_date: datetime) -> bool:
    """Set Schedule on YouTube Studio Visibility page.
    1. Hide ytcp-uploads-review overlay (same hack as _set_public)
    2. Click Schedule <p> with real MouseEvents
    3. Open date picker, click target day
    4. Fill time in tp-yt-paper-input shadow DOM
    5. Verify #done-button стал "Schedule" (язык-независимо).
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    import time as _t

    date_str = schedule_date.strftime("%b %d, %Y")
    time_str = schedule_date.strftime("%I:%M %p")
    time_str_24 = schedule_date.strftime("%H:%M")

    # Step 1: Click Schedule via real MouseEvent.
    # Радио ищем по name=SCHEDULE / publishing-type (язык-независимо), затем — по тексту (мультиязычно).
    schedule_words_json = json.dumps(_SCHEDULE_WORDS)
    schedule_clicked = driver.execute_script("""
        var SCHED_WORDS = arguments[0];
        var overlay = document.querySelector('ytcp-uploads-review');
        var od = null;
        if (overlay) { od = overlay.style.display; overlay.style.display = 'none'; }

        function matchesSchedule(el) {
            var n = (el.getAttribute && (el.getAttribute('name') || '') || '').toUpperCase();
            if (n === 'SCHEDULE') return true;
            var pt = (el.getAttribute && (el.getAttribute('publishing-type') || el.getAttribute('data-publishing-type') || '') || '').toUpperCase();
            if (pt.indexOf('SCHEDULE') >= 0) return true;
            var t = (el.textContent || '').trim().toLowerCase();
            for (var w of SCHED_WORDS) { if (t === w || t.indexOf(w) === 0) return true; }
            return false;
        }

        var target = null;
        // 1) radio-кнопки
        for (var r of document.querySelectorAll('tp-yt-paper-radio-button, [role=radio]')) {
            if (matchesSchedule(r)) { target = r; break; }
        }
        // 2) <p> внутри visibility-select
        if (!target) {
            for (var p of document.querySelectorAll('ytcp-video-visibility-select p, [role=radio] p')) {
                var t = (p.textContent || '').trim().toLowerCase();
                for (var w of SCHED_WORDS) { if (t === w || t.indexOf(w) === 0) { target = p; break; } }
                if (target) break;
            }
        }
        if (!target) {
            if (overlay) overlay.style.display = od || '';
            return {ok: false, error: 'Schedule control not found'};
        }
        target.scrollIntoView({block:'center'});
        var rect = target.getBoundingClientRect();
        var cx = rect.left + rect.width/2;
        var cy = rect.top + rect.height/2;
        var opts = {bubbles:true, cancelable:true, view:window, clientX:cx, clientY:cy, button:0};
        target.dispatchEvent(new MouseEvent('mousedown', opts));
        target.dispatchEvent(new MouseEvent('mouseup', opts));
        target.dispatchEvent(new MouseEvent('click', opts));

        if (overlay) overlay.style.display = od || '';
        return {ok: true};
    """, _SCHEDULE_WORDS)
    if not schedule_clicked.get("ok"):
        logger.warning("Schedule click: %s", schedule_clicked.get("error"))
        return False
    logger.info("Schedule radio clicked (MouseEvent)")
    _t.sleep(3)

    # 3. Set DATE — click the ytcp-text-dropdown-trigger (shows "Apr 14, 2026")
    date_filled = False
    try:
        date_trigger = driver.find_element(By.CSS_SELECTOR, "ytcp-datetime-picker ytcp-text-dropdown-trigger")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", date_trigger)
        _t.sleep(0.5)
        driver.execute_script("arguments[0].click();", date_trigger)
        logger.info("Date dropdown opened (current: %s)", date_trigger.text.strip()[:20])
        _t.sleep(2)

        target_day = str(schedule_date.day)
        day_buttons = driver.find_elements(By.CSS_SELECTOR, "ytcp-date-picker td button, .calendar-day, [role='gridcell'] button")
        for btn in day_buttons:
            if btn.text.strip() == target_day and btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                logger.info("Calendar day %s clicked", target_day)
                date_filled = True
                _t.sleep(1)
                break

        if not date_filled:
            for inp in driver.find_elements(By.CSS_SELECTOR, "ytcp-date-picker input, input[type='text']"):
                if inp.is_displayed():
                    inp.click()
                    inp.send_keys(Keys.CONTROL + "a")
                    inp.send_keys(date_str)
                    inp.send_keys(Keys.ENTER)
                    logger.info("Date typed: %s", date_str)
                    date_filled = True
                    _t.sleep(1)
                    break
    except Exception as e:
        logger.warning("Date picker error: %s", e)

    # 4. Set TIME — pierce shadow DOM of tp-yt-paper-input
    time_filled = False
    try:
        def _set_time(value):
            return driver.execute_script("""
            var picker = document.querySelector('ytcp-datetime-picker');
            if (!picker) return null;
            var paperInput = picker.querySelector('tp-yt-paper-input');
            if (!paperInput) return null;
            var input = paperInput.querySelector('input') || (paperInput.shadowRoot && paperInput.shadowRoot.querySelector('input'));
            if (!input) {
                input = paperInput.inputElement;
            }
            if (input) {
                input.focus();
                input.value = arguments[0];
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
                return String(input.value || '');
            }
            return false;
        """, value)

        result = _set_time(time_str)
        if result and not _schedule_time_matches(result, schedule_date):
            logger.info("Time read-back %r не совпал; пробую 24ч формат %s",
                        result, time_str_24)
            result = _set_time(time_str_24)
        if result:
            logger.info("Time set via JS: %s (read-back=%r)", time_str_24, result)
            time_filled = _schedule_time_matches(result, schedule_date)
            if not time_filled:
                logger.error("Time read-back %r не равен %s", result, time_str_24)
            _t.sleep(1)
        else:
            logger.warning("Time input not found in shadow DOM")
    except Exception as e:
        logger.warning("Time input error: %s", e)

    logger.info("Schedule result: date=%s time=%s", date_filled, time_filled)

    # READ-BACK даты: сверяем РЕАЛЬНО выбранную в пикере дату с запрошенной. Пикер кликает по
    # числу дня БЕЗ навигации по месяцам → дата из ДРУГОГО месяца (напр. июль при открытом июне)
    # выберется неверно. При ПОДТВЕРЖДЁННОМ расхождении — отказ (лучше провал, чем эфир в неверный день).
    date_mismatch = False
    date_verified = False
    try:
        _shown = driver.find_element(By.CSS_SELECTOR, "ytcp-datetime-picker ytcp-text-dropdown-trigger").text.strip()
        import datetime as _dtmod
        _parsed = None
        for _fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                _parsed = _dtmod.datetime.strptime(_shown, _fmt).date(); break
            except Exception:
                pass
        if _parsed:
            if (_parsed.year, _parsed.month, _parsed.day) != (schedule_date.year, schedule_date.month, schedule_date.day):
                date_mismatch = True
                logger.error("READ-BACK ДАТЫ: пикер показывает '%s', запрошено %s — РАСХОЖДЕНИЕ", _shown, schedule_date.strftime("%b %d, %Y"))
            else:
                date_verified = True
                logger.info("READ-BACK даты OK: %s", _shown)
        else:
            logger.error("READ-BACK: дату пикера '%s' не распарсил — дата не подтверждена", _shown)
    except Exception as _e:
        logger.error("READ-BACK даты не выполнен: %s — дата не подтверждена", _e)
    # Fail-closed gate: одной кнопки Schedule недостаточно. Клик по секции сам
    # подставляет дефолтные дату/время, поэтому дополнительно требуем read-back
    # точной даты и времени. Иначе ролик формально Scheduled, но не в наш слот.
    _t.sleep(1)
    done_kind = _classify_done_button(driver)
    if done_kind != "schedule":
        logger.error(
            "Schedule button NOT verified — kind=%r (expected schedule). "
            "Refusing to publish as Draft. (date_filled=%s time_filled=%s)",
            done_kind, date_filled, time_filled
        )
        return False
    if date_mismatch or not date_verified:
        logger.error("Отказываюсь планировать: нужная дата не подтверждена read-back. Запрошено %s — оставляю как есть (не публикую).", schedule_date.strftime("%Y-%m-%d"))
        return False
    if not (date_filled and time_filled):
        logger.error(
            "Schedule button OK (kind=%r), но date_filled=%s time_filled=%s — "
            "точный слот %s не подтверждён, финализация запрещена.",
            done_kind, date_filled, time_filled, schedule_date.strftime("%Y-%m-%d %H:%M")
        )
        return False
    logger.info("Schedule button verified: kind=%r (date+time set)", done_kind)
    return True


def _click_done(driver):
    """Нажимает #done-button. Логирует тип кнопки (язык-независимо) чтобы понять что произошло.
    После вызова caller должен проверить что диалог закрылся (Publish/Schedule)
    или что видео стало Public (не Draft)."""
    time.sleep(1)
    done_kind = _classify_done_button(driver)
    btn_present = driver.execute_script("return !!document.querySelector('#done-button')")
    if not btn_present:
        logger.warning("#done-button не найден")
        return False

    logger.info("Кнопка финализации (kind): '%s'", done_kind)
    if done_kind == "save":
        logger.warning("Кнопка 'Save' означает что видео будет сохранено как Draft! Visibility не выбран.")

    try:
        ok = driver.execute_script("""
            var b = document.querySelector('#done-button');
            if (!b) return false;
            b.scrollIntoView({block:'center'});
            var t = b.querySelector('button, tp-yt-paper-button') || b;
            var r = t.getBoundingClientRect();
            var o = {bubbles:true,cancelable:true,view:window,clientX:r.left+r.width/2,clientY:r.top+r.height/2,button:0};
            t.dispatchEvent(new MouseEvent('mousedown',o));
            t.dispatchEvent(new MouseEvent('mouseup',o));
            t.dispatchEvent(new MouseEvent('click',o));
            return true;
        """)
        if not ok:
            logger.warning("#done-button не найден для клика")
            return False
        time.sleep(5)
        return True
    except Exception as e:
        logger.warning("Не удалось кликнуть #done-button: %s", e)
        return False


def _extract_url(driver):
    """Ссылка на НАШ ролик — только из того, что опознано как наше.

    Прежние два метода (первая ссылка youtu.be на странице и regex по всему
    page_source) брали ЛЮБУЮ подходящую ссылку документа. А под мастером лежит
    список видео канала, и его ссылки подходят под тот же шаблон → возвращался
    videoId ЧУЖОГО ролика, и дальше по нему шли и вердикт заливки, и
    пост-публикация (монетизация, A/B-обложки, коллаборация, мидроллы).

    Источники, про которые известно, что они про нашу заливку:
      1) подтверждающий диалог Studio (tp-yt-paper-dialog вне мастера) — его
         ссылку отдаёт YS.read_final_state()['videoUrl'];
      2) адрес страницы /video/{id}/… — на неё мы пришли сами, по своему id.
    Ничего не нашли — пустая строка: id возьмётся из строки ролика по состоянию
    (раздел 15), а не угадается со страницы.
    """
    if YS is not None:
        try:
            # диалог всплывает не мгновенно; ждём его состояние, а не спим наугад
            url = YS.wait_until(
                driver, lambda d: (YS.read_final_state(d) or {}).get("videoUrl"),
                timeout=12, poll=3, desc="ссылка в подтверждении Studio")
            if url:
                return url
        except Exception as e:
            logger.debug("_extract_url: подтверждение не прочитано: %s", e)

    # Адрес страницы: на /video/{id}/… мы переходим сами со СВОИМ id
    try:
        url = driver.current_url
        match = re.search(r'/video/([a-zA-Z0-9_-]{11})(?:[/?#]|$)', url)
        if match:
            return f"https://youtu.be/{match.group(1)}"
    except Exception:
        pass

    logger.info("Ссылка на ролик из подтверждения не получена — id возьму из строки канала")
    return ""


def _parse_schedule(value: str):
    """Единственный parser: ISO8601 absolute instant с UTC offset."""
    return upload_contract.parse_absolute_schedule(value)


def _load_receipt_io(path: str = ""):
    """Load the shared receipt module without making desktop installs depend on it."""
    import importlib.util

    candidate = path or os.getenv("VIDEO_OPS_RECEIPT_IO", "")
    if not candidate:
        candidate = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tools", "video_ops_watchdog", "receipt_io.py",
        )
    spec = importlib.util.spec_from_file_location("video_ops_receipt_io", candidate)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"receipt_io не найден: {candidate}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare_receipt_support(args):
    """Validate producer settings through the canonical receipt contract."""
    receipt_fields = (
        args.receipt_context_schema_version is not None,
        args.receipt_outbox,
        args.receipt_manifest_sha256,
        args.receipt_manifest_name,
        args.receipt_operational_date,
        args.receipt_expected_scheduled_at,
        args.receipt_source_host,
        args.receipt_ssh_target,
        args.receipt_io_path,
        args.receipt_existing_upload_policy,
        args.receipt_ssh_timeout is not None,
    )
    if not any(receipt_fields):
        return None
    module = _load_receipt_io(args.receipt_io_path)
    push = (
        {
            "enabled": True,
            "ssh_target": args.receipt_ssh_target,
            "timeout_seconds": 30.0 if args.receipt_ssh_timeout is None else args.receipt_ssh_timeout,
        }
        if args.receipt_ssh_target
        else {"enabled": False}
    )
    context = module.normalize_producer_context(
        {
            "schema_version": args.receipt_context_schema_version,
            "manifest_sha256": args.receipt_manifest_sha256,
            "manifest_name": args.receipt_manifest_name,
            "operational_date": args.receipt_operational_date,
            "expected_scheduled_at": args.receipt_expected_scheduled_at,
            "source_host": args.receipt_source_host,
            "outbox_dir": args.receipt_outbox,
            "receipt_io_path": args.receipt_io_path,
            "existing_upload_policy": args.receipt_existing_upload_policy,
            "push": push,
        }
    )
    args.receipt_ssh_timeout = context["push"].get("timeout_seconds", 30.0)
    args.receipt_expected_scheduled_at = context["expected_scheduled_at"]
    os.makedirs(context["outbox_dir"], mode=0o700, exist_ok=True)
    return module


def _persist_and_push_receipt(result: dict, args, receipt_module) -> dict | None:
    """Persist proof first; delivery failure never changes the upload verdict."""
    if receipt_module is None:
        return None
    evidence = result.get("exact_studio_evidence")
    if not isinstance(evidence, dict):
        logger.error(
            "Квитанция не выпущена: нет exact Studio evidence; video_id=%s "
            "do_not_reupload=%s",
            result.get("video_id") or "unknown", bool(result.get("do_not_reupload")),
        )
        raise RuntimeError("exact Studio evidence missing")
    receipt = receipt_module.build_scheduled_receipt(
        evidence=evidence,
        manifest_sha256=args.receipt_manifest_sha256,
        manifest_name=args.receipt_manifest_name,
        expected_operational_date=args.receipt_operational_date,
        source_host=args.receipt_source_host,
        expected_scheduled_at=args.receipt_expected_scheduled_at,
        verified_at=datetime.now(timezone.utc),
    )
    path = receipt_module.write_outbox(receipt, args.receipt_outbox)
    print(
        f"__UPLOAD_RECEIPT__|{receipt['receipt_sha256']}|{path}",
        flush=True,
    )
    if args.receipt_ssh_target:
        try:
            delivery = receipt_module.push_receipt(
                path,
                args.receipt_ssh_target,
                timeout_seconds=args.receipt_ssh_timeout,
            )
        except Exception as exc:
            logger.error(
                "SSH-доставка квитанции не выполнена (%s): квитанция остаётся в retry-outbox; "
                "ролик повторно НЕ заливать",
                exc,
            )
        else:
            if delivery.get("rejected"):
                logger.error(
                    "VPS навсегда отклонил квитанцию: deadletter=%s; ролик повторно "
                    "НЕ заливать, исправить контекст/политику по локальному audit",
                    delivery.get("deadlettered", 0),
                )
            elif delivery.get("failed"):
                logger.error(
                    "SSH-доставка квитанции не выполнена: pending retry-outbox=%s failed=%s; "
                    "ролик повторно НЕ заливать",
                    delivery.get("pending"), delivery.get("failed"),
                )
            else:
                logger.info("Квитанции доставлены на VPS: %s", delivery.get("sent", 0))
    return receipt


def _normalized_upload_result(result) -> dict:
    """Stable stdout schema for every terminal CLI path."""
    return upload_contract.build_result(result, _current_upload_phase())


def _emit_upload_result(result) -> dict:
    normalized = _normalized_upload_result(result)
    print(
        upload_contract.RESULT_MARKER + json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        flush=True,
    )
    return normalized


if __name__ == "__main__":
    import argparse

    class _UploadArgumentParser(argparse.ArgumentParser):
        def error(self, message):
            failure = _emit_upload_result(
                {
                    "success": False,
                    "url": "",
                    "error": f"argument_error: {message}",
                    "visibility": "unknown",
                }
            )
            print(f"__UPLOAD_ERROR__|{failure['error']}", flush=True)
            self.exit(2)

    # На Windows стдаут/стдерр могут быть в cp1251 — форсим utf-8 чтобы маркеры/кириллица не падали.
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = _UploadArgumentParser(description="YouTube uploader (AdsPower + Selenium)")
    parser.add_argument("--video", required=True, help="Путь к видеофайлу")
    parser.add_argument("--title", required=True, help="Заголовок")
    parser.add_argument("--description", default="", help="Описание")
    parser.add_argument("--profile", required=True, help="AdsPower profile id")
    parser.add_argument("--channel-url", dest="channel_url", required=True,
                        help="URL канала (https://www.youtube.com/channel/UC... или /@handle)")
    parser.add_argument(
        "--schedule", default="",
        help="Дата публикации ISO8601 с обязательным UTC offset, точность до минуты",
    )
    parser.add_argument("--thumbnail", default="", help="Путь к обложке (опц.)")
    parser.add_argument("--headless", action="store_true", help="Headless-режим")
    parser.add_argument("--finalize-draft", dest="finalize_draft", action="store_true",
                        help="Не заливать файл, а до-планировать уже залитый ЧЕРНОВИК с таким title")
    parser.add_argument("--visibility", dest="visibility", default="public",
                        help="public|private|unlisted — какую видимость ставить на шаге Visibility")
    parser.add_argument("--upload-only", dest="upload_only", action="store_true",
                        help="Фаза A: только залить (resume/fresh)+обложка/altered, оставить черновиком (не планировать)")
    parser.add_argument("--search-title", dest="search_title", default="",
                        help="По чему ИСКАТЬ черновик/дубль, если отличается от --title "
                             "(напр. ASCII-фрагмент: DOM YouTube отдаёт диакритику в NFD)")
    parser.add_argument("--monetize", dest="monetize", action="store_true",
                        help="После публикации включить монетизацию в той же сессии (только YPP-канал)")
    parser.add_argument("--ab-thumbs", dest="ab_thumbs", default="",
                        help="A/B-обложки: пути через запятую (1-2 доп. варианта, слот-1 = обложка ролика)")
    parser.add_argument("--midrolls", dest="midrolls", action="store_true",
                        help="сразу после публикации расставить рекламные слоты "
                             "(по --chapters, иначе равномерно каждые --midroll-step секунд)")
    parser.add_argument("--chapters", dest="chapters", default="",
                        help="секунды начала глав через запятую (слот ставится на +1с от главы)")
    parser.add_argument("--midroll-step", dest="midroll_step", type=int, default=0,
                        help="шаг сетки слотов в секундах (по умолчанию 150 = 2.5 мин)")
    parser.add_argument("--collaborator", dest="collaborator", default="",
                        help="Пригласить соавтора после публикации: ТОЧНЫЙ @handle канала "
                             "(напр. @PL_AktRzeszy). По названию не ищем — уйдёт чужому каналу")
    parser.add_argument(
        "--receipt-context-schema-version",
        type=int,
        default=os.getenv("VIDEO_OPS_RECEIPT_CONTEXT_SCHEMA_VERSION") or None,
        help="Версия producer context; обязательна вместе с receipt-параметрами",
    )
    parser.add_argument(
        "--receipt-outbox",
        default=os.getenv("VIDEO_OPS_RECEIPT_OUTBOX", ""),
        help="Локальный атомарный retry-outbox exact Studio квитанций",
    )
    parser.add_argument(
        "--receipt-manifest-sha256",
        default=os.getenv("VIDEO_OPS_RECEIPT_MANIFEST_SHA256", ""),
        help="SHA-256 frozen manifest, обязательный при --receipt-outbox",
    )
    parser.add_argument(
        "--receipt-manifest-name",
        default=os.getenv("VIDEO_OPS_RECEIPT_MANIFEST_NAME", ""),
        help="Безопасное имя frozen manifest, обязательное при --receipt-outbox",
    )
    parser.add_argument(
        "--receipt-operational-date",
        default=os.getenv("VIDEO_OPS_RECEIPT_OPERATIONAL_DATE", ""),
        help="Ожидаемая операционная дата YYYY-MM-DD; VPS сверит её с actual epoch",
    )
    parser.add_argument(
        "--receipt-source-host",
        default=os.getenv("VIDEO_OPS_RECEIPT_SOURCE_HOST", ""),
        help="Доверенный идентификатор источника: mac или minsk",
    )
    parser.add_argument(
        "--receipt-expected-scheduled-at",
        default=os.getenv("VIDEO_OPS_RECEIPT_EXPECTED_SCHEDULED_AT", ""),
        help="Ожидаемый абсолютный ISO8601 instant для exact epoch receipt v2",
    )
    parser.add_argument(
        "--receipt-ssh-target",
        default=os.getenv("VIDEO_OPS_RECEIPT_SSH_TARGET", ""),
        help="SSH host alias с forced-command ingest; ошибка оставляет outbox для retry",
    )
    parser.add_argument(
        "--receipt-ssh-timeout", type=float,
        default=os.getenv("VIDEO_OPS_RECEIPT_SSH_TIMEOUT") or None,
    )
    parser.add_argument(
        "--receipt-io-path", default=os.getenv("VIDEO_OPS_RECEIPT_IO", ""),
        help="Явный путь receipt_io.py для deployment-копии",
    )
    parser.add_argument(
        "--receipt-existing-upload-policy",
        default=os.getenv("VIDEO_OPS_RECEIPT_EXISTING_UPLOAD_POLICY", ""),
        choices=("skip", "reconcile_exact"),
        help="Явная политика для уже известного uploaded_url",
    )
    parser.add_argument(
        "--reconcile-video-id",
        default="",
        help="Только сверить exact Studio row; ветка fresh file upload запрещена",
    )
    parser.add_argument(
        "--finalize-video-id", default="",
        help="Strict only: finalize this exact existing draft ID; every file handoff forbidden",
    )
    args = parser.parse_args()
    globals()['VISIBILITY'] = (args.visibility or 'public').upper()

    try:
        _receipt_module = _prepare_receipt_support(args)
    except Exception as e:
        _failure = _emit_upload_result({
            "success": False,
            "url": "",
            "error": f"receipt_preflight_failed: {e}",
            "visibility": "unknown",
        })
        print(f"__UPLOAD_ERROR__|{_failure['error']}", flush=True)
        sys.exit(1)

    _ab_list = [p for p in (args.ab_thumbs.split(",") if args.ab_thumbs else []) if p.strip()]

    try:
        sched = _parse_schedule(args.schedule)
    except ValueError as e:
        _failure = _emit_upload_result({
            "success": False, "url": "", "error": str(e), "visibility": "unknown",
        })
        print(f"__UPLOAD_ERROR__|{_failure['error']}", flush=True)
        sys.exit(1)

    if args.reconcile_video_id and upload_contract.VIDEO_ID_RE.fullmatch(
        args.reconcile_video_id
    ) is None:
        _failure = _emit_upload_result({
            "success": False,
            "url": "",
            "error": "reconcile_video_id_invalid",
            "visibility": "unknown",
        })
        print(f"__UPLOAD_ERROR__|{_failure['error']}", flush=True)
        sys.exit(1)
    if args.reconcile_video_id and sched is None:
        _failure = _emit_upload_result({
            "success": False,
            "url": "",
            "error": "reconcile_exact requires absolute schedule; fresh upload forbidden",
            "visibility": "unknown",
        })
        print(f"__UPLOAD_ERROR__|{_failure['error']}", flush=True)
        sys.exit(1)
    if _receipt_module and sched is None:
        _failure = _emit_upload_result({
            "success": False,
            "url": "",
            "error": "receipt context requires absolute --schedule",
            "visibility": "unknown",
        })
        print(f"__UPLOAD_ERROR__|{_failure['error']}", flush=True)
        sys.exit(1)
    if _receipt_module:
        _expected_receipt_schedule = _parse_schedule(args.receipt_expected_scheduled_at)
        if int(_expected_receipt_schedule.timestamp()) != int(sched.timestamp()):
            _failure = _emit_upload_result({
                "success": False,
                "url": "",
                "error": "receipt expected schedule differs from --schedule",
                "visibility": "unknown",
            })
            print(f"__UPLOAD_ERROR__|{_failure['error']}", flush=True)
            sys.exit(1)

    try:
        result = upload_to_youtube(
            video_path=args.video,
            title=args.title,
            description=args.description or args.title,
            profile_id=args.profile,
            thumbnail_path=args.thumbnail,
            headless=args.headless,
            schedule_date=sched,
            channel_url=args.channel_url,
            finalize_draft=args.finalize_draft,
            upload_only=args.upload_only,
            search_title=args.search_title,
            monetize=args.monetize,
            ab_thumbnails=_ab_list or None,
            collaborator=args.collaborator,
            midrolls=args.midrolls,
            chapters=[float(x) for x in args.chapters.split(',') if x.strip()] or None,
            midroll_step=args.midroll_step,
            reconcile_video_id=args.reconcile_video_id,
            finalize_video_id=args.finalize_video_id,
        )
    except Exception as e:
        _failure = _emit_upload_result({
            "success": False, "url": "", "error": str(e), "visibility": "unknown",
        })
        print(f"__UPLOAD_ERROR__|{_failure['error']}", flush=True)
        sys.exit(1)

    result = _normalized_upload_result(result)

    if result.get("success") and result.get("visibility") == "SCHEDULED" and _receipt_module:
        try:
            _persist_and_push_receipt(result, args, _receipt_module)
        except Exception as e:
            # Unlike SSH delivery, a failed *local* durable write has no retry
            # artifact. Return a known-ID failure before the manager records
            # uploaded_url; the next worker run will dedup this same Studio row
            # and create the receipt without uploading the file again.
            logger.error(
                "Локальная квитанция не сохранена (%s); video_id=%s, повторно НЕ заливать",
                e, result.get("video_id") or "unknown",
            )
            result["success"] = False
            result["visibility"] = "RECEIPT_PERSIST_FAILED"
            result["error"] = (
                "receipt_persist_failed: known video_id="
                f"{result.get('video_id') or 'unknown'}; do not reupload"
            )

    result = _emit_upload_result(result)

    if result.get("success") and result.get("url"):
        print(f"__UPLOAD_SUCCESS__|{result['url']}", flush=True)
        sys.exit(0)
    else:
        err = result.get("error") or "upload failed (no url)"
        print(f"__UPLOAD_ERROR__|{err}", flush=True)
        sys.exit(1)
