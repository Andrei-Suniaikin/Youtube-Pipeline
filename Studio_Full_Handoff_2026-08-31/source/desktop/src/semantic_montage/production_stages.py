"""Production-v2 stage adapters for the durable semantic-montage runner.

The adapters deliberately separate portable manifests from runtime paths.  A
persisted candidate refers to ``source_asset_id``; the absolute mount/path is
hydrated only while a local operation is running.  Stages that require an
unconfigured provider finish as an explicit ``needs_*`` terminal rather than
inventing a result or silently weakening the release contract.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from subprocess import SubprocessError
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .beats import BeatPlanningError, repartition_beats_for_visual_limit, write_beats_json
from .broad_lazy_adapter import (
    BroadLazyAdapterError,
    BroadLazyRuntimeError,
    materialize_exact_repair_candidate,
    validate_exact_candidate_contract,
)
from .audio import AMBIENCE_ENERGY_FLOOR_POLICY_VERSION
from .acoustic_scene import CAUSAL_RECONSTRUCTION_POLICY_VERSION
from .catalog_resolver import CATALOG_RESOLVER_VERSION
from .common import atomic_write_json, canonical_hash, interval_frame_span, sha256_file
from .llm_client import extract_json
from .project import ProjectConfig, ProjectError
from .runner import StageBlocked, StageContext, StageDefinition, StageRegistry
from .repair_controller import (
    RepairController,
    RepairExhausted,
    RepairRequest,
    canonical_candidate_range,
    next_non_overlapping_candidate_batch,
)
from .render import EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION
from .script_qa import ScriptQualityError, require_script_quality
from .semantic import (
    MUST_HAVE_LOCATIVE_FLORA_COMPILER_VERSION,
    VISUAL_BRIEF_CONTRACT_VERSION,
    request_visual_briefs_raw,
    validate_visual_brief_contract,
)
from .semantic_repair_brief import (
    GROUNDED_MAGNITUDE_DROP_POLICY_VERSION,
    HONEST_VISIBLE_FALLBACK_POLICY_VERSION,
    HONEST_VISIBLE_FALLBACK_RECEIPT_FIELD,
    NATURAL_CARRIER_POLICY_VERSION,
    NATURAL_STATUS_ENVIRONMENT_POLICY_VERSION,
    POST_RESEG_SEMANTIC_PROXY_REPAIR_PROMPT_VERSION,
    SEMANTIC_PROXY_REPAIR_PROMPT_VERSION,
    build_honest_visible_fallback_policy,
    pop_honest_visible_fallback_receipt,
    request_post_resegment_semantic_proxy_repair_raw,
    request_semantic_proxy_repair_raw,
    validate_post_resegment_semantic_proxy_repair_response,
    validate_semantic_proxy_repair_response,
)
from .semantic_broad_repair import (
    SemanticBroadRepairBlocked,
    SemanticBroadRepairError,
    build_semantic_broad_repair_binding,
    materialize_semantic_broad_repair,
    validate_semantic_broad_repair_block,
    validate_semantic_broad_repair_receipt,
)
from .selected_visual_gate import SELECTED_VISUAL_GATE_VERSION
from .sound_qa import REQUIRED_CUE_RENDER_RECEIPT_CONTRACT
from .tts import load_words_document, synthesize_edge_tts_sync
from .strict_candidate import (
    STRICT_ANALYZER_VERSION,
    STRICT_REMATERIALIZATION_CONTRACT,
    StrictCandidateError,
    rematerialize_strict_candidate_variants,
)


PRODUCTION_STAGE_VERSION = "production-orchestrator-v2.3"
TEMPORAL_RETRIEVAL_STAGE_VERSION = (
    f"{PRODUCTION_STAGE_VERSION}:strict-multispan-v1"
    ":bounded-brief-regeneration-v2"
    f":{MUST_HAVE_LOCATIVE_FLORA_COMPILER_VERSION}"
    f":{STRICT_ANALYZER_VERSION}"
)
ACOUSTIC_PLAN_STAGE_VERSION = (
    f"{PRODUCTION_STAGE_VERSION}:acoustic-raw-paid-v1"
    f":{CAUSAL_RECONSTRUCTION_POLICY_VERSION}"
)
SOUND_RESOLUTION_STAGE_VERSION = (
    f"{PRODUCTION_STAGE_VERSION}:{CATALOG_RESOLVER_VERSION}"
)
SEMANTIC_REPAIR_POLICY_VERSION = "bounded_semantic_repair_v8"
POST_RESEG_SEMANTIC_REPAIR_POLICY_VERSION = "post-resegment-semantic-repair-v3"
EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION = "semantic-exact-repair-candidate-v1"
TARGETED_SEMANTIC_BROAD_REPAIR_CONTRACT_VERSION = (
    "targeted-semantic-broad-repair-orchestration-v2"
)
GLOBAL_COLLISION_REPAIR_POLICY_VERSION = "global-collision-broad-repair-v2"
GLOBAL_COLLISION_REPAIR_NAMESPACE = "__global_collision_repair_v2__"
GLOBAL_COLLISION_TARGET_RULE_VERSION = (
    "untried-core-max-accepted-earliest-v2"
)
GLOBAL_COLLISION_SELECTABLE_CANDIDATES_PER_SLOT = 20
GLOBAL_COLLISION_PAID_CHAIN_CONTRACT = "global-collision-paid-judge-chain-v1"
GLOBAL_COLLISION_TARGET_OUTCOME_CONTRACT = "global-collision-target-outcome-v1"
GLOBAL_ASSIGNMENT_MATERIALIZATION_POLICY_VERSION = (
    "global-assignment-adaptive-beam-exact-witness-v1"
)
GLOBAL_ASSIGNMENT_EXACT_WITNESS_CONTRACT = (
    "global-assignment-exact-feasible-witness-v1"
)
GLOBAL_ASSIGNMENT_BEAM_WIDTH_CEILING = 1024
SELECTED_VISUAL_ACCEPTANCE_CONTRACT = "selected-visual-span-acceptance-v1"
SELECTED_VISUAL_PAID_CHAIN_CONTRACT = "selected-visual-paid-judge-chain-v1"
SELECTED_VISUAL_REPAIR_POLICY_VERSION = "selected-visual-span-broad-repair-v1"
SELECTED_VISUAL_REPAIR_NAMESPACE = "__selected_visual_span_repair_v1__"
SELECTED_VISUAL_REPAIR_ATTEMPT_CONTRACT = "selected-visual-repair-attempt-v1"
SELECTED_VISUAL_CACHE_REPLAY_CONTRACT = "selected-cache-replay-v2"
SELECTED_VISUAL_REPAIR_TARGET_RULE_VERSION = (
    "selected-visual-empty-core-fair-rotation-v2"
)
SELECTED_VISUAL_REPAIR_TARGET_OUTCOME_CONTRACT = (
    "selected-visual-target-outcome-v1"
)
SELECTED_VISUAL_TRANSFORM_CONTRACT = "portable-deterministic-transform-v1"
SEMANTIC_RESEGMENT_PROJECTION_CONTRACT = (
    "semantic-resegment-changed-slot-projection-v1"
)
PAID_CONTENT_IDEMPOTENCY_CONTRACT = "paid-content-idempotency-v2"
PAID_COMPLETED_REPLAY_POLICY = "earliest-valid-completed-v1"
GLOBAL_COLLISION_REPAIR_STAGE_VERSION = (
    f"{PRODUCTION_STAGE_VERSION}:{POST_RESEG_SEMANTIC_REPAIR_POLICY_VERSION}"
    ":exact-repair-v1:honest-broad-fallback-v1:global-collision-repair-v2"
)
SEMANTIC_JUDGE_STAGE_VERSION = (
    f"{GLOBAL_COLLISION_REPAIR_STAGE_VERSION}"
    f":exact-feasible-witness-v1:{SELECTED_VISUAL_GATE_VERSION}"
    ":portable-selected-transform-v1"
    f":{SELECTED_VISUAL_REPAIR_TARGET_RULE_VERSION}"
    f":{SELECTED_VISUAL_CACHE_REPLAY_CONTRACT}"
    f":{EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION}"
    f":{SEMANTIC_RESEGMENT_PROJECTION_CONTRACT}"
    f":{PAID_CONTENT_IDEMPOTENCY_CONTRACT}"
)
VISUAL_QA_PROVENANCE_CONTRACT = "visual-qa-local-render-provenance-v2"
VISUAL_QA_STAGE_VERSION = (
    f"{PRODUCTION_STAGE_VERSION}:visual-qa-local-render-provenance-v2"
    f":{EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION}"
)
RENDER_STAGE_VERSION = (
    f"{PRODUCTION_STAGE_VERSION}:portable-deterministic-transform-v1"
    f":{EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION}"
    f":{AMBIENCE_ENERGY_FLOOR_POLICY_VERSION}"
    f":{REQUIRED_CUE_RENDER_RECEIPT_CONTRACT}"
)
REQUIRED_PHYSICAL_FOLEY_PREMASTER_GAIN = 1.5
REQUIRED_PHYSICAL_FOLEY_PREMASTER_DUCK_RATIO = 1.0
BROAD_LAZY_INDEX_MODE = "broad_lazy_v1"
BROAD_LAZY_OVERLAY_REPORT_CONTRACT = "broad_lazy_overlay_deferred_v1"
BROAD_LAZY_DIAGNOSTICS_CONTRACT = "broad_lazy_stage_diagnostics_v1"


def _global_collision_selectable_prefix(
    raw_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Mirror the selector's first-20 raw-row universe before filtering."""

    return [
        dict(row)
        for row in raw_rows[
            :GLOBAL_COLLISION_SELECTABLE_CANDIDATES_PER_SLOT
        ]
    ]


def _settings(context: StageContext) -> Mapping[str, Any]:
    value = context.project.settings.get("production") or {}
    if not isinstance(value, Mapping):
        raise ValueError("settings.production must be an object")
    return value


def _visual_brief_paid_request(
    model: str,
    beats: Sequence[Mapping[str, Any]],
    **metadata: Any,
) -> dict[str, Any]:
    """Build the single versioned idempotency payload for every brief call."""

    return {
        "visual_brief_contract_version": VISUAL_BRIEF_CONTRACT_VERSION,
        "model": str(model),
        "beats": [dict(beat) for beat in beats],
        **metadata,
    }


def _asset_id(context: StageContext, key: str, default: str | None = None) -> str:
    value = str(_settings(context).get(key) or default or "").strip()
    if not value:
        raise StageBlocked(f"needs_{key}", f"settings.production.{key} is required")
    return value


def _resolve_asset(context: StageContext, asset_id: str) -> Path:
    cached = context.resolved_assets.get(asset_id)
    if cached is not None:
        return cached
    try:
        resolved = context.project.resolve_asset(asset_id, mounts=context.runtime_mounts)
    except ProjectError as exc:
        raise StageBlocked("needs_project_asset", str(exc)) from exc
    context.resolved_assets[asset_id] = resolved
    return resolved


def _artifact_path(context: StageContext, stage: str, artifact_id: str) -> Path:
    result = context.upstream.get(stage) or {}
    matches = [
        row
        for row in result.get("artifacts") or ()
        if isinstance(row, Mapping) and str(row.get("artifact_id")) == artifact_id
    ]
    if len(matches) != 1:
        raise RuntimeError(f"upstream artifact {stage}:{artifact_id} is missing or duplicate")
    path = (context.project.root / str(matches[0]["relative_path"])).resolve()
    try:
        path.relative_to(context.project.root)
    except ValueError as exc:
        raise RuntimeError("upstream artifact escapes project root") from exc
    if not path.is_file() or sha256_file(path) != str(matches[0]["sha256"]):
        raise RuntimeError(f"upstream artifact {stage}:{artifact_id} is stale")
    return path


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path.name}")
    return payload


def _artifact(artifact_id: str, path: Path, **metadata: Any) -> dict[str, Any]:
    return {"artifact_id": artifact_id, "path": path, "metadata": metadata}


def _write_json(context: StageContext, name: str, payload: Mapping[str, Any]) -> Path:
    target = context.attempt_root / name
    atomic_write_json(target, dict(payload))
    return target


def _donor_index_mode(production: Mapping[str, Any]) -> str:
    """Return the explicit donor index mode without changing legacy defaults."""

    mode = str(production.get("donor_index_mode") or "").strip()
    if mode and mode != BROAD_LAZY_INDEX_MODE:
        raise ValueError(
            "settings.production.donor_index_mode must be broad_lazy_v1 or omitted"
        )
    return mode


def _bounded_production_int(
    production: Mapping[str, Any],
    key: str,
    default: int,
    *,
    maximum: int = 16,
) -> int:
    raw = production.get(key, default)
    if isinstance(raw, bool):
        raise ValueError(f"settings.production.{key} must be an integer from 1 to {maximum}")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"settings.production.{key} must be an integer from 1 to {maximum}"
        ) from exc
    if value < 1 or value > maximum or str(raw).strip() != str(value):
        raise ValueError(
            f"settings.production.{key} must be an integer from 1 to {maximum}"
        )
    return value


def _validate_broad_lazy_catalog(payload: Mapping[str, Any]) -> dict[str, Any]:
    from .broad_lazy import validate_broad_catalog

    catalog = validate_broad_catalog(payload)
    policy = catalog.get("policy") or {}
    if not (
        policy.get("full_source_coverage") is True
        and policy.get("allow_duration_cap") is False
        and policy.get("allow_manual_selection") is False
    ):
        raise ValueError("broad donor catalog weakens full-coverage/no-manual policy")
    if int(catalog.get("donor_count") or 0) < 1 or int(
        catalog.get("candidate_count") or 0
    ) < 1:
        raise ValueError("broad donor catalog is empty")
    if any(
        row.get("retrieval_only") is not True or row.get("renderable") is not False
        for row in catalog.get("candidates") or ()
    ):
        raise ValueError("broad donor catalog contains a prematurely renderable candidate")
    for donor in catalog.get("donors") or ():
        if abs(float(donor["covered_seconds"]) - float(donor["duration"])) > 1e-5:
            raise ValueError("broad donor catalog does not cover a donor completely")
    return catalog


def _broad_lazy_error_status(error: BaseException) -> str:
    if isinstance(error, (ImportError, ModuleNotFoundError)):
        return "needs_clip_model"
    detail = str(error).casefold()
    if any(
        marker in detail
        for marker in (
            "open_clip",
            "open-clip",
            "clip model",
            "embedding",
            "model weights",
            "torch",
        )
    ):
        return "needs_clip_model"
    return "needs_clean_donor_ranges"


def _raise_broad_lazy_block(
    context: StageContext,
    *,
    status: str,
    phase: str,
    error: BaseException,
    source_artifact: Mapping[str, Any] | None = None,
) -> None:
    diagnostics: dict[str, Any] = {
        "schema_version": 1,
        "contract": BROAD_LAZY_DIAGNOSTICS_CONTRACT,
        "mode": BROAD_LAZY_INDEX_MODE,
        "status": status,
        "phase": phase,
        "error_type": type(error).__name__,
    }
    if isinstance(source_artifact, Mapping):
        diagnostics["runtime_block"] = json.loads(json.dumps(source_artifact))
    target = _write_json(context, "broad_lazy_diagnostics.json", diagnostics)
    raise StageBlocked(
        status,
        f"{BROAD_LAZY_INDEX_MODE} {phase} failed ({type(error).__name__})",
        result={
            "status": status,
            "mode": BROAD_LAZY_INDEX_MODE,
            "artifacts": [_artifact("broad_lazy_diagnostics", target)],
        },
    )


def _paid_content_identity(
    context: StageContext,
    *,
    provider: str,
    operation_kind: str,
    request_hash: str,
) -> dict[str, Any]:
    """Return the portable identity of one exact paid provider request."""

    return {
        "contract": PAID_CONTENT_IDEMPOTENCY_CONTRACT,
        "provider": provider,
        "project_id": context.project.project_id,
        "stage": context.stage_name,
        "operation_kind": operation_kind,
        "request_hash": request_hash,
    }


def _paid_completed_envelope(
    row: Mapping[str, Any],
) -> tuple[bool, Mapping[str, Any] | None, Any]:
    raw_envelope = row.get("result_json")
    try:
        envelope = (
            json.loads(raw_envelope)
            if isinstance(raw_envelope, str)
            else raw_envelope
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return False, None, None
    if not isinstance(envelope, Mapping) or "value" not in envelope:
        return False, None, None
    return True, dict(envelope), envelope["value"]


def _resolve_exact_paid_replay(
    context: StageContext,
    *,
    provider: str,
    operation_kind: str,
    request_hash: str,
    exclude_idempotency_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Resolve exact legacy/v2 paid history without relying on project paths.

    A provider can legitimately return different stochastic values for the same
    request.  Durable restart therefore selects the earliest structurally valid
    completed envelope.  A claimed/submitted/corrupt-completed row blocks only
    when no such completed proof exists.
    """

    excluded = {str(value) for value in exclude_idempotency_keys}
    with context.state.connect() as connection:
        raw_rows = connection.execute(
            "SELECT provider,idempotency_key,operation_kind,request_hash,status,"
            "provider_job_id,result_json,created_at,updated_at "
            "FROM paid_operations WHERE provider=? AND operation_kind=? "
            "AND request_hash=? ORDER BY created_at,idempotency_key",
            (provider, operation_kind, request_hash),
        ).fetchall()
    rows = [
        dict(row)
        for row in raw_rows
        if str(row["idempotency_key"] or "") not in excluded
    ]
    completed: list[tuple[dict[str, Any], Mapping[str, Any], Any]] = []
    unsafe: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("status") or "")
        if status == "completed":
            valid, envelope, value = _paid_completed_envelope(row)
            if valid and envelope is not None:
                completed.append((row, envelope, value))
            else:
                unsafe.append(row)
        elif status in {"claimed", "submitted"}:
            unsafe.append(row)
    if completed:
        selected, envelope, value = completed[0]
        audit = {
            "contract": PAID_CONTENT_IDEMPOTENCY_CONTRACT,
            "selection_policy": PAID_COMPLETED_REPLAY_POLICY,
            "provider": provider,
            "operation_kind": operation_kind,
            "request_hash": request_hash,
            "idempotency_key": str(selected["idempotency_key"]),
            "created_at": float(selected["created_at"]),
            "result_envelope_hash": canonical_hash(envelope),
            "value_hash": canonical_hash(value),
            "exact_match_count": len(rows),
            "valid_completed_count": len(completed),
        }
        return {
            "action": "completed",
            "value": value,
            "envelope": dict(envelope),
            "row": selected,
            "audit": audit,
        }
    if unsafe:
        selected = unsafe[0]
        return {
            "action": "uncertain",
            "row": selected,
            "audit": {
                "contract": PAID_CONTENT_IDEMPOTENCY_CONTRACT,
                "selection_policy": PAID_COMPLETED_REPLAY_POLICY,
                "provider": provider,
                "operation_kind": operation_kind,
                "request_hash": request_hash,
                "idempotency_key": str(selected["idempotency_key"]),
                "status": str(selected.get("status") or "unknown"),
                "exact_match_count": len(rows),
                "valid_completed_count": 0,
            },
        }
    return {
        "action": "missing",
        "audit": {
            "contract": PAID_CONTENT_IDEMPOTENCY_CONTRACT,
            "selection_policy": PAID_COMPLETED_REPLAY_POLICY,
            "provider": provider,
            "operation_kind": operation_kind,
            "request_hash": request_hash,
            "exact_match_count": len(rows),
            "valid_completed_count": 0,
        },
    }


def _raise_paid_reconciliation(
    *,
    provider: str,
    operation_kind: str,
    request_hash: str,
    replay: Mapping[str, Any],
) -> None:
    row = replay.get("row")
    row = dict(row) if isinstance(row, Mapping) else {}
    idempotency_key = str(row.get("idempotency_key") or "")
    predecessor = {
        "provider": provider,
        "operation_kind": operation_kind,
        "idempotency_key": idempotency_key,
        "request_hash": request_hash,
        "status": str(row.get("status") or "unknown"),
        "provider_job_id": str(row.get("provider_job_id") or ""),
        "replay_audit": dict(replay.get("audit") or {}),
    }
    raise StageBlocked(
        "needs_paid_operation_reconciliation",
        f"{provider}/{operation_kind} may already have been submitted; "
        f"idempotency_key={idempotency_key}",
        result={"status": "paid_operation_uncertain", "paid_operation": predecessor},
    )


def _paid_call(
    context: StageContext,
    *,
    provider: str,
    operation_kind: str,
    request: Mapping[str, Any],
    call: Callable[[], Any],
    submission_receipt: Mapping[str, Any] | None = None,
) -> Any:
    """Execute once or stop for reconciliation after an uncertain crash.

    The local LLM facade has no remote job lookup endpoint.  We therefore mark
    the durable operation as submitted *before* entering it.  A process crash
    can never cause an automatic duplicate charge: restart either reuses the
    completed value or lands in ``needs_paid_operation_reconciliation``.
    """

    request_hash = canonical_hash(request)
    replay = _resolve_exact_paid_replay(
        context,
        provider=provider,
        operation_kind=operation_kind,
        request_hash=request_hash,
    )
    if replay["action"] == "completed":
        return replay["value"]
    if replay["action"] == "uncertain":
        _raise_paid_reconciliation(
            provider=provider,
            operation_kind=operation_kind,
            request_hash=request_hash,
            replay=replay,
        )
    paid_identity = _paid_content_identity(
        context,
        provider=provider,
        operation_kind=operation_kind,
        request_hash=request_hash,
    )
    idempotency_key = canonical_hash(paid_identity)
    claimed = context.state.claim_paid_operation(
        provider=provider,
        idempotency_key=idempotency_key,
        operation_kind=operation_kind,
        request_hash=request_hash,
    )
    if claimed["action"] == "resume":
        resumed = _resolve_exact_paid_replay(
            context,
            provider=provider,
            operation_kind=operation_kind,
            request_hash=request_hash,
        )
        if resumed["action"] == "completed":
            return resumed["value"]
        _raise_paid_reconciliation(
            provider=provider,
            operation_kind=operation_kind,
            request_hash=request_hash,
            replay=resumed,
        )
    raced = _resolve_exact_paid_replay(
        context,
        provider=provider,
        operation_kind=operation_kind,
        request_hash=request_hash,
        exclude_idempotency_keys=(idempotency_key,),
    )
    if raced["action"] == "completed":
        context.state.complete_paid_operation(
            provider,
            idempotency_key,
            {"value": raced["value"]},
        )
        return raced["value"]
    if raced["action"] == "uncertain":
        _raise_paid_reconciliation(
            provider=provider,
            operation_kind=operation_kind,
            request_hash=request_hash,
            replay=raced,
        )
    token = str(claimed["claim_token"])
    receipt = {
        "status": "entered_provider_call",
        "remote_job_lookup": False,
        "request_hash": request_hash,
        "paid_content_identity": paid_identity,
        "paid_content_identity_hash": idempotency_key,
    }
    if submission_receipt:
        receipt.update(dict(submission_receipt))
    context.state.record_paid_submission(
        provider,
        idempotency_key,
        token,
        f"sync-unqueryable-{idempotency_key[:20]}",
        receipt,
    )
    value = call()
    context.state.complete_paid_operation(
        provider,
        idempotency_key,
        {"value": value},
    )
    return value


def _salvage_visual_brief_slots(
    beats: Sequence[Mapping[str, Any]],
    raw_response: Any,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Validate expected rows independently and preserve every clean slot."""

    portable_beats = [dict(beat) for beat in beats]
    expected_ids = [str(beat.get("slot_id") or "") for beat in portable_beats]
    if not all(expected_ids) or len(set(expected_ids)) != len(expected_ids):
        raise ValueError("visual brief chunk has empty or duplicate expected slot_ids")

    def all_invalid(reason: str) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        return {}, {slot_id: reason for slot_id in expected_ids}

    if not isinstance(raw_response, Mapping):
        return all_invalid("raw visual brief response must be an object")
    if str(raw_response.get("contract_version") or "") != VISUAL_BRIEF_CONTRACT_VERSION:
        return all_invalid("raw visual brief response contract_version is missing or stale")
    raw_batches = raw_response.get("batches")
    if not isinstance(raw_batches, list) or not raw_batches:
        return all_invalid("raw visual brief response batches must be a non-empty array")

    declared_ids: list[str] = []
    raw_rows: list[Mapping[str, Any]] = []
    try:
        for raw_batch in raw_batches:
            if not isinstance(raw_batch, Mapping):
                return all_invalid("raw visual brief batch must be an object")
            slot_ids = raw_batch.get("slot_ids")
            if not isinstance(slot_ids, list) or not slot_ids:
                return all_invalid("raw visual brief batch slot_ids must be non-empty")
            declared_ids.extend(str(slot_id) for slot_id in slot_ids)
            content = raw_batch.get("content")
            if not isinstance(content, str) or not content.strip():
                return all_invalid("raw visual brief batch has no provider content")
            parsed = extract_json(content)
            if not isinstance(parsed, list):
                return all_invalid("visual brief response must be a JSON array")
            raw_rows.extend(row for row in parsed if isinstance(row, Mapping))
    except (TypeError, ValueError) as exc:
        return all_invalid(f"visual brief response JSON is malformed: {exc}")
    if declared_ids != expected_ids:
        return all_invalid("raw visual brief batch is bound to different slots")

    rows_by_id: dict[str, list[Mapping[str, Any]]] = {
        slot_id: [] for slot_id in expected_ids
    }
    for row in raw_rows:
        slot_id = str(row.get("slot_id") or "")
        if slot_id in rows_by_id:
            rows_by_id[slot_id].append(row)

    valid: dict[str, dict[str, Any]] = {}
    invalid: dict[str, str] = {}
    for beat, slot_id in zip(portable_beats, expected_ids):
        matches = rows_by_id[slot_id]
        if not matches:
            invalid[slot_id] = "provider response is missing this slot"
            continue
        if len(matches) != 1:
            invalid[slot_id] = "provider response duplicated this slot"
            continue
        try:
            valid[slot_id] = validate_visual_brief_contract(beat, dict(matches[0]))
        except (TypeError, ValueError) as exc:
            invalid[slot_id] = str(exc)
    return valid, invalid


def _request_visual_briefs_in_chunks(
    context: StageContext,
    beats: Sequence[Mapping[str, Any]],
    *,
    model: str,
    operation_prefix: str,
    request_metadata: Mapping[str, Any] | None = None,
    chunk_size: int | None = None,
    workers: int | None = None,
) -> list[dict[str, Any]]:
    """Run durable brief chunks with bounded repair and adaptive decomposition.

    Each base response is persisted before validation.  Valid rows are
    salvaged independently; only missing/invalid slot IDs get a bounded series
    of separately idempotent regenerations tied to the preceding raw hash and
    validation errors.  The default remains one regeneration; an explicit
    runtime opt-in may raise that limit to at most three.  A separate runtime
    opt-in may decompose an unresolved call after its uncertain-operation
    budget is exhausted; every child remains an independently durable paid
    operation and the recursion is capped at 8 -> 4 -> 2 -> 1.
    """

    portable_beats = [dict(beat) for beat in beats]
    if not portable_beats:
        return []
    production = _settings(context)
    runtime_uncertain_limit = os.environ.get(
        "SEMANTIC_MONTAGE_PAID_UNCERTAIN_RETRY_LIMIT"
    )
    uncertain_retry_limit = int(
        runtime_uncertain_limit
        if runtime_uncertain_limit is not None
        else production.get("paid_uncertain_retry_limit") or 0
    )
    if not 0 <= uncertain_retry_limit <= 3:
        raise ValueError("paid_uncertain_retry_limit must be between 0 and 3")
    split_on_empty_value = os.environ.get(
        "SEMANTIC_MONTAGE_VISUAL_BRIEF_SPLIT_ON_EMPTY", "0"
    ).strip()
    if split_on_empty_value not in {"0", "1"}:
        raise ValueError(
            "SEMANTIC_MONTAGE_VISUAL_BRIEF_SPLIT_ON_EMPTY must be 0 or 1"
        )
    split_on_empty = split_on_empty_value == "1"
    regeneration_limit_value = os.environ.get(
        "SEMANTIC_MONTAGE_VISUAL_BRIEF_REGENERATION_LIMIT", "1"
    ).strip()
    try:
        regeneration_limit = int(regeneration_limit_value)
    except ValueError as exc:
        raise ValueError(
            "SEMANTIC_MONTAGE_VISUAL_BRIEF_REGENERATION_LIMIT must be "
            "an integer between 1 and 3"
        ) from exc
    if not 1 <= regeneration_limit <= 3:
        raise ValueError(
            "SEMANTIC_MONTAGE_VISUAL_BRIEF_REGENERATION_LIMIT must be "
            "between 1 and 3"
        )
    configured_size = int(
        chunk_size
        if chunk_size is not None
        else production.get("visual_brief_batch_size") or 8
    )
    if configured_size < 1:
        raise ValueError("visual_brief_batch_size must be positive")
    effective_size = min(configured_size, 8)
    chunks = [
        portable_beats[start:start + effective_size]
        for start in range(0, len(portable_beats), effective_size)
    ]
    configured_workers = int(
        workers
        if workers is not None
        else production.get("visual_brief_workers") or 4
    )
    if configured_workers < 1:
        raise ValueError("visual_brief_workers must be positive")
    worker_count = min(configured_workers, len(chunks))
    base_metadata = dict(request_metadata or {})

    def provider_call(
        chunk: list[dict[str, Any]],
        *,
        operation_kind: str,
        request: Mapping[str, Any],
        submission_receipt: Mapping[str, Any] | None = None,
    ) -> Any:
        try:
            return _paid_call(
                context,
                provider="semantic_llm",
                operation_kind=operation_kind,
                request=request,
                call=lambda: request_visual_briefs_raw(
                    chunk,
                    model=model,
                    batch_size=len(chunk),
                    contract_version=VISUAL_BRIEF_CONTRACT_VERSION,
                ),
                submission_receipt=submission_receipt,
            )
        except StageBlocked as blocked:
            if (
                blocked.status != "needs_paid_operation_reconciliation"
                or uncertain_retry_limit == 0
            ):
                raise

            def predecessor_from(error: StageBlocked) -> dict[str, Any] | None:
                value = (
                    (error.result or {}).get("paid_operation")
                    if isinstance(error.result, Mapping)
                    else None
                )
                if not isinstance(value, Mapping) or value.get("status") != "submitted":
                    return None
                return dict(value)

            current_blocked = blocked
            predecessor_chain: list[dict[str, Any]] = []
            for recovery_attempt in range(1, uncertain_retry_limit + 1):
                predecessor = predecessor_from(current_blocked)
                if predecessor is None:
                    raise current_blocked
                predecessor_chain.append(predecessor)
                recovery_suffix = canonical_hash(predecessor)[:16]
                recovery_kind = (
                    f"{operation_kind}_uncertain_recovery_"
                    f"{recovery_attempt}_{recovery_suffix}"
                )
                recovery_audit: dict[str, Any] = {
                    "attempt": recovery_attempt,
                    "predecessor": predecessor,
                }
                # Keep attempt-1 byte-compatible with already persisted
                # recovery rows.  From attempt 2 onward the complete chain is
                # explicit in both the paid request and submission receipt.
                if recovery_attempt > 1:
                    recovery_audit["predecessor_chain"] = [
                        dict(row) for row in predecessor_chain
                    ]
                recovery_request = {
                    **dict(request),
                    "uncertain_recovery": recovery_audit,
                }
                try:
                    return _paid_call(
                        context,
                        provider="semantic_llm",
                        operation_kind=recovery_kind,
                        request=recovery_request,
                        call=lambda: request_visual_briefs_raw(
                            chunk,
                            model=model,
                            batch_size=len(chunk),
                            contract_version=VISUAL_BRIEF_CONTRACT_VERSION,
                        ),
                        submission_receipt={
                            **dict(submission_receipt or {}),
                            "uncertain_recovery": recovery_audit,
                        },
                    )
                except StageBlocked as recovery_blocked:
                    if recovery_blocked.status != "needs_paid_operation_reconciliation":
                        raise
                    next_predecessor = predecessor_from(recovery_blocked)
                    if next_predecessor is None:
                        raise
                    if recovery_attempt >= uncertain_retry_limit:
                        exhausted_chain = [
                            *predecessor_chain,
                            next_predecessor,
                        ]
                        raise StageBlocked(
                            "needs_paid_operation_reconciliation",
                            f"visual brief paid recovery limit "
                            f"{uncertain_retry_limit} exhausted for {operation_kind}",
                            result={
                                "status": "paid_operation_recovery_exhausted",
                                "paid_operation": next_predecessor,
                                "predecessor_chain": exhausted_chain,
                            },
                        ) from recovery_blocked
                    current_blocked = recovery_blocked
                except Exception as exc:
                    raise StageBlocked(
                        "needs_visual_brief_provider",
                        f"visual brief recovery failed for {operation_kind}: {exc}",
                    ) from exc
            raise current_blocked
        except Exception as exc:
            raise StageBlocked(
                "needs_visual_brief_provider",
                f"visual brief provider failed for {operation_kind}: {exc}",
            ) from exc

    def eligible_split_parent(blocked: StageBlocked) -> dict[str, Any] | None:
        """Return the portable paid-operation audit that permits a split."""

        if not split_on_empty or blocked.status != "needs_paid_operation_reconciliation":
            return None
        result = blocked.result
        if not isinstance(result, Mapping) or result.get("status") not in {
            "paid_operation_uncertain",
            "paid_operation_recovery_exhausted",
        }:
            return None
        paid_operation = result.get("paid_operation")
        if (
            not isinstance(paid_operation, Mapping)
            or paid_operation.get("status") != "submitted"
        ):
            return None
        return dict(result)

    def invalid_split_parent(
        *,
        operation_kind: str,
        request: Mapping[str, Any],
        raw_response: Any,
        invalid_by_id: Mapping[str, str],
    ) -> dict[str, Any]:
        return {
            "status": "invalid_visual_brief_response",
            "operation_kind": operation_kind,
            "request_hash": canonical_hash(request),
            "raw_response_hash": canonical_hash(raw_response),
            "invalid_slot_ids": list(invalid_by_id),
            "invalid_slot_errors": {
                str(slot_id): str(detail)[:500]
                for slot_id, detail in invalid_by_id.items()
            },
        }

    def resolve_by_adaptive_split(
        unresolved_beats: list[dict[str, Any]],
        *,
        root_operation_kind: str,
        chunk_index: int,
        parent_depth: int,
        parent_audit: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Resolve only unresolved slots through a bounded durable split tree."""

        unresolved_ids = [
            str(beat.get("slot_id") or "") for beat in unresolved_beats
        ]
        if not unresolved_beats or not all(unresolved_ids):
            raise ValueError("adaptive visual brief split needs non-empty slot_ids")
        if parent_depth >= 3 or len(unresolved_beats) == 1:
            raise StageBlocked(
                "needs_visual_brief_provider",
                "adaptive visual brief split exhausted at singleton slots "
                + ",".join(unresolved_ids),
                result={
                    "status": "visual_brief_split_exhausted",
                    "root_operation_kind": root_operation_kind,
                    "split_depth": parent_depth,
                    "unresolved_slot_ids": unresolved_ids,
                    "split_parent": dict(parent_audit),
                },
            )

        midpoint = (len(unresolved_beats) + 1) // 2
        child_chunks = [
            unresolved_beats[:midpoint],
            unresolved_beats[midpoint:],
        ]
        resolved: dict[str, dict[str, Any]] = {}
        child_depth = parent_depth + 1
        for child_index, child in enumerate(child_chunks):
            child_ids = [str(beat.get("slot_id") or "") for beat in child]
            split_binding = {
                "schema_version": 1,
                "root_operation_kind": root_operation_kind,
                "split_parent": dict(parent_audit),
                "split_depth": child_depth,
                "split_child_index": child_index,
                "split_child_count": len(child_chunks),
                "split_slot_ids": child_ids,
            }
            binding_hash = canonical_hash(split_binding)[:16]
            child_kind = (
                f"{root_operation_kind}_split_d{child_depth}_"
                f"{child_index:02d}_{binding_hash}"
            )
            child_metadata = {
                **base_metadata,
                "chunk_index": chunk_index,
                "chunk_count": len(chunks),
                "chunk_slot_ids": child_ids,
                "provider_calls_in_operation": 1,
                "adaptive_split": split_binding,
            }
            child_request = _visual_brief_paid_request(
                model,
                child,
                **child_metadata,
            )
            try:
                child_raw = provider_call(
                    child,
                    operation_kind=child_kind,
                    request=child_request,
                    submission_receipt={"adaptive_split": split_binding},
                )
            except StageBlocked as blocked:
                exhausted_parent = eligible_split_parent(blocked)
                if exhausted_parent is None:
                    raise
                nested = resolve_by_adaptive_split(
                    child,
                    root_operation_kind=root_operation_kind,
                    chunk_index=chunk_index,
                    parent_depth=child_depth,
                    parent_audit=exhausted_parent,
                )
                resolved.update(nested)
                continue

            child_valid, child_invalid = _salvage_visual_brief_slots(
                child, child_raw
            )
            resolved.update(child_valid)
            if child_invalid:
                invalid_child_beats = [
                    beat
                    for beat in child
                    if str(beat.get("slot_id") or "") in child_invalid
                ]
                nested = resolve_by_adaptive_split(
                    invalid_child_beats,
                    root_operation_kind=root_operation_kind,
                    chunk_index=chunk_index,
                    parent_depth=child_depth,
                    parent_audit=invalid_split_parent(
                        operation_kind=child_kind,
                        request=child_request,
                        raw_response=child_raw,
                        invalid_by_id=child_invalid,
                    ),
                )
                resolved.update(nested)
        return resolved

    def process_chunk(index: int, chunk: list[dict[str, Any]]) -> list[dict[str, Any]]:
        slot_ids = [str(beat.get("slot_id") or "") for beat in chunk]
        operation_kind = f"{operation_prefix}_chunk_{index:03d}"
        metadata = {
            **base_metadata,
            "chunk_index": index,
            "chunk_count": len(chunks),
            "chunk_slot_ids": slot_ids,
            "provider_calls_in_operation": 1,
        }
        request = _visual_brief_paid_request(model, chunk, **metadata)
        try:
            raw_response = provider_call(
                chunk,
                operation_kind=operation_kind,
                request=request,
            )
        except StageBlocked as blocked:
            exhausted_parent = eligible_split_parent(blocked)
            if exhausted_parent is None:
                raise
            split_by_id = resolve_by_adaptive_split(
                chunk,
                root_operation_kind=operation_kind,
                chunk_index=index,
                parent_depth=0,
                parent_audit=exhausted_parent,
            )
            return [split_by_id[slot_id] for slot_id in slot_ids]
        valid_by_id, invalid_by_id = _salvage_visual_brief_slots(chunk, raw_response)
        if not invalid_by_id:
            return [valid_by_id[slot_id] for slot_id in slot_ids]

        previous_raw_response = raw_response
        previous_regeneration_kind: str | None = None
        previous_regeneration_request: Mapping[str, Any] | None = None
        for regeneration_attempt in range(1, regeneration_limit + 1):
            invalid_beats = [
                beat
                for beat in chunk
                if str(beat.get("slot_id") or "") in invalid_by_id
            ]
            invalid_ids = [
                str(beat.get("slot_id") or "") for beat in invalid_beats
            ]
            invalid_raw_hash = canonical_hash(previous_raw_response)
            invalid_slot_errors = {
                slot_id: invalid_by_id[slot_id][:500] for slot_id in invalid_ids
            }
            subset_binding = {
                "slot_ids": invalid_ids,
                "invalid_raw_response_hash": invalid_raw_hash,
            }
            # Attempt 1 remains byte-compatible with persisted production
            # operations.  Later attempts also bind the operation namespace to
            # the current validator errors, so each retry is deterministic and
            # cannot alias a predecessor with different failure evidence.
            predecessor_binding: dict[str, Any] = {}
            if regeneration_attempt > 1:
                if (
                    previous_regeneration_kind is None
                    or previous_regeneration_request is None
                ):
                    raise AssertionError(
                        "visual brief regeneration predecessor is missing"
                    )
                predecessor_binding = {
                    "predecessor_operation_kind": previous_regeneration_kind,
                    "predecessor_request_hash": canonical_hash(
                        previous_regeneration_request
                    ),
                }
                subset_binding.update(
                    {
                        "invalid_slot_errors": invalid_slot_errors,
                        **predecessor_binding,
                    }
                )
            subset_key = canonical_hash(subset_binding)[:16]
            regeneration_kind = (
                f"{operation_kind}_regenerate_{regeneration_attempt}_"
                f"{subset_key}"
            )
            regeneration_metadata = {
                **base_metadata,
                "chunk_index": index,
                "chunk_count": len(chunks),
                "chunk_slot_ids": invalid_ids,
                "provider_calls_in_operation": 1,
                "regeneration_attempt": regeneration_attempt,
                "regeneration_reason": "invalid_visual_brief_slots",
                "invalid_raw_response_hash": invalid_raw_hash,
                "invalid_slot_errors": invalid_slot_errors,
                **predecessor_binding,
            }
            regeneration_request = _visual_brief_paid_request(
                model,
                invalid_beats,
                **regeneration_metadata,
            )
            try:
                regenerated_raw = provider_call(
                    invalid_beats,
                    operation_kind=regeneration_kind,
                    request=regeneration_request,
                )
            except StageBlocked as blocked:
                exhausted_parent = eligible_split_parent(blocked)
                if exhausted_parent is None:
                    raise
                split_by_id = resolve_by_adaptive_split(
                    invalid_beats,
                    root_operation_kind=operation_kind,
                    chunk_index=index,
                    parent_depth=0,
                    parent_audit=exhausted_parent,
                )
                merged = {**valid_by_id, **split_by_id}
                return [merged[slot_id] for slot_id in slot_ids]

            repaired_by_id, still_invalid = _salvage_visual_brief_slots(
                invalid_beats,
                regenerated_raw,
            )
            valid_by_id.update(repaired_by_id)
            if not still_invalid:
                return [valid_by_id[slot_id] for slot_id in slot_ids]

            invalid_by_id = still_invalid
            previous_raw_response = regenerated_raw
            previous_regeneration_kind = regeneration_kind
            previous_regeneration_request = regeneration_request
            if regeneration_attempt < regeneration_limit:
                continue

            failed_ids = [
                slot_id for slot_id in invalid_ids if slot_id in still_invalid
            ]
            if split_on_empty:
                split_by_id = resolve_by_adaptive_split(
                    [
                        beat
                        for beat in invalid_beats
                        if str(beat.get("slot_id") or "") in still_invalid
                    ],
                    root_operation_kind=operation_kind,
                    chunk_index=index,
                    parent_depth=0,
                    parent_audit=invalid_split_parent(
                        operation_kind=regeneration_kind,
                        request=regeneration_request,
                        raw_response=regenerated_raw,
                        invalid_by_id=still_invalid,
                    ),
                )
                merged = {**valid_by_id, **split_by_id}
                return [merged[slot_id] for slot_id in slot_ids]

            regeneration_count = (
                "one" if regeneration_limit == 1 else str(regeneration_limit)
            )
            raise StageBlocked(
                "needs_visual_brief_provider",
                f"visual brief chunk {index} slots {','.join(failed_ids)} remained "
                f"malformed after {regeneration_count} bounded subset "
                "regeneration"
                + ("" if regeneration_limit == 1 else "s")
                + ": "
                + "; ".join(
                    f"{slot_id}: {still_invalid[slot_id]}"
                    for slot_id in failed_ids
                ),
            )

        raise AssertionError("visual brief regeneration loop did not terminate")

    results: list[list[dict[str, Any]] | None] = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(process_chunk, index, chunk): index
            for index, chunk in enumerate(chunks)
        }
        for future in as_completed(futures):
            index = futures[future]
            results[index] = future.result()
    return [brief for rows in results if rows is not None for brief in rows]


def _fixture_stage(context: StageContext) -> Mapping[str, Any] | None:
    """Small deterministic orchestration E2E; never a releaseable media path."""

    if _settings(context).get("execution_mode") != "orchestration_fixture":
        return None
    if os.environ.get("SEMANTIC_MONTAGE_ALLOW_FIXTURE") != "1":
        raise StageBlocked(
            "needs_fixture_opt_in",
            "orchestration_fixture requires SEMANTIC_MONTAGE_ALLOW_FIXTURE=1",
        )
    upstream = {
        name: canonical_hash(value)
        for name, value in sorted(context.upstream.items())
    }
    payload = {
        "schema_version": 2,
        "stage": context.stage_name,
        "mode": "orchestration_fixture",
        "releaseable": False,
        "upstream": upstream,
    }
    path = _write_json(context, f"{context.stage_name}.fixture.json", payload)
    return {
        "status": "fixture_completed",
        "releaseable": False,
        "artifacts": [_artifact("document", path, kind="orchestration_fixture")],
    }


def _portable_candidate(candidate: Mapping[str, Any], source_asset_id: str) -> dict[str, Any]:
    row = dict(candidate)
    row.pop("source_path", None)
    row["source_asset_id"] = source_asset_id
    if row.get("contact_sheet"):
        row.pop("contact_sheet", None)
    return row


def _normalize_strict_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the missing strict overlay status without weakening its action."""

    row = dict(candidate)
    overlay = row.get("overlay_analysis")
    if isinstance(overlay, Mapping):
        normalized = dict(overlay)
        if not str(normalized.get("status") or "").strip():
            normalized["status"] = (
                "unresolved"
                if normalized.get("action") in {"inpaint_required", "crop_and_inpaint"}
                else "resolved"
            )
        row["overlay_analysis"] = normalized
    return row


def _unique_strict_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Give every distinct source range a stable artifact-safe candidate id.

    Imported repair pools may contain the same parent ``clip_id`` at several
    different in/out ranges.  Contact-sheet filenames and the durable artifact
    registry are keyed by ``clip_id``, so treating the parent id as a candidate
    id silently overwrites sheets and finally violates the artifact UNIQUE
    constraint.  Exact duplicate ranges are merged; distinct ranges are kept.
    """

    output: list[dict[str, Any]] = []
    by_range: dict[str, dict[str, Any]] = {}
    for raw in candidates:
        row = _normalize_strict_candidate(raw)
        parent_clip_id = str(row.get("clip_id") or "").strip()
        if not parent_clip_id:
            raise ValueError("strict candidate has no clip_id")
        identity = canonical_candidate_range(row)
        identity.pop("clip_id", None)
        range_hash = canonical_hash(identity)
        existing = by_range.get(range_hash)
        if existing is not None:
            merged_origins = sorted(
                {
                    *[str(value) for value in existing.get("cache_origins") or ()],
                    *[str(value) for value in row.get("cache_origins") or ()],
                }
            )
            if merged_origins:
                existing["cache_origins"] = merged_origins
            continue
        row["parent_clip_id"] = parent_clip_id
        row["clip_id"] = f"{parent_clip_id}__cand_{range_hash[:16]}"
        row["candidate_range_hash"] = range_hash
        by_range[range_hash] = row
        output.append(row)
    return output


def _strict_candidate_capacity(
    candidate: Mapping[str, Any], *, output_fps: float
) -> tuple[int, float]:
    """Return the exact output-frame capacity of a clean strict source span."""

    if output_fps <= 0:
        raise ValueError("output_fps must be positive")
    try:
        capacity_frames, capacity_seconds = validate_exact_candidate_contract(
            candidate,
            output_fps=output_fps,
        )
    except BroadLazyAdapterError:
        return 0, 0.0
    return int(capacity_frames), float(capacity_seconds)


def _strict_candidate_supports_beat(
    candidate: Mapping[str, Any], beat: Mapping[str, Any], *, output_fps: float
) -> bool:
    capacity_frames, _ = _strict_candidate_capacity(candidate, output_fps=output_fps)
    try:
        _start_frame, _end_frame, render_frames = interval_frame_span(
            float(beat["start"]), float(beat["end"]), output_fps
        )
    except (KeyError, TypeError, ValueError):
        return False
    return render_frames > 0 and capacity_frames >= render_frames


def _candidate_with_capacity(
    candidate: Mapping[str, Any], *, output_fps: float
) -> dict[str, Any]:
    row = _normalize_strict_candidate(candidate)
    frames, seconds = _strict_candidate_capacity(row, output_fps=output_fps)
    row["max_render_frames"] = frames
    row["max_render_seconds"] = round(seconds, 9)
    return row


def _beat_render_frames(beat: Mapping[str, Any], *, output_fps: float) -> int:
    try:
        _start_frame, _end_frame, render_frames = interval_frame_span(
            float(beat["start"]), float(beat["end"]), output_fps
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"semantic beat {beat.get('slot_id')} has invalid frame timing"
        ) from exc
    if render_frames < 1:
        raise ValueError(f"semantic beat {beat.get('slot_id')} has no render frames")
    return render_frames


def _validate_exact_candidate_for_beat(
    candidate: Mapping[str, Any],
    beat: Mapping[str, Any],
    *,
    output_fps: float,
) -> dict[str, Any]:
    """Return one path-free row only when it exactly matches the beat frames."""

    required_frames = _beat_render_frames(beat, output_fps=output_fps)
    row = dict(candidate)
    row.pop("source_path", None)
    row.pop("contact_sheet", None)
    capacity_frames, capacity_seconds = validate_exact_candidate_contract(
        row,
        output_fps=output_fps,
        required_render_frames=required_frames,
    )
    artifact_id = str(row.get("contact_sheet_artifact_id") or "").strip()
    if not artifact_id:
        raise BroadLazyAdapterError(
            "frame-exact semantic candidate has no contact-sheet artifact"
        )
    if not str(row.get("source_asset_id") or "").strip():
        raise BroadLazyAdapterError(
            "frame-exact semantic candidate has no portable source asset"
        )
    row["max_render_frames"] = int(capacity_frames)
    row["max_render_seconds"] = round(float(capacity_seconds), 9)
    return row


def _exact_repair_candidate_binding(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Bind the judged exact range, immutable PTS evidence and sheet identity."""

    frame_contract = candidate.get("frame_contract")
    if not isinstance(frame_contract, Mapping):
        raise ValueError("exact repair candidate has no frame contract")
    core = {
        "range": canonical_candidate_range(candidate),
        "candidate_range_hash": str(candidate.get("candidate_range_hash") or ""),
        "strict_variant_key": str(candidate.get("strict_variant_key") or ""),
        "render_frames": int(candidate.get("render_frames") or 0),
        "frame_contract_hash": canonical_hash(frame_contract),
    }
    binding = {
        **core,
        "candidate_hash": canonical_hash(core),
        "contact_sheet_artifact_id": str(
            candidate.get("contact_sheet_artifact_id") or ""
        ),
    }
    if binding["contact_sheet_artifact_id"].startswith("repair-sheet-"):
        sheet_sha256 = str(candidate.get("repair_sheet_sha256") or "")
        try:
            sheet_bytes = int(candidate.get("repair_sheet_bytes") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("exact repair sheet byte binding is malformed") from exc
        if len(sheet_sha256) != 64 or sheet_bytes <= 2048:
            raise ValueError("exact repair candidate has no content-bound repair sheet")
        binding.update(
            {
                "repair_sheet_sha256": sheet_sha256,
                "repair_sheet_bytes": sheet_bytes,
            }
        )
    return binding


def _exact_repair_candidate_bindings(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [_exact_repair_candidate_binding(row) for row in candidates]


def _semantic_resegment_changed_slot_projection(
    updates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Bind only the slots a resegmentation is allowed to replace."""

    projection: list[dict[str, Any]] = []
    for update in updates:
        slot_id = str(update.get("slot_id") or "")
        brief = update.get("brief")
        candidate_rows = update.get("candidate_rows")
        judge_result = update.get("judge_result")
        if (
            not slot_id
            or not isinstance(brief, Mapping)
            or not isinstance(candidate_rows, list)
            or not isinstance(judge_result, Mapping)
        ):
            raise ValueError("semantic resegmentation projection update is invalid")
        exact_bindings = _exact_repair_candidate_bindings(candidate_rows)
        projection.append(
            {
                "slot_id": slot_id,
                "brief_hash": canonical_hash(brief),
                "candidate_ranges_hash": canonical_hash(
                    [canonical_candidate_range(row) for row in candidate_rows]
                ),
                "exact_candidate_bindings": exact_bindings,
                "exact_candidate_bindings_hash": canonical_hash(exact_bindings),
                "judge_result_hash": canonical_hash(judge_result),
            }
        )
    return projection


def _portable_selected_visual_transform(
    slot_id: str,
    candidate: Mapping[str, Any],
    *,
    source_path: str | os.PathLike[str],
    output_fps: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Plan one render transform without binding it to a machine mount path."""

    from .render import (
        DETERMINISTIC_TRANSFORM_POLICY_VERSION,
        build_portable_transform_identity,
        deterministic_transform,
    )

    if DETERMINISTIC_TRANSFORM_POLICY_VERSION != (
        SELECTED_VISUAL_TRANSFORM_CONTRACT
    ):
        raise ValueError("selected visual transform policy is stale")
    runtime_candidate = {
        **dict(candidate),
        "source_path": str(Path(source_path).expanduser().resolve()),
    }
    runtime_slot = {
        "slot_id": str(slot_id).strip(),
        "selected_clip": runtime_candidate,
    }
    identity = build_portable_transform_identity(runtime_slot, fps=output_fps)
    transform = deterministic_transform(runtime_slot, fps=output_fps)
    binding_base = {
        "contract": SELECTED_VISUAL_TRANSFORM_CONTRACT,
        "identity": identity,
        "identity_hash": canonical_hash(identity),
        "render_transform": transform,
        "render_transform_hash": canonical_hash(transform),
    }
    return transform, {
        **binding_base,
        "binding_sha256": canonical_hash(binding_base),
    }


def _validate_selected_visual_gate_candidate_binding(
    receipt: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Cross-bind a validated gate receipt to the exact selected candidate."""

    binding = receipt.get("binding")
    content = binding.get("content") if isinstance(binding, Mapping) else None
    if not isinstance(content, Mapping):
        raise ValueError("selected visual gate receipt lost candidate content")
    expected = {
        "clip_id": str(candidate.get("clip_id") or ""),
        "candidate_key": str(candidate.get("candidate_key") or ""),
        "candidate_range_hash": str(
            candidate.get("candidate_range_hash") or ""
        ),
        "source_asset_id": str(candidate.get("source_asset_id") or ""),
        "source_sha256": str(
            candidate.get("source_sha256") or candidate.get("source_id") or ""
        ),
        "render_frames": int(candidate.get("render_frames") or 0),
    }
    observed = {key: content.get(key) for key in expected}
    if any(value in {"", 0} for value in expected.values()) or observed != expected:
        raise ValueError("selected visual gate receipt binds a different candidate")
    _validated_selected_visual_gate_exact_render_span(receipt, candidate)
    return expected


def _validated_selected_visual_gate_exact_render_span(
    receipt: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind the gate's half-open integer PTS range to the selected candidate."""

    from .render import (
        EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION,
        RenderError,
        build_exact_selected_pts_render_contract,
    )

    binding = receipt.get("binding")
    content = binding.get("content") if isinstance(binding, Mapping) else None
    gate_range = content.get("range") if isinstance(content, Mapping) else None
    if not isinstance(gate_range, Mapping):
        raise ValueError("selected visual gate receipt lost its exact PTS range")
    try:
        expected = build_exact_selected_pts_render_contract(
            {"selected_clip": dict(candidate)}
        )
    except (RenderError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("selected visual candidate exact PTS range is invalid") from exc
    if not isinstance(expected, Mapping):
        raise ValueError("selected visual candidate has no exact PTS render contract")
    exact_keys = (
        "source_in",
        "source_out",
        "source_frame_start",
        "source_frame_end",
        "source_frames",
        "start_pts",
        "end_pts",
        "time_base",
        "exact_pts_hash",
    )
    expected_range = {key: expected.get(key) for key in exact_keys}
    if dict(gate_range) != expected_range:
        raise ValueError("selected visual gate exact PTS range is stale")
    return {
        "contract": EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION,
        **expected_range,
    }


def _selected_visual_candidate_crop_margins(
    candidate: Mapping[str, Any],
) -> dict[str, float]:
    """Normalize the persisted detector crop that the renderer must consume."""

    overlay = candidate.get("overlay_analysis")
    if not isinstance(overlay, Mapping):
        raise ValueError("selected visual candidate lost overlay analysis")
    action = str(overlay.get("action") or "")
    raw = overlay.get("crop_margins") or {
        "left": 0.0,
        "right": 0.0,
        "top": 0.0,
        "bottom": 0.0,
    }
    keys = ("left", "right", "top", "bottom")
    if not isinstance(raw, Mapping) or set(raw) != set(keys):
        raise ValueError("selected visual candidate crop margins are malformed")
    margins: dict[str, float] = {}
    for key in keys:
        value = raw.get(key)
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "selected visual candidate crop margins are malformed"
            ) from exc
        if (
            isinstance(value, bool)
            or not math.isfinite(parsed)
            or not 0.0 <= parsed <= 0.25
        ):
            raise ValueError("selected visual candidate crop margins are malformed")
        margins[key] = parsed
    crop_actions = {"crop", "crop_and_exclude", "crop_and_inpaint"}
    if action == "none":
        if any(margins.values()):
            raise ValueError("selected visual action none carries a crop")
    elif action in crop_actions:
        if overlay.get("crop_safe") is not True or not any(margins.values()):
            raise ValueError("selected visual detector crop is not proven safe")
    else:
        raise ValueError("selected visual candidate crop action is not renderable")
    explicit = candidate.get("edge_cleanup_margins")
    if explicit is not None and explicit != margins:
        raise ValueError("selected visual explicit crop differs from detector crop")
    return margins


def _deduplicate_exact_repair_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in candidates:
        row = dict(raw)
        key = canonical_hash(
            {
                "range": canonical_candidate_range(row),
                "strict_variant_key": str(row.get("strict_variant_key") or ""),
            }
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output


def _hydrate_candidate(
    context: StageContext,
    candidate: Mapping[str, Any],
    *,
    sheet_stage: str | None = None,
) -> dict[str, Any]:
    row = dict(candidate)
    row["source_path"] = str(_resolve_asset(context, str(row["source_asset_id"])))
    if sheet_stage and row.get("contact_sheet_artifact_id"):
        row["contact_sheet"] = str(
            _artifact_path(context, sheet_stage, str(row["contact_sheet_artifact_id"]))
        )
    return row


def _asset_id_by_path(project: ProjectConfig, context: StageContext) -> dict[str, str]:
    output: dict[str, str] = {}
    for asset in project.assets:
        try:
            output[str(_resolve_asset(context, asset.asset_id))] = asset.asset_id
        except StageBlocked:
            continue
    return output


def stage_script_qa(context: StageContext) -> Mapping[str, Any]:
    fixture = _fixture_stage(context)
    if fixture is not None:
        return fixture
    script_id = _asset_id(context, "script_asset_id", "script")
    script = _resolve_asset(context, script_id).read_text(encoding="utf-8-sig").strip()
    try:
        report = require_script_quality(script).to_dict()
    except ScriptQualityError as exc:
        report_path = _write_json(context, "script_qa.json", exc.report.to_dict())
        raise StageBlocked("needs_script_edit", f"{exc}; report={report_path.name}") from exc
    report["script_asset_id"] = script_id
    report["script_sha256"] = hashlib.sha256(script.encode("utf-8")).hexdigest()
    path = _write_json(context, "script_qa.json", report)
    return {"status": "passed", "artifacts": [_artifact("script_qa", path)]}


def stage_tts_or_narration_import(context: StageContext) -> Mapping[str, Any]:
    fixture = _fixture_stage(context)
    if fixture is not None:
        return fixture
    production = _settings(context)
    narration_id = str(production.get("narration_asset_id") or "").strip()
    words_id = str(production.get("words_asset_id") or "").strip()
    if narration_id:
        narration = _resolve_asset(context, narration_id)
        if not words_id:
            raise StageBlocked(
                "needs_words_timing",
                "imported narration requires exact Edge/Whisper words JSON; ASR guessing is disabled",
            )
        words = _resolve_asset(context, words_id)
        load_words_document(words)
        return {
            "status": "imported",
            "narration_asset_id": narration_id,
            "words_asset_id": words_id,
            "artifacts": [],
        }

    script_id = _asset_id(context, "script_asset_id", "script")
    text = _resolve_asset(context, script_id).read_text(encoding="utf-8-sig").strip()
    voice = str(production.get("edge_voice") or "ru-RU-DmitryNeural")
    rate = str(production.get("edge_rate") or "+0%")
    audio = context.attempt_root / "narration.mp3"
    words = context.attempt_root / "words.json"
    try:
        result = synthesize_edge_tts_sync(
            text,
            audio,
            words_path=words,
            voice=voice,
            rate=rate,
        )
    except Exception as exc:
        raise StageBlocked("needs_tts_provider", f"Edge TTS unavailable: {exc}") from exc
    return {
        "status": "generated",
        "duration": result.audio_duration,
        "artifacts": [
            _artifact("narration", result.audio_path, media_type="audio/mpeg"),
            _artifact("words", result.words_path, media_type="application/json"),
        ],
    }


def _tts_paths(context: StageContext) -> tuple[Path, Path]:
    row = context.upstream["tts_or_narration_import"]
    if row.get("status") == "imported":
        return (
            _resolve_asset(context, str(row["narration_asset_id"])),
            _resolve_asset(context, str(row["words_asset_id"])),
        )
    return (
        _artifact_path(context, "tts_or_narration_import", "narration"),
        _artifact_path(context, "tts_or_narration_import", "words"),
    )


def stage_beats(context: StageContext) -> Mapping[str, Any]:
    fixture = _fixture_stage(context)
    if fixture is not None:
        return fixture
    _narration, words_path = _tts_paths(context)
    words = load_words_document(words_path)
    fps = float(context.project.settings.get("fps") or 25.0)
    target = context.attempt_root / "beats.json"
    payload = write_beats_json(
        target,
        words,
        fps=fps,
        min_seconds=float(context.project.settings.get("clip_seconds_min") or 4.0),
        max_seconds=float(context.project.settings.get("clip_seconds_max") or 8.0),
        target_seconds=float(_settings(context).get("target_clip_seconds") or 6.0),
    )
    return {
        "status": "planned",
        "beat_count": len(payload["beats"]),
        "artifacts": [_artifact("beats", target)],
    }


def _strict_frame_contract(path: Path, *, chunk_frames: int = 240) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bounded-memory full-frame transition/overlay analysis for one donor."""

    try:
        import cv2
    except ImportError as exc:
        raise StageBlocked("needs_opencv", "opencv-python is required for strict donor analysis") from exc
    from .donor import build_frame_clean_spans, detect_transition_zones, inspect_donor, probe_frame_timeline
    from .overlay_detector import (
        analyze_overlay_observations,
        compute_transition_metrics,
        detect_overlay_observations,
    )

    media = inspect_donor(path)
    frame_timeline = probe_frame_timeline(path)
    frame_count = int(frame_timeline["frame_count"])
    luma: list[float | None] = [None] * frame_count
    mae: list[float | None] = [None] * frame_count
    hist: list[float | None] = [None] * frame_count
    observations: dict[tuple[Any, ...], dict[str, Any]] = {}
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError(f"cannot decode donor: {path.name}")
    overlap = 8
    buffer: list[Any] = []
    buffer_start = 0
    decoded = 0
    analysed_until = 0
    dimensions: tuple[int, int] | None = None

    def analyse(frames: list[Any], start: int) -> None:
        nonlocal analysed_until, dimensions
        if not frames or start + len(frames) <= analysed_until:
            return
        prior_analysed_until = analysed_until
        metrics = compute_transition_metrics(frames) if len(frames) >= 2 else {
            "frame_luma": [float(np.asarray(frames[0]).mean())],
            "frame_mae": [0.0],
            "histogram_distance": [0.0],
        }
        for local, value in enumerate(metrics["frame_luma"]):
            index = start + local
            if index < frame_count and index >= prior_analysed_until:
                luma[index] = float(value)
                mae[index] = float(metrics["frame_mae"][local])
                hist[index] = float(metrics["histogram_distance"][local])
        if len(frames) >= 3:
            for raw in detect_overlay_observations(frames):
                item = dict(raw)
                item["frame_index"] = start + int(item["frame_index"])
                bbox = tuple(int(value) for value in item["bbox"])
                key = (item["frame_index"], bbox, str(item.get("class_hint") or ""))
                observations[key] = item
        height, width = np.asarray(frames[0]).shape[:2]
        dimensions = (int(width), int(height))
        analysed_until = max(analysed_until, start + len(frames))

    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            height, width = frame.shape[:2]
            if width > 640:
                scaled_height = max(2, int(round(height * 640.0 / width)))
                frame = cv2.resize(frame, (640, scaled_height), interpolation=cv2.INTER_AREA)
            buffer.append(frame)
            decoded += 1
            if len(buffer) >= max(32, int(chunk_frames)):
                analyse(buffer, buffer_start)
                buffer = buffer[-overlap:]
                buffer_start = decoded - len(buffer)
        analyse(buffer, buffer_start)
    finally:
        capture.release()
    if decoded != frame_count:
        raise ValueError(
            f"strict donor decode/frame probe mismatch for {path.name}: decoded={decoded}, probed={frame_count}"
        )
    if dimensions is None or any(value is None for value in luma + mae + hist):
        raise ValueError(f"strict donor analysis is incomplete: {path.name}")
    transition_zones = detect_transition_zones(
        [float(value) for value in luma],
        frame_mae=[float(value) for value in mae],
        histogram_distance=[float(value) for value in hist],
    )
    width, height = dimensions
    overlay = analyze_overlay_observations(
        list(observations.values()),
        frame_count=frame_count,
        width=width,
        height=height,
        fps=media.fps,
    )
    overlay["status"] = (
        "unresolved"
        if overlay.get("action") in {"inpaint_required", "crop_and_inpaint"}
        else "resolved"
    )
    contract = build_frame_clean_spans(
        frame_timeline["frame_pts"],
        frame_timeline["time_base"],
        transition_zones=transition_zones,
        contamination_zones=overlay.get("excluded_ranges") or (),
        end_pts=int(frame_timeline["end_pts"]),
        detector_metadata={
            "transition_detector": "semantic-transition-v2.0",
            "overlay_detector": dict(overlay["detector"]),
            "analysis_mode": "bounded_memory_full_frame",
        },
    )
    return contract, overlay


def stage_donor_index(context: StageContext) -> Mapping[str, Any]:
    fixture = _fixture_stage(context)
    if fixture is not None:
        return fixture
    from .donor import index_donor

    production = _settings(context)
    imported_id = str(production.get("strict_donor_index_asset_id") or "").strip()
    if imported_id:
        payload = _json(_resolve_asset(context, imported_id))
        indexes = payload.get("donors")
        if payload.get("schema_version") != 2 or not isinstance(indexes, list) or not indexes:
            raise ValueError("imported strict donor index must be schema v2 with donors")
        for donor in indexes:
            if not isinstance(donor, Mapping) or donor.get("schema_version") != 2:
                raise ValueError("every imported donor index must be strict schema v2")
        target = _write_json(context, "donor_index.json", payload)
        return {"status": "imported", "artifacts": [_artifact("donor_index", target)]}

    mode = _donor_index_mode(production)
    donor_ids = production.get("donor_asset_ids") or [
        asset.asset_id
        for asset in context.project.assets
        if str(asset.media_type or "").startswith("video/")
    ]
    if not isinstance(donor_ids, Sequence) or isinstance(donor_ids, (str, bytes)) or not donor_ids:
        raise StageBlocked("needs_donor_assets", "no donor_asset_ids are configured")
    if mode == BROAD_LAZY_INDEX_MODE:
        from .broad_lazy import BroadLazyError
        from .broad_lazy_runtime import BroadLazyRuntimeError, build_complete_broad_catalog

        normalized_ids = [str(raw_id).strip() for raw_id in donor_ids]
        if not all(normalized_ids) or len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("broad_lazy_v1 donor_asset_ids must be non-empty and unique")
        asset_paths = {
            asset_id: _resolve_asset(context, asset_id) for asset_id in normalized_ids
        }
        try:
            catalog = build_complete_broad_catalog(
                asset_paths,
                index_workers=_bounded_production_int(
                    production, "broad_lazy_index_workers", 2
                ),
            )
            catalog = _validate_broad_lazy_catalog(catalog)
        except (BroadLazyRuntimeError, BroadLazyError) as exc:
            _raise_broad_lazy_block(
                context,
                status="needs_clean_donor_ranges",
                phase="complete_donor_index",
                error=exc,
            )
        if {str(row["source_asset_id"]) for row in catalog["donors"]} != set(
            normalized_ids
        ):
            raise ValueError("broad donor catalog does not cover every configured asset")
        target = _write_json(context, "donor_index.json", catalog)
        return {
            "status": "broad_indexed",
            "donor_index_mode": BROAD_LAZY_INDEX_MODE,
            "donor_count": int(catalog["donor_count"]),
            "clip_count": int(catalog["candidate_count"]),
            "coverage_gate": {
                "complete": True,
                "capped_donor_count": 0,
                "manual_selection_count": 0,
            },
            "artifacts": [_artifact("donor_index", target)],
        }

    donors: list[dict[str, Any]] = []
    for raw_id in donor_ids:
        asset_id = str(raw_id)
        source = _resolve_asset(context, asset_id)
        contract, overlay = _strict_frame_contract(
            source, chunk_frames=int(production.get("donor_analysis_chunk_frames") or 240)
        )
        index = index_donor(source, frame_contract=contract, overlay_analysis=overlay).to_dict()
        index["media"].pop("path", None)
        index["media"]["asset_id"] = asset_id
        for clip in index["clips"]:
            clip.pop("source_path", None)
            clip["source_asset_id"] = asset_id
        donors.append(index)
    payload = {"schema_version": 2, "contract": "strict_frame_index", "donors": donors}
    target = _write_json(context, "donor_index.json", payload)
    return {
        "status": "indexed",
        "donor_count": len(donors),
        "clip_count": sum(len(row["clips"]) for row in donors),
        "artifacts": [_artifact("donor_index", target)],
    }


def stage_overlay_analysis(context: StageContext) -> Mapping[str, Any]:
    fixture = _fixture_stage(context)
    if fixture is not None:
        return fixture
    donor_index_path = _artifact_path(context, "donor_index", "donor_index")
    payload = _json(donor_index_path)
    production = _settings(context)
    mode = _donor_index_mode(production)
    broad_catalog = payload.get("contract") == "portable_broad_catalog_v1"
    if broad_catalog:
        if mode != BROAD_LAZY_INDEX_MODE:
            raise ValueError("broad donor catalog requires donor_index_mode=broad_lazy_v1")
        catalog = _validate_broad_lazy_catalog(payload)
        report = {
            "schema_version": 2,
            "contract": BROAD_LAZY_OVERLAY_REPORT_CONTRACT,
            "passed": True,
            "mode": BROAD_LAZY_INDEX_MODE,
            "status": "deferred_candidate_specific",
            "donor_count": int(catalog["donor_count"]),
            "candidate_count": int(catalog["candidate_count"]),
            "broad_catalog_hash": str(catalog["content_hash"]),
            "donor_index_sha256": sha256_file(donor_index_path),
            "candidate_specific_analysis": {
                "status": "deferred",
                "runtime_profile_contexts": _bounded_production_int(
                    production, "broad_lazy_profile_contexts", 5
                ),
                "reason": "ranked_candidates_only_in_temporal_retrieval",
            },
            "coverage_gate": {
                "complete": True,
                "capped_donor_count": 0,
                "manual_selection_count": 0,
            },
        }
        target = _write_json(context, "overlay_analysis.json", report)
        return {
            "status": "deferred_candidate_specific",
            "donor_index_mode": BROAD_LAZY_INDEX_MODE,
            "artifacts": [_artifact("overlay_analysis", target)],
        }
    if mode == BROAD_LAZY_INDEX_MODE and str(
        (context.upstream.get("donor_index") or {}).get("status") or ""
    ) != "imported":
        raise ValueError("broad_lazy_v1 produced a non-broad donor index")
    accepted = 0
    blocked = 0
    for donor in payload.get("donors") or ():
        for clip in donor.get("clips") or ():
            analysis = clip.get("overlay_analysis")
            spans = clip.get("clean_spans")
            if not isinstance(analysis, Mapping) or not isinstance(spans, list):
                raise ValueError("strict donor candidate lacks overlay/clean-frame evidence")
            if clip.get("overlay_blocked") or analysis.get("action") in {
                "inpaint_required", "crop_and_inpaint"
            }:
                blocked += 1
            elif analysis.get("status", "clean") in {"clean", "resolved"} and spans:
                accepted += 1
    if not accepted:
        raise StageBlocked("needs_clean_donor_ranges", "overlay analysis left no clean candidates")
    report = {
        "schema_version": 2,
        "passed": True,
        "accepted_candidates": accepted,
        "blocked_candidates": blocked,
        "donor_index_sha256": sha256_file(donor_index_path),
    }
    target = _write_json(context, "overlay_analysis.json", report)
    return {"status": "passed", "artifacts": [_artifact("overlay_analysis", target)]}


def _stage_broad_lazy_temporal_retrieval(
    context: StageContext,
    beats: Sequence[Mapping[str, Any]],
    index_doc: Mapping[str, Any],
) -> Mapping[str, Any]:
    from .broad_lazy import BroadLazyError
    from .broad_lazy_adapter import (
        BroadLazyAdapterError,
        BroadLazyRuntimeBlocked,
        BroadLazyRuntimeError,
        materialize_temporal_retrieval_v2,
    )
    from .broad_lazy_strict import (
        BroadLazyStrictError,
        build_runtime_overlay_profiles,
        make_lazy_strict_callback,
    )
    from .clip_model import clip_model_contract

    production = _settings(context)
    try:
        catalog = _validate_broad_lazy_catalog(index_doc)
    except BroadLazyError as exc:
        _raise_broad_lazy_block(
            context,
            status="needs_clean_donor_ranges",
            phase="broad_catalog_validation",
            error=exc,
        )

    overlay_path = _artifact_path(context, "overlay_analysis", "overlay_analysis")
    overlay_report = _json(overlay_path)
    if not (
        overlay_report.get("contract") == BROAD_LAZY_OVERLAY_REPORT_CONTRACT
        and overlay_report.get("mode") == BROAD_LAZY_INDEX_MODE
        and overlay_report.get("status") == "deferred_candidate_specific"
        and overlay_report.get("broad_catalog_hash") == catalog["content_hash"]
        and overlay_report.get("donor_index_sha256")
        == sha256_file(_artifact_path(context, "donor_index", "donor_index"))
    ):
        raise ValueError("broad lazy overlay deferral report is stale or invalid")

    brief_model = str(production.get("visual_brief_model") or "gpt-5-nano")
    briefs = _request_visual_briefs_in_chunks(
        context,
        beats,
        model=brief_model,
        operation_prefix="visual_briefs_initial",
    )
    output_fps = float(context.project.settings.get("fps") or 25.0)
    if not math.isfinite(output_fps) or output_fps <= 0:
        raise ValueError("settings.fps must be positive and finite")
    slot_render_frames = {
        str(beat["slot_id"]): int(round(float(beat["end"]) * output_fps))
        - int(round(float(beat["start"]) * output_fps))
        for beat in beats
    }
    if any(value < 1 for value in slot_render_frames.values()):
        raise ValueError("broad lazy beat has no render frames")

    resolver = {
        str(donor["source_asset_id"]): _resolve_asset(
            context, str(donor["source_asset_id"])
        )
        for donor in catalog["donors"]
    }
    raw_detector_policy = production.get("broad_lazy_detector_policy") or {}
    if not isinstance(raw_detector_policy, Mapping):
        raise ValueError("settings.production.broad_lazy_detector_policy must be an object")
    detector_policy = json.loads(json.dumps(dict(raw_detector_policy)))
    if detector_policy.get("allow_inpaint") is True or detector_policy.get(
        "allow_manual_crop"
    ) is True:
        raise ValueError("broad lazy detector policy cannot enable inpaint/manual crop")
    detector_policy["allow_inpaint"] = False
    detector_policy["allow_manual_crop"] = False

    cache_root = (
        context.project.work_root
        / "broad_lazy_v1_cache"
        / str(catalog["cache_key"])
    )
    artifact_root = context.attempt_root / "broad_lazy_retrieval"
    profile_contexts = _bounded_production_int(
        production, "broad_lazy_profile_contexts", 5
    )
    survivor_target = _bounded_production_int(
        production, "broad_lazy_survivor_target", 12, maximum=100
    )
    page_size = _bounded_production_int(
        production, "broad_lazy_page_size", 20, maximum=200
    )
    strict_workers = _bounded_production_int(
        production, "broad_lazy_strict_workers", 4
    )
    profile_workers = _bounded_production_int(
        production, "broad_lazy_profile_workers", 2
    )
    broad_sheet_workers = _bounded_production_int(
        production, "broad_lazy_broad_sheet_workers", 4
    )
    exact_sheet_workers = _bounded_production_int(
        production, "broad_lazy_exact_sheet_workers", 4
    )
    repair_pool_size = _bounded_production_int(
        production, "semantic_repair_pool_size", 60, maximum=500
    )
    embedding_batch_size = _bounded_production_int(
        production, "broad_lazy_embedding_batch_size", 64, maximum=512
    )
    model_contract = clip_model_contract()
    weights_sha256 = str(model_contract.get("weights_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", weights_sha256):
        raise StageBlocked(
            "needs_clip_model", "pinned CLIP model has no verified weights SHA-256"
        )
    model_contract_hash = canonical_hash(model_contract)

    try:
        profiles = build_runtime_overlay_profiles(
            catalog,
            resolver,
            cache_root,
            contexts_per_donor=profile_contexts,
            detector_policy=detector_policy,
            max_workers=profile_workers,
        )
        strict_callback = make_lazy_strict_callback(
            output_fps,
            profiles,
            cache_root,
            detector_policy,
            model_contract_hash=model_contract_hash,
        )
        callback_contract = str(
            getattr(strict_callback, "callback_contract", "") or ""
        )
        if not callback_contract:
            raise BroadLazyStrictError("lazy strict callback has no versioned contract")
        payload, paths = materialize_temporal_retrieval_v2(
            catalog,
            beats,
            briefs,
            slot_render_frames=slot_render_frames,
            asset_resolver=resolver,
            cache_directory=cache_root,
            artifact_directory=artifact_root,
            model_contract=model_contract,
            strict_upgrade_fn=strict_callback,
            detector_policy=detector_policy,
            output_fps=output_fps,
            survivor_target=survivor_target,
            repair_pool_size=repair_pool_size,
            page_size=page_size,
            broad_sheet_workers=broad_sheet_workers,
            strict_workers=strict_workers,
            exact_sheet_workers=exact_sheet_workers,
            embedding_batch_size=embedding_batch_size,
            overlay_profiles=profiles,
            callback_contract=callback_contract,
        )
    except BroadLazyRuntimeBlocked as exc:
        status = (
            exc.code
            if str(exc.code).startswith("needs_")
            else "needs_clean_donor_ranges"
        )
        _raise_broad_lazy_block(
            context,
            status=status,
            phase="lazy_strict_upgrade",
            error=exc,
            source_artifact=exc.artifact,
        )
    except (
        BroadLazyAdapterError,
        BroadLazyStrictError,
        BroadLazyRuntimeError,
        BroadLazyError,
    ) as exc:
        _raise_broad_lazy_block(
            context,
            status=_broad_lazy_error_status(exc),
            phase="temporal_materialization",
            error=exc,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        _raise_broad_lazy_block(
            context,
            status="needs_clip_model",
            phase="clip_model_loading",
            error=exc,
        )

    if payload.get("schema_version") != 2:
        raise ValueError("broad lazy adapter returned a non-v2 retrieval document")
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, Mapping):
        raise ValueError("broad lazy retrieval lacks path-free diagnostics")
    strict_diagnostics = diagnostics.get("strict_upgrade")
    if not isinstance(strict_diagnostics, Mapping):
        raise ValueError("broad lazy retrieval lacks strict diagnostics")
    provenance = strict_diagnostics.get("provenance")
    lineage_policy = strict_diagnostics.get("lineage_policy")
    if not isinstance(provenance, Mapping) or not isinstance(lineage_policy, Mapping):
        raise ValueError("broad lazy strict diagnostics are incomplete")
    if int(provenance.get("capped_donor_count", -1)) != 0 or int(
        provenance.get("manual_selection_count", -1)
    ) != 0:
        raise ValueError("broad lazy strict upgrade used caps or manual selection")
    if any(
        lineage_policy.get(key) is not False
        for key in (
            "manual_selection_allowed",
            "duration_caps_allowed",
            "fill_allowed",
            "retime_allowed",
        )
    ):
        raise ValueError("broad lazy strict lineage policy was weakened")

    portable_rows: list[Mapping[str, Any]] = []
    for field in ("ranked_candidates", "repair_candidates"):
        arrays = payload.get(field)
        if not isinstance(arrays, list) or len(arrays) != len(beats):
            raise ValueError(f"broad lazy retrieval has invalid {field}")
        for rows in arrays:
            if (
                not isinstance(rows, list)
                or not rows
                or not all(isinstance(row, Mapping) for row in rows)
            ):
                raise ValueError(f"broad lazy retrieval has an empty {field} slot")
            portable_rows.extend(rows)
    universe = payload.get("candidate_universe")
    if not isinstance(universe, list) or not universe or not all(
        isinstance(row, Mapping) for row in universe
    ):
        raise ValueError("broad lazy candidate universe is empty or invalid")
    portable_rows.extend(universe)
    if any(
        row.get("fill_strategy") != "none"
        or abs(float(row.get("retime_factor", 0.0)) - 1.0) > 1e-9
        or bool(row.get("manual_selection"))
        for row in portable_rows
    ):
        raise ValueError("broad lazy retrieval enabled fill, retiming, or manual selection")

    candidate_count = int(catalog["candidate_count"])
    strict_tasks = int(provenance.get("strict_unique_tasks") or 0)
    ranked_union_count = int(provenance.get("ranked_union_candidate_count") or 0)
    one_frame_contract = len(set(slot_render_frames.values())) == 1
    task_reduction_applicable = one_frame_contract and ranked_union_count < candidate_count
    if task_reduction_applicable and strict_tasks >= candidate_count:
        raise ValueError("lazy strict task count did not stay below the broad catalog")

    retrieval_path = Path(paths.get("retrieval") or "")
    embeddings_path = Path(paths.get("temporal_embeddings") or "")
    sheet_paths = paths.get("contact_sheets")
    if not retrieval_path.is_file() or not embeddings_path.is_file():
        raise ValueError("broad lazy adapter did not materialize retrieval/NPZ artifacts")
    if not isinstance(sheet_paths, Mapping) or not sheet_paths:
        raise ValueError("broad lazy adapter did not materialize exact contact sheets")
    sheet_artifacts = []
    for artifact_id, raw_path in sorted(sheet_paths.items()):
        path = Path(raw_path)
        if not path.is_file():
            raise ValueError("broad lazy exact contact sheet is missing")
        sheet_artifacts.append(_artifact(str(artifact_id), path))

    stage_diagnostics = {
        "schema_version": 1,
        "contract": BROAD_LAZY_DIAGNOSTICS_CONTRACT,
        "mode": BROAD_LAZY_INDEX_MODE,
        "status": "ready",
        "broad_catalog_hash": str(catalog["content_hash"]),
        "overlay_report_sha256": sha256_file(overlay_path),
        "model_contract_hash": model_contract_hash,
        "weights_sha256": weights_sha256,
        "slot_render_frames_hash": canonical_hash(slot_render_frames),
        "adapter": json.loads(json.dumps(diagnostics)),
        "gates": {
            "complete_broad_catalog": True,
            "capped_donor_count": 0,
            "manual_selection_count": 0,
            "fill_strategy": "none",
            "retime_factor": 1.0,
            "strict_unique_tasks": strict_tasks,
            "broad_catalog_candidate_count": candidate_count,
            "strict_tasks_less_than_broad_catalog": strict_tasks < candidate_count,
            "task_reduction_applicable": task_reduction_applicable,
        },
    }
    diagnostics_path = _write_json(
        context, "broad_lazy_diagnostics.json", stage_diagnostics
    )
    return {
        "status": "ranked",
        "donor_index_mode": BROAD_LAZY_INDEX_MODE,
        "slot_count": len(beats),
        "artifacts": [
            _artifact("retrieval", retrieval_path),
            _artifact(
                "temporal_embeddings",
                embeddings_path,
                kind="temporal_clip_embeddings",
                clip_ids_hash=str(payload.get("embedding_clip_ids_hash") or ""),
            ),
            *sheet_artifacts,
            _artifact("broad_lazy_diagnostics", diagnostics_path),
        ],
    }


def stage_temporal_retrieval(context: StageContext) -> Mapping[str, Any]:
    fixture = _fixture_stage(context)
    if fixture is not None:
        return fixture
    beats_doc = _json(_artifact_path(context, "beats", "beats"))
    beats = list(beats_doc["beats"])
    original_beats_hash = canonical_hash(beats)
    index_doc = _json(_artifact_path(context, "donor_index", "donor_index"))
    mode = _donor_index_mode(_settings(context))
    broad_catalog = index_doc.get("contract") == "portable_broad_catalog_v1"
    if broad_catalog:
        if mode != BROAD_LAZY_INDEX_MODE:
            raise ValueError("broad donor catalog requires donor_index_mode=broad_lazy_v1")
        return _stage_broad_lazy_temporal_retrieval(context, beats, index_doc)
    if mode == BROAD_LAZY_INDEX_MODE and str(
        (context.upstream.get("donor_index") or {}).get("status") or ""
    ) != "imported":
        raise ValueError("broad_lazy_v1 produced a non-broad donor index")
    from .pipeline import rank_beats
    from .retrieval import (
        embed_contact_sheet_rows,
        materialize_contact_sheets,
        rank_for_query,
    )
    clips = _unique_strict_candidates(
        [clip for donor in index_doc["donors"] for clip in donor["clips"]]
    )
    runtime_clips = [_hydrate_candidate(context, row) for row in clips]
    brief_model = str(_settings(context).get("visual_brief_model") or "gpt-5-nano")
    briefs = _request_visual_briefs_in_chunks(
        context,
        beats,
        model=brief_model,
        operation_prefix="visual_briefs_initial",
    )
    sheets = materialize_contact_sheets(
        [runtime_clips],
        context.attempt_root / "contact_sheets",
        candidates_per_slot=len(runtime_clips),
        workers=int(_settings(context).get("contact_sheet_workers") or 8),
    )[0]
    try:
        vectors = embed_contact_sheet_rows(sheets)
    except Exception as exc:
        raise StageBlocked("needs_clip_model", f"temporal CLIP is unavailable: {exc}") from exc
    output_fps = float(context.project.settings.get("fps") or 25.0)
    top_k = int(_settings(context).get("retrieval_top_k") or 20)
    repair_pool_size = int(_settings(context).get("semantic_repair_pool_size") or 60)
    if top_k < 1 or repair_pool_size < 1:
        raise ValueError("retrieval_top_k and semantic_repair_pool_size must be positive")
    try:
        ranked = rank_beats(
            beats,
            briefs,
            sheets,
            vectors,
            top_k=top_k,
            production=True,
            output_fps=output_fps,
        )
        raw_repair_ranked = [
            rank_for_query(
                str(brief.get("query_en") or ""),
                sheets,
                vectors,
                top_k=min(len(sheets), max(repair_pool_size, top_k * 3)),
            )
            for brief in briefs
        ]
    except Exception as exc:
        raise StageBlocked("needs_clip_model", f"temporal retrieval failed: {exc}") from exc
    sheet_artifacts: list[dict[str, Any]] = []
    sheet_ids: dict[str, str] = {}
    for clip in sheets:
        clip_id = str(clip["clip_id"])
        artifact_id = "sheet-" + hashlib.sha256(clip_id.encode("utf-8")).hexdigest()[:20]
        sheet_ids[clip_id] = artifact_id
        sheet_artifacts.append(_artifact(artifact_id, Path(str(clip["contact_sheet"])), clip_id=clip_id))

    candidate_universe: list[dict[str, Any]] = []
    universe_clip_ids: list[str] = []
    for row in sheets:
        item = _candidate_with_capacity(row, output_fps=output_fps)
        clip_id = str(item["clip_id"])
        portable = _portable_candidate(item, str(item["source_asset_id"]))
        portable["contact_sheet_artifact_id"] = sheet_ids[clip_id]
        candidate_universe.append(portable)
        universe_clip_ids.append(clip_id)

    embeddings_path = context.attempt_root / "temporal_embeddings.npz"
    temporary_embeddings = embeddings_path.with_name(
        f".{embeddings_path.name}.{os.getpid()}.tmp"
    )
    try:
        with temporary_embeddings.open("wb") as handle:
            np.savez_compressed(
                handle,
                vectors=np.asarray(vectors, dtype=np.float32),
                clip_ids=np.asarray(universe_clip_ids, dtype=np.str_),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_embeddings, embeddings_path)
    finally:
        temporary_embeddings.unlink(missing_ok=True)

    portable_ranked = []
    for rows in ranked:
        current = []
        for row in rows:
            item = _portable_candidate(row, str(row["source_asset_id"]))
            item["contact_sheet_artifact_id"] = sheet_ids[str(row["clip_id"])]
            current.append(item)
        portable_ranked.append(current)

    minimum_frames = int(math.ceil(4.0 * output_fps - 1e-9))
    portable_repair_ranked: list[list[dict[str, Any]]] = []
    for rows in raw_repair_ranked:
        current: list[dict[str, Any]] = []
        for row in rows:
            item = _candidate_with_capacity(row, output_fps=output_fps)
            if int(item.get("max_render_frames") or 0) < minimum_frames:
                continue
            portable = _portable_candidate(item, str(item["source_asset_id"]))
            portable["contact_sheet_artifact_id"] = sheet_ids[str(item["clip_id"])]
            current.append(portable)
            if len(current) >= repair_pool_size:
                break
        portable_repair_ranked.append(current)
    payload = {
        "schema_version": 2,
        "original_beats_hash": original_beats_hash,
        "effective_beats": json.loads(json.dumps(beats)),
        "effective_beats_hash": original_beats_hash,
        "visual_brief_contract_version": VISUAL_BRIEF_CONTRACT_VERSION,
        "briefs": briefs,
        "ranked_candidates": portable_ranked,
        "repair_candidates": portable_repair_ranked,
        "candidate_universe": candidate_universe,
        "embedding_artifact_id": "temporal_embeddings",
        "embedding_clip_ids_hash": canonical_hash(universe_clip_ids),
        "embedding_contract": "four_temporal_cells_mean_normalized",
    }
    target = _write_json(context, "temporal_retrieval.json", payload)
    return {
        "status": "ranked",
        "slot_count": len(portable_ranked),
        "artifacts": [
            _artifact("retrieval", target),
            _artifact(
                "temporal_embeddings",
                embeddings_path,
                kind="temporal_clip_embeddings",
                clip_ids_hash=canonical_hash(universe_clip_ids),
            ),
            *sheet_artifacts,
        ],
    }


def _semantic_selection_input_hash(
    beats: Sequence[Mapping[str, Any]],
    briefs: Sequence[Mapping[str, Any]],
    candidates: Sequence[Sequence[Mapping[str, Any]]],
    results: Sequence[Mapping[str, Any]],
) -> str:
    return canonical_hash(
        {
            "effective_beats": list(beats),
            "effective_briefs": list(briefs),
            "effective_ranked_candidates": [list(rows) for rows in candidates],
            "results": list(results),
        }
    )


def _semantic_timing_beat_identity(beat: Mapping[str, Any]) -> dict[str, Any]:
    """Return only fields that can change narration or render timing.

    ``boundary_reason`` is planner provenance, not part of the audiovisual
    contract.  A local repartition keeps the outer window edge fixed but may
    label it ``repair_window_end``; treating that label as a content change
    needlessly re-judges an otherwise untouched neighbour.
    """

    return {
        "slot_id": str(beat["slot_id"]),
        "start": float(beat["start"]),
        "end": float(beat["end"]),
        "duration": float(beat["duration"]),
        "text": str(beat["text"]),
        "word_start_index": int(beat["word_start_index"]),
        "word_end_index": int(beat["word_end_index"]),
    }


def _semantic_content_beat_identity(beat: Mapping[str, Any]) -> dict[str, Any]:
    """Return the narration content carried by one stable semantic slot.

    Start/end/duration are deliberately excluded.  A repair boundary may move
    within the same pause while the neighbour still carries exactly the same
    words; regenerating its visual brief in that case can replace a previously
    correct species/action brief even though only render capacity changed.
    """

    return {
        "slot_id": str(beat["slot_id"]),
        "text": str(beat["text"]),
        "word_start_index": int(beat["word_start_index"]),
        "word_end_index": int(beat["word_end_index"]),
    }


def _semantic_timing_changed_indices(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
) -> list[int]:
    """Find beats whose semantic/timing contract genuinely changed."""

    if len(before) != len(after):
        raise ValueError("semantic beat plans have different lengths")
    return [
        index
        for index, (prior, current) in enumerate(zip(before, after))
        if _semantic_timing_beat_identity(prior)
        != _semantic_timing_beat_identity(current)
    ]


def _post_resegment_brief_refresh_indices(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    *,
    target_index: int,
    changed_indices: Sequence[int],
) -> list[int]:
    """Choose which timing-changed slots need a fresh semantic brief.

    The target is always refreshed because its visual-duration contract caused
    the repair.  A neighbour is refreshed only when its narrated content or
    word coverage changed.  All ``changed_indices`` still continue through
    capacity filtering and strict judging; this helper controls only paid
    brief regeneration.
    """

    if len(before) != len(after):
        raise ValueError("semantic beat plans have different lengths")
    ordered = [int(index) for index in changed_indices]
    if len(ordered) != len(set(ordered)) or any(
        index < 0 or index >= len(before) for index in ordered
    ):
        raise ValueError("semantic timing-changed indices are invalid")
    if target_index not in ordered:
        raise ValueError("semantic timing-changed indices omit the target slot")
    return [
        index
        for index in ordered
        if index == target_index
        or _semantic_content_beat_identity(before[index])
        != _semantic_content_beat_identity(after[index])
    ]


def _strict_judge_resolved(result: Mapping[str, Any]) -> bool:
    if result.get("status") != "resolved" or result.get("judge_error"):
        return False
    chosen = result.get("chosen_index")
    evaluations = result.get("evaluations")
    if type(chosen) is not int or not isinstance(evaluations, list):
        return False
    if not 0 <= chosen < len(evaluations):
        return False
    evaluation = evaluations[chosen]
    if not isinstance(evaluation, Mapping) or evaluation.get("index") != chosen:
        return False
    try:
        confidence = float(evaluation.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return False
    return (
        str(evaluation.get("verdict") or "").casefold() in {"exact", "relevant"}
        and confidence > 0.0
    )


def _global_collision_eligible_matrix(
    beats: Sequence[Mapping[str, Any]],
    candidate_matrix: Sequence[Sequence[Mapping[str, Any]]],
    result_matrix: Sequence[Mapping[str, Any]],
    *,
    output_fps: float,
    allow_empty: bool = False,
) -> list[list[dict[str, Any]]]:
    """Build exactly the selector's capped, ordered, judged candidate universe."""

    if not (len(candidate_matrix) == len(result_matrix) == len(beats)):
        raise ValueError("global collision arrays differ in length")
    output: list[list[dict[str, Any]]] = []
    for beat, raw_rows, result in zip(beats, candidate_matrix, result_matrix):
        if not _strict_judge_resolved(result) and not (
            allow_empty
            and result.get("status") == "unresolved"
            and not result.get("judge_error")
        ):
            raise ValueError(
                f"global collision input is unresolved for {beat.get('slot_id')}"
            )
        evaluations = result.get("evaluations")
        if not isinstance(evaluations, list) or len(evaluations) != len(raw_rows):
            raise ValueError(
                f"global collision judge audit is stale for {beat.get('slot_id')}"
            )
        bounded_raw_rows = _global_collision_selectable_prefix(raw_rows)
        bounded_evaluations = evaluations[: len(bounded_raw_rows)]
        eligible: list[dict[str, Any]] = []
        for index, (raw, evaluation) in enumerate(
            zip(bounded_raw_rows, bounded_evaluations)
        ):
            if (
                not isinstance(raw, Mapping)
                or not isinstance(evaluation, Mapping)
                or evaluation.get("index") != index
            ):
                raise ValueError(
                    f"global collision candidate order is stale for "
                    f"{beat.get('slot_id')}"
                )
            verdict = str(evaluation.get("verdict") or "").casefold()
            try:
                confidence = float(evaluation.get("confidence") or 0.0)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "global collision judge confidence is malformed"
                ) from exc
            if (
                verdict not in {"exact", "relevant"}
                or not math.isfinite(confidence)
                or confidence <= 0.0
                or bool(raw.get("overlay_blocked"))
                or bool(raw.get("unresolved_overlay"))
            ):
                continue
            candidate = _validate_exact_candidate_for_beat(
                raw, beat, output_fps=output_fps
            )
            candidate.update(
                {
                    "judge_verdict": verdict,
                    "judge_confidence": confidence,
                    "judge_reason": str(evaluation.get("reason") or "")[:500],
                }
            )
            eligible.append(candidate)
        if not eligible and not allow_empty:
            raise ValueError(
                f"global collision slot {beat.get('slot_id')} has no eligible rows"
            )
        output.append(eligible)
    return output


def _global_assignment_beam_widths(configured_width: int) -> list[int]:
    """Return the deterministic bounded capacity ladder, preserving its base."""

    if type(configured_width) is not int or configured_width < 1:
        raise ValueError("global selection beam width must be a positive integer")
    ceiling = max(configured_width, GLOBAL_ASSIGNMENT_BEAM_WIDTH_CEILING)
    widths = [configured_width]
    while widths[-1] < ceiling:
        next_width = min(ceiling, widths[-1] * 2)
        if next_width == widths[-1]:
            raise ValueError("global selection beam ladder did not advance")
        widths.append(next_width)
    return widths


def _global_collision_exact_witness_selection(
    beats: Sequence[Mapping[str, Any]],
    candidate_matrix: Sequence[Sequence[Mapping[str, Any]]],
    result_matrix: Sequence[Mapping[str, Any]],
    diagnostic: Mapping[str, Any],
    *,
    output_fps: float,
    expected_receipt: Mapping[str, Any] | None = None,
) -> tuple[
    list[list[dict[str, Any]]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    """Validate an exact diagnostic witness and project it into judged rows."""

    from .retrieval import validate_global_collision_feasibility_witness

    eligible = _global_collision_eligible_matrix(
        beats,
        candidate_matrix,
        result_matrix,
        output_fps=output_fps,
    )
    slot_ids = [str(beat.get("slot_id") or "") for beat in beats]
    selected_indices = validate_global_collision_feasibility_witness(
        eligible,
        diagnostic,
        slot_ids=slot_ids,
    )
    if (
        len(selected_indices) != len(eligible)
        or any(type(index) is not int for index in selected_indices)
    ):
        raise ValueError("global collision witness indices are malformed")
    selected_rows = [
        dict(rows[index]) for rows, index in zip(eligible, selected_indices)
    ]
    singleton_candidates = [[row] for row in selected_rows]
    singleton_results = [
        {
            "chosen_index": 0,
            "status": "resolved",
            "evaluations": [
                {
                    "index": 0,
                    "verdict": str(row.get("judge_verdict") or ""),
                    "confidence": float(row.get("judge_confidence") or 0.0),
                    "reason": str(row.get("judge_reason") or "")[:500],
                }
            ],
            "reason": str(row.get("judge_reason") or "")[:500],
            "judge_error": None,
        }
        for row in selected_rows
    ]
    selected_bindings = _exact_repair_candidate_bindings(selected_rows)
    base_receipt = {
        "schema_version": 1,
        "contract": GLOBAL_ASSIGNMENT_EXACT_WITNESS_CONTRACT,
        "semantic_stage_version": SEMANTIC_JUDGE_STAGE_VERSION,
        "slot_ids": slot_ids,
        "selectable_candidates_per_slot": (
            GLOBAL_COLLISION_SELECTABLE_CANDIDATES_PER_SLOT
        ),
        "diagnostic_receipt_sha256": str(
            diagnostic.get("receipt_sha256") or ""
        ),
        "diagnostic_hash": canonical_hash(diagnostic),
        "feasible_witness_input_sha256": str(
            diagnostic.get("feasible_witness_input_sha256") or ""
        ),
        "selected_candidate_indices": selected_indices,
        "selected_candidate_indices_hash": canonical_hash(selected_indices),
        "selected_candidate_bindings": selected_bindings,
        "selected_candidate_bindings_hash": canonical_hash(selected_bindings),
        "singleton_judge_results_hash": canonical_hash(singleton_results),
    }
    receipt = {
        **base_receipt,
        "receipt_sha256": canonical_hash(base_receipt),
    }
    if expected_receipt is not None:
        cached = json.loads(json.dumps(dict(expected_receipt)))
        if cached != receipt:
            raise ValueError("cached global exact witness receipt is stale")
    return singleton_candidates, singleton_results, receipt


def _materialize_global_assignment(
    beats: Sequence[Mapping[str, Any]],
    briefs: Sequence[Mapping[str, Any]],
    candidate_matrix: Sequence[Sequence[Mapping[str, Any]]],
    result_matrix: Sequence[Mapping[str, Any]],
    diagnostic: Mapping[str, Any],
    *,
    output_fps: float,
    configured_beam_width: int,
    expected_receipt: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Materialize one deterministic assignment, with an exact witness fallback."""

    from .pipeline import SelectionError, assemble_timeline

    if str(diagnostic.get("status") or "") != "feasible":
        raise ValueError("global assignment materialization requires feasibility")
    selection_input_hash = _semantic_selection_input_hash(
        beats, briefs, candidate_matrix, result_matrix
    )
    beam_widths = _global_assignment_beam_widths(configured_beam_width)
    beam_failures: list[dict[str, Any]] = []
    timeline: dict[str, Any] | None = None
    selected_beam_width: int | None = None
    for beam_width in beam_widths:
        try:
            timeline = assemble_timeline(
                beats,
                briefs,
                candidate_matrix,
                result_matrix,
                fps=output_fps,
                production=True,
                beam_width=beam_width,
            )
        except SelectionError as exc:
            error = str(exc)
            beam_failures.append(
                {
                    "beam_width": beam_width,
                    "error": error,
                    "error_hash": canonical_hash(error),
                }
            )
            continue
        selected_beam_width = beam_width
        break

    witness_receipt: dict[str, Any] | None = None
    if timeline is None:
        witness_candidates, witness_results, witness_receipt = (
            _global_collision_exact_witness_selection(
                beats,
                candidate_matrix,
                result_matrix,
                diagnostic,
                output_fps=output_fps,
                expected_receipt=(
                    expected_receipt.get("exact_witness")
                    if isinstance(expected_receipt, Mapping)
                    and isinstance(expected_receipt.get("exact_witness"), Mapping)
                    else None
                ),
            )
        )
        timeline = assemble_timeline(
            beats,
            briefs,
            witness_candidates,
            witness_results,
            fps=output_fps,
            production=True,
            beam_width=1,
        )
        method = "exact_feasible_witness"
    else:
        method = "adaptive_global_beam"

    for slot_index, slot in enumerate(timeline.get("slots") or ()):
        provenance = dict(slot.get("selection_provenance") or {})
        provenance.update(
            {
                "assignment_materialization_policy": (
                    GLOBAL_ASSIGNMENT_MATERIALIZATION_POLICY_VERSION
                ),
                "assignment_method": method,
                "selected_beam_width": selected_beam_width,
            }
        )
        if witness_receipt is not None:
            provenance["exact_witness_candidate_index"] = witness_receipt[
                "selected_candidate_indices"
            ][slot_index]
        slot["selection_provenance"] = provenance

    base_receipt = {
        "schema_version": 1,
        "contract": GLOBAL_ASSIGNMENT_MATERIALIZATION_POLICY_VERSION,
        "semantic_stage_version": SEMANTIC_JUDGE_STAGE_VERSION,
        "selection_input_hash": selection_input_hash,
        "diagnostic_receipt_sha256": str(
            diagnostic.get("receipt_sha256") or ""
        ),
        "diagnostic_hash": canonical_hash(diagnostic),
        "configured_beam_width": configured_beam_width,
        "beam_width_ceiling": max(
            configured_beam_width, GLOBAL_ASSIGNMENT_BEAM_WIDTH_CEILING
        ),
        "beam_widths": beam_widths,
        "beam_failures": beam_failures,
        "beam_failures_hash": canonical_hash(beam_failures),
        "selected_beam_width": selected_beam_width,
        "method": method,
        "exact_witness": witness_receipt,
        "exact_witness_hash": canonical_hash(witness_receipt),
        "timeline_hash": canonical_hash(timeline),
    }
    receipt = {
        **base_receipt,
        "receipt_sha256": canonical_hash(base_receipt),
    }
    if expected_receipt is not None:
        cached = json.loads(json.dumps(dict(expected_receipt)))
        if cached != receipt:
            raise ValueError("cached global assignment materialization is stale")
    return timeline, receipt


def _resegmentation_resolution_outcome(
    *,
    target_index: int,
    changed_indices: Sequence[int],
    beats: Sequence[Mapping[str, Any]],
    prior_results: Sequence[Mapping[str, Any]],
    repaired_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Classify a local resegmentation without hiding collateral failures."""

    if target_index not in changed_indices:
        raise ValueError("resegmentation did not include its target slot")
    if len(changed_indices) != len(repaired_results):
        raise ValueError("resegmentation result count does not match changed slots")
    if not (len(beats) == len(prior_results)):
        raise ValueError("resegmentation beats/results have different lengths")

    result_by_index = {
        index: result for index, result in zip(changed_indices, repaired_results)
    }
    unresolved_indices = [
        index
        for index in changed_indices
        if not _strict_judge_resolved(result_by_index[index])
    ]
    newly_broken_indices = [
        index
        for index in unresolved_indices
        if index != target_index and _strict_judge_resolved(prior_results[index])
    ]
    queued_indices = [
        index
        for index in unresolved_indices
        if index != target_index and not _strict_judge_resolved(prior_results[index])
    ]
    target_resolved = _strict_judge_resolved(result_by_index[target_index])
    return {
        "target_resolved": target_resolved,
        "commit_allowed": target_resolved and not newly_broken_indices,
        "unresolved_changed_slots": [
            str(beats[index]["slot_id"]) for index in unresolved_indices
        ],
        "newly_broken_slots": [
            str(beats[index]["slot_id"]) for index in newly_broken_indices
        ],
        "queued_unresolved_neighbors": [
            str(beats[index]["slot_id"]) for index in queued_indices
        ],
    }


def _validated_receipt_slot_sequence(
    receipt: Mapping[str, Any],
    key: str,
    *,
    expected: Sequence[str] | None = None,
) -> list[str]:
    """Normalize a cached slot sequence and optionally bind it to recomputation."""

    raw = receipt.get(key)
    if raw is None:
        values: list[str] = []
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        values = [str(value or "").strip() for value in raw]
    else:
        raise ValueError(f"cached resegmentation receipt {key} is not a sequence")
    if any(not value for value in values) or len(values) != len(set(values)):
        raise ValueError(f"cached resegmentation receipt {key} is invalid")
    if expected is not None and values != [str(value) for value in expected]:
        raise ValueError(f"cached resegmentation receipt {key} disagrees with evidence")
    return values


_PROXY_ANCHOR_TOKEN = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
_PROXY_ANCHOR_STOPWORDS = frozenset(
    {
        "action",
        "camera",
        "close",
        "documentary",
        "environment",
        "live",
        "natural",
        "scene",
        "shot",
        "subject",
        "view",
        "visible",
        "wide",
    }
)


def _strict_semantic_failure_summary(
    judge_results: Sequence[Mapping[str, Any]],
) -> tuple[list[str], dict[str, Any]]:
    """Build stable re-brief evidence from audited strict judge rows."""

    reasons: set[str] = set()
    verdict_counts: dict[str, int] = {}
    evaluated_count = 0
    for result in judge_results:
        evaluations = result.get("evaluations")
        if isinstance(evaluations, list):
            ordered = sorted(
                (row for row in evaluations if isinstance(row, Mapping)),
                key=lambda row: int(row.get("index") or 0),
            )
            for row in ordered:
                verdict = str(row.get("verdict") or "wrong").strip().casefold()
                verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
                evaluated_count += 1
                if verdict in {"exact", "relevant"}:
                    continue
                reason = " ".join(str(row.get("reason") or "").split())
                reasons.add(
                    f"{verdict}: {reason}"
                    if reason
                    else f"{verdict}: candidate did not satisfy the visual brief"
                )
        judge_error = " ".join(str(result.get("judge_error") or "").split())
        if judge_error:
            reasons.add(f"judge_error: {judge_error}")
        if not evaluations:
            reason = " ".join(str(result.get("reason") or "").split())
            if reason:
                reasons.add(reason)
    if not reasons:
        reasons.add("strict judge found no exact or relevant camera-visible candidate")
    ordered_reasons = sorted(reasons, key=str.casefold)
    return ordered_reasons, {
        "evaluated_candidate_count": evaluated_count,
        "verdict_counts": {
            key: verdict_counts[key] for key in sorted(verdict_counts)
        },
        "no_acceptable_candidate": True,
        "natural_carrier_policy": {
            "contract_version": NATURAL_CARRIER_POLICY_VERSION,
            "enforce_compound_status_visible_scale": True,
            "continuous_shot_required": True,
            "drop_scarce_exact_landmark": True,
            "water_context_requires_opposite_natural_banks": True,
            "applies_when": (
                "narration combines a camera-visible scale or measurement with "
                "a non-visible status, legal, administrative, or jurisdictional claim"
            ),
            "preference": (
                "Prefer one broad camera-visible natural carrier or context, such as "
                "a watercourse with opposite natural banks, over a scarce exact "
                "landmark or fabricated physical proof."
            ),
            "status_handling": (
                "Keep the status, legal, administrative, or jurisdictional fact in "
                "narration only; never invent signs, flags, checkpoints, labels, "
                "borders, or infrastructure as evidence."
            ),
            "query_terms_are_advisory": True,
        },
        "natural_status_environment_policy": {
            "contract_version": NATURAL_STATUS_ENVIRONMENT_POLICY_VERSION,
            "compile_unnarrated_status_proof": True,
            "applies_when": (
                "a non-visible legal, recognition, protected, or heritage status "
                "is attached to a concrete natural environment"
            ),
            "preference": (
                "Use one continuous live-action view of the named natural environment; "
                "keep status in narration only."
            ),
            "forbidden_invented_proof": [
                "sign",
                "plaque",
                "logo",
                "emblem",
                "flag",
                "gate",
                "visitor centre",
                "certificate",
                "map",
                "infrastructure",
            ],
        },
        "policy": (
            "Simplify unsupported hard constraints; do not invent evidence. "
            "Treat a rare anchor as reserved only when adjacent narration names it "
            "and target narration does not."
        ),
    }


def _proxy_neighbor_context(
    beats: Sequence[Mapping[str, Any]],
    briefs: Sequence[Mapping[str, Any]],
    target_index: int,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Return portable adjacent text plus conservative rare-anchor allocation."""

    if len(beats) != len(briefs) or not 0 <= target_index < len(beats):
        raise ValueError("semantic proxy neighbor arrays are invalid")
    target_text = str(beats[target_index].get("text") or "").casefold()
    target_visible = " ".join(
        [
            str(briefs[target_index].get("query_en") or ""),
            *[str(value) for value in briefs[target_index].get("must_have") or ()],
            str(briefs[target_index].get("observable_basis_en") or ""),
        ]
    ).casefold()
    neighbor_texts: dict[str, str] = {}
    adjacent_context: list[dict[str, Any]] = []
    for relation, index in (("previous", target_index - 1), ("next", target_index + 1)):
        if not 0 <= index < len(beats):
            continue
        neighbor = beats[index]
        neighbor_brief = briefs[index]
        neighbor_text = str(neighbor.get("text") or "").strip()
        neighbor_texts[relation] = neighbor_text
        allocated: set[str] = set()
        for raw in neighbor_brief.get("must_have") or ():
            phrase = " ".join(str(raw).casefold().split())
            candidates = [phrase] if len(phrase) >= 4 else []
            candidates.extend(_PROXY_ANCHOR_TOKEN.findall(phrase))
            for anchor in candidates:
                if (
                    anchor not in _PROXY_ANCHOR_STOPWORDS
                    and anchor in neighbor_text.casefold()
                    and anchor not in target_text
                    and anchor in target_visible
                ):
                    allocated.add(anchor)
        row: dict[str, Any] = {
            "relation": relation,
            "slot_id": str(neighbor.get("slot_id") or ""),
            "narration_text": neighbor_text,
            "current_brief": dict(neighbor_brief),
        }
        if allocated:
            row.update(
                {
                    "anchor_en": sorted(allocated, key=str.casefold),
                    "allocated_to_slot_id": str(neighbor.get("slot_id") or ""),
                    "reason": (
                        "adjacent narration names this visible anchor while target "
                        "narration does not"
                    ),
                }
            )
        adjacent_context.append(row)
    return neighbor_texts, adjacent_context


def stage_semantic_judge(context: StageContext) -> Mapping[str, Any]:
    fixture = _fixture_stage(context)
    if fixture is not None:
        return fixture
    from .pipeline import build_judge_jobs
    from .retrieval import diagnose_global_collision_feasibility, rank_for_query
    from .semantic import judge_many_strict
    from .selected_visual_gate import (
        SELECTED_VISUAL_GATE_RECEIPT_CONTRACT,
        SELECTED_VISUAL_GATE_VERSION,
        SelectedVisualGateError,
        SelectedVisualGateRejected,
        build_selected_visual_actionable_profile_binding,
        evaluate_selected_visual_span,
        validate_selected_visual_gate_receipt,
    )

    settings = _settings(context)
    original_beats = _json(_artifact_path(context, "beats", "beats"))["beats"]
    original_beats_hash = canonical_hash(original_beats)
    retrieval = _json(_artifact_path(context, "temporal_retrieval", "retrieval"))
    if str(retrieval.get("original_beats_hash") or "") != original_beats_hash:
        raise ValueError("temporal retrieval is bound to different original beats")
    effective_beats = json.loads(json.dumps(retrieval.get("effective_beats") or ()))
    if not effective_beats or canonical_hash(effective_beats) != str(
        retrieval.get("effective_beats_hash") or ""
    ):
        raise ValueError("temporal retrieval effective_beats hash is invalid")
    if str(retrieval.get("visual_brief_contract_version") or "") != (
        VISUAL_BRIEF_CONTRACT_VERSION
    ):
        raise ValueError("temporal retrieval visual brief contract is stale")
    raw_effective_briefs = json.loads(json.dumps(retrieval.get("briefs") or ()))
    if not isinstance(raw_effective_briefs, list) or len(raw_effective_briefs) != len(
        effective_beats
    ):
        raise ValueError("temporal retrieval visual brief array is invalid")
    effective_briefs = [
        validate_visual_brief_contract(dict(beat), dict(brief))
        for beat, brief in zip(effective_beats, raw_effective_briefs)
        if isinstance(brief, Mapping)
    ]
    if len(effective_briefs) != len(effective_beats):
        raise ValueError("temporal retrieval contains a non-object visual brief")
    ranked_candidates = json.loads(
        json.dumps(retrieval.get("ranked_candidates") or ())
    )
    if not (
        len(effective_beats)
        == len(effective_briefs)
        == len(ranked_candidates)
    ):
        raise ValueError("temporal retrieval slot arrays have different lengths")

    judge_batch_size = int(settings.get("visual_judge_batch_size") or 6)
    repair_pool_size = int(settings.get("semantic_repair_pool_size") or 60)
    judge_workers = int(settings.get("visual_judge_workers") or 6)
    if judge_batch_size < 1 or repair_pool_size < 1 or judge_workers < 1:
        raise ValueError("semantic judge batch, pool and worker settings must be positive")
    judge_model = str(settings.get("visual_judge_model") or "gpt-5-nano")
    brief_model = str(settings.get("visual_brief_model") or "gpt-5-nano")
    proxy_repair_model = str(
        settings.get("semantic_proxy_repair_model") or brief_model
    )
    output_fps = float(context.project.settings.get("fps") or 25.0)
    min_seconds = float(context.project.settings.get("clip_seconds_min") or 4.0)
    max_seconds = float(context.project.settings.get("clip_seconds_max") or 8.0)
    target_seconds = float(settings.get("target_clip_seconds") or 6.0)
    window_radius = int(settings.get("semantic_repair_window_radius") or 2)
    collision_repair_max_attempts = _bounded_production_int(
        settings,
        "semantic_collision_repair_max_attempts",
        4,
        maximum=8,
    )
    selected_visual_repair_max_attempts = _bounded_production_int(
        settings,
        "selected_visual_repair_max_attempts",
        16,
        maximum=16,
    )
    selection_beam_width = int(settings.get("selection_beam_width") or 256)
    if selection_beam_width < 1:
        raise ValueError("settings.production.selection_beam_width must be positive")
    repair_cache_root = (
        context.project.work_root
        / "semantic_exact_repair_cache"
        / EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION
    )
    repair_ffmpeg = str(settings.get("ffmpeg") or "ffmpeg")
    repair_asset_resolver: dict[str, Path] = {}
    repair_sheet_records: dict[str, dict[str, Any]] = {}
    repair_sheet_candidates: dict[str, dict[str, Any]] = {}
    targeted_broad_runtime_cache: dict[str, Any] | None = None

    def register_repair_sheet(
        candidate: Mapping[str, Any], artifact: Mapping[str, Any]
    ) -> None:
        artifact_id = str(artifact.get("artifact_id") or "")
        candidate_artifact_id = str(
            candidate.get("contact_sheet_artifact_id") or ""
        )
        path = Path(str(artifact.get("path") or "")).expanduser().resolve()
        sheet_sha256 = str(candidate.get("repair_sheet_sha256") or "")
        try:
            sheet_bytes = int(candidate.get("repair_sheet_bytes") or 0)
        except (TypeError, ValueError):
            sheet_bytes = 0
        if (
            not artifact_id.startswith("repair-sheet-")
            or artifact_id != candidate_artifact_id
            or str(artifact.get("candidate_range_hash") or "")
            != str(candidate.get("candidate_range_hash") or "")
            or not path.is_file()
            or path.stat().st_size <= 2048
            or sheet_bytes != path.stat().st_size
            or sheet_sha256 != sha256_file(path)
            or str(artifact.get("repair_sheet_sha256") or "") != sheet_sha256
            or int(artifact.get("repair_sheet_bytes") or 0) != sheet_bytes
        ):
            raise ValueError("exact repair sheet artifact binding is invalid")
        try:
            path.relative_to(repair_cache_root.resolve())
        except ValueError as exc:
            raise ValueError("exact repair sheet escaped its project cache") from exc
        record = {
            "artifact_id": artifact_id,
            "path": path,
            "clip_id": str(candidate.get("clip_id") or ""),
            "candidate_range_hash": str(candidate.get("candidate_range_hash") or ""),
            "repair_sheet_sha256": sheet_sha256,
            "repair_sheet_bytes": sheet_bytes,
        }
        existing = repair_sheet_records.get(artifact_id)
        if existing is not None and existing != record:
            raise ValueError("exact repair sheet artifact id collision")
        repair_sheet_records[artifact_id] = record
        repair_sheet_candidates[artifact_id] = dict(candidate)

    def resolve_repair_asset(candidate: Mapping[str, Any]) -> dict[str, Path]:
        source_asset_id = str(candidate.get("source_asset_id") or "").strip()
        if not source_asset_id:
            raise ValueError("exact repair candidate lost source_asset_id")
        if source_asset_id not in repair_asset_resolver:
            repair_asset_resolver[source_asset_id] = _resolve_asset(
                context, source_asset_id
            )
        return {source_asset_id: repair_asset_resolver[source_asset_id]}

    def materialize_repair_sheet(
        candidate: Mapping[str, Any], *, cache_only: bool
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "asset_resolver": resolve_repair_asset(candidate),
            "cache_directory": repair_cache_root,
            "output_fps": output_fps,
            "ffmpeg": repair_ffmpeg,
        }
        if cache_only:
            def reject_regeneration(*_args: Any, **_kwargs: Any) -> None:
                raise BroadLazyAdapterError(
                    "cached exact repair sheet is missing or stale"
                )

            kwargs["sheet_maker"] = reject_regeneration
        try:
            portable, artifact = materialize_exact_repair_candidate(candidate, **kwargs)
            sheet_path = Path(str(artifact.get("path") or "")).expanduser().resolve()
            portable = {
                **portable,
                "repair_sheet_sha256": sha256_file(sheet_path),
                "repair_sheet_bytes": sheet_path.stat().st_size,
            }
            artifact = {
                **artifact,
                "repair_sheet_sha256": portable["repair_sheet_sha256"],
                "repair_sheet_bytes": portable["repair_sheet_bytes"],
            }
            register_repair_sheet(portable, artifact)
        except StageBlocked:
            raise
        except (
            BroadLazyAdapterError,
            BroadLazyRuntimeError,
            OSError,
            RuntimeError,
            SubprocessError,
            ValueError,
        ) as exc:
            # This is the runtime boundary for public exact-sheet materialization:
            # adapter/runtime validation, FFmpeg and sheet-maker failures all become
            # an explicit repair terminal instead of leaking a raw stage error.
            if cache_only:
                raise ValueError(
                    f"cached exact repair sheet failed verification: {exc}"
                ) from exc
            raise StageBlocked(
                "needs_exact_repair_sheet",
                f"frame-exact repair sheet materialization failed: {exc}",
            ) from exc
        return portable

    def exactify_repair_candidates(
        candidates: Sequence[Mapping[str, Any]],
        beat: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        required_frames = _beat_render_frames(beat, output_fps=output_fps)
        exact_rows: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        kept_exact = 0
        rematerialized = 0
        for raw in candidates:
            row = dict(raw)
            try:
                declared_frames = int(row.get("render_frames") or 0)
            except (TypeError, ValueError):
                declared_frames = 0
            if declared_frames == required_frames:
                try:
                    portable = _validate_exact_candidate_for_beat(
                        row, beat, output_fps=output_fps
                    )
                    if str(portable.get("contact_sheet_artifact_id") or "").startswith(
                        "repair-sheet-"
                    ):
                        portable = materialize_repair_sheet(
                            portable, cache_only=True
                        )
                    exact_rows.append(portable)
                    kept_exact += 1
                except (BroadLazyAdapterError, ValueError) as exc:
                    failures.append(
                        {
                            "clip_id": str(row.get("clip_id") or ""),
                            "reason": str(exc)[:500],
                        }
                    )
                continue
            try:
                variants = rematerialize_strict_candidate_variants(
                    row,
                    output_fps=output_fps,
                    render_frames=required_frames,
                )
            except StrictCandidateError as exc:
                failures.append(
                    {
                        "clip_id": str(row.get("clip_id") or ""),
                        "reason": str(exc)[:500],
                    }
                )
                continue
            for variant in variants:
                portable = materialize_repair_sheet(variant, cache_only=False)
                portable = _validate_exact_candidate_for_beat(
                    portable, beat, output_fps=output_fps
                )
                exact_rows.append(portable)
                rematerialized += 1
        deduplicated = _deduplicate_exact_repair_candidates(exact_rows)
        audit = {
            "contract": EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION,
            "strict_rematerialization_contract": STRICT_REMATERIALIZATION_CONTRACT,
            "slot_id": str(beat.get("slot_id") or ""),
            "required_render_frames": required_frames,
            "input_candidate_count": len(candidates),
            "kept_exact_count": kept_exact,
            "rematerialized_variant_count": rematerialized,
            "output_candidate_count": len(deduplicated),
            "failures": failures,
            "failures_hash": canonical_hash(failures),
        }
        return deduplicated, audit

    def progressively_exactify_ranked_candidates(
        candidates: Sequence[Mapping[str, Any]],
        beat: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Materialize only the ranked prefix needed for one bounded judge batch."""

        ranked = list(candidates)
        materialization_limit = min(len(ranked), repair_pool_size)
        page_size = max(1, judge_batch_size)
        considered = 0
        exact_rows: list[dict[str, Any]] = []
        page_audits: list[dict[str, Any]] = []
        while considered < materialization_limit:
            page_end = min(materialization_limit, considered + page_size)
            page_rows, page_audit = exactify_repair_candidates(
                ranked[considered:page_end], beat
            )
            page_audits.append(
                {
                    "rank_start": considered,
                    "rank_end": page_end,
                    "audit": page_audit,
                }
            )
            considered = page_end
            exact_rows = _deduplicate_exact_repair_candidates(
                [*exact_rows, *page_rows]
            )
            preview = next_non_overlapping_candidate_batch(
                exact_rows,
                (),
                batch_size=judge_batch_size,
            )
            if len(preview) >= judge_batch_size:
                break
        audit = {
            "contract": EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION,
            "mode": "progressive_ranked_prefix_v1",
            "slot_id": str(beat.get("slot_id") or ""),
            "ranked_candidate_count": len(ranked),
            "materialization_limit": materialization_limit,
            "considered_candidate_count": considered,
            "unconsidered_candidate_count": len(ranked) - considered,
            "output_candidate_count": len(exact_rows),
            "page_audits": page_audits,
            "page_audits_hash": canonical_hash(page_audits),
        }
        return exact_rows, audit

    def validate_exact_rematerialization_audit(
        audit: Mapping[str, Any],
        beat: Mapping[str, Any],
        *,
        progressive: bool = False,
    ) -> None:
        slot_id = str(beat.get("slot_id") or "")
        required_frames = _beat_render_frames(beat, output_fps=output_fps)
        if (
            str(audit.get("contract") or "")
            != EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION
            or str(audit.get("slot_id") or "") != slot_id
        ):
            raise ValueError("cached exact rematerialization audit identity is stale")
        if progressive:
            if str(audit.get("mode") or "") != "progressive_ranked_prefix_v1":
                raise ValueError("cached progressive exact audit mode is stale")
            page_audits = audit.get("page_audits")
            if (
                not isinstance(page_audits, list)
                or str(audit.get("page_audits_hash") or "")
                != canonical_hash(page_audits)
            ):
                raise ValueError("cached progressive exact page audit hash is invalid")
            try:
                ranked_count = int(audit.get("ranked_candidate_count"))
                limit = int(audit.get("materialization_limit"))
                considered = int(audit.get("considered_candidate_count"))
                unconsidered = int(audit.get("unconsidered_candidate_count"))
                output_count = int(audit.get("output_candidate_count"))
            except (TypeError, ValueError) as exc:
                raise ValueError("cached progressive exact audit counts are malformed") from exc
            if not (
                0 <= considered <= limit <= ranked_count
                and unconsidered == ranked_count - considered
                and output_count >= 0
            ):
                raise ValueError("cached progressive exact audit counts are invalid")
            cursor = 0
            for page in page_audits:
                if not isinstance(page, Mapping):
                    raise ValueError("cached progressive exact audit page is invalid")
                try:
                    rank_start = int(page.get("rank_start"))
                    rank_end = int(page.get("rank_end"))
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "cached progressive exact audit page bounds are malformed"
                    ) from exc
                nested = page.get("audit")
                if (
                    rank_start != cursor
                    or not rank_start < rank_end <= considered
                    or not isinstance(nested, Mapping)
                ):
                    raise ValueError("cached progressive exact audit page bounds are invalid")
                validate_exact_rematerialization_audit(nested, beat)
                if int(nested.get("input_candidate_count") or -1) != (
                    rank_end - rank_start
                ):
                    raise ValueError("cached progressive exact page input count is invalid")
                cursor = rank_end
            if cursor != considered:
                raise ValueError("cached progressive exact pages do not cover considered rows")
            return

        failures = audit.get("failures")
        if (
            str(audit.get("strict_rematerialization_contract") or "")
            != STRICT_REMATERIALIZATION_CONTRACT
            or int(audit.get("required_render_frames") or 0) != required_frames
            or not isinstance(failures, list)
            or str(audit.get("failures_hash") or "") != canonical_hash(failures)
        ):
            raise ValueError("cached exact rematerialization audit contract is invalid")
        try:
            input_count = int(audit.get("input_candidate_count"))
            kept_count = int(audit.get("kept_exact_count"))
            variant_count = int(audit.get("rematerialized_variant_count"))
            output_count = int(audit.get("output_candidate_count"))
        except (TypeError, ValueError) as exc:
            raise ValueError("cached exact rematerialization audit counts are malformed") from exc
        if not (
            input_count >= 0
            and 0 <= kept_count <= input_count
            and variant_count >= 0
            and 0 <= output_count <= kept_count + variant_count
            and len(failures) <= input_count - kept_count
            and all(
                isinstance(row, Mapping)
                and str(row.get("clip_id") or "")
                and str(row.get("reason") or "")
                for row in failures
            )
        ):
            raise ValueError("cached exact rematerialization audit counts are invalid")

    def validate_receipt_exact_audit(
        receipt: Mapping[str, Any],
        beat: Mapping[str, Any],
        *,
        progressive: bool = False,
    ) -> None:
        audit = receipt.get("exact_rematerialization_audit")
        if (
            not isinstance(audit, Mapping)
            or str(receipt.get("exact_rematerialization_audit_hash") or "")
            != canonical_hash(audit)
        ):
            raise ValueError("cached exact rematerialization receipt audit is invalid")
        validate_exact_rematerialization_audit(
            audit,
            beat,
            progressive=progressive,
        )

    def exact_receipt_fields(
        candidates: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        bindings = _exact_repair_candidate_bindings(candidates)
        return {
            "exact_repair_contract": EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION,
            "exact_candidate_bindings": bindings,
            "exact_candidate_bindings_hash": canonical_hash(bindings),
        }

    def validate_cached_exact_batch(
        candidates: Sequence[Mapping[str, Any]],
        beat: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        if str(receipt.get("exact_repair_contract") or "") != (
            EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION
        ):
            raise ValueError("cached semantic repair exact contract is stale")
        validated: list[dict[str, Any]] = []
        for raw in candidates:
            row = _validate_exact_candidate_for_beat(
                raw, beat, output_fps=output_fps
            )
            if str(row.get("contact_sheet_artifact_id") or "").startswith(
                "repair-sheet-"
            ):
                recovered = materialize_repair_sheet(row, cache_only=True)
                if _exact_repair_candidate_binding(recovered) != (
                    _exact_repair_candidate_binding(row)
                ):
                    raise ValueError("cached exact repair sheet changed candidate identity")
                row = recovered
            validated.append(row)
        bindings = _exact_repair_candidate_bindings(validated)
        if (
            receipt.get("exact_candidate_bindings") != bindings
            or str(receipt.get("exact_candidate_bindings_hash") or "")
            != canonical_hash(bindings)
        ):
            raise ValueError("cached semantic repair exact candidate receipt is invalid")
        return validated

    def build_honest_fallback_bundle(
        source_brief: Mapping[str, Any],
        active_brief: Mapping[str, Any],
        policy: Mapping[str, Any] | None,
        compiler_receipt: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        source = json.loads(json.dumps(dict(source_brief)))
        active = json.loads(json.dumps(dict(active_brief)))
        portable_policy = (
            json.loads(json.dumps(dict(policy)))
            if isinstance(policy, Mapping)
            else None
        )
        portable_receipt = (
            json.loads(json.dumps(dict(compiler_receipt)))
            if isinstance(compiler_receipt, Mapping)
            else None
        )
        bundle = {
            "contract": HONEST_VISIBLE_FALLBACK_POLICY_VERSION,
            "source_brief": source,
            "source_brief_hash": canonical_hash(source),
            "policy": portable_policy,
            "policy_hash": canonical_hash(portable_policy),
            "active": portable_receipt is not None,
            "active_brief_hash": canonical_hash(active),
            "compiler_receipt": portable_receipt,
            "compiler_receipt_hash": canonical_hash(portable_receipt),
        }
        validate_honest_fallback_bundle(bundle, active)
        return bundle

    def validate_honest_fallback_bundle(
        bundle: Mapping[str, Any],
        active_brief: Mapping[str, Any],
        *,
        expected_source_brief: Mapping[str, Any] | None = None,
        expected_source_brief_hash: str | None = None,
    ) -> dict[str, Any]:
        if str(bundle.get("contract") or "") != (
            HONEST_VISIBLE_FALLBACK_POLICY_VERSION
        ):
            raise ValueError("cached honest visible fallback contract is stale")
        source = bundle.get("source_brief")
        policy = bundle.get("policy")
        compiler_receipt = bundle.get("compiler_receipt")
        if not isinstance(source, Mapping):
            raise ValueError("cached honest visible fallback source brief is missing")
        source = dict(source)
        active = dict(active_brief)
        if (
            str(bundle.get("source_brief_hash") or "") != canonical_hash(source)
            or str(bundle.get("policy_hash") or "") != canonical_hash(policy)
            or str(bundle.get("active_brief_hash") or "") != canonical_hash(active)
            or str(bundle.get("compiler_receipt_hash") or "")
            != canonical_hash(compiler_receipt)
        ):
            raise ValueError("cached honest visible fallback bundle hash is invalid")
        if expected_source_brief is not None and source != dict(expected_source_brief):
            raise ValueError("cached honest visible fallback source brief is stale")
        if expected_source_brief_hash is not None and canonical_hash(source) != str(
            expected_source_brief_hash
        ):
            raise ValueError("cached honest visible fallback source brief hash is stale")
        expected_policy = build_honest_visible_fallback_policy(
            source, {"no_acceptable_candidate": True}
        )
        if policy != expected_policy:
            raise ValueError("cached honest visible fallback policy is stale")
        active_flag = bundle.get("active") is True
        if not active_flag:
            if compiler_receipt is not None:
                raise ValueError("inactive honest visible fallback has a compiler receipt")
            return dict(bundle)
        if not isinstance(policy, Mapping) or not isinstance(
            compiler_receipt, Mapping
        ):
            raise ValueError("active honest visible fallback evidence is incomplete")
        clean, popped = pop_honest_visible_fallback_receipt(
            {
                **active,
                HONEST_VISIBLE_FALLBACK_RECEIPT_FIELD: dict(compiler_receipt),
            }
        )
        if clean != active or popped != dict(compiler_receipt):
            raise ValueError("cached honest visible fallback receipt is not detachable")
        expected_receipt_keys = {
            "contract_version",
            "compiler",
            "source_brief_sha256",
            "policy_source_fallback_sha256",
            "source_fallback_sha256",
            "policy_sha256",
            "retained_facets",
            "retained_facet_matches",
            "unresolved_narrated_details",
            "demoted_secondary_facets",
            "active_brief_sha256",
        }
        if set(str(key) for key in compiler_receipt) != expected_receipt_keys:
            raise ValueError("cached honest visible fallback receipt shape is stale")
        compiler_policy = {
            "contract_version": str(policy.get("contract_version") or ""),
            "source_brief_sha256": str(policy.get("source_brief_sha256") or ""),
            "source_fallback_sha256": str(
                policy.get("source_fallback_sha256") or ""
            ),
            "primary_visible_facets": dict(
                policy.get("primary_visible_facets") or {}
            ),
            "secondary_visible_facets": sorted(
                [
                    dict(row)
                    for row in policy.get("secondary_visible_facets") or ()
                    if isinstance(row, Mapping)
                ],
                key=lambda row: (
                    str(row.get("kind") or ""),
                    str(row.get("label") or "").casefold(),
                ),
            ),
        }
        if (
            str(compiler_receipt.get("contract_version") or "")
            != HONEST_VISIBLE_FALLBACK_POLICY_VERSION
            or str(compiler_receipt.get("compiler") or "")
            != HONEST_VISIBLE_FALLBACK_POLICY_VERSION
            or str(compiler_receipt.get("source_brief_sha256") or "")
            != str(policy.get("source_brief_sha256") or "")
            or str(compiler_receipt.get("policy_source_fallback_sha256") or "")
            != str(policy.get("source_fallback_sha256") or "")
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(compiler_receipt.get("source_fallback_sha256") or ""),
            )
            is None
            or str(compiler_receipt.get("policy_sha256") or "")
            != canonical_hash(compiler_policy)
            or str(compiler_receipt.get("active_brief_sha256") or "")
            != canonical_hash(active)
        ):
            raise ValueError("cached honest visible fallback receipt binding is stale")
        primary = policy.get("primary_visible_facets")
        secondary = policy.get("secondary_visible_facets")
        retained = compiler_receipt.get("retained_facets")
        retained_matches = compiler_receipt.get("retained_facet_matches")
        demoted = compiler_receipt.get("demoted_secondary_facets")
        unresolved_details = compiler_receipt.get("unresolved_narrated_details")
        if not all(
            isinstance(value, Mapping)
            for value in (primary, retained, retained_matches)
        ) or not isinstance(secondary, list) or not isinstance(demoted, list):
            raise ValueError("cached honest visible fallback facet evidence is malformed")
        expected_roles = {"subject", "action", "spatial_relation"}
        if set(primary) != expected_roles or set(retained) != expected_roles or set(
            retained_matches
        ) != expected_roles:
            raise ValueError("cached honest visible fallback primary facets are stale")
        for role in expected_roles:
            facet = primary[role]
            if not isinstance(facet, Mapping) or str(retained[role]) != str(
                facet.get("label") or ""
            ) or str(retained_matches[role]) not in [
                str(value) for value in facet.get("match_any") or ()
            ]:
                raise ValueError("cached honest visible fallback retained facet is stale")
        allowed_demotions = {
            canonical_hash(
                {"kind": str(row.get("kind") or ""), "label": str(row.get("label") or "")}
            )
            for row in secondary
            if isinstance(row, Mapping)
        }
        demotion_keys = [
            canonical_hash(
                {"kind": str(row.get("kind") or ""), "label": str(row.get("label") or "")}
            )
            for row in demoted
            if isinstance(row, Mapping)
        ]
        if (
            not demoted
            or len(demotion_keys) != len(demoted)
            or len(set(demotion_keys)) != len(demotion_keys)
            or any(key not in allowed_demotions for key in demotion_keys)
            or unresolved_details
            != [str(row.get("label") or "") for row in demoted]
        ):
            raise ValueError("cached honest visible fallback demotions are stale")
        return dict(bundle)

    def merge_targeted_broad_candidates(
        broad_rows: Sequence[Mapping[str, Any]],
        frozen_rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen_ranges: set[str] = set()
        seen_parents: set[str] = set()
        for raw in [*broad_rows, *frozen_rows]:
            row = dict(raw)
            range_key = str(row.get("candidate_range_hash") or "") or canonical_hash(
                canonical_candidate_range(row)
            )
            parent_key = str(row.get("parent_clip_id") or "").strip()
            if range_key in seen_ranges or (
                parent_key and parent_key in seen_parents
            ):
                continue
            seen_ranges.add(range_key)
            if parent_key:
                seen_parents.add(parent_key)
            merged.append(row)
        return merged

    def frozen_candidate_universe() -> list[dict[str, Any]]:
        universe = retrieval.get("candidate_universe")
        if not isinstance(universe, list) or not universe or any(
            not isinstance(row, Mapping) for row in universe
        ):
            raise ValueError("temporal retrieval has no frozen exact candidate universe")
        return [dict(row) for row in universe]

    def load_targeted_broad_runtime() -> dict[str, Any]:
        nonlocal targeted_broad_runtime_cache
        if targeted_broad_runtime_cache is not None:
            return targeted_broad_runtime_cache
        from .broad_lazy import BroadLazyError
        from .broad_lazy_strict import (
            BroadLazyStrictError,
            build_runtime_overlay_profiles,
            make_lazy_strict_callback,
        )
        from .clip_model import clip_model_contract

        index_path = _artifact_path(context, "donor_index", "donor_index")
        index_doc = _json(index_path)
        if index_doc.get("contract") != "portable_broad_catalog_v1":
            raise ValueError(
                "honest visible fallback requires the immutable broad donor catalog"
            )
        try:
            catalog = _validate_broad_lazy_catalog(index_doc)
        except BroadLazyError as exc:
            raise ValueError("targeted semantic broad catalog is invalid") from exc
        overlay_path = _artifact_path(context, "overlay_analysis", "overlay_analysis")
        overlay_report = _json(overlay_path)
        if not (
            overlay_report.get("contract") == BROAD_LAZY_OVERLAY_REPORT_CONTRACT
            and overlay_report.get("mode") == BROAD_LAZY_INDEX_MODE
            and overlay_report.get("status") == "deferred_candidate_specific"
            and overlay_report.get("broad_catalog_hash") == catalog["content_hash"]
            and overlay_report.get("donor_index_sha256") == sha256_file(index_path)
        ):
            raise ValueError("targeted semantic broad overlay report is stale")
        resolver = {
            str(donor["source_asset_id"]): _resolve_asset(
                context, str(donor["source_asset_id"])
            )
            for donor in catalog["donors"]
        }
        raw_detector_policy = settings.get("broad_lazy_detector_policy") or {}
        if not isinstance(raw_detector_policy, Mapping):
            raise ValueError(
                "settings.production.broad_lazy_detector_policy must be an object"
            )
        detector_policy = json.loads(json.dumps(dict(raw_detector_policy)))
        if detector_policy.get("allow_inpaint") is True or detector_policy.get(
            "allow_manual_crop"
        ) is True:
            raise ValueError(
                "targeted semantic broad detector cannot enable inpaint/manual crop"
            )
        detector_policy["allow_inpaint"] = False
        detector_policy["allow_manual_crop"] = False
        broad_cache_root = (
            context.project.work_root
            / "broad_lazy_v1_cache"
            / str(catalog["cache_key"])
        )
        profile_contexts = _bounded_production_int(
            settings, "broad_lazy_profile_contexts", 5
        )
        profile_workers = min(
            8,
            _bounded_production_int(settings, "broad_lazy_profile_workers", 2),
        )
        page_size = _bounded_production_int(
            settings, "semantic_broad_repair_page_size", 20, maximum=64
        )
        max_workers = _bounded_production_int(
            settings, "semantic_broad_repair_workers", 4, maximum=8
        )
        embedding_batch_size = _bounded_production_int(
            settings,
            "semantic_broad_repair_embedding_batch_size",
            64,
            maximum=256,
        )
        try:
            search_seconds = float(
                settings.get("semantic_broad_repair_search_seconds") or 4.0
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "settings.production.semantic_broad_repair_search_seconds must be numeric"
            ) from exc
        if not math.isfinite(search_seconds) or not 0 < search_seconds <= 30.0:
            raise ValueError(
                "settings.production.semantic_broad_repair_search_seconds must be in (0,30]"
            )
        model_contract = clip_model_contract()
        model_contract_hash = canonical_hash(model_contract)
        try:
            profiles = build_runtime_overlay_profiles(
                catalog,
                resolver,
                broad_cache_root,
                contexts_per_donor=profile_contexts,
                detector_policy=detector_policy,
                max_workers=profile_workers,
            )
            strict_callback = make_lazy_strict_callback(
                output_fps,
                profiles,
                broad_cache_root,
                detector_policy,
                model_contract_hash=model_contract_hash,
            )
        except BroadLazyStrictError as exc:
            raise ValueError(
                f"targeted semantic broad strict runtime is unavailable: {exc}"
            ) from exc
        callback_contract = str(
            getattr(strict_callback, "callback_contract", "") or ""
        )
        if not callback_contract:
            raise ValueError("targeted semantic broad callback has no contract")
        targeted_broad_runtime_cache = {
            "catalog": catalog,
            "asset_resolver": resolver,
            "broad_cache_directory": broad_cache_root,
            "repair_cache_directory": repair_cache_root,
            "model_contract": model_contract,
            "strict_upgrade_fn": strict_callback,
            "detector_policy": detector_policy,
            "callback_contract": callback_contract,
            "overlay_profiles": profiles,
            "survivor_target": min(judge_batch_size, 16),
            "page_size": page_size,
            "max_workers": max_workers,
            "embedding_batch_size": embedding_batch_size,
            "search_seconds": search_seconds,
        }
        return targeted_broad_runtime_cache

    def targeted_broad_binding(
        beat: Mapping[str, Any],
        active_brief: Mapping[str, Any],
        *,
        frozen_universe: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        runtime = load_targeted_broad_runtime()
        bound_universe = (
            frozen_candidate_universe()
            if frozen_universe is None
            else [dict(row) for row in frozen_universe]
        )
        binding = build_semantic_broad_repair_binding(
            runtime["catalog"],
            beat,
            active_brief,
            render_frames=_beat_render_frames(beat, output_fps=output_fps),
            output_fps=output_fps,
            model_contract=runtime["model_contract"],
            callback_contract=str(runtime["callback_contract"]),
            detector_policy=runtime["detector_policy"],
            frozen_candidate_universe=bound_universe,
        )
        return runtime, binding

    def validate_blocked_targeted_broad_receipt(
        block: Mapping[str, Any], expected_binding: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            return validate_semantic_broad_repair_block(
                block, expected_binding=expected_binding
            )
        except SemanticBroadRepairError as exc:
            raise ValueError(
                f"cached targeted semantic broad block is stale: {exc}"
            ) from exc

    def register_targeted_broad_runtime(
        broad_receipt: Mapping[str, Any],
        runtime_artifacts: Mapping[str, Mapping[str, Any]],
        beat: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        candidates = broad_receipt.get("candidates")
        artifacts = broad_receipt.get("artifacts")
        if not isinstance(candidates, list) or not isinstance(artifacts, list):
            raise ValueError("targeted semantic broad receipt lost candidates/artifacts")
        artifact_by_id = {
            str(row.get("artifact_id") or ""): dict(row)
            for row in artifacts
            if isinstance(row, Mapping)
        }
        if len(artifact_by_id) != len(artifacts):
            raise ValueError("targeted semantic broad receipt repeats an artifact")
        output: list[dict[str, Any]] = []
        for raw_candidate in candidates:
            if not isinstance(raw_candidate, Mapping):
                raise ValueError("targeted semantic broad candidate is malformed")
            artifact_id = str(raw_candidate.get("contact_sheet_artifact_id") or "")
            portable_artifact = artifact_by_id.get(artifact_id)
            runtime_artifact = runtime_artifacts.get(artifact_id)
            if not isinstance(portable_artifact, Mapping) or not isinstance(
                runtime_artifact, Mapping
            ):
                raise ValueError("targeted semantic broad runtime artifact is missing")
            candidate = {
                **dict(raw_candidate),
                "repair_sheet_sha256": str(portable_artifact.get("sha256") or ""),
                "repair_sheet_bytes": int(portable_artifact.get("bytes") or 0),
            }
            candidate = _validate_exact_candidate_for_beat(
                candidate, beat, output_fps=output_fps
            )
            register_repair_sheet(
                candidate,
                {
                    "artifact_id": artifact_id,
                    "path": runtime_artifact.get("path"),
                    "candidate_range_hash": candidate.get("candidate_range_hash"),
                    "repair_sheet_sha256": candidate["repair_sheet_sha256"],
                    "repair_sheet_bytes": candidate["repair_sheet_bytes"],
                },
            )
            output.append(candidate)
        return output

    def run_targeted_broad_repair(
        beat: Mapping[str, Any],
        active_brief: Mapping[str, Any],
        honest_bundle: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if honest_bundle.get("active") is not True:
            return [], {
                "contract": TARGETED_SEMANTIC_BROAD_REPAIR_CONTRACT_VERSION,
                "status": "not_requested",
                "binding": None,
                "binding_hash": canonical_hash(None),
                "receipt": None,
                "receipt_hash": canonical_hash(None),
                "block": None,
                "block_hash": canonical_hash(None),
            }
        runtime, binding = targeted_broad_binding(beat, active_brief)
        slot_component = re.sub(
            r"[^A-Za-z0-9_.-]+", "_", str(beat.get("slot_id") or "slot")
        ).strip("._") or "slot"
        artifact_directory = (
            context.attempt_root
            / "semantic_broad_repair"
            / f"{slot_component}-{canonical_hash(active_brief)[:16]}"
        )
        try:
            broad_receipt, runtime_artifacts = materialize_semantic_broad_repair(
                runtime["catalog"],
                beat,
                active_brief,
                render_frames=_beat_render_frames(beat, output_fps=output_fps),
                asset_resolver=runtime["asset_resolver"],
                broad_cache_directory=runtime["broad_cache_directory"],
                repair_cache_directory=runtime["repair_cache_directory"],
                artifact_directory=artifact_directory,
                model_contract=runtime["model_contract"],
                strict_upgrade_fn=runtime["strict_upgrade_fn"],
                detector_policy=runtime["detector_policy"],
                output_fps=output_fps,
                callback_contract=str(runtime["callback_contract"]),
                overlay_profiles=runtime["overlay_profiles"],
                frozen_candidate_universe=frozen_candidate_universe(),
                active_binding=binding,
                survivor_target=int(runtime["survivor_target"]),
                page_size=int(runtime["page_size"]),
                max_workers=int(runtime["max_workers"]),
                embedding_batch_size=int(runtime["embedding_batch_size"]),
                search_seconds=float(runtime["search_seconds"]),
                ffmpeg=repair_ffmpeg,
            )
        except SemanticBroadRepairBlocked as exc:
            block = validate_blocked_targeted_broad_receipt(exc.artifact, binding)
            return [], {
                "contract": TARGETED_SEMANTIC_BROAD_REPAIR_CONTRACT_VERSION,
                "status": "blocked",
                "binding": binding,
                "binding_hash": canonical_hash(binding),
                "receipt": None,
                "receipt_hash": canonical_hash(None),
                "block": block,
                "block_hash": canonical_hash(block),
            }
        except SemanticBroadRepairError as exc:
            raise ValueError(
                f"targeted semantic broad repair failed integrity validation: {exc}"
            ) from exc
        validate_semantic_broad_repair_receipt(
            broad_receipt,
            expected_binding=binding,
            runtime_artifacts=runtime_artifacts,
            repair_cache_directory=repair_cache_root,
        )
        rows = register_targeted_broad_runtime(
            broad_receipt, runtime_artifacts, beat
        )
        return rows, {
            "contract": TARGETED_SEMANTIC_BROAD_REPAIR_CONTRACT_VERSION,
            "status": "ready",
            "binding": binding,
            "binding_hash": canonical_hash(binding),
            "receipt": broad_receipt,
            "receipt_hash": canonical_hash(broad_receipt),
            "block": None,
            "block_hash": canonical_hash(None),
        }

    def fallback_broad_receipt_fields(
        honest_bundle: Mapping[str, Any], broad_bundle: Mapping[str, Any]
    ) -> dict[str, Any]:
        honest = json.loads(json.dumps(dict(honest_bundle)))
        broad = json.loads(json.dumps(dict(broad_bundle)))
        return {
            "honest_visible_fallback": honest,
            "honest_visible_fallback_hash": canonical_hash(honest),
            "semantic_broad_repair": broad,
            "semantic_broad_repair_hash": canonical_hash(broad),
        }

    def validate_cached_ready_broad_receipt(
        broad_receipt: Mapping[str, Any],
        *,
        binding: Mapping[str, Any],
        runtime: Mapping[str, Any],
        beat: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        artifacts = broad_receipt.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("cached targeted semantic broad artifacts are missing")
        runtime_artifacts: dict[str, dict[str, Any]] = {}
        for raw in artifacts:
            if not isinstance(raw, Mapping):
                raise ValueError("cached targeted semantic broad artifact is malformed")
            artifact = dict(raw)
            artifact_id = str(artifact.get("artifact_id") or "")
            record = repair_sheet_records.get(artifact_id)
            if record is None:
                raise ValueError("cached targeted semantic broad sheet was not recovered")
            runtime_artifacts[artifact_id] = {
                **artifact,
                "path": str(record["path"]),
                "cache_hit": True,
            }
        try:
            validate_semantic_broad_repair_receipt(
                broad_receipt,
                expected_binding=binding,
                runtime_artifacts=runtime_artifacts,
                repair_cache_directory=runtime["repair_cache_directory"],
            )
        except SemanticBroadRepairError as exc:
            raise ValueError(
                f"cached targeted semantic broad receipt failed validation: {exc}"
            ) from exc
        return register_targeted_broad_runtime(
            broad_receipt, runtime_artifacts, beat
        )

    def run_collision_broad_repair(
        beat: Mapping[str, Any],
        brief: Mapping[str, Any],
        frozen_universe: Sequence[Mapping[str, Any]],
        *,
        attempt: int,
        diagnostic_before_hash: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        runtime, binding = targeted_broad_binding(
            beat, brief, frozen_universe=frozen_universe
        )
        slot_id = str(beat.get("slot_id") or "")
        slot_component = re.sub(
            r"[^A-Za-z0-9_.-]+", "_", slot_id or "slot"
        ).strip("._") or "slot"
        artifact_directory = (
            context.attempt_root
            / "semantic_collision_repair"
            / f"round-{attempt:02d}-{slot_component}-{diagnostic_before_hash[:16]}"
        )
        common = {
            "contract": GLOBAL_COLLISION_REPAIR_POLICY_VERSION,
            "policy": GLOBAL_COLLISION_REPAIR_POLICY_VERSION,
            "target_rule": GLOBAL_COLLISION_TARGET_RULE_VERSION,
            "attempt": int(attempt),
            "target_slot_id": slot_id,
            "target_beat_hash": canonical_hash(beat),
            "target_brief_hash": canonical_hash(brief),
            "diagnostic_before_hash": diagnostic_before_hash,
            "frozen_universe_count": len(frozen_universe),
            "frozen_universe_hash": canonical_hash(list(frozen_universe)),
            "binding": binding,
            "binding_hash": canonical_hash(binding),
        }
        try:
            broad_receipt, runtime_artifacts = materialize_semantic_broad_repair(
                runtime["catalog"],
                beat,
                brief,
                render_frames=_beat_render_frames(beat, output_fps=output_fps),
                asset_resolver=runtime["asset_resolver"],
                broad_cache_directory=runtime["broad_cache_directory"],
                repair_cache_directory=runtime["repair_cache_directory"],
                artifact_directory=artifact_directory,
                model_contract=runtime["model_contract"],
                strict_upgrade_fn=runtime["strict_upgrade_fn"],
                detector_policy=runtime["detector_policy"],
                output_fps=output_fps,
                callback_contract=str(runtime["callback_contract"]),
                overlay_profiles=runtime["overlay_profiles"],
                frozen_candidate_universe=list(frozen_universe),
                active_binding=binding,
                survivor_target=int(runtime["survivor_target"]),
                page_size=int(runtime["page_size"]),
                max_workers=int(runtime["max_workers"]),
                embedding_batch_size=int(runtime["embedding_batch_size"]),
                search_seconds=float(runtime["search_seconds"]),
                ffmpeg=repair_ffmpeg,
            )
        except SemanticBroadRepairBlocked as exc:
            block = validate_blocked_targeted_broad_receipt(exc.artifact, binding)
            return [], {
                **common,
                "status": "blocked",
                "receipt": None,
                "receipt_hash": canonical_hash(None),
                "block": block,
                "block_hash": canonical_hash(block),
            }
        except SemanticBroadRepairError as exc:
            raise ValueError(
                f"global collision broad repair failed integrity validation: {exc}"
            ) from exc
        try:
            validate_semantic_broad_repair_receipt(
                broad_receipt,
                expected_binding=binding,
                runtime_artifacts=runtime_artifacts,
                repair_cache_directory=runtime["repair_cache_directory"],
            )
        except SemanticBroadRepairError as exc:
            raise ValueError(
                f"global collision broad repair receipt is invalid: {exc}"
            ) from exc
        rows = register_targeted_broad_runtime(
            broad_receipt, runtime_artifacts, beat
        )
        return rows, {
            **common,
            "status": "ready",
            "receipt": broad_receipt,
            "receipt_hash": canonical_hash(broad_receipt),
            "block": None,
            "block_hash": canonical_hash(None),
        }

    def validate_cached_collision_broad_repair(
        bundle: Mapping[str, Any],
        beat: Mapping[str, Any],
        brief: Mapping[str, Any],
        frozen_universe: Sequence[Mapping[str, Any]],
        *,
        attempt: int,
        diagnostic_before_hash: str,
    ) -> list[dict[str, Any]]:
        runtime, binding = targeted_broad_binding(
            beat, brief, frozen_universe=frozen_universe
        )
        if (
            str(bundle.get("contract") or "")
            != GLOBAL_COLLISION_REPAIR_POLICY_VERSION
            or str(bundle.get("policy") or "")
            != GLOBAL_COLLISION_REPAIR_POLICY_VERSION
            or str(bundle.get("target_rule") or "")
            != GLOBAL_COLLISION_TARGET_RULE_VERSION
            or int(bundle.get("attempt") or 0) != attempt
            or str(bundle.get("target_slot_id") or "")
            != str(beat.get("slot_id") or "")
            or str(bundle.get("target_beat_hash") or "") != canonical_hash(beat)
            or str(bundle.get("target_brief_hash") or "")
            != canonical_hash(brief)
            or str(bundle.get("diagnostic_before_hash") or "")
            != diagnostic_before_hash
            or int(bundle.get("frozen_universe_count") or -1)
            != len(frozen_universe)
            or str(bundle.get("frozen_universe_hash") or "")
            != canonical_hash(list(frozen_universe))
            or bundle.get("binding") != binding
            or str(bundle.get("binding_hash") or "") != canonical_hash(binding)
        ):
            raise ValueError("cached global collision broad binding is stale")
        status = str(bundle.get("status") or "")
        if status == "blocked":
            block = bundle.get("block")
            if (
                not isinstance(block, Mapping)
                or bundle.get("receipt") is not None
                or str(bundle.get("receipt_hash") or "") != canonical_hash(None)
                or str(bundle.get("block_hash") or "")
                != canonical_hash(block)
            ):
                raise ValueError("cached global collision broad block is malformed")
            validate_blocked_targeted_broad_receipt(block, binding)
            return []
        broad_receipt = bundle.get("receipt")
        if (
            status != "ready"
            or not isinstance(broad_receipt, Mapping)
            or bundle.get("block") is not None
            or str(bundle.get("receipt_hash") or "")
            != canonical_hash(broad_receipt)
            or str(bundle.get("block_hash") or "") != canonical_hash(None)
        ):
            raise ValueError("cached global collision broad receipt is malformed")
        return validate_cached_ready_broad_receipt(
            broad_receipt,
            binding=binding,
            runtime=runtime,
            beat=beat,
        )

    def validate_cached_fallback_broad_evidence(
        receipt: Mapping[str, Any],
        active_brief: Mapping[str, Any],
        beat: Mapping[str, Any],
        *,
        expected_source_brief: Mapping[str, Any] | None = None,
        expected_source_brief_hash: str | None = None,
    ) -> None:
        honest_bundle = receipt.get("honest_visible_fallback")
        broad_bundle = receipt.get("semantic_broad_repair")
        if not isinstance(honest_bundle, Mapping) or not isinstance(
            broad_bundle, Mapping
        ):
            raise ValueError("cached fallback/broad repair evidence is missing")
        if (
            str(receipt.get("honest_visible_fallback_hash") or "")
            != canonical_hash(honest_bundle)
            or str(receipt.get("semantic_broad_repair_hash") or "")
            != canonical_hash(broad_bundle)
        ):
            raise ValueError("cached fallback/broad repair evidence hash is invalid")
        validate_honest_fallback_bundle(
            honest_bundle,
            active_brief,
            expected_source_brief=expected_source_brief,
            expected_source_brief_hash=expected_source_brief_hash,
        )
        status = str(broad_bundle.get("status") or "")
        if str(broad_bundle.get("contract") or "") != (
            TARGETED_SEMANTIC_BROAD_REPAIR_CONTRACT_VERSION
        ):
            raise ValueError("cached targeted semantic broad orchestration is stale")
        if honest_bundle.get("active") is not True:
            if (
                status != "not_requested"
                or broad_bundle.get("binding") is not None
                or broad_bundle.get("receipt") is not None
                or broad_bundle.get("block") is not None
                or str(broad_bundle.get("binding_hash") or "")
                != canonical_hash(None)
                or str(broad_bundle.get("receipt_hash") or "")
                != canonical_hash(None)
                or str(broad_bundle.get("block_hash") or "")
                != canonical_hash(None)
            ):
                raise ValueError("inactive honest fallback invoked targeted broad repair")
            return
        runtime, binding = targeted_broad_binding(beat, active_brief)
        if (
            broad_bundle.get("binding") != binding
            or str(broad_bundle.get("binding_hash") or "")
            != canonical_hash(binding)
        ):
            raise ValueError("cached targeted semantic broad binding is stale")
        if status == "blocked":
            block = broad_bundle.get("block")
            if (
                not isinstance(block, Mapping)
                or broad_bundle.get("receipt") is not None
                or str(broad_bundle.get("receipt_hash") or "")
                != canonical_hash(None)
                or str(broad_bundle.get("block_hash") or "")
                != canonical_hash(block)
            ):
                raise ValueError("cached targeted semantic broad block is malformed")
            validate_blocked_targeted_broad_receipt(block, binding)
            return
        broad_receipt = broad_bundle.get("receipt")
        if (
            status != "ready"
            or not isinstance(broad_receipt, Mapping)
            or broad_bundle.get("block") is not None
            or str(broad_bundle.get("receipt_hash") or "")
            != canonical_hash(broad_receipt)
            or str(broad_bundle.get("block_hash") or "")
            != canonical_hash(None)
        ):
            raise ValueError("cached targeted semantic broad receipt is malformed")
        validate_cached_ready_broad_receipt(
            broad_receipt,
            binding=binding,
            runtime=runtime,
            beat=beat,
        )

    def semantic_stage_artifacts(judge_path: Path) -> list[dict[str, Any]]:
        artifacts = [_artifact("judge", judge_path)]
        for artifact_id in sorted(repair_sheet_records):
            record = repair_sheet_records[artifact_id]
            artifacts.append(
                _artifact(
                    artifact_id,
                    Path(record["path"]),
                    kind="semantic_exact_repair_sheet",
                    exact_repair_contract=EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION,
                    clip_id=record["clip_id"],
                    candidate_range_hash=record["candidate_range_hash"],
                    repair_sheet_sha256=record["repair_sheet_sha256"],
                    repair_sheet_bytes=record["repair_sheet_bytes"],
                )
            )
        return artifacts

    def recover_result_repair_sheets(result: Mapping[str, Any]) -> None:
        durable_candidates = result.get("repair_sheet_candidates")
        durable_candidates_hash = str(
            result.get("repair_sheet_candidates_hash") or ""
        )
        durable_receipt = result.get("repair_receipt")
        if (
            not isinstance(durable_candidates, list)
            or durable_candidates_hash != canonical_hash(durable_candidates)
            or not isinstance(durable_receipt, Mapping)
            or str(durable_receipt.get("repair_sheet_candidates_hash") or "")
            != durable_candidates_hash
        ):
            raise ValueError("cached repair-sheet candidate registry is invalid")
        batches: list[Sequence[Mapping[str, Any]]] = []
        batches.append(
            [row for row in durable_candidates if isinstance(row, Mapping)]
        )
        if len(batches[0]) != len(durable_candidates):
            raise ValueError("cached repair-sheet candidate registry has invalid rows")
        direct = result.get("candidate_rows")
        if isinstance(direct, list):
            batches.append([row for row in direct if isinstance(row, Mapping)])
        updates = result.get("updates")
        if isinstance(updates, list):
            for update in updates:
                if not isinstance(update, Mapping):
                    continue
                rows = update.get("candidate_rows")
                if isinstance(rows, list):
                    batches.append(
                        [row for row in rows if isinstance(row, Mapping)]
                    )
        for rows in batches:
            for row in rows:
                if str(row.get("contact_sheet_artifact_id") or "").startswith(
                    "repair-sheet-"
                ):
                    recovered = materialize_repair_sheet(row, cache_only=True)
                    if _exact_repair_candidate_binding(recovered) != (
                        _exact_repair_candidate_binding(row)
                    ):
                        raise ValueError(
                            "cached repair-sheet registry changed artifact content"
                        )

    def semantic_judge_paid_request(
        beats: Sequence[Mapping[str, Any]],
        briefs: Sequence[Mapping[str, Any]],
        exact_batches: Sequence[Sequence[Mapping[str, Any]]],
    ) -> dict[str, Any]:
        if not (len(beats) == len(briefs) == len(exact_batches)):
            raise ValueError("semantic judge paid request arrays differ in length")
        return {
            "model": judge_model,
            "beats_hash": canonical_hash(list(beats)),
            "briefs": list(briefs),
            "candidate_contracts": [
                [
                    {
                        "range": canonical_candidate_range(row),
                        "contact_sheet_artifact_id": str(
                            row.get("contact_sheet_artifact_id") or ""
                        ),
                    }
                    for row in rows
                ]
                for rows in exact_batches
            ],
            "exact_candidate_bindings": [
                _exact_repair_candidate_bindings(rows) for rows in exact_batches
            ],
        }

    def judge_batches(
        operation_kind: str,
        beats: Sequence[Mapping[str, Any]],
        briefs: Sequence[Mapping[str, Any]],
        portable_batches: Sequence[Sequence[Mapping[str, Any]]],
    ) -> list[dict[str, Any]]:
        if not (len(beats) == len(briefs) == len(portable_batches)):
            raise ValueError("semantic judge exact batch arrays differ in length")
        exact_batches: list[list[dict[str, Any]]] = []
        runtime_batches: list[list[dict[str, Any]]] = []
        for beat, rows in zip(beats, portable_batches):
            exact_rows: list[dict[str, Any]] = []
            runtime_rows: list[dict[str, Any]] = []
            for raw in rows:
                exact = _validate_exact_candidate_for_beat(
                    raw, beat, output_fps=output_fps
                )
                artifact_id = str(exact.get("contact_sheet_artifact_id") or "")
                if artifact_id.startswith("repair-sheet-"):
                    exact = materialize_repair_sheet(exact, cache_only=True)
                    record = repair_sheet_records.get(artifact_id)
                    if record is None:
                        raise ValueError("semantic repair sheet was not registered")
                    runtime = _hydrate_candidate(context, exact)
                    runtime["contact_sheet"] = str(record["path"])
                else:
                    runtime = _hydrate_candidate(
                        context, exact, sheet_stage="temporal_retrieval"
                    )
                exact_rows.append(exact)
                runtime_rows.append(runtime)
            if not exact_rows:
                raise ValueError(
                    f"semantic judge exact batch is empty for {beat.get('slot_id')}"
                )
            exact_batches.append(exact_rows)
            runtime_batches.append(runtime_rows)
        jobs = build_judge_jobs(beats, briefs, runtime_batches)
        request = semantic_judge_paid_request(beats, briefs, exact_batches)
        try:
            raw_results = _paid_call(
                context,
                provider="semantic_llm",
                operation_kind=operation_kind,
                request=request,
                call=lambda: judge_many_strict(
                    jobs,
                    model=judge_model,
                    workers=judge_workers,
                ),
            )
        except StageBlocked:
            raise
        except Exception as exc:
            raise StageBlocked(
                "needs_visual_judge_provider",
                f"strict temporal judge provider failed: {exc}",
            ) from exc
        if not isinstance(raw_results, list) or len(raw_results) != len(beats):
            raise StageBlocked(
                "needs_visual_judge_provider",
                "strict temporal judge returned an invalid result count",
            )
        results = [dict(row) for row in raw_results if isinstance(row, Mapping)]
        if len(results) != len(beats):
            raise StageBlocked(
                "needs_visual_judge_provider",
                "strict temporal judge returned a non-object result",
            )
        # A multimodal provider occasionally omits one item from an otherwise
        # valid per-candidate array.  Retry only those malformed slots as a
        # separate, durably fenced paid operation; never discard the 70+ valid
        # judgements or silently select candidate zero.
        for schema_attempt in range(1, 3):
            failed_indices = [
                index for index, row in enumerate(results) if row.get("judge_error")
            ]
            if not failed_indices:
                break
            failed_jobs = [jobs[index] for index in failed_indices]
            repair_request = {
                "model": judge_model,
                "parent_operation_kind": operation_kind,
                "schema_attempt": schema_attempt,
                "failed_indices": failed_indices,
                "failure_codes": [
                    str(results[index].get("judge_error") or "")
                    for index in failed_indices
                ],
                "beats_hash": canonical_hash([beats[index] for index in failed_indices]),
                "candidate_contracts": [
                    request["candidate_contracts"][index] for index in failed_indices
                ],
            }
            try:
                repaired_raw = _paid_call(
                    context,
                    provider="semantic_llm",
                    operation_kind=f"{operation_kind}_schema_repair_{schema_attempt}",
                    request=repair_request,
                    call=lambda failed_jobs=failed_jobs: judge_many_strict(
                        failed_jobs,
                        model=judge_model,
                        workers=min(judge_workers, len(failed_jobs)),
                    ),
                )
            except StageBlocked:
                raise
            except Exception as exc:
                raise StageBlocked(
                    "needs_visual_judge_provider",
                    f"strict temporal judge schema repair failed: {exc}",
                ) from exc
            if not isinstance(repaired_raw, list) or len(repaired_raw) != len(
                failed_indices
            ):
                raise StageBlocked(
                    "needs_visual_judge_provider",
                    "strict temporal judge schema repair returned an invalid result count",
                )
            for result_index, repaired in zip(failed_indices, repaired_raw):
                if not isinstance(repaired, Mapping):
                    raise StageBlocked(
                        "needs_visual_judge_provider",
                        "strict temporal judge schema repair returned a non-object result",
                    )
                results[result_index] = dict(repaired)
        errors = [str(row.get("judge_error")) for row in results if row.get("judge_error")]
        if errors:
            raise StageBlocked(
                "needs_visual_judge_provider",
                f"strict temporal judge did not produce auditable evidence: {errors[0]}",
            )
        return results

    def completed_paid_value(
        operation_kind: str, request: Mapping[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        request_hash = canonical_hash(request)
        replay = _resolve_exact_paid_replay(
            context,
            provider="semantic_llm",
            operation_kind=operation_kind,
            request_hash=request_hash,
        )
        if replay["action"] == "uncertain":
            _raise_paid_reconciliation(
                provider="semantic_llm",
                operation_kind=operation_kind,
                request_hash=request_hash,
                replay=replay,
            )
        if replay["action"] != "completed":
            raise ValueError("cached global collision paid operation is stale")
        envelope = replay["envelope"]
        value = replay["value"]
        replay_audit = replay["audit"]
        return value, {
            "operation_kind": operation_kind,
            "request_hash": request_hash,
            "idempotency_key": str(replay_audit["idempotency_key"]),
            "result_envelope_hash": canonical_hash(envelope),
            "value_hash": canonical_hash(value),
        }

    def reconstruct_collision_paid_chain(
        operation_kind: str,
        beats: Sequence[Mapping[str, Any]],
        briefs: Sequence[Mapping[str, Any]],
        exact_batches: Sequence[Sequence[Mapping[str, Any]]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        request = semantic_judge_paid_request(beats, briefs, exact_batches)
        raw_results, base_audit = completed_paid_value(operation_kind, request)
        if not isinstance(raw_results, list) or len(raw_results) != len(beats):
            raise ValueError("cached global collision base paid result is stale")
        results = [dict(row) for row in raw_results if isinstance(row, Mapping)]
        if len(results) != len(beats):
            raise ValueError("cached global collision base paid result is stale")
        schema_repairs: list[dict[str, Any]] = []
        for schema_attempt in range(1, 3):
            failed_indices = [
                index for index, row in enumerate(results) if row.get("judge_error")
            ]
            if not failed_indices:
                break
            failure_codes = [
                str(results[index].get("judge_error") or "")
                for index in failed_indices
            ]
            repair_request = {
                "model": judge_model,
                "parent_operation_kind": operation_kind,
                "schema_attempt": schema_attempt,
                "failed_indices": failed_indices,
                "failure_codes": failure_codes,
                "beats_hash": canonical_hash(
                    [beats[index] for index in failed_indices]
                ),
                "candidate_contracts": [
                    request["candidate_contracts"][index]
                    for index in failed_indices
                ],
            }
            repair_kind = f"{operation_kind}_schema_repair_{schema_attempt}"
            repaired_raw, repair_audit = completed_paid_value(
                repair_kind, repair_request
            )
            if not isinstance(repaired_raw, list) or len(repaired_raw) != len(
                failed_indices
            ):
                raise ValueError(
                    "cached global collision schema paid result is stale"
                )
            repaired_rows = [
                dict(row) for row in repaired_raw if isinstance(row, Mapping)
            ]
            if len(repaired_rows) != len(failed_indices):
                raise ValueError(
                    "cached global collision schema paid result is stale"
                )
            schema_repairs.append(
                {
                    **repair_audit,
                    "schema_attempt": schema_attempt,
                    "failed_indices": failed_indices,
                    "failed_indices_hash": canonical_hash(failed_indices),
                    "failure_codes": failure_codes,
                    "failure_codes_hash": canonical_hash(failure_codes),
                }
            )
            for result_index, repaired in zip(failed_indices, repaired_rows):
                results[result_index] = repaired
        if any(row.get("judge_error") for row in results):
            raise ValueError(
                "cached global collision paid schema chain is unresolved"
            )
        chain = {
            "contract": GLOBAL_COLLISION_PAID_CHAIN_CONTRACT,
            "base": base_audit,
            "schema_repairs": schema_repairs,
            "schema_repairs_hash": canonical_hash(schema_repairs),
            "final_results_hash": canonical_hash(results),
        }
        chain["chain_hash"] = canonical_hash(chain)
        return results, chain

    def judge_selected_visual_repair_raw(
        operation_kind: str,
        beat: Mapping[str, Any],
        brief: Mapping[str, Any],
        portable_rows: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Judge only a newly discovered gate-clean repair batch via raw v2.

        Legacy semantic operations intentionally stay on their original
        request/result contract so a semantic stage-version bump cannot repay
        an already completed film.  This helper is reachable only from the new
        selected-visual repair namespace and uses its own operation prefix.
        """

        from .semantic import (
            STRICT_JUDGE_SCHEMA_REPAIR_MAX_ATTEMPTS,
            build_strict_judge_request,
            build_strict_judge_schema_repair_request,
            normalize_strict_judge_response,
            normalize_strict_judge_schema_repair_response,
            request_strict_judge_raw,
        )

        exact_rows: list[dict[str, Any]] = []
        runtime_rows: list[dict[str, Any]] = []
        for raw in portable_rows:
            exact = _validate_exact_candidate_for_beat(
                raw, beat, output_fps=output_fps
            )
            artifact_id = str(exact.get("contact_sheet_artifact_id") or "")
            if artifact_id.startswith("repair-sheet-"):
                exact = materialize_repair_sheet(exact, cache_only=True)
                record = repair_sheet_records.get(artifact_id)
                if record is None:
                    raise ValueError(
                        "selected visual repair sheet was not registered"
                    )
                runtime = _hydrate_candidate(context, exact)
                runtime["contact_sheet"] = str(record["path"])
            else:
                runtime = _hydrate_candidate(
                    context, exact, sheet_stage="temporal_retrieval"
                )
            exact_rows.append(exact)
            runtime_rows.append(runtime)
        if not exact_rows:
            raise ValueError("selected visual repair judge batch is empty")

        request = build_strict_judge_request(
            beat, brief, runtime_rows, model=judge_model
        )
        try:
            raw_response = _paid_call(
                context,
                provider="semantic_llm",
                operation_kind=operation_kind,
                request=request,
                call=lambda: request_strict_judge_raw(request, runtime_rows),
            )
        except StageBlocked:
            raise
        except Exception as exc:
            raise StageBlocked(
                "needs_visual_judge_provider",
                f"selected visual repair judge provider failed: {exc}",
            ) from exc
        completed_raw, base_audit = completed_paid_value(
            operation_kind, request
        )
        if completed_raw != raw_response:
            raise ValueError("selected visual repair raw paid result changed")

        repairs: list[dict[str, Any]] = []
        current_request = request
        current_raw = raw_response
        normalized: dict[str, Any] | None = None
        normalize_error: Exception | None = None
        for schema_attempt in range(
            0, STRICT_JUDGE_SCHEMA_REPAIR_MAX_ATTEMPTS + 1
        ):
            try:
                normalized = dict(
                    normalize_strict_judge_response(
                        current_request, current_raw
                    )
                    if schema_attempt == 0
                    else normalize_strict_judge_schema_repair_response(
                        current_request, current_raw
                    )
                )
                break
            except (TypeError, ValueError) as exc:
                normalize_error = exc
            if schema_attempt >= STRICT_JUDGE_SCHEMA_REPAIR_MAX_ATTEMPTS:
                break
            repair_attempt = schema_attempt + 1
            repair_request = build_strict_judge_schema_repair_request(
                current_request,
                current_raw,
                attempt=repair_attempt,
                max_attempts=STRICT_JUDGE_SCHEMA_REPAIR_MAX_ATTEMPTS,
            )
            repair_kind = (
                f"{operation_kind}_schema_repair_{repair_attempt}"
            )
            try:
                repaired_raw = _paid_call(
                    context,
                    provider="semantic_llm",
                    operation_kind=repair_kind,
                    request=repair_request,
                    call=lambda repair_request=repair_request: (
                        request_strict_judge_raw(
                            repair_request, runtime_rows
                        )
                    ),
                )
            except StageBlocked:
                raise
            except Exception as exc:
                raise StageBlocked(
                    "needs_visual_judge_provider",
                    "selected visual repair schema provider failed: "
                    f"{exc}",
                ) from exc
            completed_repair, repair_audit = completed_paid_value(
                repair_kind, repair_request
            )
            if completed_repair != repaired_raw:
                raise ValueError(
                    "selected visual repair schema paid result changed"
                )
            repairs.append(
                {
                    **repair_audit,
                    "schema_attempt": repair_attempt,
                    "repair_request_hash": canonical_hash(repair_request),
                    "raw_envelope_hash": canonical_hash(repaired_raw),
                }
            )
            current_request = repair_request
            current_raw = repaired_raw
        if normalized is None:
            raise StageBlocked(
                "needs_semantic_judge_schema_repair",
                "selected visual repair exhausted its bounded raw-schema "
                f"repair: {type(normalize_error).__name__}: {normalize_error}",
                result={
                    "status": "unresolved",
                    "slot_id": str(beat.get("slot_id") or ""),
                    "operation_kind": operation_kind,
                    "schema_repair_attempts": (
                        STRICT_JUDGE_SCHEMA_REPAIR_MAX_ATTEMPTS
                    ),
                },
            )
        normalized["judge_error"] = None
        base_chain = {
            "contract": SELECTED_VISUAL_PAID_CHAIN_CONTRACT,
            "operation_kind": operation_kind,
            "base": {
                **base_audit,
                "strict_request_hash": canonical_hash(request),
                "raw_envelope_hash": canonical_hash(raw_response),
            },
            "schema_repairs": repairs,
            "schema_repairs_hash": canonical_hash(repairs),
            "exact_candidate_bindings": _exact_repair_candidate_bindings(
                exact_rows
            ),
            "final_result_hash": canonical_hash(normalized),
        }
        chain = {
            **base_chain,
            "chain_hash": canonical_hash(base_chain),
        }
        return normalized, chain

    def reconstruct_selected_visual_paid_chain(
        operation_kind: str,
        beat: Mapping[str, Any],
        brief: Mapping[str, Any],
        portable_rows: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Rebuild a selected-repair judge result without making a provider call."""

        from .semantic import (
            STRICT_JUDGE_SCHEMA_REPAIR_MAX_ATTEMPTS,
            build_strict_judge_request,
            build_strict_judge_schema_repair_request,
            normalize_strict_judge_response,
            normalize_strict_judge_schema_repair_response,
        )

        exact_rows: list[dict[str, Any]] = []
        runtime_rows: list[dict[str, Any]] = []
        for raw in portable_rows:
            exact = _validate_exact_candidate_for_beat(
                raw, beat, output_fps=output_fps
            )
            artifact_id = str(exact.get("contact_sheet_artifact_id") or "")
            if artifact_id.startswith("repair-sheet-"):
                exact = materialize_repair_sheet(exact, cache_only=True)
                record = repair_sheet_records.get(artifact_id)
                if record is None:
                    raise ValueError(
                        "selected visual repair sheet was not registered"
                    )
                runtime = _hydrate_candidate(context, exact)
                runtime["contact_sheet"] = str(record["path"])
            else:
                runtime = _hydrate_candidate(
                    context, exact, sheet_stage="temporal_retrieval"
                )
            exact_rows.append(exact)
            runtime_rows.append(runtime)
        if not exact_rows:
            raise ValueError("selected visual repair judge batch is empty")
        request = build_strict_judge_request(
            beat, brief, runtime_rows, model=judge_model
        )
        current_raw, base_audit = completed_paid_value(operation_kind, request)
        repairs: list[dict[str, Any]] = []
        current_request = request
        normalized: dict[str, Any] | None = None
        last_error: Exception | None = None
        for schema_attempt in range(
            0, STRICT_JUDGE_SCHEMA_REPAIR_MAX_ATTEMPTS + 1
        ):
            try:
                normalized = dict(
                    normalize_strict_judge_response(
                        current_request, current_raw
                    )
                    if schema_attempt == 0
                    else normalize_strict_judge_schema_repair_response(
                        current_request, current_raw
                    )
                )
                break
            except (TypeError, ValueError) as exc:
                last_error = exc
            if schema_attempt >= STRICT_JUDGE_SCHEMA_REPAIR_MAX_ATTEMPTS:
                break
            repair_attempt = schema_attempt + 1
            repair_request = build_strict_judge_schema_repair_request(
                current_request,
                current_raw,
                attempt=repair_attempt,
                max_attempts=STRICT_JUDGE_SCHEMA_REPAIR_MAX_ATTEMPTS,
            )
            repair_kind = f"{operation_kind}_schema_repair_{repair_attempt}"
            current_raw, repair_audit = completed_paid_value(
                repair_kind, repair_request
            )
            repairs.append(
                {
                    **repair_audit,
                    "schema_attempt": repair_attempt,
                    "repair_request_hash": canonical_hash(repair_request),
                    "raw_envelope_hash": canonical_hash(current_raw),
                }
            )
            current_request = repair_request
        if normalized is None:
            raise ValueError(
                "cached selected visual paid schema chain is unresolved: "
                f"{type(last_error).__name__}: {last_error}"
            )
        normalized["judge_error"] = None
        base_chain = {
            "contract": SELECTED_VISUAL_PAID_CHAIN_CONTRACT,
            "operation_kind": operation_kind,
            "base": {
                **base_audit,
                "strict_request_hash": canonical_hash(request),
                "raw_envelope_hash": canonical_hash(
                    completed_paid_value(operation_kind, request)[0]
                ),
            },
            "schema_repairs": repairs,
            "schema_repairs_hash": canonical_hash(repairs),
            "exact_candidate_bindings": _exact_repair_candidate_bindings(
                exact_rows
            ),
            "final_result_hash": canonical_hash(normalized),
        }
        return normalized, {
            **base_chain,
            "chain_hash": canonical_hash(base_chain),
        }

    def collision_eligible_matrix(
        candidate_matrix: Sequence[Sequence[Mapping[str, Any]]],
        result_matrix: Sequence[Mapping[str, Any]],
        *,
        allow_empty: bool = False,
    ) -> list[list[dict[str, Any]]]:
        return _global_collision_eligible_matrix(
            effective_beats,
            candidate_matrix,
            result_matrix,
            output_fps=output_fps,
            allow_empty=allow_empty,
        )

    def validate_collision_diagnostic(
        diagnostic: Mapping[str, Any],
        eligible: Sequence[Sequence[Mapping[str, Any]]],
    ) -> dict[str, Any]:
        row = json.loads(json.dumps(dict(diagnostic)))
        receipt_sha256 = str(row.pop("receipt_sha256", "") or "")
        slot_ids = [str(beat.get("slot_id") or "") for beat in effective_beats]
        status = str(row.get("status") or "")
        if (
            row.get("schema_version") != 1
            or row.get("diagnostic_version")
            != "global-collision-feasibility-v1"
            or receipt_sha256 != canonical_hash(row)
            or row.get("slot_ids") != slot_ids
            or row.get("candidate_counts")
            != [len(rows) for rows in eligible]
            or row.get("limits")
            != {
                "max_slots": 128,
                "max_candidates_per_slot": (
                    GLOBAL_COLLISION_SELECTABLE_CANDIDATES_PER_SLOT
                ),
                "max_nodes": 1_000_000,
            }
            or status not in {"feasible", "true_infeasible", "limit_exceeded"}
        ):
            raise ValueError("global collision diagnostic receipt is stale")
        core = row.get("irreducible_core_indices")
        core_slots = row.get("irreducible_core_slot_ids")
        augmentation = row.get("single_slot_augmentation_indices")
        augmentation_slots = row.get("single_slot_augmentation_slot_ids")
        if not all(
            isinstance(value, list)
            for value in (core, core_slots, augmentation, augmentation_slots)
        ):
            raise ValueError("global collision diagnostic sets are malformed")
        if any(type(index) is not int or not 0 <= index < len(slot_ids) for index in core):
            raise ValueError("global collision diagnostic core is malformed")
        if any(
            type(index) is not int or index not in core for index in augmentation
        ):
            raise ValueError("global collision diagnostic augmentation is malformed")
        if core_slots != [slot_ids[index] for index in core] or augmentation_slots != [
            slot_ids[index] for index in augmentation
        ]:
            raise ValueError("global collision diagnostic slot identities are stale")
        if status == "true_infeasible" and not core:
            raise ValueError("global collision diagnostic lost its conflict core")
        return {**row, "receipt_sha256": receipt_sha256}

    def diagnose_collision_assignment(
        candidate_matrix: Sequence[Sequence[Mapping[str, Any]]],
        result_matrix: Sequence[Mapping[str, Any]],
        *,
        allow_empty: bool = False,
    ) -> tuple[list[list[dict[str, Any]]], dict[str, Any]]:
        eligible = collision_eligible_matrix(
            candidate_matrix, result_matrix, allow_empty=allow_empty
        )
        diagnostic = diagnose_global_collision_feasibility(
            eligible,
            slot_ids=[str(beat.get("slot_id") or "") for beat in effective_beats],
            max_candidates_per_slot=(
                GLOBAL_COLLISION_SELECTABLE_CANDIDATES_PER_SLOT
            ),
        )
        return eligible, validate_collision_diagnostic(diagnostic, eligible)

    def authenticated_cached_collision_diagnostic(
        stored: Mapping[str, Any],
        current: Mapping[str, Any],
        eligible: Sequence[Sequence[Mapping[str, Any]]],
    ) -> dict[str, Any]:
        """Accept only the two authenticated pre/post-witness non-feasible views."""

        validated = validate_collision_diagnostic(stored, eligible)
        current_unsigned = json.loads(json.dumps(dict(current)))
        current_unsigned.pop("receipt_sha256", None)
        legacy_unsigned = dict(current_unsigned)
        legacy_unsigned.pop("feasible_witness_candidate_indices", None)
        legacy_unsigned.pop("feasible_witness_input_sha256", None)
        accepted = [
            dict(current),
            {
                **legacy_unsigned,
                "receipt_sha256": canonical_hash(legacy_unsigned),
            },
        ]
        if str(current.get("status") or "") != "feasible":
            transient_unsigned = {
                **legacy_unsigned,
                "feasible_witness_candidate_indices": [],
                "feasible_witness_input_sha256": None,
            }
            accepted.append(
                {
                    **transient_unsigned,
                    "receipt_sha256": canonical_hash(transient_unsigned),
                }
            )
        if validated not in accepted:
            raise ValueError(
                "cached global collision diagnostic is not an authenticated "
                "witness-transition view"
            )
        return validated

    def collision_target_selection(
        diagnostic: Mapping[str, Any],
        eligible: Sequence[Sequence[Mapping[str, Any]]],
        attempted_target_history: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[int, dict[str, Any]]:
        if str(diagnostic.get("status") or "") != "true_infeasible":
            raise ValueError("global collision target requires true infeasibility")
        augmentation = [
            int(index)
            for index in diagnostic.get("single_slot_augmentation_indices") or ()
        ]
        core = [
            int(index) for index in diagnostic.get("irreducible_core_indices") or ()
        ]
        pool = augmentation or core
        if not pool:
            raise ValueError("global collision target pool is empty")
        history = [dict(row) for row in attempted_target_history]
        terminal_no_accept_slots: set[str] = set()
        for expected_attempt, row in enumerate(history, start=1):
            if (
                row.get("contract") != GLOBAL_COLLISION_TARGET_OUTCOME_CONTRACT
                or int(row.get("attempt") or 0) != expected_attempt
                or not str(row.get("target_slot_id") or "")
                or type(row.get("target_index")) is not int
                or not 0 <= int(row["target_index"]) < len(effective_beats)
                or str(row.get("target_slot_id") or "")
                != str(
                    effective_beats[int(row["target_index"])].get("slot_id")
                    or ""
                )
                or type(row.get("accepted_candidate_count")) is not int
                or int(row["accepted_candidate_count"]) < 0
                or bool(row.get("terminal_no_accept"))
                != (int(row["accepted_candidate_count"]) == 0)
            ):
                raise ValueError("global collision attempted-target history is stale")
            if row["terminal_no_accept"] is True:
                terminal_no_accept_slots.add(str(row["target_slot_id"]))
        available = [
            index
            for index in pool
            if str(effective_beats[index].get("slot_id") or "")
            not in terminal_no_accept_slots
        ]
        if not available:
            raise ValueError(
                "global collision core has no untried target after bounded "
                "zero-accept repairs"
            )
        selected = min(
            available, key=lambda index: (-len(eligible[index]), index)
        )
        evidence = {
            "contract": GLOBAL_COLLISION_TARGET_RULE_VERSION,
            "pool_kind": (
                "single_slot_augmentation" if augmentation else "irreducible_core"
            ),
            "pool_indices": pool,
            "pool_slot_ids": [
                str(effective_beats[index].get("slot_id") or "") for index in pool
            ],
            "selectable_candidates_per_slot": (
                GLOBAL_COLLISION_SELECTABLE_CANDIDATES_PER_SLOT
            ),
            "attempted_target_history": history,
            "attempted_target_history_hash": canonical_hash(history),
            "terminal_no_accept_slot_ids": sorted(terminal_no_accept_slots),
            "available_pool_indices": available,
            "available_pool_slot_ids": [
                str(effective_beats[index].get("slot_id") or "")
                for index in available
            ],
            "accepted_candidate_counts": {
                str(effective_beats[index].get("slot_id") or ""): len(
                    eligible[index]
                )
                for index in pool
            },
            "selected_index": selected,
            "selected_slot_id": str(
                effective_beats[selected].get("slot_id") or ""
            ),
            "tie_break": (
                "exclude_zero_accept_targets_then_highest_accepted_candidate_"
                "count_then_earliest_slot"
            ),
        }
        return selected, evidence

    def collision_frozen_universe(
        candidate_matrix: Sequence[Sequence[Mapping[str, Any]]],
        attempted_rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        raw_universe: list[Mapping[str, Any]] = [
            *frozen_candidate_universe(),
            *[
                row
                for rows in candidate_matrix
                for row in rows
                if isinstance(row, Mapping)
            ],
            *[row for row in attempted_rows if isinstance(row, Mapping)],
        ]
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_universe:
            row = dict(raw)
            row.pop("source_path", None)
            row.pop("contact_sheet", None)
            range_hash = str(row.get("candidate_range_hash") or "")
            if re.fullmatch(r"[0-9a-f]{64}", range_hash) is None:
                raise ValueError(
                    "global collision frozen universe lost exact range identity"
                )
            if range_hash in seen:
                continue
            seen.add(range_hash)
            output.append(row)
        return output

    def collision_target_outcome(
        *,
        attempt: int,
        target_index: int,
        broad_status: str,
        broad_candidate_count: int,
        accepted_candidate_count: int,
        diagnostic_before: Mapping[str, Any],
        diagnostic_after: Mapping[str, Any],
    ) -> dict[str, Any]:
        if broad_status not in {"ready", "blocked"}:
            raise ValueError("global collision broad outcome status is invalid")
        if (
            type(target_index) is not int
            or not 0 <= target_index < len(effective_beats)
            or type(broad_candidate_count) is not int
            or broad_candidate_count < 0
            or type(accepted_candidate_count) is not int
            or not 0 <= accepted_candidate_count <= broad_candidate_count
        ):
            raise ValueError("global collision target outcome counters are invalid")
        return {
            "contract": GLOBAL_COLLISION_TARGET_OUTCOME_CONTRACT,
            "attempt": int(attempt),
            "target_index": target_index,
            "target_slot_id": str(
                effective_beats[target_index].get("slot_id") or ""
            ),
            "diagnostic_before_hash": canonical_hash(diagnostic_before),
            "broad_status": broad_status,
            "broad_candidate_count": broad_candidate_count,
            "accepted_candidate_count": accepted_candidate_count,
            "terminal_no_accept": accepted_candidate_count == 0,
            "diagnostic_after_hash": canonical_hash(diagnostic_after),
        }

    def collision_accepted_rows(
        rows: Sequence[Mapping[str, Any]],
        judge_result: Mapping[str, Any],
        beat: Mapping[str, Any],
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        evaluations = judge_result.get("evaluations")
        if not isinstance(evaluations, list) or len(evaluations) != len(rows):
            raise ValueError("global collision judge result lost candidate audit")
        accepted: list[tuple[dict[str, Any], dict[str, Any]]] = []
        resolved = _strict_judge_resolved(judge_result)
        for index, (raw, evaluation) in enumerate(zip(rows, evaluations)):
            if not isinstance(evaluation, Mapping) or evaluation.get("index") != index:
                raise ValueError("global collision judge candidate order is stale")
            try:
                confidence = float(evaluation.get("confidence") or 0.0)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "global collision judge confidence is malformed"
                ) from exc
            if (
                not resolved
                or str(evaluation.get("verdict") or "").casefold()
                not in {"exact", "relevant"}
                or not math.isfinite(confidence)
                or confidence <= 0.0
            ):
                continue
            accepted.append(
                (
                    _validate_exact_candidate_for_beat(
                        raw, beat, output_fps=output_fps
                    ),
                    dict(evaluation),
                )
            )
        return accepted

    def merge_collision_acceptance(
        prior_rows: Sequence[Mapping[str, Any]],
        prior_result: Mapping[str, Any],
        accepted: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        prior_evaluations = prior_result.get("evaluations")
        if not isinstance(prior_evaluations, list) or len(prior_evaluations) != len(
            prior_rows
        ):
            raise ValueError("global collision prior judge audit is stale")
        seen_ranges = {
            str(row.get("candidate_range_hash") or "") for row in prior_rows
        }
        seen_parents = {
            str(row.get("parent_clip_id") or "")
            for row in prior_rows
            if str(row.get("parent_clip_id") or "")
        }
        accepted_rows: list[dict[str, Any]] = []
        accepted_evaluations: list[dict[str, Any]] = []
        for raw, raw_evaluation in accepted:
            row = dict(raw)
            range_hash = str(row.get("candidate_range_hash") or "")
            parent = str(row.get("parent_clip_id") or "")
            if range_hash in seen_ranges or (parent and parent in seen_parents):
                continue
            seen_ranges.add(range_hash)
            if parent:
                seen_parents.add(parent)
            accepted_rows.append(row)
            accepted_evaluations.append(dict(raw_evaluation))
        merged_rows = [*accepted_rows, *[dict(row) for row in prior_rows]]
        merged_evaluations: list[dict[str, Any]] = []
        for index, evaluation in enumerate(
            [*accepted_evaluations, *[dict(row) for row in prior_evaluations]]
        ):
            merged_evaluations.append({**evaluation, "index": index})
        merged_result = dict(prior_result)
        prior_chosen = prior_result.get("chosen_index")
        if type(prior_chosen) is not int:
            raise ValueError("global collision prior judge choice is stale")
        merged_result["chosen_index"] = len(accepted_rows) + prior_chosen
        merged_result["evaluations"] = merged_evaluations
        if not _strict_judge_resolved(merged_result):
            raise ValueError("global collision merge broke strict resolution")
        return merged_rows, merged_result, accepted_rows

    selected_visual_gate_workers = _bounded_production_int(
        settings,
        "selected_visual_gate_workers",
        min(
            4,
            _bounded_production_int(
                settings, "semantic_broad_repair_workers", 4, maximum=8
            ),
        ),
        maximum=4,
    )
    selected_visual_gate_max_decode_bytes = 64 * 1024 * 1024
    selected_gate_row_cache: dict[str, dict[str, Any]] = {}

    def selected_gate_cache_key(
        candidate: Mapping[str, Any], slot_id: str
    ) -> str:
        return canonical_hash(
            {
                "gate_version": SELECTED_VISUAL_GATE_VERSION,
                "transform_contract": SELECTED_VISUAL_TRANSFORM_CONTRACT,
                "slot_id": str(slot_id),
                "candidate_binding": _exact_repair_candidate_binding(candidate),
            }
        )

    def authenticate_selected_gate_runtime_profile(
        receipt: Mapping[str, Any],
        candidate: Mapping[str, Any],
        runtime: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Anchor a cached actionable subset to the current donor profile."""

        source_asset_id = str(candidate.get("source_asset_id") or "").strip()
        donor_profile = runtime["overlay_profiles"].get(source_asset_id)
        if not isinstance(donor_profile, Mapping):
            raise ValueError(
                "selected visual gate runtime donor profile is missing"
            )
        try:
            expected = build_selected_visual_actionable_profile_binding(
                donor_profile
            )
        except SelectedVisualGateError as exc:
            raise ValueError(
                "selected visual gate runtime donor profile is invalid"
            ) from exc
        binding = receipt.get("binding")
        evidence = receipt.get("evidence")
        if (
            not isinstance(binding, Mapping)
            or not isinstance(evidence, Mapping)
            or binding.get("actionable_profile_binding") != expected
            or evidence.get("actionable_profile_binding") != expected
        ):
            raise ValueError(
                "selected visual gate actionable profile differs from runtime donor profile"
            )
        return expected

    def evaluate_selected_gate_candidate(
        candidate: Mapping[str, Any],
        *,
        slot_id: str,
        runtime: Mapping[str, Any],
        cached_gate_row: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        beat_matches = [
            beat
            for beat in effective_beats
            if str(beat.get("slot_id") or "") == slot_id
        ]
        if len(beat_matches) != 1:
            raise ValueError("selected visual gate slot is missing or duplicate")
        exact = _validate_exact_candidate_for_beat(
            candidate, beat_matches[0], output_fps=output_fps
        )
        source_asset_id = str(exact.get("source_asset_id") or "").strip()
        source_path = runtime["asset_resolver"].get(source_asset_id)
        donor_profile = runtime["overlay_profiles"].get(source_asset_id)
        if source_path is None or not isinstance(donor_profile, Mapping):
            raise StageBlocked(
                "needs_selected_visual_gate",
                f"selected visual gate runtime is missing for {source_asset_id}",
            )
        expected_binding = _exact_repair_candidate_binding(exact)
        render_transform, transform_binding = _portable_selected_visual_transform(
            slot_id,
            exact,
            source_path=source_path,
            output_fps=output_fps,
        )
        cached_receipt: Mapping[str, Any] | None = None
        if cached_gate_row is not None:
            if (
                str(cached_gate_row.get("slot_id") or "") != slot_id
                or cached_gate_row.get("candidate_binding") != expected_binding
                or str(cached_gate_row.get("candidate_binding_hash") or "")
                != canonical_hash(expected_binding)
                or cached_gate_row.get("transform_binding") != transform_binding
                or str(cached_gate_row.get("transform_binding_hash") or "")
                != canonical_hash(transform_binding)
                or not isinstance(cached_gate_row.get("gate_receipt"), Mapping)
            ):
                raise ValueError("cached selected visual gate binding is stale")
            cached_receipt = cached_gate_row["gate_receipt"]
        rejected = False
        reason_codes: list[str] = []
        try:
            receipt = evaluate_selected_visual_span(
                exact,
                donor_profile,
                source_path=source_path,
                output_fps=output_fps,
                render_transform=render_transform,
                ffmpeg=repair_ffmpeg,
                ffprobe=str(settings.get("ffprobe") or "ffprobe"),
                max_decode_bytes=selected_visual_gate_max_decode_bytes,
                cached_receipt=cached_receipt,
            )
        except SelectedVisualGateRejected as exc:
            rejected = True
            reason_codes = list(exc.reason_codes)
            receipt = exc.receipt
        except SelectedVisualGateError as exc:
            raise StageBlocked(
                "needs_selected_visual_gate",
                f"selected visual gate could not verify an exact span: {exc}",
            ) from exc
        validated = validate_selected_visual_gate_receipt(receipt)
        _validate_selected_visual_gate_candidate_binding(validated, exact)
        authenticate_selected_gate_runtime_profile(validated, exact, runtime)
        expected_status = "rejected" if rejected else "passed"
        if (
            validated.get("contract") != SELECTED_VISUAL_GATE_RECEIPT_CONTRACT
            or validated.get("gate_version") != SELECTED_VISUAL_GATE_VERSION
            or validated.get("status") != expected_status
            or list(validated.get("rejection_codes") or ()) != reason_codes
        ):
            raise ValueError("selected visual gate result/receipt disagree")
        base = {
            "contract": SELECTED_VISUAL_ACCEPTANCE_CONTRACT,
            "slot_id": slot_id,
            "candidate_binding": expected_binding,
            "candidate_binding_hash": canonical_hash(expected_binding),
            "render_transform": render_transform,
            "render_transform_hash": canonical_hash(render_transform),
            "transform_binding": transform_binding,
            "transform_binding_hash": canonical_hash(transform_binding),
            "status": expected_status,
            "rejection_codes": reason_codes,
            "gate_receipt": validated,
            "gate_receipt_hash": canonical_hash(validated),
        }
        row = {**base, "row_hash": canonical_hash(base)}
        if cached_gate_row is not None and row != dict(cached_gate_row):
            raise ValueError("cached selected visual gate row is stale")
        return row

    def evaluate_selected_gate_rows(
        candidates: Sequence[Mapping[str, Any]],
        *,
        slot_ids: Sequence[str],
        cached_rows: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if len(candidates) != len(slot_ids):
            raise ValueError("selected visual gate candidate/slot counts differ")
        if cached_rows is not None and len(cached_rows) != len(candidates):
            raise ValueError("cached selected visual gate row count is stale")
        if not candidates:
            return []
        normalized_slot_ids = [str(slot_id) for slot_id in slot_ids]
        if any(not slot_id for slot_id in normalized_slot_ids):
            raise ValueError("selected visual gate slot_id is empty")
        runtime = load_targeted_broad_runtime()
        prepared = [dict(candidate) for candidate in candidates]
        output: list[dict[str, Any] | None] = [None] * len(prepared)

        def run(index: int) -> tuple[int, dict[str, Any]]:
            explicit_cached = (
                cached_rows[index]
                if cached_rows is not None
                and isinstance(cached_rows[index], Mapping)
                else None
            )
            cache_key = selected_gate_cache_key(
                prepared[index], normalized_slot_ids[index]
            )
            cached = explicit_cached or selected_gate_row_cache.get(cache_key)
            return index, evaluate_selected_gate_candidate(
                prepared[index],
                slot_id=normalized_slot_ids[index],
                runtime=runtime,
                cached_gate_row=cached,
            )

        with ThreadPoolExecutor(
            max_workers=min(
                selected_visual_gate_workers, max(1, len(prepared))
            ),
            thread_name_prefix="selected-visual-gate",
        ) as executor:
            # All decodes run concurrently, but results (and any raised error)
            # are consumed in slot order so scheduling cannot alter receipts.
            futures = [
                executor.submit(run, index) for index in range(len(prepared))
            ]
            for future in futures:
                index, row = future.result()
                cache_key = selected_gate_cache_key(
                    prepared[index], normalized_slot_ids[index]
                )
                prior = selected_gate_row_cache.get(cache_key)
                if prior is not None and prior != row:
                    raise ValueError("selected visual gate cache row changed")
                selected_gate_row_cache[cache_key] = dict(row)
                output[index] = row
        if any(row is None for row in output):
            raise ValueError("selected visual gate lost a deterministic row")
        return [dict(row) for row in output if row is not None]

    def recompute_gate_aware_judge_result(
        candidates: Sequence[Mapping[str, Any]],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        evaluations = result.get("evaluations")
        if not isinstance(evaluations, list) or len(evaluations) != len(candidates):
            raise ValueError("selected visual gate judge audit is stale")
        normalized = [dict(row) for row in evaluations if isinstance(row, Mapping)]
        if len(normalized) != len(candidates) or any(
            row.get("index") != index for index, row in enumerate(normalized)
        ):
            raise ValueError("selected visual gate judge order is stale")
        eligible = [
            index
            for index, row in enumerate(normalized)
            if str(row.get("verdict") or "").casefold() in {"exact", "relevant"}
            and math.isfinite(float(row.get("confidence") or 0.0))
            and float(row.get("confidence") or 0.0) > 0.0
        ]
        priority = {"exact": 2, "relevant": 1}
        chosen = max(
            eligible,
            key=lambda index: (
                priority[str(normalized[index]["verdict"]).casefold()],
                float(normalized[index]["confidence"]),
                float(candidates[index].get("retrieval_score") or 0.0),
                -index,
            ),
            default=None,
        )
        output = dict(result)
        output.update(
            {
                "chosen_index": chosen,
                "status": "resolved" if chosen is not None else "unresolved",
                "evaluations": normalized,
                "reason": (
                    str(normalized[chosen].get("reason") or "")
                    if chosen is not None
                    else "all exact semantic candidates failed selected visual gate"
                ),
            }
        )
        return output

    def apply_selected_gate_rejections(
        candidate_matrix: list[list[dict[str, Any]]],
        result_matrix: list[dict[str, Any]],
        assignment_timeline: Mapping[str, Any],
        gate_rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        slots = assignment_timeline.get("slots")
        if not isinstance(slots, list) or len(slots) != len(gate_rows):
            raise ValueError("selected visual gate assignment row count is stale")
        rejected_candidates: list[dict[str, Any]] = []
        slot_index_by_id = {
            str(beat.get("slot_id") or ""): index
            for index, beat in enumerate(effective_beats)
        }
        for slot, gate_row in zip(slots, gate_rows):
            if gate_row.get("status") != "rejected":
                continue
            slot_id = str(slot.get("slot_id") or "")
            target_index = slot_index_by_id.get(slot_id)
            selected = slot.get("selected_clip")
            if target_index is None or not isinstance(selected, Mapping):
                raise ValueError("selected visual gate rejected an unknown slot")
            selected_binding = _exact_repair_candidate_binding(selected)
            if gate_row.get("candidate_binding") != selected_binding:
                raise ValueError("selected visual gate rejected a different range")
            matches = [
                index
                for index, candidate in enumerate(candidate_matrix[target_index])
                if _exact_repair_candidate_binding(candidate) == selected_binding
            ]
            if len(matches) != 1:
                raise ValueError("selected visual gate range is missing or duplicate")
            candidate_index = matches[0]
            evaluation = dict(
                result_matrix[target_index]["evaluations"][candidate_index]
            )
            receipt_hash = str(gate_row.get("gate_receipt_hash") or "")
            if evaluation.get("selected_visual_gate_rejected") is True:
                if (
                    str(evaluation.get("selected_visual_gate_receipt_hash") or "")
                    != receipt_hash
                ):
                    raise ValueError("selected visual gate rejection changed")
                continue
            evaluation.update(
                {
                    "semantic_verdict_before_selected_visual_gate": str(
                        evaluation.get("verdict") or ""
                    ),
                    "semantic_confidence_before_selected_visual_gate": float(
                        evaluation.get("confidence") or 0.0
                    ),
                    "semantic_reason_before_selected_visual_gate": str(
                        evaluation.get("reason") or ""
                    )[:500],
                    "selected_visual_gate_rejected": True,
                    "selected_visual_gate_receipt_hash": receipt_hash,
                    "selected_visual_gate_rejection_codes": list(
                        gate_row.get("rejection_codes") or ()
                    ),
                    "verdict": "wrong",
                    "confidence": 0.0,
                    "reason": "selected visual gate rejected the exact rendered span",
                }
            )
            result_matrix[target_index]["evaluations"][candidate_index] = evaluation
            result_matrix[target_index] = recompute_gate_aware_judge_result(
                candidate_matrix[target_index], result_matrix[target_index]
            )
            rejected_candidates.append(dict(selected))
        return rejected_candidates

    def settle_selected_visual_assignment(
        candidate_matrix: list[list[dict[str, Any]]],
        result_matrix: list[dict[str, Any]],
        *,
        cached_rounds: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        rounds: list[dict[str, Any]] = []
        max_rounds = 1 + sum(
            min(
                GLOBAL_COLLISION_SELECTABLE_CANDIDATES_PER_SLOT,
                len(rows),
            )
            for rows in candidate_matrix
        )
        for round_index in range(max_rounds):
            eligible, diagnostic = diagnose_collision_assignment(
                candidate_matrix, result_matrix, allow_empty=True
            )
            status = str(diagnostic.get("status") or "")
            if status == "limit_exceeded":
                raise StageBlocked(
                    "needs_selected_visual_diagnostic",
                    "selected visual assignment exceeded its exact diagnostic budget",
                )
            if status != "feasible":
                if cached_rounds is not None and len(cached_rounds) != len(rounds):
                    raise ValueError("cached selected visual gate rounds are stale")
                return {
                    "status": "needs_repair",
                    "diagnostic": diagnostic,
                    "eligible": eligible,
                    "rounds": rounds,
                    "timeline": None,
                    "materialization": None,
                    "final_gate_rows": [],
                }
            cached_round = (
                cached_rounds[round_index]
                if cached_rounds is not None and round_index < len(cached_rounds)
                else None
            )
            timeline, materialization = _materialize_global_assignment(
                effective_beats,
                effective_briefs,
                candidate_matrix,
                result_matrix,
                diagnostic,
                output_fps=output_fps,
                configured_beam_width=selection_beam_width,
                expected_receipt=(
                    cached_round.get("assignment_materialization")
                    if isinstance(cached_round, Mapping)
                    and isinstance(
                        cached_round.get("assignment_materialization"), Mapping
                    )
                    else None
                ),
            )
            selected = [dict(slot["selected_clip"]) for slot in timeline["slots"]]
            slot_ids = [str(slot["slot_id"]) for slot in timeline["slots"]]
            gate_rows = evaluate_selected_gate_rows(
                selected,
                slot_ids=slot_ids,
                cached_rows=(
                    cached_round.get("gate_rows")
                    if isinstance(cached_round, Mapping)
                    and isinstance(cached_round.get("gate_rows"), list)
                    else None
                ),
            )
            for slot, gate_row in zip(timeline["slots"], gate_rows):
                render_transform = dict(gate_row["render_transform"])
                exact_render_span = (
                    _validated_selected_visual_gate_exact_render_span(
                        gate_row["gate_receipt"], slot["selected_clip"]
                    )
                )
                slot["transform"] = render_transform
                slot["selected_clip"] = {
                    **dict(slot["selected_clip"]),
                    "transform": render_transform,
                    "exact_render_span": exact_render_span,
                }
            selected_bindings = _exact_repair_candidate_bindings(selected)
            rejected_bindings = [
                dict(row["candidate_binding"])
                for row in gate_rows
                if row.get("status") == "rejected"
            ]
            round_base = {
                "contract": SELECTED_VISUAL_ACCEPTANCE_CONTRACT,
                "round": round_index + 1,
                "selection_input_hash": _semantic_selection_input_hash(
                    effective_beats,
                    effective_briefs,
                    candidate_matrix,
                    result_matrix,
                ),
                "diagnostic": diagnostic,
                "diagnostic_hash": canonical_hash(diagnostic),
                "assignment_materialization": materialization,
                "assignment_materialization_hash": canonical_hash(
                    materialization
                ),
                "selected_candidate_bindings": selected_bindings,
                "selected_candidate_bindings_hash": canonical_hash(
                    selected_bindings
                ),
                "gate_rows": gate_rows,
                "gate_rows_hash": canonical_hash(gate_rows),
                "planned_timeline_hash": canonical_hash(timeline),
                "rejected_candidate_bindings": rejected_bindings,
                "rejected_candidate_bindings_hash": canonical_hash(
                    rejected_bindings
                ),
            }
            round_receipt = {
                **round_base,
                "round_hash": canonical_hash(round_base),
            }
            if cached_round is not None and round_receipt != dict(cached_round):
                raise ValueError("cached selected visual gate round is stale")
            rounds.append(round_receipt)
            if not rejected_bindings:
                if cached_rounds is not None and len(cached_rounds) != len(rounds):
                    raise ValueError("cached selected visual gate rounds are stale")
                return {
                    "status": "passed",
                    "diagnostic": diagnostic,
                    "eligible": eligible,
                    "rounds": rounds,
                    "timeline": timeline,
                    "materialization": materialization,
                    "final_gate_rows": gate_rows,
                }
            rejected = apply_selected_gate_rejections(
                candidate_matrix, result_matrix, timeline, gate_rows
            )
            if not rejected:
                raise ValueError("selected visual gate rejection made no progress")
        raise StageBlocked(
            "needs_selected_visual_repair",
            "selected visual gate exceeded its bounded candidate universe",
        )

    def merge_selected_visual_acceptance(
        prior_rows: Sequence[Mapping[str, Any]],
        prior_result: Mapping[str, Any],
        accepted: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        prior_evaluations = prior_result.get("evaluations")
        if not isinstance(prior_evaluations, list) or len(prior_evaluations) != len(
            prior_rows
        ):
            raise ValueError("selected visual prior judge audit is stale")
        seen_ranges = {
            str(row.get("candidate_range_hash") or "") for row in prior_rows
        }
        seen_parents = {
            str(row.get("parent_clip_id") or "")
            for row in prior_rows
            if str(row.get("parent_clip_id") or "")
        }
        accepted_rows: list[dict[str, Any]] = []
        accepted_evaluations: list[dict[str, Any]] = []
        for raw, raw_evaluation in accepted:
            row = dict(raw)
            range_hash = str(row.get("candidate_range_hash") or "")
            parent = str(row.get("parent_clip_id") or "")
            if range_hash in seen_ranges or (parent and parent in seen_parents):
                continue
            seen_ranges.add(range_hash)
            if parent:
                seen_parents.add(parent)
            accepted_rows.append(row)
            accepted_evaluations.append(dict(raw_evaluation))
        merged_rows = [*accepted_rows, *[dict(row) for row in prior_rows]]
        merged_evaluations = [
            {**dict(row), "index": index}
            for index, row in enumerate(
                [*accepted_evaluations, *[dict(row) for row in prior_evaluations]]
            )
        ]
        merged_result = recompute_gate_aware_judge_result(
            merged_rows,
            {**dict(prior_result), "evaluations": merged_evaluations},
        )
        return merged_rows, merged_result, accepted_rows

    def authenticate_selected_gate_row(
        raw_row: Mapping[str, Any],
        candidate: Mapping[str, Any],
        *,
        slot_id: str,
    ) -> dict[str, Any]:
        row = json.loads(json.dumps(dict(raw_row)))
        unsigned = dict(row)
        row_hash = str(unsigned.pop("row_hash", "") or "")
        expected_binding = _exact_repair_candidate_binding(candidate)
        runtime = load_targeted_broad_runtime()
        source_asset_id = str(candidate.get("source_asset_id") or "")
        source_path = runtime["asset_resolver"].get(source_asset_id)
        if source_path is None:
            raise ValueError("selected visual gate source asset is missing")
        expected_transform, expected_transform_binding = (
            _portable_selected_visual_transform(
                slot_id,
                candidate,
                source_path=source_path,
                output_fps=output_fps,
            )
        )
        receipt = row.get("gate_receipt")
        if not isinstance(receipt, Mapping):
            raise ValueError("selected visual gate receipt is missing")
        try:
            validated = validate_selected_visual_gate_receipt(
                receipt,
                expected_cache_key=str(receipt.get("cache_key") or ""),
            )
        except SelectedVisualGateError as exc:
            raise ValueError("selected visual gate receipt is invalid") from exc
        _validate_selected_visual_gate_candidate_binding(validated, candidate)
        authenticate_selected_gate_runtime_profile(
            validated, candidate, runtime
        )
        geometry = (validated.get("binding") or {}).get(
            "render_geometry_proxy"
        )
        status = str(validated.get("status") or "")
        rejection_codes = list(validated.get("rejection_codes") or ())
        if (
            row_hash != canonical_hash(unsigned)
            or row.get("contract") != SELECTED_VISUAL_ACCEPTANCE_CONTRACT
            or str(row.get("slot_id") or "") != slot_id
            or row.get("candidate_binding") != expected_binding
            or str(row.get("candidate_binding_hash") or "")
            != canonical_hash(expected_binding)
            or row.get("render_transform") != expected_transform
            or str(row.get("render_transform_hash") or "")
            != canonical_hash(expected_transform)
            or row.get("transform_binding") != expected_transform_binding
            or str(row.get("transform_binding_hash") or "")
            != canonical_hash(expected_transform_binding)
            or row.get("gate_receipt") != validated
            or str(row.get("gate_receipt_hash") or "")
            != canonical_hash(validated)
            or row.get("status") != status
            or list(row.get("rejection_codes") or ()) != rejection_codes
            or not isinstance(geometry, Mapping)
            or geometry.get("render_transform") != expected_transform
            or not isinstance(geometry.get("crop_margins"), Mapping)
        ):
            raise ValueError("selected visual gate row binding is stale")
        cache_key = selected_gate_cache_key(candidate, slot_id)
        prior = selected_gate_row_cache.get(cache_key)
        if prior is not None and prior != row:
            raise ValueError("selected visual authenticated cache row changed")
        selected_gate_row_cache[cache_key] = dict(row)
        return row

    def selected_visual_target_selection(
        diagnostic: Mapping[str, Any],
        eligible: Sequence[Sequence[Mapping[str, Any]]],
        attempted_target_history: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[int, dict[str, Any]]:
        if str(diagnostic.get("status") or "") != "true_infeasible":
            raise ValueError("selected visual repair target requires infeasibility")
        empty = [index for index, rows in enumerate(eligible) if not rows]
        augmentation = [
            int(index)
            for index in diagnostic.get("single_slot_augmentation_indices") or ()
        ]
        core = [
            int(index)
            for index in diagnostic.get("irreducible_core_indices") or ()
        ]
        pool = empty or augmentation or core
        if not pool:
            raise ValueError("selected visual repair target pool is empty")
        history = [dict(row) for row in attempted_target_history]
        terminal_slot_ids: set[str] = set()
        attempt_counts: dict[str, int] = {}
        for expected_attempt, row in enumerate(history, start=1):
            target_index = row.get("target_index")
            target_slot_id = str(row.get("target_slot_id") or "")
            if (
                row.get("contract")
                != SELECTED_VISUAL_REPAIR_TARGET_OUTCOME_CONTRACT
                or int(row.get("broad_attempt") or 0) != expected_attempt
                or type(target_index) is not int
                or not 0 <= target_index < len(effective_beats)
                or target_slot_id
                != str(effective_beats[target_index].get("slot_id") or "")
                or type(row.get("broad_candidate_count")) is not int
                or int(row["broad_candidate_count"]) < 0
                or type(row.get("gate_clean_candidate_count")) is not int
                or not 0
                <= int(row["gate_clean_candidate_count"])
                <= int(row["broad_candidate_count"])
                or type(row.get("semantic_accepted_candidate_count")) is not int
                or not 0
                <= int(row["semantic_accepted_candidate_count"])
                <= int(row["gate_clean_candidate_count"])
                or bool(row.get("terminal_catalog_exhausted"))
                != (int(row["broad_candidate_count"]) == 0)
            ):
                raise ValueError("selected visual attempted-target history is stale")
            attempt_counts[target_slot_id] = attempt_counts.get(target_slot_id, 0) + 1
            if row["terminal_catalog_exhausted"] is True:
                terminal_slot_ids.add(target_slot_id)
        available = [
            index
            for index in pool
            if str(effective_beats[index].get("slot_id") or "")
            not in terminal_slot_ids
        ]
        if not available:
            raise RepairExhausted(
                "selected visual repair exhausted every current target pool slot"
            )
        target_index = min(
            available,
            key=lambda index: (
                attempt_counts.get(
                    str(effective_beats[index].get("slot_id") or ""), 0
                ),
                index,
            ),
        )
        evidence = {
            "contract": SELECTED_VISUAL_REPAIR_TARGET_RULE_VERSION,
            "pool_kind": (
                "empty_slot"
                if empty
                else "single_slot_augmentation"
                if augmentation
                else "irreducible_core"
            ),
            "pool_indices": pool,
            "pool_slot_ids": [
                str(effective_beats[index].get("slot_id") or "")
                for index in pool
            ],
            "candidate_counts": [len(rows) for rows in eligible],
            "attempted_target_history": history,
            "attempted_target_history_hash": canonical_hash(history),
            "attempt_counts": {
                str(effective_beats[index].get("slot_id") or ""): (
                    attempt_counts.get(
                        str(effective_beats[index].get("slot_id") or ""), 0
                    )
                )
                for index in pool
            },
            "terminal_catalog_exhausted_slot_ids": sorted(terminal_slot_ids),
            "available_pool_indices": available,
            "available_pool_slot_ids": [
                str(effective_beats[index].get("slot_id") or "")
                for index in available
            ],
            "selected_index": target_index,
            "selected_slot_id": str(
                effective_beats[target_index].get("slot_id") or ""
            ),
            "tie_break": (
                "empty-slot-first-exclude-catalog-exhausted-then-fewest-"
                "attempts-then-earliest-slot"
            ),
        }
        return target_index, evidence

    def selected_visual_accepted_rows(
        broad_rows: Sequence[Mapping[str, Any]],
        gate_rows: Sequence[Mapping[str, Any]],
        judge_result: Mapping[str, Any] | None,
        beat: Mapping[str, Any],
    ) -> tuple[
        list[dict[str, Any]],
        list[tuple[dict[str, Any], dict[str, Any]]],
    ]:
        if len(broad_rows) != len(gate_rows):
            raise ValueError("selected broad gate row count is stale")
        clean_rows: list[dict[str, Any]] = []
        clean_gate_rows: list[dict[str, Any]] = []
        slot_id = str(beat.get("slot_id") or "")
        for raw, gate_row in zip(broad_rows, gate_rows):
            authenticated = authenticate_selected_gate_row(
                gate_row, raw, slot_id=slot_id
            )
            if authenticated["status"] == "passed":
                clean_rows.append(dict(raw))
                clean_gate_rows.append(authenticated)
        if not clean_rows:
            if judge_result is not None:
                raise ValueError("selected visual judge ran without gate-clean rows")
            return [], []
        if not isinstance(judge_result, Mapping):
            raise ValueError("selected visual gate-clean rows lost paid judge evidence")
        evaluations = judge_result.get("evaluations")
        if not isinstance(evaluations, list) or len(evaluations) != len(clean_rows):
            raise ValueError("selected visual judge candidate count is stale")
        accepted: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for index, (candidate, gate_row, evaluation) in enumerate(
            zip(clean_rows, clean_gate_rows, evaluations)
        ):
            if not isinstance(evaluation, Mapping) or evaluation.get("index") != index:
                raise ValueError("selected visual judge order is stale")
            try:
                confidence = float(evaluation.get("confidence") or 0.0)
            except (TypeError, ValueError) as exc:
                raise ValueError("selected visual judge confidence is stale") from exc
            if (
                str(evaluation.get("verdict") or "").casefold()
                not in {"exact", "relevant"}
                or not math.isfinite(confidence)
                or confidence <= 0.0
            ):
                continue
            exact = _validate_exact_candidate_for_beat(
                candidate, beat, output_fps=output_fps
            )
            accepted.append(
                (
                    exact,
                    {
                        **dict(evaluation),
                        "selected_visual_gate_status": "passed",
                        "selected_visual_gate_receipt_hash": str(
                            gate_row["gate_receipt_hash"]
                        ),
                        "selected_visual_transform_binding_hash": str(
                            gate_row["transform_binding_hash"]
                        ),
                    },
                )
            )
        return clean_rows, accepted

    def selected_settlement_document(
        settlement: Mapping[str, Any],
    ) -> dict[str, Any]:
        rounds = [dict(row) for row in settlement.get("rounds") or ()]
        diagnostic = dict(settlement["diagnostic"])
        materialization = settlement.get("materialization")
        final_gate_rows = [
            dict(row) for row in settlement.get("final_gate_rows") or ()
        ]
        planned_timeline_hash = (
            canonical_hash(settlement["timeline"])
            if isinstance(settlement.get("timeline"), Mapping)
            else canonical_hash(None)
        )
        return {
            "contract": SELECTED_VISUAL_ACCEPTANCE_CONTRACT,
            "status": str(settlement.get("status") or ""),
            "diagnostic": diagnostic,
            "diagnostic_hash": canonical_hash(diagnostic),
            "rounds": rounds,
            "rounds_hash": canonical_hash(rounds),
            "assignment_materialization": materialization,
            "assignment_materialization_hash": canonical_hash(materialization),
            "final_gate_rows": final_gate_rows,
            "final_gate_rows_hash": canonical_hash(final_gate_rows),
            "planned_timeline_hash": planned_timeline_hash,
        }

    def validate_selected_visual_attempt_result(
        result: Mapping[str, Any],
        *,
        expected_attempt: int,
        base_candidate_matrix: Sequence[Sequence[Mapping[str, Any]]],
        base_result_matrix: Sequence[Mapping[str, Any]],
    ) -> tuple[
        list[list[dict[str, Any]]],
        list[dict[str, Any]],
        dict[str, Any],
        list[dict[str, Any]],
    ]:
        recover_result_repair_sheets(result)
        candidate_matrix = result.get("effective_candidate_rows")
        result_matrix = result.get("effective_results")
        settlement = result.get("selected_visual_settlement")
        audits = result.get("broad_attempt_audits")
        gate_round_history = result.get("gate_round_history")
        receipt = result.get("repair_receipt")
        if not (
            isinstance(candidate_matrix, list)
            and len(candidate_matrix) == len(effective_beats)
            and all(isinstance(rows, list) for rows in candidate_matrix)
            and isinstance(result_matrix, list)
            and len(result_matrix) == len(effective_beats)
            and isinstance(settlement, Mapping)
            and isinstance(audits, list)
            and isinstance(gate_round_history, list)
            and isinstance(receipt, Mapping)
        ):
            raise ValueError("cached selected visual repair snapshot is malformed")
        if (
            len(base_candidate_matrix) != len(effective_beats)
            or len(base_result_matrix) != len(effective_beats)
            or any(not isinstance(rows, Sequence) for rows in base_candidate_matrix)
            or any(not isinstance(row, Mapping) for row in base_result_matrix)
        ):
            raise ValueError("selected visual replay baseline is malformed")
        candidates = json.loads(json.dumps(candidate_matrix))
        if any(
            not isinstance(rows, list)
            or any(not isinstance(row, Mapping) for row in rows)
            for rows in candidates
        ):
            raise ValueError("cached selected visual candidate row is malformed")
        results_snapshot = json.loads(json.dumps(result_matrix))
        if any(not isinstance(row, Mapping) for row in results_snapshot):
            raise ValueError("cached selected visual result row is malformed")
        replayed_candidates = json.loads(json.dumps(base_candidate_matrix))
        replayed_results = json.loads(json.dumps(base_result_matrix))
        if any(
            not isinstance(rows, list)
            or any(not isinstance(row, Mapping) for row in rows)
            for rows in replayed_candidates
        ) or any(not isinstance(row, Mapping) for row in replayed_results):
            raise ValueError("selected visual replay baseline row is malformed")
        audit_rows = json.loads(json.dumps(audits))
        gate_history_rows = json.loads(json.dumps(gate_round_history))
        if (
            any(not isinstance(row, Mapping) for row in audit_rows)
            or any(not isinstance(row, Mapping) for row in gate_history_rows)
            or len(audit_rows) != max(0, expected_attempt - 1)
        ):
            raise ValueError("cached selected visual broad audit is malformed")

        receipt_unsigned = json.loads(json.dumps(dict(receipt)))
        receipt_hash = str(receipt_unsigned.pop("receipt_sha256", "") or "")
        if (
            receipt_hash != canonical_hash(receipt_unsigned)
            or receipt.get("contract") != SELECTED_VISUAL_REPAIR_ATTEMPT_CONTRACT
            or receipt.get("policy") != SELECTED_VISUAL_REPAIR_POLICY_VERSION
            or receipt.get("target_rule")
            != SELECTED_VISUAL_REPAIR_TARGET_RULE_VERSION
            or int(receipt.get("attempt") or 0) != expected_attempt
            or receipt.get("gate_contract")
            != SELECTED_VISUAL_GATE_RECEIPT_CONTRACT
            or receipt.get("gate_version") != SELECTED_VISUAL_GATE_VERSION
            or receipt.get("transform_contract")
            != SELECTED_VISUAL_TRANSFORM_CONTRACT
            or receipt.get("cache_replay_contract")
            != SELECTED_VISUAL_CACHE_REPLAY_CONTRACT
            or receipt.get("exact_render_span_contract")
            != EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION
            or str(receipt.get("effective_candidate_rows_hash") or "")
            != canonical_hash(candidates)
            or str(receipt.get("effective_results_hash") or "")
            != canonical_hash(results_snapshot)
            or str(receipt.get("broad_attempt_audits_hash") or "")
            != canonical_hash(audit_rows)
            or str(receipt.get("gate_round_history_hash") or "")
            != canonical_hash(gate_history_rows)
            or str(receipt.get("selected_visual_settlement_hash") or "")
            != canonical_hash(settlement)
        ):
            raise ValueError("cached selected visual repair receipt is stale")

        gate_receipt_status: dict[str, str] = {}
        validated_target_history: list[dict[str, Any]] = []
        attempted_broad_rows: list[dict[str, Any]] = []
        gate_round_index = 0
        settlement_round_number = 1
        final_settlement_round_start = 0

        def replay_gate_round(
            raw_round: Mapping[str, Any], *, expected_round: int
        ) -> bool:
            eligible_before, diagnostic_before = diagnose_collision_assignment(
                replayed_candidates, replayed_results, allow_empty=True
            )
            if str(diagnostic_before.get("status") or "") != "feasible":
                raise ValueError(
                    "cached selected visual gate round starts from infeasible state"
                )
            materialization = raw_round.get("assignment_materialization")
            stored_gate_rows = raw_round.get("gate_rows")
            if not isinstance(materialization, Mapping) or not isinstance(
                stored_gate_rows, list
            ):
                raise ValueError("cached selected visual gate round is malformed")
            timeline, validated_materialization = _materialize_global_assignment(
                effective_beats,
                effective_briefs,
                replayed_candidates,
                replayed_results,
                diagnostic_before,
                output_fps=output_fps,
                configured_beam_width=selection_beam_width,
                expected_receipt=materialization,
            )
            selected = [
                dict(slot["selected_clip"]) for slot in timeline.get("slots") or ()
            ]
            slot_ids = [
                str(slot.get("slot_id") or "")
                for slot in timeline.get("slots") or ()
            ]
            if len(selected) != len(effective_beats):
                raise ValueError("cached selected visual gate round lost a slot")
            if len(stored_gate_rows) != len(selected):
                raise ValueError(
                    "cached selected visual gate round row count is stale"
                )
            authenticated_gate_rows = [
                authenticate_selected_gate_row(
                    gate_row,
                    candidate,
                    slot_id=slot_id,
                )
                for candidate, slot_id, gate_row in zip(
                    selected, slot_ids, stored_gate_rows
                )
            ]
            for slot, gate_row in zip(
                timeline["slots"], authenticated_gate_rows
            ):
                render_transform = dict(gate_row["render_transform"])
                exact_render_span = (
                    _validated_selected_visual_gate_exact_render_span(
                        gate_row["gate_receipt"], slot["selected_clip"]
                    )
                )
                slot["transform"] = render_transform
                slot["selected_clip"] = {
                    **dict(slot["selected_clip"]),
                    "transform": render_transform,
                    "exact_render_span": exact_render_span,
                }
                gate_receipt_status[
                    str(gate_row["gate_receipt_hash"])
                ] = str(gate_row["status"])
            selected_bindings = _exact_repair_candidate_bindings(selected)
            rejected_bindings = [
                dict(row["candidate_binding"])
                for row in authenticated_gate_rows
                if row.get("status") == "rejected"
            ]
            round_base = {
                "contract": SELECTED_VISUAL_ACCEPTANCE_CONTRACT,
                "round": expected_round,
                "selection_input_hash": _semantic_selection_input_hash(
                    effective_beats,
                    effective_briefs,
                    replayed_candidates,
                    replayed_results,
                ),
                "diagnostic": diagnostic_before,
                "diagnostic_hash": canonical_hash(diagnostic_before),
                "assignment_materialization": validated_materialization,
                "assignment_materialization_hash": canonical_hash(
                    validated_materialization
                ),
                "selected_candidate_bindings": selected_bindings,
                "selected_candidate_bindings_hash": canonical_hash(
                    selected_bindings
                ),
                "gate_rows": authenticated_gate_rows,
                "gate_rows_hash": canonical_hash(authenticated_gate_rows),
                "planned_timeline_hash": canonical_hash(timeline),
                "rejected_candidate_bindings": rejected_bindings,
                "rejected_candidate_bindings_hash": canonical_hash(
                    rejected_bindings
                ),
            }
            expected_receipt = {
                **round_base,
                "round_hash": canonical_hash(round_base),
            }
            if expected_receipt != dict(raw_round):
                raise ValueError("cached selected visual gate round is stale")
            if not rejected_bindings:
                return True
            rejected = apply_selected_gate_rejections(
                replayed_candidates,
                replayed_results,
                timeline,
                authenticated_gate_rows,
            )
            if not rejected:
                raise ValueError(
                    "cached selected visual gate rejection made no progress"
                )
            return False

        for audit_index, audit in enumerate(audit_rows, start=1):
            broad_attempt = int(audit.get("broad_attempt") or 0)
            target_index = audit.get("target_index")
            stored_diagnostic_before = audit.get("diagnostic_before")
            stored_frozen_universe = audit.get("frozen_universe")
            broad_bundle = audit.get("semantic_broad_repair")
            stored_broad_rows = audit.get("broad_candidate_rows")
            gate_rows = audit.get("broad_gate_rows")
            if (
                broad_attempt != audit_index
                or type(target_index) is not int
                or not 0 <= target_index < len(effective_beats)
                or audit.get("contract")
                != SELECTED_VISUAL_REPAIR_ATTEMPT_CONTRACT
                or not isinstance(stored_diagnostic_before, Mapping)
                or not isinstance(stored_frozen_universe, list)
                or not isinstance(broad_bundle, Mapping)
                or not isinstance(stored_broad_rows, list)
                or any(
                    not isinstance(row, Mapping) for row in stored_broad_rows
                )
                or not isinstance(gate_rows, list)
                or any(not isinstance(row, Mapping) for row in gate_rows)
                or len(gate_rows) != len(stored_broad_rows)
                or str(audit.get("audit_sha256") or "")
                != canonical_hash(
                    {
                        key: value
                        for key, value in audit.items()
                        if key != "audit_sha256"
                    }
                )
                or str(audit.get("frozen_universe_hash") or "")
                != canonical_hash(stored_frozen_universe)
                or str(audit.get("semantic_broad_repair_hash") or "")
                != canonical_hash(broad_bundle)
            ):
                raise ValueError("cached selected visual broad audit is stale")

            diagnostic_before: dict[str, Any] | None = None
            eligible_before: list[list[dict[str, Any]]] | None = None
            while diagnostic_before is None:
                current_eligible, current_diagnostic = (
                    diagnose_collision_assignment(
                        replayed_candidates,
                        replayed_results,
                        allow_empty=True,
                    )
                )
                try:
                    diagnostic_before = authenticated_cached_collision_diagnostic(
                        stored_diagnostic_before,
                        current_diagnostic,
                        current_eligible,
                    )
                    eligible_before = current_eligible
                except ValueError as exc:
                    if (
                        str(current_diagnostic.get("status") or "") != "feasible"
                        or gate_round_index >= len(gate_history_rows)
                    ):
                        raise ValueError(
                            "cached selected visual diagnostic-before is stale"
                        ) from exc
                    if replay_gate_round(
                        gate_history_rows[gate_round_index],
                        expected_round=settlement_round_number,
                    ):
                        raise ValueError(
                            "cached selected visual repair follows a passed gate round"
                        )
                    gate_round_index += 1
                    settlement_round_number += 1
            assert eligible_before is not None
            if (
                str(audit.get("diagnostic_before_hash") or "")
                != canonical_hash(diagnostic_before)
            ):
                raise ValueError("cached selected visual diagnostic-before is stale")
            beat = effective_beats[target_index]
            brief = effective_briefs[target_index]
            target_slot_id = str(beat.get("slot_id") or "")
            target_selection = audit.get("target_selection")
            if not isinstance(target_selection, Mapping):
                raise ValueError("cached selected visual target selection is missing")
            expected_target_index, expected_target_selection = (
                selected_visual_target_selection(
                    diagnostic_before,
                    eligible_before,
                    validated_target_history,
                )
            )
            if (
                target_index != expected_target_index
                or str(audit.get("target_slot_id") or "") != target_slot_id
                or target_selection != expected_target_selection
                or str(audit.get("target_selection_hash") or "")
                != canonical_hash(expected_target_selection)
            ):
                raise ValueError("cached selected visual target rotation is stale")

            frozen_universe = collision_frozen_universe(
                replayed_candidates, attempted_broad_rows
            )
            if stored_frozen_universe != frozen_universe:
                raise ValueError("cached selected visual frozen universe is stale")
            validated_broad_rows = validate_cached_collision_broad_repair(
                broad_bundle,
                beat,
                brief,
                frozen_universe,
                attempt=broad_attempt,
                diagnostic_before_hash=canonical_hash(diagnostic_before),
            )
            if (
                validated_broad_rows != stored_broad_rows
                or str(audit.get("broad_candidate_rows_hash") or "")
                != canonical_hash(validated_broad_rows)
                or str(audit.get("broad_gate_rows_hash") or "")
                != canonical_hash(gate_rows)
            ):
                raise ValueError("cached selected visual broad candidates are stale")
            for candidate, gate_row in zip(validated_broad_rows, gate_rows):
                authenticated = authenticate_selected_gate_row(
                    gate_row,
                    candidate,
                    slot_id=target_slot_id,
                )
                gate_receipt_status[str(authenticated["gate_receipt_hash"])] = str(
                    authenticated["status"]
                )
            clean_rows = [
                dict(candidate)
                for candidate, gate_row in zip(validated_broad_rows, gate_rows)
                if gate_row.get("status") == "passed"
            ]
            clean_bindings = _exact_repair_candidate_bindings(clean_rows)
            if (
                audit.get("gate_clean_candidate_bindings") != clean_bindings
                or str(audit.get("gate_clean_candidate_bindings_hash") or "")
                != canonical_hash(clean_bindings)
            ):
                raise ValueError("cached selected visual gate-clean audit is stale")
            judge_result = audit.get("judge_result")
            operation_kind = audit.get("judge_operation_kind")
            paid_chain = audit.get("paid_judge_chain")
            if clean_rows:
                expected_kind = (
                    f"semantic_selected_visual_repair_judge_{broad_attempt:02d}_"
                    f"{beat['slot_id']}"
                )
                if operation_kind != expected_kind or not isinstance(
                    paid_chain, Mapping
                ):
                    raise ValueError("cached selected visual paid judge binding is stale")
                reconstructed, reconstructed_chain = (
                    reconstruct_selected_visual_paid_chain(
                        expected_kind, beat, brief, clean_rows
                    )
                )
                if (
                    judge_result != reconstructed
                    or paid_chain != reconstructed_chain
                    or str(audit.get("judge_result_hash") or "")
                    != canonical_hash(reconstructed)
                    or str(audit.get("paid_judge_chain_hash") or "")
                    != canonical_hash(reconstructed_chain)
                ):
                    raise ValueError("cached selected visual paid judge chain is stale")
            elif any(
                value is not None
                for value in (judge_result, operation_kind, paid_chain)
            ) or (
                str(audit.get("judge_result_hash") or "")
                != canonical_hash(None)
                or str(audit.get("paid_judge_chain_hash") or "")
                != canonical_hash(None)
            ):
                raise ValueError("cached selected visual empty gate batch was paid")
            _clean, accepted_pairs = selected_visual_accepted_rows(
                validated_broad_rows, gate_rows, judge_result, beat
            )
            expected_accepted = _exact_repair_candidate_bindings(
                [row for row, _evaluation in accepted_pairs]
            )
            if (
                audit.get("semantic_accepted_candidate_bindings")
                != expected_accepted
                or str(
                    audit.get("semantic_accepted_candidate_bindings_hash") or ""
                )
                != canonical_hash(expected_accepted)
            ):
                raise ValueError("cached selected visual accepted audit is stale")
            merged_rows, merged_result, inserted_rows = (
                merge_selected_visual_acceptance(
                    replayed_candidates[target_index],
                    replayed_results[target_index],
                    accepted_pairs,
                )
            )
            expected_inserted_bindings = _exact_repair_candidate_bindings(
                inserted_rows
            )
            if (
                audit.get("inserted_candidate_bindings")
                != expected_inserted_bindings
                or str(audit.get("inserted_candidate_bindings_hash") or "")
                != canonical_hash(expected_inserted_bindings)
                or str(audit.get("merged_candidate_rows_hash") or "")
                != canonical_hash(merged_rows)
                or str(audit.get("merged_result_hash") or "")
                != canonical_hash(merged_result)
            ):
                raise ValueError("cached selected visual merge audit is stale")
            target_outcome = audit.get("target_outcome")
            expected_target_outcome = {
                "contract": SELECTED_VISUAL_REPAIR_TARGET_OUTCOME_CONTRACT,
                "broad_attempt": broad_attempt,
                "target_index": target_index,
                "target_slot_id": target_slot_id,
                "broad_status": str(broad_bundle.get("status") or ""),
                "broad_candidate_count": len(validated_broad_rows),
                "gate_clean_candidate_count": len(clean_rows),
                "semantic_accepted_candidate_count": len(accepted_pairs),
                "inserted_candidate_count": len(inserted_rows),
                "terminal_catalog_exhausted": len(validated_broad_rows) == 0,
            }
            if (
                target_outcome != expected_target_outcome
                or str(audit.get("target_outcome_hash") or "")
                != canonical_hash(expected_target_outcome)
            ):
                raise ValueError("cached selected visual target outcome is stale")
            replayed_candidates[target_index] = merged_rows
            replayed_results[target_index] = merged_result
            attempted_broad_rows.extend(
                json.loads(json.dumps(validated_broad_rows))
            )
            validated_target_history.append(expected_target_outcome)
            final_settlement_round_start = gate_round_index
            settlement_round_number = 1

        rounds = settlement.get("rounds")
        if not isinstance(rounds, list):
            raise ValueError("cached selected visual settlement rounds are missing")
        passed_gate_round = False
        while gate_round_index < len(gate_history_rows):
            if passed_gate_round:
                raise ValueError("cached selected visual gate history continues after pass")
            passed_gate_round = replay_gate_round(
                gate_history_rows[gate_round_index],
                expected_round=settlement_round_number,
            )
            gate_round_index += 1
            settlement_round_number += 1
        final_rounds = gate_history_rows[final_settlement_round_start:]
        if rounds != final_rounds:
            raise ValueError("cached selected visual final gate history is stale")
        if (
            replayed_candidates != candidates
            or replayed_results != results_snapshot
        ):
            raise ValueError(
                "cached selected visual final matrices differ from sequential replay"
            )

        for row in replayed_results:
            evaluations = row.get("evaluations")
            if not isinstance(evaluations, list):
                raise ValueError("selected visual snapshot lost judge evaluations")
            for evaluation in evaluations:
                if not isinstance(evaluation, Mapping):
                    raise ValueError("selected visual snapshot judge row is malformed")
                receipt_hash_value = str(
                    evaluation.get("selected_visual_gate_receipt_hash") or ""
                )
                if evaluation.get("selected_visual_gate_rejected") is True:
                    if gate_receipt_status.get(receipt_hash_value) != "rejected":
                        raise ValueError("selected visual rejection lost gate receipt")
                elif evaluation.get("selected_visual_gate_status") == "passed":
                    if gate_receipt_status.get(receipt_hash_value) != "passed":
                        raise ValueError("selected visual acceptance lost gate receipt")

        eligible, current_diagnostic = diagnose_collision_assignment(
            replayed_candidates, replayed_results, allow_empty=True
        )
        if (
            settlement.get("contract") != SELECTED_VISUAL_ACCEPTANCE_CONTRACT
            or settlement.get("diagnostic") != current_diagnostic
            or str(settlement.get("diagnostic_hash") or "")
            != canonical_hash(current_diagnostic)
            or str(settlement.get("rounds_hash") or "")
            != canonical_hash(rounds)
            or str(settlement.get("final_gate_rows_hash") or "")
            != canonical_hash(settlement.get("final_gate_rows") or [])
        ):
            raise ValueError("cached selected visual final settlement is stale")
        settlement_status = str(settlement.get("status") or "")
        if settlement_status == "passed":
            materialization = settlement.get("assignment_materialization")
            final_gate_rows = settlement.get("final_gate_rows")
            if not isinstance(materialization, Mapping) or not isinstance(
                final_gate_rows, list
            ) or len(final_gate_rows) != len(effective_beats):
                raise ValueError("selected visual passed settlement is incomplete")
            timeline, validated_materialization = _materialize_global_assignment(
                effective_beats,
                effective_briefs,
                replayed_candidates,
                replayed_results,
                current_diagnostic,
                output_fps=output_fps,
                configured_beam_width=selection_beam_width,
                expected_receipt=materialization,
            )
            for slot, gate_row in zip(timeline["slots"], final_gate_rows):
                selected = slot["selected_clip"]
                authenticated = authenticate_selected_gate_row(
                    gate_row,
                    selected,
                    slot_id=str(slot.get("slot_id") or ""),
                )
                if authenticated["status"] != "passed":
                    raise ValueError("selected visual final gate receipt rejected")
                transform = dict(authenticated["render_transform"])
                exact_render_span = (
                    _validated_selected_visual_gate_exact_render_span(
                        authenticated["gate_receipt"], selected
                    )
                )
                slot["transform"] = transform
                slot["selected_clip"] = {
                    **dict(selected),
                    "transform": transform,
                    "exact_render_span": exact_render_span,
                }
            if (
                validated_materialization != materialization
                or str(settlement.get("assignment_materialization_hash") or "")
                != canonical_hash(materialization)
                or str(settlement.get("planned_timeline_hash") or "")
                != canonical_hash(timeline)
                or str(result.get("status") or "") != "resolved"
            ):
                raise ValueError("selected visual final timeline binding is stale")
        elif (
            settlement_status != "needs_repair"
            or current_diagnostic.get("status") != "true_infeasible"
            or settlement.get("assignment_materialization") is not None
            or str(settlement.get("assignment_materialization_hash") or "")
            != canonical_hash(None)
            or settlement.get("final_gate_rows") != []
            or str(settlement.get("planned_timeline_hash") or "")
            != canonical_hash(None)
            or str(result.get("status") or "") != "unresolved"
        ):
            raise ValueError("selected visual repair terminal state is stale")
        return replayed_candidates, replayed_results, dict(settlement), audit_rows

    effective_candidates = [
        [dict(row) for row in rows[:judge_batch_size]]
        for rows in ranked_candidates
    ]
    if any(not rows for rows in effective_candidates):
        raise ValueError("temporal retrieval left an empty initial judge batch")
    results = judge_batches(
        "temporal_judge_initial",
        effective_beats,
        effective_briefs,
        effective_candidates,
    )
    initial_unresolved_slots = [
        str(effective_beats[index]["slot_id"])
        for index, result in enumerate(results)
        if not _strict_judge_resolved(result)
    ]
    repair_provenance: list[dict[str, Any]] = []
    controller = RepairController(context.state, max_attempts=3)
    universe_cache: tuple[list[dict[str, Any]], np.ndarray] | None = None
    _narration_path, words_path = _tts_paths(context)
    words_timing_sha256 = sha256_file(words_path)
    words_document: Mapping[str, Any] | None = None

    def load_repair_universe() -> tuple[list[dict[str, Any]], np.ndarray]:
        nonlocal universe_cache
        if universe_cache is not None:
            return universe_cache
        universe = retrieval.get("candidate_universe")
        if not isinstance(universe, list) or not universe:
            raise ValueError("temporal retrieval has no candidate repair universe")
        portable_universe = [dict(row) for row in universe if isinstance(row, Mapping)]
        clip_ids = [str(row.get("clip_id") or "") for row in portable_universe]
        if canonical_hash(clip_ids) != str(retrieval.get("embedding_clip_ids_hash") or ""):
            raise ValueError("temporal embedding clip order hash is invalid")
        embedding_path = _artifact_path(
            context, "temporal_retrieval", str(retrieval.get("embedding_artifact_id"))
        )
        with np.load(embedding_path, allow_pickle=False) as data:
            vectors = np.asarray(data["vectors"], dtype=np.float32)
            stored_ids = [str(value) for value in data["clip_ids"].tolist()]
        if stored_ids != clip_ids or len(vectors) != len(portable_universe):
            raise ValueError("temporal embedding artifact disagrees with candidate universe")
        runtime_universe = [
            _hydrate_candidate(context, row, sheet_stage="temporal_retrieval")
            for row in portable_universe
        ]
        universe_cache = runtime_universe, vectors
        return universe_cache

    def portable_runtime_candidate(row: Mapping[str, Any]) -> dict[str, Any]:
        item = _candidate_with_capacity(row, output_fps=output_fps)
        source_asset_id = str(item.get("source_asset_id") or "")
        if not source_asset_id:
            raise ValueError("repair candidate lost source_asset_id")
        portable = _portable_candidate(item, source_asset_id)
        artifact_id = str(item.get("contact_sheet_artifact_id") or "")
        if not artifact_id:
            raise ValueError("repair candidate lost contact-sheet artifact binding")
        portable["contact_sheet_artifact_id"] = artifact_id
        return portable

    def blocked_payload(
        unresolved_slots: Sequence[str],
        *,
        failed_attempts: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        selection_input_hash = _semantic_selection_input_hash(
            effective_beats, effective_briefs, effective_candidates, results
        )
        return {
            "schema_version": 3,
            "status": "unresolved" if unresolved_slots else "resolved",
            "visual_brief_contract_version": VISUAL_BRIEF_CONTRACT_VERSION,
            "original_beats_hash": original_beats_hash,
            "retrieval_effective_beats_hash": str(
                retrieval["effective_beats_hash"]
            ),
            "words_timing_sha256": words_timing_sha256,
            "effective_beats": effective_beats,
            "effective_beats_hash": canonical_hash(effective_beats),
            "effective_briefs": effective_briefs,
            "effective_ranked_candidates": effective_candidates,
            "results": results,
            "selection_input_hash": selection_input_hash,
            "unresolved_slots": list(unresolved_slots),
            "repair_provenance": repair_provenance,
            "failed_repair_attempts": list(failed_attempts),
        }

    repair_queue = list(initial_unresolved_slots)
    pending_repair_slots = set(repair_queue)
    processed_repair_slots: set[str] = set()
    while repair_queue:
        slot_id = repair_queue.pop(0)
        pending_repair_slots.discard(slot_id)
        target_matches = [
            index
            for index, beat in enumerate(effective_beats)
            if str(beat.get("slot_id") or "") == slot_id
        ]
        if len(target_matches) != 1:
            raise ValueError(f"dynamic repair slot {slot_id} is missing or duplicate")
        target_index = target_matches[0]
        if _strict_judge_resolved(results[target_index]):
            processed_repair_slots.add(slot_id)
            continue
        initial_rows = [dict(row) for row in effective_candidates[target_index]]
        initial_ranges = [canonical_candidate_range(row) for row in initial_rows]
        target_timing_identity = _semantic_timing_beat_identity(
            effective_beats[target_index]
        )
        target_brief_hash = canonical_hash(effective_briefs[target_index])
        _initial_failure_reasons, initial_scarce_context = (
            _strict_semantic_failure_summary([results[target_index]])
        )
        repair_honest_fallback_policy = build_honest_visible_fallback_policy(
            effective_briefs[target_index], initial_scarce_context
        )
        request = RepairRequest(
            slot_id=slot_id,
            reason_code="strict_judge_unresolved",
            failed_range=initial_ranges[0],
            details={
                # The policy version is part of the durable request hash.  It
                # invalidates receipts produced under older beat/queue
                # semantics without mutating durable state by hand.
                "policy": SEMANTIC_REPAIR_POLICY_VERSION,
                "semantic_proxy_repair_prompt_version": (
                    SEMANTIC_PROXY_REPAIR_PROMPT_VERSION
                ),
                "post_resegment_policy": POST_RESEG_SEMANTIC_REPAIR_POLICY_VERSION,
                "post_resegment_proxy_prompt_version": (
                    POST_RESEG_SEMANTIC_PROXY_REPAIR_PROMPT_VERSION
                ),
                "exact_repair_contract": EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION,
                "honest_visible_fallback_policy_version": (
                    HONEST_VISIBLE_FALLBACK_POLICY_VERSION
                ),
                "targeted_semantic_broad_repair_contract": (
                    TARGETED_SEMANTIC_BROAD_REPAIR_CONTRACT_VERSION
                ),
                "honest_visible_fallback_policy": repair_honest_fallback_policy,
                "honest_visible_fallback_policy_hash": canonical_hash(
                    repair_honest_fallback_policy
                ),
                "effective_beats_hash": canonical_hash(effective_beats),
                "effective_target_brief_hash": target_brief_hash,
                "effective_target_timing_identity": target_timing_identity,
                "effective_target_timing_identity_hash": canonical_hash(
                    target_timing_identity
                ),
                "words_timing_sha256": words_timing_sha256,
                "initial_candidate_ids": [str(row.get("clip_id")) for row in initial_rows],
                "initial_banned_ranges": initial_ranges,
            },
        )

        def repair_handler(
            attempt: int, banned: tuple[Mapping[str, Any], ...]
        ) -> Mapping[str, Any]:
            nonlocal words_document
            if attempt == 1:
                current_exact, exact_audit = exactify_repair_candidates(
                    [
                        *[dict(row) for row in effective_candidates[target_index]],
                        *[dict(row) for row in ranked_candidates[target_index]],
                    ],
                    effective_beats[target_index],
                )
                next_batch = next_non_overlapping_candidate_batch(
                    current_exact,
                    banned,
                    batch_size=judge_batch_size,
                )
                if not next_batch:
                    return {
                        "status": "unresolved",
                        "strategy": "next_candidate_batch",
                        "reason": "no untried non-overlapping duration-compatible candidates",
                        "rejected_ranges": [],
                        "repair_receipt": {
                            "slot_id": slot_id,
                            "candidate_ids": [],
                            "exact_rematerialization_audit": exact_audit,
                            "exact_rematerialization_audit_hash": canonical_hash(
                                exact_audit
                            ),
                            **exact_receipt_fields([]),
                        },
                    }
                judged = judge_batches(
                    f"temporal_judge_repair1_{slot_id}",
                    [effective_beats[target_index]],
                    [effective_briefs[target_index]],
                    [next_batch],
                )[0]
                rejected_ranges = (
                    []
                    if _strict_judge_resolved(judged)
                    else [canonical_candidate_range(row) for row in next_batch]
                )
                chosen_index = judged.get("chosen_index")
                chosen_range = (
                    canonical_candidate_range(next_batch[int(chosen_index)])
                    if type(chosen_index) is int
                    and 0 <= int(chosen_index) < len(next_batch)
                    else None
                )
                return {
                    "status": (
                        "resolved" if _strict_judge_resolved(judged) else "unresolved"
                    ),
                    "strategy": "next_candidate_batch",
                    "candidate_rows": next_batch,
                    "judge_result": judged,
                    "rejected_ranges": rejected_ranges,
                    "repair_receipt": {
                        "slot_id": slot_id,
                        "candidate_ids": [str(row.get("clip_id")) for row in next_batch],
                        "candidate_ranges_hash": canonical_hash(
                            [canonical_candidate_range(row) for row in next_batch]
                        ),
                        "judge_result_hash": canonical_hash(judged),
                        "chosen_range": chosen_range,
                        "acceptance_policy": "strict_exact_or_relevant",
                        "exact_rematerialization_audit": exact_audit,
                        "exact_rematerialization_audit_hash": canonical_hash(
                            exact_audit
                        ),
                        **exact_receipt_fields(next_batch),
                    },
                }

            if attempt == 2:
                judge_evidence: list[Mapping[str, Any]] = [results[target_index]]
                for durable_attempt in controller.attempts(context.run_id, slot_id):
                    prior_repair = durable_attempt.get("result")
                    if not isinstance(prior_repair, Mapping):
                        continue
                    prior_judge = prior_repair.get("judge_result")
                    judge_evidence.append(
                        prior_judge
                        if isinstance(prior_judge, Mapping)
                        else prior_repair
                    )
                failure_reasons, scarce_context = _strict_semantic_failure_summary(
                    judge_evidence
                )
                natural_carrier_policy = dict(
                    scarce_context["natural_carrier_policy"]
                )
                natural_status_environment_policy = dict(
                    scarce_context["natural_status_environment_policy"]
                )
                honest_fallback_policy = build_honest_visible_fallback_policy(
                    effective_briefs[target_index], scarce_context
                )
                if honest_fallback_policy != repair_honest_fallback_policy:
                    raise ValueError(
                        "semantic repair honest fallback policy changed within one request"
                    )
                if honest_fallback_policy is not None:
                    scarce_context["honest_visible_fallback_policy"] = (
                        honest_fallback_policy
                    )
                neighbor_texts, adjacent_context = _proxy_neighbor_context(
                    effective_beats, effective_briefs, target_index
                )
                proxy_request = {
                    "beat": dict(effective_beats[target_index]),
                    "current_brief": dict(effective_briefs[target_index]),
                    "neighbor_beat_texts": neighbor_texts,
                    "failure_reasons": failure_reasons,
                    "scarce_candidate_context": scarce_context,
                    "adjacent_anchor_context": adjacent_context,
                }
                paid_request = {
                    "policy": SEMANTIC_REPAIR_POLICY_VERSION,
                    "repair_prompt_version": SEMANTIC_PROXY_REPAIR_PROMPT_VERSION,
                    "visual_brief_contract_version": VISUAL_BRIEF_CONTRACT_VERSION,
                    "model": proxy_repair_model,
                    "target_timing_identity": _semantic_timing_beat_identity(
                        effective_beats[target_index]
                    ),
                    "current_brief": dict(effective_briefs[target_index]),
                    "current_brief_hash": canonical_hash(
                        effective_briefs[target_index]
                    ),
                    "failure_reasons": failure_reasons,
                    "neighbor_beat_texts": neighbor_texts,
                    "scarce_candidate_context": scarce_context,
                    "natural_carrier_policy": natural_carrier_policy,
                    "natural_status_environment_policy": (
                        natural_status_environment_policy
                    ),
                    "honest_visible_fallback_policy_version": (
                        HONEST_VISIBLE_FALLBACK_POLICY_VERSION
                    ),
                    "honest_visible_fallback_policy": honest_fallback_policy,
                    "honest_visible_fallback_policy_hash": canonical_hash(
                        honest_fallback_policy
                    ),
                    "adjacent_anchor_context": adjacent_context,
                }
                try:
                    raw_proxy_response = _paid_call(
                        context,
                        provider="semantic_llm",
                        operation_kind=f"semantic_proxy_rebrief_{slot_id}",
                        request=paid_request,
                        call=lambda: request_semantic_proxy_repair_raw(
                            [proxy_request], model=proxy_repair_model
                        ),
                    )
                except StageBlocked:
                    raise
                except Exception as exc:
                    raise StageBlocked(
                        "needs_semantic_proxy_provider",
                        f"semantic proxy re-brief provider failed: {exc}",
                    ) from exc
                raw_proxy_hash = canonical_hash(raw_proxy_response)
                try:
                    validated_proxy = validate_semantic_proxy_repair_response(
                        [proxy_request],
                        raw_proxy_response,
                        model=proxy_repair_model,
                    )
                except ValueError as exc:
                    return {
                        "status": "unresolved",
                        "strategy": "semantic_proxy_rebrief",
                        "reason": f"semantic proxy re-brief rejected locally: {exc}",
                        "rejected_ranges": [],
                        "repair_receipt": {
                            "slot_id": slot_id,
                            "validation_status": "invalid",
                            "paid_request_hash": canonical_hash(paid_request),
                            "raw_response_hash": raw_proxy_hash,
                            "repair_prompt_version": SEMANTIC_PROXY_REPAIR_PROMPT_VERSION,
                        },
                    }
                if len(validated_proxy) != 1:
                    raise ValueError("semantic proxy re-brief returned an invalid row count")
                replacement_brief, honest_fallback_receipt = (
                    pop_honest_visible_fallback_receipt(validated_proxy[0])
                )
                replacement_brief = validate_visual_brief_contract(
                    dict(effective_beats[target_index]), replacement_brief
                )
                honest_bundle = build_honest_fallback_bundle(
                    effective_briefs[target_index],
                    replacement_brief,
                    honest_fallback_policy,
                    honest_fallback_receipt,
                )
                broad_rows, broad_bundle = run_targeted_broad_repair(
                    effective_beats[target_index], replacement_brief, honest_bundle
                )
                fallback_broad_fields = fallback_broad_receipt_fields(
                    honest_bundle, broad_bundle
                )
                runtime_universe, vectors = load_repair_universe()
                try:
                    reranked = rank_for_query(
                        str(replacement_brief.get("query_en") or ""),
                        runtime_universe,
                        vectors,
                        top_k=min(len(runtime_universe), repair_pool_size),
                    )
                except Exception as exc:
                    raise StageBlocked(
                        "needs_clip_model", f"proxy re-brief retrieval failed: {exc}"
                    ) from exc
                compatible, exact_audit = exactify_repair_candidates(
                    reranked,
                    effective_beats[target_index],
                )
                compatible = merge_targeted_broad_candidates(
                    broad_rows, compatible
                )
                # A changed semantic brief resets semantic rejection bans.  We
                # still deduplicate ranges and avoid overlap inside this new
                # batch, but never suppress a candidate due to the old brief.
                proxy_batch = next_non_overlapping_candidate_batch(
                    compatible,
                    (),
                    batch_size=judge_batch_size,
                )
                if not proxy_batch:
                    return {
                        "status": "unresolved",
                        "strategy": "semantic_proxy_rebrief",
                        "reason": "proxy re-brief left no duration-compatible candidate",
                        "rejected_ranges": [],
                        "repair_receipt": {
                            "slot_id": slot_id,
                            "validation_status": "valid",
                            "paid_request_hash": canonical_hash(paid_request),
                            "raw_response_hash": raw_proxy_hash,
                            "replacement_brief_hash": canonical_hash(replacement_brief),
                            "candidate_ids": [],
                            "exact_rematerialization_audit": exact_audit,
                            "exact_rematerialization_audit_hash": canonical_hash(
                                exact_audit
                            ),
                            **fallback_broad_fields,
                            **exact_receipt_fields([]),
                        },
                    }
                proxy_judged = judge_batches(
                    f"temporal_judge_repair2_proxy_{slot_id}",
                    [effective_beats[target_index]],
                    [replacement_brief],
                    [proxy_batch],
                )[0]
                proxy_resolved = _strict_judge_resolved(proxy_judged)
                return {
                    "status": "resolved" if proxy_resolved else "unresolved",
                    "strategy": "semantic_proxy_rebrief",
                    "reason": (
                        "semantic proxy re-brief resolved the slot"
                        if proxy_resolved
                        else "semantic proxy re-brief remained unresolved"
                    ),
                    "replacement_brief": replacement_brief,
                    "replacement_brief_hash": canonical_hash(replacement_brief),
                    "candidate_rows": proxy_batch,
                    "candidate_ranges_hash": canonical_hash(
                        [canonical_candidate_range(row) for row in proxy_batch]
                    ),
                    "judge_result": proxy_judged,
                    "judge_result_hash": canonical_hash(proxy_judged),
                    # Attempt 3 changes timing/brief again; these semantic
                    # failures are evidence, not source-range bans.
                    "rejected_ranges": [],
                    "repair_receipt": {
                        "slot_id": slot_id,
                        "validation_status": "valid",
                        "paid_request_hash": canonical_hash(paid_request),
                        "raw_response_hash": raw_proxy_hash,
                        "replacement_brief_hash": canonical_hash(replacement_brief),
                        "candidate_ids": [
                            str(row.get("clip_id") or "") for row in proxy_batch
                        ],
                        "candidate_ranges_hash": canonical_hash(
                            [canonical_candidate_range(row) for row in proxy_batch]
                        ),
                        "judge_result_hash": canonical_hash(proxy_judged),
                        "acceptance_policy": "strict_exact_or_relevant",
                        "exact_rematerialization_audit": exact_audit,
                        "exact_rematerialization_audit_hash": canonical_hash(
                            exact_audit
                        ),
                        **fallback_broad_fields,
                        **exact_receipt_fields(proxy_batch),
                    },
                }

            if attempt != 3:
                raise ValueError(f"unsupported semantic repair attempt: {attempt}")

            current_duration = float(effective_beats[target_index]["end"]) - float(
                effective_beats[target_index]["start"]
            )
            raw_repair_candidates = retrieval.get("repair_candidates") or ()
            slot_repair_candidates = (
                raw_repair_candidates[target_index]
                if target_index < len(raw_repair_candidates)
                else ()
            )
            short_candidates: list[dict[str, Any]] = []
            for raw in slot_repair_candidates:
                if not isinstance(raw, Mapping):
                    continue
                candidate = _candidate_with_capacity(raw, output_fps=output_fps)
                capacity = float(candidate.get("max_render_seconds") or 0.0)
                if (
                    capacity >= min_seconds
                    and capacity < current_duration - (1.0 / output_fps)
                ):
                    short_candidates.append(candidate)
            untried_short = next_non_overlapping_candidate_batch(
                short_candidates,
                (),
                batch_size=repair_pool_size,
            )
            if not untried_short:
                return {
                    "status": "unresolved",
                    "strategy": "visual_resegmentation",
                    "reason": "no untried clean short candidate can justify resegmentation",
                    "rejected_ranges": [],
                    "repair_receipt": {"slot_id": slot_id, "candidate_ids": []},
                }
            preferred = untried_short[0]
            visual_limit = float(preferred["max_render_seconds"])
            if words_document is None:
                words_document = load_words_document(words_path)
            try:
                repartitioned = repartition_beats_for_visual_limit(
                    effective_beats,
                    words_document,
                    slot_id,
                    visual_limit,
                    fps=output_fps,
                    min_seconds=min_seconds,
                    target_seconds=target_seconds,
                    max_seconds=max_seconds,
                    window_radius=window_radius,
                )
            except BeatPlanningError as exc:
                return {
                    "status": "unresolved",
                    "strategy": "visual_resegmentation",
                    "reason": str(exc),
                    "rejected_ranges": [canonical_candidate_range(preferred)],
                    "repair_receipt": {
                        "slot_id": slot_id,
                        "candidate_ids": [str(preferred.get("clip_id"))],
                        "visual_limit": visual_limit,
                    },
                }
            changed_indices = _semantic_timing_changed_indices(
                effective_beats, repartitioned
            )
            if not changed_indices:
                return {
                    "status": "unresolved",
                    "strategy": "visual_resegmentation",
                    "reason": "visual limit did not change the effective beat plan",
                    "rejected_ranges": [canonical_candidate_range(preferred)],
                }
            changed_beats = [repartitioned[index] for index in changed_indices]
            refreshed_brief_indices = _post_resegment_brief_refresh_indices(
                effective_beats,
                repartitioned,
                target_index=target_index,
                changed_indices=changed_indices,
            )
            refreshed_brief_index_set = set(refreshed_brief_indices)
            preserved_brief_indices = [
                index
                for index in changed_indices
                if index not in refreshed_brief_index_set
            ]
            replacement_briefs = _request_visual_briefs_in_chunks(
                context,
                [repartitioned[index] for index in refreshed_brief_indices],
                model=brief_model,
                # Keep the established paid operation identity: attempt 3 is
                # the same visual-resegmentation contract formerly reached as
                # attempt 2, so durable raw/results remain safely reusable.
                operation_prefix=f"visual_briefs_repair2_{slot_id}",
                request_metadata={
                    "reason": "visual_duration_limit",
                    "visual_limit": visual_limit,
                },
            )
            repaired_briefs = json.loads(json.dumps(effective_briefs))
            for index, brief in zip(refreshed_brief_indices, replacement_briefs):
                repaired_briefs[index] = dict(brief)

            runtime_universe, vectors = load_repair_universe()
            repaired_batches: list[list[dict[str, Any]]] = []
            carried_forward_candidates: list[dict[str, Any]] = []
            exact_rematerialization_audits: dict[str, Any] = {}
            preferred_exact_ranges: list[dict[str, Any]] = []
            preferred_range = canonical_candidate_range(preferred)
            for index in changed_indices:
                changed_slot_id = str(repartitioned[index]["slot_id"])
                brief = repaired_briefs[index]
                try:
                    reranked = rank_for_query(
                        str(brief.get("query_en") or ""),
                        runtime_universe,
                        vectors,
                        top_k=min(len(runtime_universe), repair_pool_size),
                    )
                except Exception as exc:
                    raise StageBlocked(
                        "needs_clip_model", f"repair temporal retrieval failed: {exc}"
                    ) from exc
                compatible, ranked_exact_audit = exactify_repair_candidates(
                    reranked,
                    repartitioned[index],
                )
                if index == target_index:
                    preferred_exact, preferred_exact_audit = (
                        exactify_repair_candidates(
                            [preferred],
                            repartitioned[index],
                        )
                    )
                    exact_rematerialization_audits[changed_slot_id] = {
                        "ranked": ranked_exact_audit,
                        "preferred": preferred_exact_audit,
                    }
                    preferred_exact_ranges = [
                        canonical_candidate_range(row) for row in preferred_exact
                    ]
                    combined = _deduplicate_exact_repair_candidates(
                        [*preferred_exact, *compatible]
                    )
                    batch = next_non_overlapping_candidate_batch(
                        combined,
                        (),
                        batch_size=judge_batch_size,
                    )
                    if not any(
                        canonical_candidate_range(row) in preferred_exact_ranges
                        for row in batch
                    ):
                        batch = []
                else:
                    combined = list(compatible)
                    prior_exact: list[dict[str, Any]] = []
                    prior_exact_audit: dict[str, Any] | None = None
                    prior_result = results[index]
                    if _strict_judge_resolved(prior_result):
                        prior_index = int(prior_result["chosen_index"])
                        prior_rows = effective_candidates[index]
                        if 0 <= prior_index < len(prior_rows):
                            prior_candidate = dict(prior_rows[prior_index])
                            prior_exact, prior_exact_audit = (
                                exactify_repair_candidates(
                                    [prior_candidate],
                                    repartitioned[index],
                                )
                            )
                            combined = [*prior_exact, *combined]
                    exact_rematerialization_audits[changed_slot_id] = {
                        "ranked": ranked_exact_audit,
                        "carried_prior": prior_exact_audit,
                    }
                    combined = _deduplicate_exact_repair_candidates(combined)
                    batch = next_non_overlapping_candidate_batch(
                        combined,
                        (),
                        batch_size=judge_batch_size,
                    )
                    selected_binding_hashes = {
                        canonical_hash(_exact_repair_candidate_binding(row))
                        for row in batch
                    }
                    for prior_candidate in prior_exact:
                        if canonical_hash(
                            _exact_repair_candidate_binding(prior_candidate)
                        ) in selected_binding_hashes:
                            carried_forward_candidates.append(
                                {
                                    "slot_id": changed_slot_id,
                                    "clip_id": str(
                                        prior_candidate.get("clip_id") or ""
                                    ),
                                    "range": canonical_candidate_range(
                                        prior_candidate
                                    ),
                                    "strict_variant_key": str(
                                        prior_candidate.get("strict_variant_key")
                                        or ""
                                    ),
                                    "contact_sheet_artifact_id": str(
                                        prior_candidate.get(
                                            "contact_sheet_artifact_id"
                                        )
                                        or ""
                                    ),
                                }
                            )
                if not batch:
                    return {
                        "status": "unresolved",
                        "strategy": "visual_resegmentation",
                        "reason": (
                            f"no frame-exact candidates after resegmentation for "
                            f"{repartitioned[index]['slot_id']}"
                        ),
                        "rejected_ranges": [preferred_range],
                        "repair_receipt": {
                            "slot_id": slot_id,
                            "candidate_ids": [str(preferred.get("clip_id"))],
                            "visual_limit": visual_limit,
                            "changed_slots": [
                                str(repartitioned[row]["slot_id"])
                                for row in changed_indices
                            ],
                            "refreshed_brief_slots": [
                                str(repartitioned[row]["slot_id"])
                                for row in refreshed_brief_indices
                            ],
                            "preserved_brief_slots": [
                                str(repartitioned[row]["slot_id"])
                                for row in preserved_brief_indices
                            ],
                            "preferred_exact_candidate_ranges": (
                                preferred_exact_ranges
                            ),
                            "exact_rematerialization_audits": (
                                exact_rematerialization_audits
                            ),
                            "exact_rematerialization_audits_hash": canonical_hash(
                                exact_rematerialization_audits
                            ),
                            **exact_receipt_fields([]),
                        },
                    }
                repaired_batches.append(batch)

            repaired_results = judge_batches(
                f"temporal_judge_repair2_{slot_id}",
                changed_beats,
                [repaired_briefs[index] for index in changed_indices],
                repaired_batches,
            )
            target_changed_position = changed_indices.index(target_index)
            first_target_result = repaired_results[target_changed_position]
            post_resegment_proxy_receipt: dict[str, Any] | None = None
            post_resegment_proxy_failure = ""
            if not _strict_judge_resolved(first_target_result):
                judge_evidence: list[Mapping[str, Any]] = [results[target_index]]
                for durable_attempt in controller.attempts(context.run_id, slot_id):
                    prior_repair = durable_attempt.get("result")
                    if not isinstance(prior_repair, Mapping):
                        continue
                    prior_judge = prior_repair.get("judge_result")
                    judge_evidence.append(
                        prior_judge
                        if isinstance(prior_judge, Mapping)
                        else prior_repair
                    )
                judge_evidence.append(first_target_result)
                failure_reasons, base_scarce_context = (
                    _strict_semantic_failure_summary(judge_evidence)
                )
                scarce_context = json.loads(json.dumps(base_scarce_context))
                grounded_magnitude_policy = {
                    "contract_version": GROUNDED_MAGNITUDE_DROP_POLICY_VERSION,
                    "compile_numeric_drop_to_natural_cues": True,
                    "number_remains_narration_only": True,
                    "forbid_unnarrated_scale_props": True,
                    "required_visible_evidence": (
                        "falling water or a natural drop edge plus natural mist, "
                        "spray, rock, or vegetation"
                    ),
                }
                scarce_context["grounded_magnitude_drop_policy"] = (
                    grounded_magnitude_policy
                )
                neighbor_texts, adjacent_context = _proxy_neighbor_context(
                    repartitioned, repaired_briefs, target_index
                )
                split_beat = dict(repartitioned[target_index])
                split_timing_identity = _semantic_timing_beat_identity(split_beat)
                fresh_split_brief = dict(repaired_briefs[target_index])
                honest_fallback_policy = build_honest_visible_fallback_policy(
                    fresh_split_brief, scarce_context
                )
                if honest_fallback_policy is not None:
                    scarce_context["honest_visible_fallback_policy"] = (
                        honest_fallback_policy
                    )
                first_target_batch = repaired_batches[target_changed_position]
                post_proxy_request = {
                    "beat": split_beat,
                    "current_brief": fresh_split_brief,
                    "neighbor_beat_texts": neighbor_texts,
                    "failure_reasons": failure_reasons,
                    "scarce_candidate_context": scarce_context,
                    "adjacent_anchor_context": adjacent_context,
                }
                post_proxy_paid_request = {
                    "policy": POST_RESEG_SEMANTIC_REPAIR_POLICY_VERSION,
                    "repair_prompt_version": (
                        POST_RESEG_SEMANTIC_PROXY_REPAIR_PROMPT_VERSION
                    ),
                    "grounded_magnitude_drop_policy_version": (
                        GROUNDED_MAGNITUDE_DROP_POLICY_VERSION
                    ),
                    "visual_brief_contract_version": VISUAL_BRIEF_CONTRACT_VERSION,
                    "model": proxy_repair_model,
                    "split_effective_beats_hash": canonical_hash(repartitioned),
                    "split_target_timing_identity": split_timing_identity,
                    "split_target_timing_identity_hash": canonical_hash(
                        split_timing_identity
                    ),
                    "fresh_split_brief": fresh_split_brief,
                    "fresh_split_brief_hash": canonical_hash(fresh_split_brief),
                    "first_post_split_judge_hash": canonical_hash(
                        first_target_result
                    ),
                    "first_post_split_candidate_ranges_hash": canonical_hash(
                        [
                            canonical_candidate_range(row)
                            for row in first_target_batch
                        ]
                    ),
                    "failure_reasons": failure_reasons,
                    "failure_reasons_hash": canonical_hash(failure_reasons),
                    "neighbor_beat_texts": neighbor_texts,
                    "adjacent_anchor_context": adjacent_context,
                    "neighbor_context_hash": canonical_hash(
                        {
                            "neighbor_beat_texts": neighbor_texts,
                            "adjacent_anchor_context": adjacent_context,
                        }
                    ),
                    "scarce_candidate_context": scarce_context,
                    "honest_visible_fallback_policy_version": (
                        HONEST_VISIBLE_FALLBACK_POLICY_VERSION
                    ),
                    "honest_visible_fallback_policy": honest_fallback_policy,
                    "honest_visible_fallback_policy_hash": canonical_hash(
                        honest_fallback_policy
                    ),
                    "exact_repair_contract": EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION,
                }
                try:
                    raw_post_proxy_response = _paid_call(
                        context,
                        provider="semantic_llm",
                        operation_kind=(
                            f"semantic_proxy_rebrief_post_resegment_{slot_id}"
                        ),
                        request=post_proxy_paid_request,
                        call=lambda: (
                            request_post_resegment_semantic_proxy_repair_raw(
                                [post_proxy_request], model=proxy_repair_model
                            )
                        ),
                    )
                except StageBlocked:
                    raise
                except Exception as exc:
                    raise StageBlocked(
                        "needs_semantic_proxy_provider",
                        f"post-resegment semantic proxy provider failed: {exc}",
                    ) from exc
                raw_post_proxy_hash = canonical_hash(raw_post_proxy_response)
                post_resegment_proxy_receipt = {
                    "policy": POST_RESEG_SEMANTIC_REPAIR_POLICY_VERSION,
                    "repair_prompt_version": (
                        POST_RESEG_SEMANTIC_PROXY_REPAIR_PROMPT_VERSION
                    ),
                    "validation_status": "invalid",
                    "paid_request": post_proxy_paid_request,
                    "paid_request_hash": canonical_hash(post_proxy_paid_request),
                    "raw_response_hash": raw_post_proxy_hash,
                    "split_target_timing_identity_hash": canonical_hash(
                        split_timing_identity
                    ),
                    "fresh_split_brief_hash": canonical_hash(fresh_split_brief),
                    "first_post_split_judge_hash": canonical_hash(
                        first_target_result
                    ),
                    "failure_reasons_hash": canonical_hash(failure_reasons),
                    "neighbor_context_hash": post_proxy_paid_request[
                        "neighbor_context_hash"
                    ],
                    "acceptance_policy": "strict_exact_or_relevant",
                    "exact_repair_contract": EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION,
                }
                try:
                    validated_post_proxy = (
                        validate_post_resegment_semantic_proxy_repair_response(
                            [post_proxy_request],
                            raw_post_proxy_response,
                            model=proxy_repair_model,
                        )
                    )
                except ValueError as exc:
                    post_resegment_proxy_failure = (
                        f"post-resegment semantic proxy rejected locally: {exc}"
                    )
                    post_resegment_proxy_receipt["validation_error"] = str(exc)
                else:
                    if len(validated_post_proxy) != 1:
                        raise ValueError(
                            "post-resegment semantic proxy returned an invalid row count"
                        )
                    replacement_brief, honest_fallback_receipt = (
                        pop_honest_visible_fallback_receipt(
                            validated_post_proxy[0]
                        )
                    )
                    replacement_brief = validate_visual_brief_contract(
                        split_beat, replacement_brief
                    )
                    honest_bundle = build_honest_fallback_bundle(
                        fresh_split_brief,
                        replacement_brief,
                        honest_fallback_policy,
                        honest_fallback_receipt,
                    )
                    broad_rows, broad_bundle = run_targeted_broad_repair(
                        split_beat, replacement_brief, honest_bundle
                    )
                    fallback_broad_fields = fallback_broad_receipt_fields(
                        honest_bundle, broad_bundle
                    )
                    repaired_briefs[target_index] = replacement_brief
                    post_resegment_proxy_receipt.update(
                        {
                            "validation_status": "valid",
                            "replacement_brief_hash": canonical_hash(
                                replacement_brief
                            ),
                            **fallback_broad_fields,
                        }
                    )
                    try:
                        reranked = rank_for_query(
                            str(replacement_brief.get("query_en") or ""),
                            runtime_universe,
                            vectors,
                            top_k=len(runtime_universe),
                        )
                    except Exception as exc:
                        raise StageBlocked(
                            "needs_clip_model",
                            f"post-resegment proxy retrieval failed: {exc}",
                        ) from exc
                    compatible, post_proxy_exact_audit = (
                        progressively_exactify_ranked_candidates(
                            reranked,
                            repartitioned[target_index],
                        )
                    )
                    compatible = merge_targeted_broad_candidates(
                        broad_rows, compatible
                    )
                    # The short clip only supplied a duration cap, so it is no
                    # longer prepended.  A changed semantic brief also resets
                    # semantic range bans: a visually honest range rejected by
                    # the over-literal first brief must remain eligible under
                    # the grounded proxy.  Candidates now enter solely through
                    # ranking for the replacement query and are judged again.
                    post_proxy_batch = next_non_overlapping_candidate_batch(
                        compatible,
                        (),
                        batch_size=judge_batch_size,
                    )
                    post_resegment_proxy_receipt["prior_first_batch_ranges_hash"] = (
                        canonical_hash(
                            [
                                canonical_candidate_range(row)
                                for row in first_target_batch
                            ]
                        )
                    )
                    if not post_proxy_batch:
                        post_resegment_proxy_failure = (
                            "post-resegment proxy left no new duration-compatible "
                            "strict candidate"
                        )
                        post_resegment_proxy_receipt.update(
                            {
                                "candidate_ids": [],
                                "candidate_ranges_hash": canonical_hash([]),
                                "exact_rematerialization_audit": (
                                    post_proxy_exact_audit
                                ),
                                "exact_rematerialization_audit_hash": canonical_hash(
                                    post_proxy_exact_audit
                                ),
                                **exact_receipt_fields([]),
                            }
                        )
                    else:
                        post_proxy_judged = judge_batches(
                            f"temporal_judge_post_resegment_proxy_{slot_id}",
                            [repartitioned[target_index]],
                            [replacement_brief],
                            [post_proxy_batch],
                        )[0]
                        repaired_batches[target_changed_position] = post_proxy_batch
                        repaired_results[target_changed_position] = post_proxy_judged
                        post_resegment_proxy_receipt.update(
                            {
                                "candidate_ids": [
                                    str(row.get("clip_id") or "")
                                    for row in post_proxy_batch
                                ],
                                "candidate_ranges_hash": canonical_hash(
                                    [
                                        canonical_candidate_range(row)
                                        for row in post_proxy_batch
                                    ]
                                ),
                                "judge_result_hash": canonical_hash(
                                    post_proxy_judged
                                ),
                                "resolved": _strict_judge_resolved(
                                    post_proxy_judged
                                ),
                                "exact_rematerialization_audit": (
                                    post_proxy_exact_audit
                                ),
                                "exact_rematerialization_audit_hash": canonical_hash(
                                    post_proxy_exact_audit
                                ),
                                **exact_receipt_fields(post_proxy_batch),
                            }
                        )
                        if not _strict_judge_resolved(post_proxy_judged):
                            post_resegment_proxy_failure = (
                                "post-resegment semantic proxy remained unresolved"
                            )
            outcome = _resegmentation_resolution_outcome(
                target_index=target_index,
                changed_indices=changed_indices,
                beats=repartitioned,
                prior_results=results,
                repaired_results=repaired_results,
            )
            updates = [
                {
                    "slot_id": str(repartitioned[index]["slot_id"]),
                    "brief": repaired_briefs[index],
                    "candidate_rows": batch,
                    "judge_result": result,
                }
                for index, batch, result in zip(
                    changed_indices, repaired_batches, repaired_results
                )
            ]
            committed_candidates = json.loads(json.dumps(effective_candidates))
            committed_results = json.loads(json.dumps(results))
            for index, batch, result in zip(
                changed_indices, repaired_batches, repaired_results
            ):
                committed_candidates[index] = json.loads(json.dumps(batch))
                committed_results[index] = dict(result)
            target_batch = repaired_batches[changed_indices.index(target_index)]
            exact_bindings_by_slot = {
                str(repartitioned[index]["slot_id"]): (
                    _exact_repair_candidate_bindings(batch)
                )
                for index, batch in zip(changed_indices, repaired_batches)
            }
            changed_slot_projection = (
                _semantic_resegment_changed_slot_projection(updates)
            )
            receipt = {
                "slot_id": slot_id,
                "candidate_ids": [str(row.get("clip_id")) for row in target_batch],
                "preferred_candidate_range": preferred_range,
                "visual_limit": visual_limit,
                "changed_slots": [row["slot_id"] for row in updates],
                "prior_effective_beats_hash": canonical_hash(effective_beats),
                "effective_beats_hash": canonical_hash(repartitioned),
                "effective_briefs_hash": canonical_hash(repaired_briefs),
                "effective_candidates_hash": canonical_hash(committed_candidates),
                "effective_results_hash": canonical_hash(committed_results),
                "words_timing_sha256": words_timing_sha256,
                "judge_results_hash": canonical_hash(repaired_results),
                "updates_hash": canonical_hash(updates),
                "changed_slot_projection_contract": (
                    SEMANTIC_RESEGMENT_PROJECTION_CONTRACT
                ),
                "changed_slot_projection": changed_slot_projection,
                "changed_slot_projection_hash": canonical_hash(
                    changed_slot_projection
                ),
                "acceptance_policy": "strict_exact_or_relevant",
                "post_resegment_policy": POST_RESEG_SEMANTIC_REPAIR_POLICY_VERSION,
                "preferred_exact_candidate_ranges": preferred_exact_ranges,
                "exact_rematerialization_audits": exact_rematerialization_audits,
                "exact_rematerialization_audits_hash": canonical_hash(
                    exact_rematerialization_audits
                ),
                "exact_candidate_bindings_by_slot": exact_bindings_by_slot,
                "exact_candidate_bindings_by_slot_hash": canonical_hash(
                    exact_bindings_by_slot
                ),
                **exact_receipt_fields(target_batch),
                "refreshed_brief_slots": [
                    str(repartitioned[index]["slot_id"])
                    for index in refreshed_brief_indices
                ],
                "preserved_brief_slots": [
                    str(repartitioned[index]["slot_id"])
                    for index in preserved_brief_indices
                ],
                "carried_forward_neighbor_candidates": carried_forward_candidates,
                "target_resolved": bool(outcome["target_resolved"]),
                "partial_commit": bool(
                    outcome["commit_allowed"]
                    and outcome["queued_unresolved_neighbors"]
                ),
                "queued_unresolved_neighbors": list(
                    outcome["queued_unresolved_neighbors"]
                ),
                "newly_broken_resolved_neighbors": list(
                    outcome["newly_broken_slots"]
                ),
            }
            if post_resegment_proxy_receipt is not None:
                receipt["post_resegment_proxy"] = post_resegment_proxy_receipt
            if outcome["newly_broken_slots"]:
                reason = (
                    "resegmentation would break previously resolved neighbors: "
                    f"{outcome['newly_broken_slots']}"
                )
            elif not outcome["target_resolved"]:
                reason = post_resegment_proxy_failure or (
                    f"target slot {slot_id} remains unresolved after resegmentation"
                )
            elif outcome["queued_unresolved_neighbors"]:
                reason = (
                    f"target slot {slot_id} resolved; changed unresolved neighbors "
                    f"queued independently: {outcome['queued_unresolved_neighbors']}"
                )
            else:
                reason = "local word-boundary resegmentation resolved the visual limit"
            return {
                "status": "resolved" if outcome["commit_allowed"] else "unresolved",
                "strategy": "visual_resegmentation",
                "reason": reason,
                "effective_beats": repartitioned,
                "effective_beats_hash": canonical_hash(repartitioned),
                "effective_briefs_hash": canonical_hash(repaired_briefs),
                "effective_candidates_hash": canonical_hash(committed_candidates),
                "effective_results_hash": canonical_hash(committed_results),
                "updates": updates,
                "updates_hash": canonical_hash(updates),
                "changed_slot_projection_contract": (
                    SEMANTIC_RESEGMENT_PROJECTION_CONTRACT
                ),
                "changed_slot_projection": changed_slot_projection,
                "changed_slot_projection_hash": canonical_hash(
                    changed_slot_projection
                ),
                "rejected_ranges": (
                    [canonical_candidate_range(row) for row in target_batch]
                    if not outcome["target_resolved"]
                    else []
                ),
                "repair_receipt": receipt,
            }

        def durable_repair_handler(
            attempt: int, banned: tuple[Mapping[str, Any], ...]
        ) -> Mapping[str, Any]:
            for prior_attempt in controller.attempts(context.run_id, slot_id):
                if int(prior_attempt.get("attempt") or 0) >= attempt:
                    continue
                prior_result = prior_attempt.get("result")
                if isinstance(prior_result, Mapping):
                    recover_result_repair_sheets(prior_result)
            raw_result = repair_handler(attempt, banned)
            result = dict(raw_result)
            durable_candidates = [
                repair_sheet_candidates[artifact_id]
                for artifact_id in sorted(repair_sheet_candidates)
            ]
            durable_hash = canonical_hash(durable_candidates)
            receipt = result.get("repair_receipt")
            if not isinstance(receipt, Mapping):
                raise ValueError("semantic repair result has no durable receipt")
            result["repair_receipt"] = {
                **dict(receipt),
                "repair_sheet_candidates_hash": durable_hash,
            }
            result["repair_sheet_candidates"] = durable_candidates
            result["repair_sheet_candidates_hash"] = durable_hash
            return result

        try:
            repair_result = controller.repair(
                context.run_id, request, durable_repair_handler
            )
        except RepairExhausted as exc:
            failed_attempts = controller.attempts(context.run_id, slot_id)
            for failed_attempt in failed_attempts:
                failed_result = failed_attempt.get("result")
                if isinstance(failed_result, Mapping):
                    recover_result_repair_sheets(failed_result)
            payload = blocked_payload(
                [slot_id], failed_attempts=failed_attempts
            )
            target = _write_json(context, "semantic_judge.needs_repair.json", payload)
            raise StageBlocked(
                "needs_semantic_repair",
                str(exc),
                result={
                    "status": "unresolved",
                    "unresolved_slots": [slot_id],
                    "artifacts": semantic_stage_artifacts(target),
                },
            ) from exc

        recover_result_repair_sheets(repair_result)

        strategy = str(repair_result.get("strategy") or "")
        if strategy == "next_candidate_batch":
            candidate_rows = repair_result.get("candidate_rows")
            judge_result = repair_result.get("judge_result")
            receipt = repair_result.get("repair_receipt")
            if not isinstance(candidate_rows, list) or not isinstance(
                judge_result, Mapping
            ) or not isinstance(receipt, Mapping):
                raise ValueError("cached candidate-batch repair result is incomplete")
            validated_rows = validate_cached_exact_batch(
                candidate_rows,
                effective_beats[target_index],
                receipt,
            )
            validate_receipt_exact_audit(
                receipt,
                effective_beats[target_index],
            )
            if (
                str(receipt.get("candidate_ranges_hash") or "")
                != canonical_hash(
                    [canonical_candidate_range(row) for row in validated_rows]
                )
                or str(receipt.get("judge_result_hash") or "")
                != canonical_hash(judge_result)
                or not _strict_judge_resolved(judge_result)
            ):
                raise ValueError("cached candidate-batch exact receipt is invalid")
            effective_candidates[target_index] = validated_rows
            results[target_index] = dict(judge_result)
        elif strategy == "semantic_proxy_rebrief":
            replacement_brief = repair_result.get("replacement_brief")
            candidate_rows = repair_result.get("candidate_rows")
            judge_result = repair_result.get("judge_result")
            receipt = repair_result.get("repair_receipt")
            if (
                not isinstance(replacement_brief, Mapping)
                or not isinstance(candidate_rows, list)
                or not isinstance(judge_result, Mapping)
                or not isinstance(receipt, Mapping)
            ):
                raise ValueError("cached semantic proxy re-brief result is incomplete")
            validated_brief = validate_visual_brief_contract(
                dict(effective_beats[target_index]), dict(replacement_brief)
            )
            candidate_ranges = [
                canonical_candidate_range(row) for row in candidate_rows
            ]
            expected_hashes = {
                "replacement_brief_hash": canonical_hash(validated_brief),
                "candidate_ranges_hash": canonical_hash(candidate_ranges),
                "judge_result_hash": canonical_hash(judge_result),
            }
            for key, actual_hash in expected_hashes.items():
                if (
                    actual_hash != str(repair_result.get(key) or "")
                    or actual_hash != str(receipt.get(key) or "")
                ):
                    raise ValueError(f"cached semantic proxy re-brief {key} is invalid")
            if not _strict_judge_resolved(judge_result):
                raise ValueError("cached semantic proxy re-brief is not strictly resolved")
            validated_rows = validate_cached_exact_batch(
                candidate_rows,
                effective_beats[target_index],
                receipt,
            )
            validate_receipt_exact_audit(
                receipt,
                effective_beats[target_index],
            )
            validate_cached_fallback_broad_evidence(
                receipt,
                validated_brief,
                effective_beats[target_index],
                expected_source_brief=effective_briefs[target_index],
            )
            effective_briefs[target_index] = validated_brief
            effective_candidates[target_index] = validated_rows
            results[target_index] = dict(judge_result)
        elif strategy == "visual_resegmentation":
            repaired_beats = repair_result.get("effective_beats")
            if not isinstance(repaired_beats, list) or canonical_hash(
                repaired_beats
            ) != str(repair_result.get("effective_beats_hash") or ""):
                raise ValueError("cached visual resegmentation hash is invalid")
            updates = repair_result.get("updates")
            if (
                not isinstance(updates, list)
                or canonical_hash(updates)
                != str(repair_result.get("updates_hash") or "")
            ):
                raise ValueError("cached visual resegmentation updates are missing")
            cached_receipt = repair_result.get("repair_receipt")
            if not isinstance(cached_receipt, Mapping):
                raise ValueError("cached visual resegmentation receipt is missing")
            committed_beats = json.loads(json.dumps(repaired_beats))
            if (
                str(cached_receipt.get("prior_effective_beats_hash") or "")
                != canonical_hash(effective_beats)
                or str(cached_receipt.get("effective_beats_hash") or "")
                != canonical_hash(committed_beats)
            ):
                raise ValueError(
                    "cached visual resegmentation beat lineage is invalid"
                )
            committed_briefs = json.loads(json.dumps(effective_briefs))
            committed_candidates = json.loads(json.dumps(effective_candidates))
            prior_results = json.loads(json.dumps(results))
            committed_results = json.loads(json.dumps(prior_results))
            by_slot = {
                str(beat["slot_id"]): index
                for index, beat in enumerate(committed_beats)
            }
            cached_bindings_by_slot = cached_receipt.get(
                "exact_candidate_bindings_by_slot"
            )
            if (
                not isinstance(cached_bindings_by_slot, Mapping)
                or str(
                    cached_receipt.get("exact_candidate_bindings_by_slot_hash")
                    or ""
                )
                != canonical_hash(cached_bindings_by_slot)
            ):
                raise ValueError(
                    "cached visual resegmentation exact binding map is invalid"
                )
            changed_indices: list[int] = []
            changed_results: list[dict[str, Any]] = []
            validated_updates: list[dict[str, Any]] = []
            for update in updates:
                if not isinstance(update, Mapping):
                    raise ValueError("cached visual resegmentation update is invalid")
                index = by_slot.get(str(update.get("slot_id") or ""))
                if index is None:
                    raise ValueError("cached visual resegmentation changed an unknown slot")
                brief = update.get("brief")
                rows = update.get("candidate_rows")
                judge_result = update.get("judge_result")
                if (
                    not isinstance(brief, Mapping)
                    or not isinstance(rows, list)
                    or not isinstance(judge_result, Mapping)
                ):
                    raise ValueError("cached visual resegmentation evidence is incomplete")
                if index in changed_indices:
                    raise ValueError("cached visual resegmentation repeats a changed slot")
                slot_id_for_update = str(committed_beats[index]["slot_id"])
                slot_bindings = cached_bindings_by_slot.get(slot_id_for_update)
                if not isinstance(slot_bindings, list):
                    raise ValueError(
                        "cached visual resegmentation slot has no exact bindings"
                    )
                validated_rows = validate_cached_exact_batch(
                    rows,
                    committed_beats[index],
                    {
                        "exact_repair_contract": cached_receipt.get(
                            "exact_repair_contract"
                        ),
                        "exact_candidate_bindings": slot_bindings,
                        "exact_candidate_bindings_hash": canonical_hash(
                            slot_bindings
                        ),
                    },
                )
                validated_brief = validate_visual_brief_contract(
                    dict(committed_beats[index]), dict(brief)
                )
                validated_judge_result = dict(judge_result)
                validated_update = {
                    "slot_id": slot_id_for_update,
                    "brief": validated_brief,
                    "candidate_rows": validated_rows,
                    "judge_result": validated_judge_result,
                }
                committed_briefs[index] = validated_brief
                committed_candidates[index] = validated_rows
                committed_results[index] = validated_judge_result
                changed_indices.append(index)
                changed_results.append(validated_judge_result)
                validated_updates.append(validated_update)
            expected_changed_indices = _semantic_timing_changed_indices(
                effective_beats, committed_beats
            )
            if changed_indices != expected_changed_indices:
                raise ValueError(
                    "cached visual resegmentation updates disagree with timing evidence"
                )
            expected_refreshed_indices = _post_resegment_brief_refresh_indices(
                effective_beats,
                committed_beats,
                target_index=target_index,
                changed_indices=expected_changed_indices,
            )
            refreshed_index_set = set(expected_refreshed_indices)
            expected_preserved_indices = [
                index
                for index in expected_changed_indices
                if index not in refreshed_index_set
            ]
            expected_changed_slots = [
                str(committed_beats[index]["slot_id"])
                for index in expected_changed_indices
            ]
            expected_refreshed_slots = [
                str(committed_beats[index]["slot_id"])
                for index in expected_refreshed_indices
            ]
            expected_preserved_slots = [
                str(committed_beats[index]["slot_id"])
                for index in expected_preserved_indices
            ]
            if set(str(key) for key in cached_bindings_by_slot) != set(
                expected_changed_slots
            ):
                raise ValueError(
                    "cached visual resegmentation exact binding slots disagree"
                )
            validate_cached_exact_batch(
                committed_candidates[target_index],
                committed_beats[target_index],
                cached_receipt,
            )
            cached_exact_audits = cached_receipt.get(
                "exact_rematerialization_audits"
            )
            if (
                not isinstance(cached_exact_audits, Mapping)
                or str(
                    cached_receipt.get("exact_rematerialization_audits_hash")
                    or ""
                )
                != canonical_hash(cached_exact_audits)
                or set(str(key) for key in cached_exact_audits)
                != set(expected_changed_slots)
            ):
                raise ValueError(
                    "cached visual resegmentation rematerialization audit is invalid"
                )
            for changed_index in expected_changed_indices:
                changed_slot_id = str(committed_beats[changed_index]["slot_id"])
                audit_bundle = cached_exact_audits.get(changed_slot_id)
                secondary_key = (
                    "preferred" if changed_index == target_index else "carried_prior"
                )
                if (
                    not isinstance(audit_bundle, Mapping)
                    or set(str(key) for key in audit_bundle)
                    != {"ranked", secondary_key}
                    or not isinstance(audit_bundle.get("ranked"), Mapping)
                ):
                    raise ValueError(
                        "cached visual resegmentation slot audit bundle is invalid"
                    )
                validate_exact_rematerialization_audit(
                    audit_bundle["ranked"],
                    committed_beats[changed_index],
                )
                secondary_audit = audit_bundle.get(secondary_key)
                if secondary_audit is not None:
                    if not isinstance(secondary_audit, Mapping):
                        raise ValueError(
                            "cached visual resegmentation secondary audit is invalid"
                        )
                    validate_exact_rematerialization_audit(
                        secondary_audit,
                        committed_beats[changed_index],
                    )
            cached_post_proxy = cached_receipt.get("post_resegment_proxy")
            if cached_post_proxy is not None:
                cached_post_paid_request = cached_post_proxy.get("paid_request")
                if (
                    not isinstance(cached_post_proxy, Mapping)
                    or not isinstance(cached_post_paid_request, Mapping)
                    or str(cached_post_proxy.get("paid_request_hash") or "")
                    != canonical_hash(cached_post_paid_request)
                    or str(cached_post_proxy.get("policy") or "")
                    != POST_RESEG_SEMANTIC_REPAIR_POLICY_VERSION
                    or str(cached_post_proxy.get("repair_prompt_version") or "")
                    != POST_RESEG_SEMANTIC_PROXY_REPAIR_PROMPT_VERSION
                    or str(cached_post_proxy.get("exact_repair_contract") or "")
                    != EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION
                    or str(cached_post_proxy.get("validation_status") or "")
                    not in {"valid", "invalid"}
                ):
                    raise ValueError(
                        "cached post-resegment semantic proxy receipt is stale"
                    )
                paid_operation_kind = (
                    f"semantic_proxy_rebrief_post_resegment_{slot_id}"
                )
                completed_post_replay = _resolve_exact_paid_replay(
                    context,
                    provider="semantic_llm",
                    operation_kind=paid_operation_kind,
                    request_hash=str(cached_post_proxy["paid_request_hash"]),
                )
                if completed_post_replay["action"] == "uncertain":
                    _raise_paid_reconciliation(
                        provider="semantic_llm",
                        operation_kind=paid_operation_kind,
                        request_hash=str(
                            cached_post_proxy["paid_request_hash"]
                        ),
                        replay=completed_post_replay,
                    )
                if completed_post_replay["action"] != "completed":
                    raise ValueError(
                        "cached post-resegment semantic proxy paid request is stale"
                    )
                completed_raw_post_proxy = completed_post_replay["value"]
                if canonical_hash(completed_raw_post_proxy) != str(
                    cached_post_proxy.get("raw_response_hash") or ""
                ):
                    raise ValueError(
                        "cached post-resegment semantic proxy provider result is stale"
                    )
                paid_source_brief = cached_post_paid_request.get(
                    "fresh_split_brief"
                )
                if not isinstance(paid_source_brief, Mapping):
                    raise ValueError(
                        "cached post-resegment semantic proxy source brief is missing"
                    )
                paid_source_brief = validate_visual_brief_contract(
                    committed_beats[target_index], dict(paid_source_brief)
                )
                current_split_timing_identity = _semantic_timing_beat_identity(
                    committed_beats[target_index]
                )
                if (
                    cached_post_paid_request.get("policy")
                    != POST_RESEG_SEMANTIC_REPAIR_POLICY_VERSION
                    or cached_post_paid_request.get("repair_prompt_version")
                    != POST_RESEG_SEMANTIC_PROXY_REPAIR_PROMPT_VERSION
                    or cached_post_paid_request.get("exact_repair_contract")
                    != EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION
                    or cached_post_paid_request.get("model") != proxy_repair_model
                    or cached_post_paid_request.get("split_effective_beats_hash")
                    != canonical_hash(committed_beats)
                    or cached_post_paid_request.get("split_target_timing_identity")
                    != current_split_timing_identity
                    or cached_post_paid_request.get(
                        "split_target_timing_identity_hash"
                    )
                    != canonical_hash(current_split_timing_identity)
                    or cached_post_paid_request.get("fresh_split_brief_hash")
                    != canonical_hash(paid_source_brief)
                    or str(cached_post_proxy.get("fresh_split_brief_hash") or "")
                    != canonical_hash(paid_source_brief)
                ):
                    raise ValueError(
                        "cached post-resegment semantic proxy paid binding is stale"
                    )
                completed_post_proxy_request = {
                    "beat": dict(committed_beats[target_index]),
                    "current_brief": paid_source_brief,
                    "neighbor_beat_texts": cached_post_paid_request.get(
                        "neighbor_beat_texts"
                    ),
                    "failure_reasons": cached_post_paid_request.get(
                        "failure_reasons"
                    ),
                    "scarce_candidate_context": cached_post_paid_request.get(
                        "scarce_candidate_context"
                    ),
                    "adjacent_anchor_context": cached_post_paid_request.get(
                        "adjacent_anchor_context"
                    ),
                }
                try:
                    completed_validated_post_proxy = (
                        validate_post_resegment_semantic_proxy_repair_response(
                            [completed_post_proxy_request],
                            completed_raw_post_proxy,
                            model=proxy_repair_model,
                        )
                    )
                except ValueError as exc:
                    raise ValueError(
                        "cached post-resegment semantic proxy provider result is invalid"
                    ) from exc
                if len(completed_validated_post_proxy) != 1:
                    raise ValueError(
                        "cached post-resegment semantic proxy provider result is invalid"
                    )
                (
                    completed_replacement_brief,
                    completed_honest_fallback_receipt,
                ) = pop_honest_visible_fallback_receipt(
                    completed_validated_post_proxy[0]
                )
                completed_replacement_brief = validate_visual_brief_contract(
                    committed_beats[target_index], completed_replacement_brief
                )
                cached_honest_bundle = cached_post_proxy.get(
                    "honest_visible_fallback"
                )
                if (
                    not isinstance(cached_honest_bundle, Mapping)
                    or completed_replacement_brief
                    != committed_briefs[target_index]
                    or completed_honest_fallback_receipt
                    != cached_honest_bundle.get("compiler_receipt")
                ):
                    raise ValueError(
                        "cached post-resegment semantic proxy provider output changed"
                    )
                post_candidate_ids = cached_post_proxy.get("candidate_ids") or []
                if not isinstance(post_candidate_ids, list) or any(
                    not isinstance(value, str) or not value
                    for value in post_candidate_ids
                ):
                    raise ValueError(
                        "cached post-resegment semantic proxy candidates are invalid"
                    )
                if post_candidate_ids:
                    if cached_post_proxy.get("validation_status") != "valid":
                        raise ValueError(
                            "cached invalid post-resegment proxy has judged candidates"
                        )
                    target_rows = committed_candidates[target_index]
                    target_result = committed_results[target_index]
                    validated_post_rows = validate_cached_exact_batch(
                        target_rows,
                        committed_beats[target_index],
                        cached_post_proxy,
                    )
                    validate_receipt_exact_audit(
                        cached_post_proxy,
                        committed_beats[target_index],
                        progressive=True,
                    )
                    validate_cached_fallback_broad_evidence(
                        cached_post_proxy,
                        committed_briefs[target_index],
                        committed_beats[target_index],
                        expected_source_brief=paid_source_brief,
                    )
                    if (
                        post_candidate_ids
                        != [str(row.get("clip_id") or "") for row in validated_post_rows]
                        or str(cached_post_proxy.get("candidate_ranges_hash") or "")
                        != canonical_hash(
                            [
                                canonical_candidate_range(row)
                                for row in validated_post_rows
                            ]
                        )
                        or str(cached_post_proxy.get("judge_result_hash") or "")
                        != canonical_hash(target_result)
                        or str(cached_post_proxy.get("replacement_brief_hash") or "")
                        != canonical_hash(committed_briefs[target_index])
                        or cached_post_proxy.get("resolved") is not True
                        or not _strict_judge_resolved(target_result)
                    ):
                        raise ValueError(
                            "cached post-resegment semantic proxy evidence is invalid"
                        )
                elif any(
                    key in cached_post_proxy
                    for key in (
                        "candidate_ranges_hash",
                        "judge_result_hash",
                        "exact_candidate_bindings",
                        "exact_rematerialization_audit",
                    )
                ):
                    # A valid no-candidate terminal can be cached only as a failed
                    # repair attempt, never applied as a resolved resegmentation.
                    raise ValueError(
                        "cached resolved resegmentation has empty post-proxy evidence"
                    )
            _validated_receipt_slot_sequence(
                cached_receipt,
                "changed_slots",
                expected=expected_changed_slots,
            )
            _validated_receipt_slot_sequence(
                cached_receipt,
                "refreshed_brief_slots",
                expected=expected_refreshed_slots,
            )
            _validated_receipt_slot_sequence(
                cached_receipt,
                "preserved_brief_slots",
                expected=expected_preserved_slots,
            )
            if str(cached_receipt.get("post_resegment_policy") or "") != (
                POST_RESEG_SEMANTIC_REPAIR_POLICY_VERSION
            ):
                raise ValueError(
                    "cached visual resegmentation policy version is invalid"
                )
            if any(
                canonical_hash(committed_briefs[index])
                != canonical_hash(effective_briefs[index])
                for index in expected_preserved_indices
            ):
                raise ValueError(
                    "cached visual resegmentation replaced a timing-only neighbor brief"
                )
            updates_hash = canonical_hash(updates)
            if (
                str(repair_result.get("updates_hash") or "") != updates_hash
                or str(cached_receipt.get("updates_hash") or "")
                != updates_hash
                or str(cached_receipt.get("judge_results_hash") or "")
                != canonical_hash(changed_results)
            ):
                raise ValueError(
                    "cached visual resegmentation changed-slot audit is invalid"
                )
            current_projection_hashes = {
                "effective_briefs_hash": canonical_hash(committed_briefs),
                "effective_candidates_hash": canonical_hash(committed_candidates),
                "effective_results_hash": canonical_hash(committed_results),
            }
            historical_hashes: dict[str, str] = {}
            for key in current_projection_hashes:
                result_hash = str(repair_result.get(key) or "")
                receipt_hash = str(cached_receipt.get(key) or "")
                if (
                    not re.fullmatch(r"[0-9a-f]{64}", result_hash)
                    or result_hash != receipt_hash
                ):
                    raise ValueError(
                        f"cached visual resegmentation historical {key} is invalid"
                    )
                historical_hashes[key] = result_hash

            # Legacy rows stored a hash of all ambient slots even though the
            # durable operation only replaces ``changed_slots``.  A later
            # stochastic initial judge can legitimately repair an unrelated
            # slot and change those three full-vector hashes.  Authenticate the
            # exact changed-slot patch and preserve every unrelated current
            # value instead of treating the historical snapshot as an apply
            # precondition.
            expected_projection = _semantic_resegment_changed_slot_projection(
                validated_updates
            )
            expected_projection_hash = canonical_hash(expected_projection)
            projection_contracts = {
                str(repair_result.get("changed_slot_projection_contract") or ""),
                str(cached_receipt.get("changed_slot_projection_contract") or ""),
            }
            projection_fields_present = any(
                key in repair_result or key in cached_receipt
                for key in (
                    "changed_slot_projection_contract",
                    "changed_slot_projection",
                    "changed_slot_projection_hash",
                )
            )
            if projection_fields_present:
                if (
                    projection_contracts
                    != {SEMANTIC_RESEGMENT_PROJECTION_CONTRACT}
                    or repair_result.get("changed_slot_projection")
                    != expected_projection
                    or cached_receipt.get("changed_slot_projection")
                    != expected_projection
                    or str(
                        repair_result.get("changed_slot_projection_hash") or ""
                    )
                    != expected_projection_hash
                    or str(
                        cached_receipt.get("changed_slot_projection_hash") or ""
                    )
                    != expected_projection_hash
                ):
                    raise ValueError(
                        "cached visual resegmentation changed-slot projection is invalid"
                    )
            elif all(
                historical_hashes[key] == current_projection_hashes[key]
                for key in current_projection_hashes
            ):
                # Exact legacy replay retains its former full-vector check.
                pass
            # A legacy ambient-drift replay has no versioned projection field,
            # but all applied content is still reconstructed above from the
            # canonical update order, exact candidate bindings, changed-result
            # hash and preserved-neighbor equality.
            cached_outcome = _resegmentation_resolution_outcome(
                target_index=target_index,
                changed_indices=changed_indices,
                beats=committed_beats,
                prior_results=prior_results,
                repaired_results=changed_results,
            )
            expected_queue = list(cached_outcome["queued_unresolved_neighbors"])
            cached_newly_broken = _validated_receipt_slot_sequence(
                cached_receipt,
                "newly_broken_resolved_neighbors",
                expected=cached_outcome["newly_broken_slots"],
            )
            cached_queue = _validated_receipt_slot_sequence(
                cached_receipt,
                "queued_unresolved_neighbors",
                expected=expected_queue,
            )
            if (
                not cached_outcome["commit_allowed"]
                or bool(cached_receipt.get("target_resolved"))
                != bool(cached_outcome["target_resolved"])
                or cached_newly_broken != list(cached_outcome["newly_broken_slots"])
                or cached_queue != expected_queue
                or bool(cached_receipt.get("partial_commit")) != bool(expected_queue)
            ):
                raise ValueError(
                    "cached visual resegmentation collateral-resolution receipt is invalid"
                )
            effective_beats = committed_beats
            effective_briefs = committed_briefs
            effective_candidates = committed_candidates
            results = committed_results
        else:
            raise ValueError(f"unknown semantic repair strategy: {strategy}")
        processed_repair_slots.add(slot_id)
        provenance = dict(repair_result.get("repair_provenance") or {})
        receipt = repair_result.get("repair_receipt")
        if isinstance(receipt, Mapping):
            provenance["receipt"] = dict(receipt)
        repair_provenance.append(provenance)
        if strategy == "visual_resegmentation" and isinstance(receipt, Mapping):
            # The full cached apply above already bound this sequence to the
            # recomputed collateral outcome.  Legacy receipts may omit the
            # field only when that recomputed queue was empty.
            queued_neighbors = _validated_receipt_slot_sequence(
                receipt, "queued_unresolved_neighbors"
            )
            current_by_slot = {
                str(beat["slot_id"]): index
                for index, beat in enumerate(effective_beats)
            }
            for neighbor_slot in queued_neighbors:
                neighbor_id = str(neighbor_slot or "")
                neighbor_index = current_by_slot.get(neighbor_id)
                if neighbor_index is None:
                    raise ValueError("resegmentation queued an unknown neighbor slot")
                if _strict_judge_resolved(results[neighbor_index]):
                    continue
                if (
                    neighbor_id not in pending_repair_slots
                    and neighbor_id not in processed_repair_slots
                ):
                    repair_queue.append(neighbor_id)
                    pending_repair_slots.add(neighbor_id)

    def apply_cached_collision_attempt(
        result: Mapping[str, Any],
        *,
        expected_attempt: int,
        working_candidates: list[list[dict[str, Any]]],
        working_results: list[dict[str, Any]],
        attempted_rows: list[dict[str, Any]],
        attempted_target_history: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        recover_result_repair_sheets(result)
        receipt = result.get("repair_receipt")
        if (
            not isinstance(receipt, Mapping)
            or str(receipt.get("contract") or "")
            != GLOBAL_COLLISION_REPAIR_POLICY_VERSION
            or str(receipt.get("policy") or "")
            != GLOBAL_COLLISION_REPAIR_POLICY_VERSION
            or str(receipt.get("target_rule") or "")
            != GLOBAL_COLLISION_TARGET_RULE_VERSION
            or int(receipt.get("selectable_candidates_per_slot") or 0)
            != GLOBAL_COLLISION_SELECTABLE_CANDIDATES_PER_SLOT
            or int(receipt.get("attempt") or 0) != expected_attempt
            or str(result.get("strategy") or "")
            != "global_collision_broad_repair"
        ):
            raise ValueError("cached global collision repair receipt is stale")
        eligible_before, diagnostic_before = diagnose_collision_assignment(
            working_candidates, working_results
        )
        stored_diagnostic_before = receipt.get("diagnostic_before")
        if (
            not isinstance(stored_diagnostic_before, Mapping)
            or result.get("diagnostic_before") != stored_diagnostic_before
        ):
            raise ValueError("cached global collision diagnostic-before is stale")
        bound_diagnostic_before = authenticated_cached_collision_diagnostic(
            stored_diagnostic_before,
            diagnostic_before,
            eligible_before,
        )
        if str(receipt.get("diagnostic_before_hash") or "") != canonical_hash(
            bound_diagnostic_before
        ):
            raise ValueError("cached global collision diagnostic-before is stale")
        target_index, target_selection = collision_target_selection(
            diagnostic_before, eligible_before, attempted_target_history
        )
        stored_target_index = result.get("target_index")
        if (
            receipt.get("attempted_target_history_before")
            != attempted_target_history
            or str(receipt.get("attempted_target_history_before_hash") or "")
            != canonical_hash(attempted_target_history)
            or receipt.get("target_selection") != target_selection
            or str(receipt.get("target_selection_hash") or "")
            != canonical_hash(target_selection)
            or type(stored_target_index) is not int
            or stored_target_index != target_index
            or str(result.get("target_slot_id") or "")
            != str(effective_beats[target_index].get("slot_id") or "")
        ):
            raise ValueError("cached global collision target selection is stale")
        frozen_universe = collision_frozen_universe(
            working_candidates, attempted_rows
        )
        broad_bundle = receipt.get("semantic_broad_repair")
        if (
            not isinstance(broad_bundle, Mapping)
            or str(receipt.get("semantic_broad_repair_hash") or "")
            != canonical_hash(broad_bundle)
        ):
            raise ValueError("cached global collision broad evidence is stale")
        broad_rows = validate_cached_collision_broad_repair(
            broad_bundle,
            effective_beats[target_index],
            effective_briefs[target_index],
            frozen_universe,
            attempt=expected_attempt,
            diagnostic_before_hash=canonical_hash(bound_diagnostic_before),
        )
        stored_broad_rows = result.get("broad_candidate_rows")
        broad_bindings = _exact_repair_candidate_bindings(broad_rows)
        if (
            not isinstance(stored_broad_rows, list)
            or _exact_repair_candidate_bindings(stored_broad_rows)
            != broad_bindings
            or receipt.get("broad_candidate_bindings") != broad_bindings
            or str(receipt.get("broad_candidate_bindings_hash") or "")
            != canonical_hash(broad_bindings)
        ):
            raise ValueError("cached global collision broad candidates are stale")
        attempted_rows.extend(broad_rows)
        judge_result = result.get("judge_result")
        judge_operation_kind = (
            f"semantic_collision_repair_judge_{expected_attempt:02d}_"
            f"{effective_beats[target_index]['slot_id']}"
        )
        if broad_rows:
            expected_judge_request = semantic_judge_paid_request(
                [effective_beats[target_index]],
                [effective_briefs[target_index]],
                [broad_rows],
            )
            expected_judge_request_hash = canonical_hash(expected_judge_request)
            if (
                not isinstance(judge_result, Mapping)
                or str(receipt.get("judge_operation_kind") or "")
                != judge_operation_kind
                or str(receipt.get("judge_request_hash") or "")
                != expected_judge_request_hash
                or str(receipt.get("judge_result_hash") or "")
                != canonical_hash(judge_result)
            ):
                raise ValueError("cached global collision judge evidence is stale")
            paid_chain = receipt.get("paid_judge_chain")
            if (
                not isinstance(paid_chain, Mapping)
                or str(receipt.get("paid_judge_chain_hash") or "")
                != canonical_hash(paid_chain)
            ):
                raise ValueError("cached global collision paid judge chain is stale")
            reconstructed, expected_paid_chain = reconstruct_collision_paid_chain(
                judge_operation_kind,
                [effective_beats[target_index]],
                [effective_briefs[target_index]],
                [broad_rows],
            )
            if (
                paid_chain != expected_paid_chain
                or reconstructed != [dict(judge_result)]
            ):
                raise ValueError(
                    "cached global collision paid judge chain is stale"
                )
            accepted_pairs = collision_accepted_rows(
                broad_rows,
                judge_result,
                effective_beats[target_index],
            )
        else:
            if (
                judge_result is not None
                or receipt.get("judge_operation_kind") is not None
                or str(receipt.get("judge_request_hash") or "")
                != canonical_hash(None)
                or str(receipt.get("judge_result_hash") or "")
                != canonical_hash(None)
                or receipt.get("paid_judge_chain") is not None
                or str(receipt.get("paid_judge_chain_hash") or "")
                != canonical_hash(None)
            ):
                raise ValueError("cached blocked collision repair has judge evidence")
            accepted_pairs = []
        merged_rows, merged_result, accepted_rows = merge_collision_acceptance(
            working_candidates[target_index],
            working_results[target_index],
            accepted_pairs,
        )
        stored_accepted = result.get("accepted_candidate_rows")
        accepted_bindings = _exact_repair_candidate_bindings(accepted_rows)
        if (
            not isinstance(stored_accepted, list)
            or _exact_repair_candidate_bindings(stored_accepted)
            != accepted_bindings
            or receipt.get("accepted_candidate_bindings") != accepted_bindings
            or str(receipt.get("accepted_candidate_bindings_hash") or "")
            != canonical_hash(accepted_bindings)
            or result.get("merged_candidate_rows") != merged_rows
            or str(receipt.get("merged_candidate_rows_hash") or "")
            != canonical_hash(merged_rows)
            or result.get("merged_judge_result") != merged_result
            or str(receipt.get("merged_judge_result_hash") or "")
            != canonical_hash(merged_result)
        ):
            raise ValueError("cached global collision merge evidence is stale")
        working_candidates[target_index] = merged_rows
        working_results[target_index] = merged_result
        eligible_after, diagnostic_after = diagnose_collision_assignment(
            working_candidates, working_results
        )
        stored_diagnostic_after = receipt.get("diagnostic_after")
        if (
            not isinstance(stored_diagnostic_after, Mapping)
            or result.get("diagnostic_after") != stored_diagnostic_after
        ):
            raise ValueError("cached global collision diagnostic-after is stale")
        bound_diagnostic_after = authenticated_cached_collision_diagnostic(
            stored_diagnostic_after,
            diagnostic_after,
            eligible_after,
        )
        expected_status = (
            "resolved"
            if diagnostic_after.get("status") == "feasible"
            else "unresolved"
        )
        if (
            str(receipt.get("diagnostic_after_hash") or "")
            != canonical_hash(bound_diagnostic_after)
            or str(result.get("status") or "") != expected_status
            or bool(receipt.get("collision_resolved"))
            != (expected_status == "resolved")
        ):
            raise ValueError("cached global collision diagnostic-after is stale")
        target_outcome = collision_target_outcome(
            attempt=expected_attempt,
            target_index=target_index,
            broad_status=str(broad_bundle.get("status") or ""),
            broad_candidate_count=len(broad_rows),
            accepted_candidate_count=len(accepted_rows),
            diagnostic_before=bound_diagnostic_before,
            diagnostic_after=bound_diagnostic_after,
        )
        if (
            receipt.get("target_outcome") != target_outcome
            or str(receipt.get("target_outcome_hash") or "")
            != canonical_hash(target_outcome)
        ):
            raise ValueError("cached global collision target outcome is stale")
        attempted_target_history.append(target_outcome)
        return diagnostic_after, bound_diagnostic_after

    def replay_collision_attempts(
        attempts: Sequence[Mapping[str, Any]],
    ) -> tuple[
        list[list[dict[str, Any]]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any],
        dict[str, Any],
    ]:
        working_candidates = json.loads(json.dumps(effective_candidates))
        working_results = json.loads(json.dumps(results))
        attempted_rows: list[dict[str, Any]] = []
        attempted_target_history: list[dict[str, Any]] = []
        provenance_rows: list[dict[str, Any]] = []
        _eligible, diagnostic = diagnose_collision_assignment(
            working_candidates, working_results
        )
        binding_diagnostic = diagnostic
        stored_previous_after: Mapping[str, Any] | None = None
        for attempt_row in attempts:
            attempt_number = int(attempt_row.get("attempt") or 0)
            raw_result = attempt_row.get("result")
            if not isinstance(raw_result, Mapping):
                continue
            raw_receipt = raw_result.get("repair_receipt")
            stored_before = (
                raw_receipt.get("diagnostic_before")
                if isinstance(raw_receipt, Mapping)
                else None
            )
            if (
                stored_previous_after is not None
                and stored_before != stored_previous_after
            ):
                raise ValueError(
                    "cached global collision diagnostic chain changes shape"
                )
            diagnostic, binding_diagnostic = apply_cached_collision_attempt(
                raw_result,
                expected_attempt=attempt_number,
                working_candidates=working_candidates,
                working_results=working_results,
                attempted_rows=attempted_rows,
                attempted_target_history=attempted_target_history,
            )
            stored_previous_after = (
                raw_receipt.get("diagnostic_after")
                if isinstance(raw_receipt, Mapping)
                and isinstance(raw_receipt.get("diagnostic_after"), Mapping)
                else None
            )
            provenance = dict(raw_result.get("repair_provenance") or {})
            receipt = raw_result.get("repair_receipt")
            if not provenance or not isinstance(receipt, Mapping):
                raise ValueError("cached global collision provenance is missing")
            provenance["receipt"] = dict(receipt)
            provenance_rows.append(provenance)
        return (
            working_candidates,
            working_results,
            attempted_rows,
            attempted_target_history,
            provenance_rows,
            diagnostic,
            binding_diagnostic,
        )

    unresolved = [
        str(beat["slot_id"])
        for beat, result in zip(effective_beats, results)
        if not _strict_judge_resolved(result)
    ]
    if unresolved:
        payload = blocked_payload(unresolved)
        payload.update(
            {
                "global_assignment_status": "not_evaluated_semantic_unresolved",
                "global_assignment_policy": GLOBAL_COLLISION_REPAIR_POLICY_VERSION,
                "global_assignment_diagnostic": None,
                "global_assignment_diagnostic_hash": canonical_hash(None),
                "collision_repair_provenance": [],
                "collision_repair_provenance_hash": canonical_hash([]),
            }
        )
        target = _write_json(context, "semantic_judge.json", payload)
        raise StageBlocked(
            "needs_semantic_repair",
            f"bounded semantic repair left unresolved slots: {unresolved[:8]}",
            result={
                "status": "unresolved",
                "unresolved_slots": unresolved,
                "artifacts": semantic_stage_artifacts(target),
            },
        )

    initial_collision_eligible, global_diagnostic = diagnose_collision_assignment(
        effective_candidates, results
    )
    collision_provenance: list[dict[str, Any]] = []
    if global_diagnostic.get("status") == "limit_exceeded":
        payload = blocked_payload([])
        payload.update(
            {
                "status": "unresolved",
                "global_assignment_status": "diagnostic_limit_exceeded",
                "global_assignment_policy": GLOBAL_COLLISION_REPAIR_POLICY_VERSION,
                "global_assignment_diagnostic": global_diagnostic,
                "global_assignment_diagnostic_hash": canonical_hash(
                    global_diagnostic
                ),
                "collision_repair_provenance": [],
                "collision_repair_provenance_hash": canonical_hash([]),
            }
        )
        target = _write_json(context, "semantic_judge.json", payload)
        raise StageBlocked(
            "needs_global_collision_diagnostic",
            "global assignment diagnostic exceeded its bounded proof budget",
            result={
                "status": "unresolved",
                "artifacts": semantic_stage_artifacts(target),
            },
        )

    if global_diagnostic.get("status") == "true_infeasible":
        collision_runtime = load_targeted_broad_runtime()
        initial_target_index, initial_target_selection = (
            collision_target_selection(
                global_diagnostic, initial_collision_eligible
            )
        )
        collision_controller = RepairController(
            context.state, max_attempts=collision_repair_max_attempts
        )
        request_initial_diagnostic = global_diagnostic
        existing_collision_attempts = collision_controller.attempts(
            context.run_id, GLOBAL_COLLISION_REPAIR_NAMESPACE
        )
        if existing_collision_attempts:
            first_result = existing_collision_attempts[0].get("result")
            first_receipt = (
                first_result.get("repair_receipt")
                if isinstance(first_result, Mapping)
                else None
            )
            stored_initial_diagnostic = (
                first_receipt.get("diagnostic_before")
                if isinstance(first_receipt, Mapping)
                else None
            )
            if isinstance(stored_initial_diagnostic, Mapping):
                request_initial_diagnostic = (
                    authenticated_cached_collision_diagnostic(
                        stored_initial_diagnostic,
                        global_diagnostic,
                        initial_collision_eligible,
                    )
                )
        collision_request = RepairRequest(
            slot_id=GLOBAL_COLLISION_REPAIR_NAMESPACE,
            reason_code="true_global_collision_infeasible",
            failed_range=canonical_candidate_range(
                initial_collision_eligible[initial_target_index][0]
            ),
            details={
                "policy": GLOBAL_COLLISION_REPAIR_POLICY_VERSION,
                "target_rule": GLOBAL_COLLISION_TARGET_RULE_VERSION,
                "selectable_candidates_per_slot": (
                    GLOBAL_COLLISION_SELECTABLE_CANDIDATES_PER_SLOT
                ),
                # Witness materialization is downstream of collision repair.
                # Keep this request binding stable so a semantic-only witness
                # stage bump replays existing v2 broad/paid repair receipts.
                "semantic_stage_version": GLOBAL_COLLISION_REPAIR_STAGE_VERSION,
                "exact_repair_contract": EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION,
                "semantic_broad_repair_contract": (
                    TARGETED_SEMANTIC_BROAD_REPAIR_CONTRACT_VERSION
                ),
                "paid_chain_contract": GLOBAL_COLLISION_PAID_CHAIN_CONTRACT,
                "max_attempts": collision_repair_max_attempts,
                "selection_input_hash": _semantic_selection_input_hash(
                    effective_beats,
                    effective_briefs,
                    effective_candidates,
                    results,
                ),
                "initial_diagnostic": request_initial_diagnostic,
                "initial_diagnostic_hash": canonical_hash(
                    request_initial_diagnostic
                ),
                "initial_target_selection": initial_target_selection,
                "initial_target_selection_hash": canonical_hash(
                    initial_target_selection
                ),
                "catalog_hash": str(
                    collision_runtime["catalog"].get("content_hash") or ""
                ),
                "catalog_candidate_count": int(
                    collision_runtime["catalog"].get("candidate_count") or 0
                ),
                "model_contract_hash": canonical_hash(
                    collision_runtime["model_contract"]
                ),
                "callback_contract": str(
                    collision_runtime["callback_contract"]
                ),
                "detector_policy_hash": canonical_hash(
                    collision_runtime["detector_policy"]
                ),
                "output_fps": output_fps,
            },
        )

        def collision_repair_handler(
            attempt: int, _banned: tuple[Mapping[str, Any], ...]
        ) -> Mapping[str, Any]:
            prior_attempts = [
                row
                for row in collision_controller.attempts(
                    context.run_id, GLOBAL_COLLISION_REPAIR_NAMESPACE
                )
                if int(row.get("attempt") or 0) < attempt
            ]
            (
                working_candidates,
                working_results,
                attempted_rows,
                attempted_target_history,
                _prior_provenance,
                diagnostic_before,
                diagnostic_before_binding,
            ) = replay_collision_attempts(prior_attempts)
            if diagnostic_before.get("status") != "true_infeasible":
                raise ValueError(
                    "global collision prior attempts have an invalid terminal state"
                )
            eligible_before = collision_eligible_matrix(
                working_candidates, working_results
            )
            target_index, target_selection = collision_target_selection(
                diagnostic_before, eligible_before, attempted_target_history
            )
            frozen_universe = collision_frozen_universe(
                working_candidates, attempted_rows
            )
            broad_rows, broad_bundle = run_collision_broad_repair(
                effective_beats[target_index],
                effective_briefs[target_index],
                frozen_universe,
                attempt=attempt,
                diagnostic_before_hash=canonical_hash(
                    diagnostic_before_binding
                ),
            )
            judge_operation_kind: str | None = None
            judge_request_hash = canonical_hash(None)
            collision_judge: dict[str, Any] | None = None
            collision_paid_chain: dict[str, Any] | None = None
            accepted_pairs: list[
                tuple[dict[str, Any], dict[str, Any]]
            ] = []
            if broad_rows:
                judge_operation_kind = (
                    f"semantic_collision_repair_judge_{attempt:02d}_"
                    f"{effective_beats[target_index]['slot_id']}"
                )
                judge_request_hash = canonical_hash(
                    semantic_judge_paid_request(
                        [effective_beats[target_index]],
                        [effective_briefs[target_index]],
                        [broad_rows],
                    )
                )
                collision_judge = judge_batches(
                    judge_operation_kind,
                    [effective_beats[target_index]],
                    [effective_briefs[target_index]],
                    [broad_rows],
                )[0]
                reconstructed, collision_paid_chain = (
                    reconstruct_collision_paid_chain(
                        judge_operation_kind,
                        [effective_beats[target_index]],
                        [effective_briefs[target_index]],
                        [broad_rows],
                    )
                )
                if reconstructed != [collision_judge]:
                    raise ValueError(
                        "global collision paid schema chain reconstruction is stale"
                    )
                accepted_pairs = collision_accepted_rows(
                    broad_rows,
                    collision_judge,
                    effective_beats[target_index],
                )
            merged_rows, merged_result, accepted_rows = (
                merge_collision_acceptance(
                    working_candidates[target_index],
                    working_results[target_index],
                    accepted_pairs,
                )
            )
            working_candidates[target_index] = merged_rows
            working_results[target_index] = merged_result
            _eligible_after, diagnostic_after = diagnose_collision_assignment(
                working_candidates, working_results
            )
            broad_bindings = _exact_repair_candidate_bindings(broad_rows)
            accepted_bindings = _exact_repair_candidate_bindings(accepted_rows)
            target_outcome = collision_target_outcome(
                attempt=attempt,
                target_index=target_index,
                broad_status=str(broad_bundle.get("status") or ""),
                broad_candidate_count=len(broad_rows),
                accepted_candidate_count=len(accepted_rows),
                diagnostic_before=diagnostic_before_binding,
                diagnostic_after=diagnostic_after,
            )
            receipt = {
                "contract": GLOBAL_COLLISION_REPAIR_POLICY_VERSION,
                "policy": GLOBAL_COLLISION_REPAIR_POLICY_VERSION,
                "target_rule": GLOBAL_COLLISION_TARGET_RULE_VERSION,
                "selectable_candidates_per_slot": (
                    GLOBAL_COLLISION_SELECTABLE_CANDIDATES_PER_SLOT
                ),
                "attempt": attempt,
                "diagnostic_before": diagnostic_before_binding,
                "diagnostic_before_hash": canonical_hash(
                    diagnostic_before_binding
                ),
                "target_selection": target_selection,
                "target_selection_hash": canonical_hash(target_selection),
                "attempted_target_history_before": attempted_target_history,
                "attempted_target_history_before_hash": canonical_hash(
                    attempted_target_history
                ),
                "target_outcome": target_outcome,
                "target_outcome_hash": canonical_hash(target_outcome),
                "semantic_broad_repair": broad_bundle,
                "semantic_broad_repair_hash": canonical_hash(broad_bundle),
                "broad_candidate_bindings": broad_bindings,
                "broad_candidate_bindings_hash": canonical_hash(broad_bindings),
                "judge_operation_kind": judge_operation_kind,
                "judge_request_hash": judge_request_hash,
                "judge_result_hash": canonical_hash(collision_judge),
                "paid_judge_chain": collision_paid_chain,
                "paid_judge_chain_hash": canonical_hash(collision_paid_chain),
                "accepted_candidate_bindings": accepted_bindings,
                "accepted_candidate_bindings_hash": canonical_hash(
                    accepted_bindings
                ),
                "merged_candidate_rows_hash": canonical_hash(merged_rows),
                "merged_judge_result_hash": canonical_hash(merged_result),
                "diagnostic_after": diagnostic_after,
                "diagnostic_after_hash": canonical_hash(diagnostic_after),
                "collision_resolved": diagnostic_after.get("status") == "feasible",
            }
            accepted_range_hashes = {
                str(row.get("candidate_range_hash") or "") for row in accepted_rows
            }
            return {
                "status": (
                    "resolved"
                    if diagnostic_after.get("status") == "feasible"
                    else "unresolved"
                ),
                "strategy": "global_collision_broad_repair",
                "reason": (
                    "global collision repaired with new exact judged ranges"
                    if diagnostic_after.get("status") == "feasible"
                    else "global assignment remains collision-infeasible"
                ),
                "target_index": target_index,
                "target_slot_id": str(
                    effective_beats[target_index].get("slot_id") or ""
                ),
                "diagnostic_before": diagnostic_before_binding,
                "broad_candidate_rows": broad_rows,
                "judge_result": collision_judge,
                "accepted_candidate_rows": accepted_rows,
                "merged_candidate_rows": merged_rows,
                "merged_judge_result": merged_result,
                "diagnostic_after": diagnostic_after,
                "rejected_ranges": [
                    canonical_candidate_range(row)
                    for row in broad_rows
                    if str(row.get("candidate_range_hash") or "")
                    not in accepted_range_hashes
                ],
                "repair_receipt": receipt,
            }

        def durable_collision_repair_handler(
            attempt: int, banned: tuple[Mapping[str, Any], ...]
        ) -> Mapping[str, Any]:
            raw_result = collision_repair_handler(attempt, banned)
            result = dict(raw_result)
            durable_candidates = [
                repair_sheet_candidates[artifact_id]
                for artifact_id in sorted(repair_sheet_candidates)
            ]
            durable_hash = canonical_hash(durable_candidates)
            receipt = result.get("repair_receipt")
            if not isinstance(receipt, Mapping):
                raise ValueError("global collision result has no durable receipt")
            result["repair_receipt"] = {
                **dict(receipt),
                "repair_sheet_candidates_hash": durable_hash,
            }
            result["repair_sheet_candidates"] = durable_candidates
            result["repair_sheet_candidates_hash"] = durable_hash
            return result

        try:
            collision_controller.repair(
                context.run_id,
                collision_request,
                durable_collision_repair_handler,
            )
        except RepairExhausted as exc:
            collision_attempts = collision_controller.attempts(
                context.run_id, GLOBAL_COLLISION_REPAIR_NAMESPACE
            )
            (
                effective_candidates,
                results,
                _attempted_rows,
                _attempted_target_history,
                collision_provenance,
                global_diagnostic,
                _global_diagnostic_binding,
            ) = replay_collision_attempts(collision_attempts)
            payload = blocked_payload([])
            collision_slots = list(
                global_diagnostic.get("irreducible_core_slot_ids") or ()
            )
            payload.update(
                {
                    "status": "unresolved",
                    "unresolved_slots": collision_slots,
                    "global_assignment_status": "collision_repair_exhausted",
                    "global_assignment_policy": (
                        GLOBAL_COLLISION_REPAIR_POLICY_VERSION
                    ),
                    "global_assignment_diagnostic": global_diagnostic,
                    "global_assignment_diagnostic_hash": canonical_hash(
                        global_diagnostic
                    ),
                    "collision_repair_provenance": collision_provenance,
                    "collision_repair_provenance_hash": canonical_hash(
                        collision_provenance
                    ),
                    "collision_failed_attempts": collision_attempts,
                }
            )
            target = _write_json(
                context, "semantic_judge.needs_collision_repair.json", payload
            )
            raise StageBlocked(
                "needs_global_collision_repair",
                f"bounded global collision repair exhausted: {exc}",
                result={
                    "status": "unresolved",
                    "unresolved_slots": collision_slots,
                    "artifacts": semantic_stage_artifacts(target),
                },
            ) from exc

        collision_attempts = collision_controller.attempts(
            context.run_id, GLOBAL_COLLISION_REPAIR_NAMESPACE
        )
        (
            effective_candidates,
            results,
            _attempted_rows,
            _attempted_target_history,
            collision_provenance,
            global_diagnostic,
            _global_diagnostic_binding,
        ) = replay_collision_attempts(collision_attempts)

    if global_diagnostic.get("status") != "feasible":
        raise ValueError("global collision repair did not reach a feasible assignment")
    _final_eligible, recomputed_diagnostic = diagnose_collision_assignment(
        effective_candidates, results
    )
    if recomputed_diagnostic != global_diagnostic:
        raise ValueError("global collision diagnostic changed before materialization")
    initial_assignment_timeline, initial_assignment_materialization = (
        _materialize_global_assignment(
            effective_beats,
            effective_briefs,
            effective_candidates,
            results,
            global_diagnostic,
            output_fps=output_fps,
            configured_beam_width=selection_beam_width,
        )
    )

    # Direct unit/integration callers can bypass the registry and omit the
    # selected-gate media dependencies. Preserve the semantic-only result for
    # those callers; the production DAG always supplies both dependencies, and
    # the versioned render stage still refuses a timeline without explicit
    # accepted transforms.
    if not all(
        isinstance(context.upstream.get(stage_name), Mapping)
        for stage_name in ("donor_index", "overlay_analysis")
    ):
        payload = blocked_payload([])
        payload.update(
            {
                "global_assignment_status": "feasible",
                "global_assignment_policy": GLOBAL_COLLISION_REPAIR_POLICY_VERSION,
                "global_assignment_diagnostic": global_diagnostic,
                "global_assignment_diagnostic_hash": canonical_hash(
                    global_diagnostic
                ),
                "collision_repair_provenance": collision_provenance,
                "collision_repair_provenance_hash": canonical_hash(
                    collision_provenance
                ),
                "global_assignment_materialization": (
                    initial_assignment_materialization
                ),
                "global_assignment_materialization_hash": canonical_hash(
                    initial_assignment_materialization
                ),
            }
        )
        target = _write_json(context, "semantic_judge.json", payload)
        return {
            "status": "resolved",
            "effective_beats_hash": payload["effective_beats_hash"],
            "artifacts": semantic_stage_artifacts(target),
        }

    selected_visual_base_candidates = json.loads(json.dumps(effective_candidates))
    selected_visual_base_results = json.loads(json.dumps(results))
    selected_runtime = load_targeted_broad_runtime()
    selected_controller = RepairController(
        context.state,
        max_attempts=selected_visual_repair_max_attempts + 1,
    )
    initial_selected_clips = [
        dict(slot["selected_clip"])
        for slot in initial_assignment_timeline["slots"]
    ]
    selected_request = RepairRequest(
        slot_id=SELECTED_VISUAL_REPAIR_NAMESPACE,
        reason_code="selected_visual_span_acceptance_required",
        failed_range=canonical_candidate_range(initial_selected_clips[0]),
        details={
            "policy": SELECTED_VISUAL_REPAIR_POLICY_VERSION,
            "attempt_contract": SELECTED_VISUAL_REPAIR_ATTEMPT_CONTRACT,
            "target_rule": SELECTED_VISUAL_REPAIR_TARGET_RULE_VERSION,
            "gate_contract": SELECTED_VISUAL_GATE_RECEIPT_CONTRACT,
            "gate_version": SELECTED_VISUAL_GATE_VERSION,
            "transform_contract": SELECTED_VISUAL_TRANSFORM_CONTRACT,
            "cache_replay_contract": SELECTED_VISUAL_CACHE_REPLAY_CONTRACT,
            "exact_render_span_contract": (
                EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION
            ),
            "semantic_stage_version": SEMANTIC_JUDGE_STAGE_VERSION,
            "semantic_broad_repair_contract": (
                TARGETED_SEMANTIC_BROAD_REPAIR_CONTRACT_VERSION
            ),
            "paid_chain_contract": SELECTED_VISUAL_PAID_CHAIN_CONTRACT,
            "broad_repair_max_attempts": selected_visual_repair_max_attempts,
            "gate_workers": selected_visual_gate_workers,
            "gate_max_decode_bytes": selected_visual_gate_max_decode_bytes,
            "output_fps": output_fps,
            "base_selection_input_hash": _semantic_selection_input_hash(
                effective_beats,
                effective_briefs,
                selected_visual_base_candidates,
                selected_visual_base_results,
            ),
            "initial_assignment_materialization_hash": canonical_hash(
                initial_assignment_materialization
            ),
            "initial_selected_candidate_bindings": (
                _exact_repair_candidate_bindings(initial_selected_clips)
            ),
            "initial_selected_candidate_bindings_hash": canonical_hash(
                _exact_repair_candidate_bindings(initial_selected_clips)
            ),
            "catalog_hash": str(
                selected_runtime["catalog"].get("content_hash") or ""
            ),
            "model_contract_hash": canonical_hash(
                selected_runtime["model_contract"]
            ),
            "callback_contract": str(selected_runtime["callback_contract"]),
            "detector_policy_hash": canonical_hash(
                selected_runtime["detector_policy"]
            ),
        },
    )

    def selected_visual_repair_handler(
        attempt: int,
        _banned: tuple[Mapping[str, Any], ...],
    ) -> Mapping[str, Any]:
        prior_rows = [
            row
            for row in selected_controller.attempts(
                context.run_id, SELECTED_VISUAL_REPAIR_NAMESPACE
            )
            if int(row.get("attempt") or 0) < attempt
        ]
        if prior_rows:
            prior_result = prior_rows[-1].get("result")
            if not isinstance(prior_result, Mapping):
                raise ValueError("selected visual prior repair result is missing")
            (
                working_candidates,
                working_results,
                prior_settlement,
                broad_audits,
            ) = validate_selected_visual_attempt_result(
                prior_result,
                expected_attempt=attempt - 1,
                base_candidate_matrix=selected_visual_base_candidates,
                base_result_matrix=selected_visual_base_results,
            )
            if prior_settlement.get("status") != "needs_repair":
                raise ValueError("selected visual prior attempt is already terminal")
            gate_round_history = [
                dict(row) for row in prior_result.get("gate_round_history") or ()
            ]
        else:
            if attempt != 1:
                raise ValueError("selected visual repair attempt sequence is stale")
            working_candidates = json.loads(
                json.dumps(selected_visual_base_candidates)
            )
            working_results = json.loads(json.dumps(selected_visual_base_results))
            broad_audits = []
            gate_round_history = []

        if attempt > 1:
            eligible_before, diagnostic_before = diagnose_collision_assignment(
                working_candidates, working_results, allow_empty=True
            )
            if diagnostic_before.get("status") != "true_infeasible":
                raise ValueError("selected visual repair baseline is not infeasible")
            target_history = [
                dict(audit["target_outcome"])
                for audit in broad_audits
                if isinstance(audit.get("target_outcome"), Mapping)
            ]
            if len(target_history) != len(broad_audits):
                raise ValueError("selected visual target history is incomplete")
            target_index, target_selection = selected_visual_target_selection(
                diagnostic_before,
                eligible_before,
                target_history,
            )
            attempted_broad_rows = [
                dict(row)
                for audit in broad_audits
                for row in audit.get("broad_candidate_rows") or ()
                if isinstance(row, Mapping)
            ]
            frozen_universe = collision_frozen_universe(
                working_candidates, attempted_broad_rows
            )
            broad_attempt = attempt - 1
            broad_rows, broad_bundle = run_collision_broad_repair(
                effective_beats[target_index],
                effective_briefs[target_index],
                frozen_universe,
                attempt=broad_attempt,
                diagnostic_before_hash=canonical_hash(diagnostic_before),
            )
            target_slot_id = str(
                effective_beats[target_index].get("slot_id") or ""
            )
            broad_gate_rows = evaluate_selected_gate_rows(
                broad_rows,
                slot_ids=[target_slot_id] * len(broad_rows),
            )
            gate_clean_rows = [
                dict(candidate)
                for candidate, gate_row in zip(broad_rows, broad_gate_rows)
                if gate_row.get("status") == "passed"
            ]
            operation_kind: str | None = None
            judge_result: dict[str, Any] | None = None
            paid_chain: dict[str, Any] | None = None
            if gate_clean_rows:
                operation_kind = (
                    f"semantic_selected_visual_repair_judge_{broad_attempt:02d}_"
                    f"{target_slot_id}"
                )
                judge_result, paid_chain = judge_selected_visual_repair_raw(
                    operation_kind,
                    effective_beats[target_index],
                    effective_briefs[target_index],
                    gate_clean_rows,
                )
            _clean_rows, accepted_pairs = selected_visual_accepted_rows(
                broad_rows,
                broad_gate_rows,
                judge_result,
                effective_beats[target_index],
            )
            merged_rows, merged_result, inserted_rows = (
                merge_selected_visual_acceptance(
                    working_candidates[target_index],
                    working_results[target_index],
                    accepted_pairs,
                )
            )
            working_candidates[target_index] = merged_rows
            working_results[target_index] = merged_result
            semantic_accepted_bindings = _exact_repair_candidate_bindings(
                [row for row, _evaluation in accepted_pairs]
            )
            inserted_bindings = _exact_repair_candidate_bindings(inserted_rows)
            target_outcome = {
                "contract": SELECTED_VISUAL_REPAIR_TARGET_OUTCOME_CONTRACT,
                "broad_attempt": broad_attempt,
                "target_index": target_index,
                "target_slot_id": target_slot_id,
                "broad_status": str(broad_bundle.get("status") or ""),
                "broad_candidate_count": len(broad_rows),
                "gate_clean_candidate_count": len(gate_clean_rows),
                "semantic_accepted_candidate_count": len(accepted_pairs),
                "inserted_candidate_count": len(inserted_rows),
                "terminal_catalog_exhausted": len(broad_rows) == 0,
            }
            audit_base = {
                "contract": SELECTED_VISUAL_REPAIR_ATTEMPT_CONTRACT,
                "broad_attempt": broad_attempt,
                "target_index": target_index,
                "target_slot_id": target_slot_id,
                "target_selection": target_selection,
                "target_selection_hash": canonical_hash(target_selection),
                "target_outcome": target_outcome,
                "target_outcome_hash": canonical_hash(target_outcome),
                "diagnostic_before": diagnostic_before,
                "diagnostic_before_hash": canonical_hash(diagnostic_before),
                "frozen_universe": frozen_universe,
                "frozen_universe_hash": canonical_hash(frozen_universe),
                "semantic_broad_repair": broad_bundle,
                "semantic_broad_repair_hash": canonical_hash(broad_bundle),
                "broad_candidate_rows": broad_rows,
                "broad_candidate_rows_hash": canonical_hash(broad_rows),
                "broad_gate_rows": broad_gate_rows,
                "broad_gate_rows_hash": canonical_hash(broad_gate_rows),
                "gate_clean_candidate_bindings": (
                    _exact_repair_candidate_bindings(gate_clean_rows)
                ),
                "gate_clean_candidate_bindings_hash": canonical_hash(
                    _exact_repair_candidate_bindings(gate_clean_rows)
                ),
                "judge_operation_kind": operation_kind,
                "judge_result": judge_result,
                "judge_result_hash": canonical_hash(judge_result),
                "paid_judge_chain": paid_chain,
                "paid_judge_chain_hash": canonical_hash(paid_chain),
                "semantic_accepted_candidate_bindings": (
                    semantic_accepted_bindings
                ),
                "semantic_accepted_candidate_bindings_hash": canonical_hash(
                    semantic_accepted_bindings
                ),
                "inserted_candidate_bindings": inserted_bindings,
                "inserted_candidate_bindings_hash": canonical_hash(
                    inserted_bindings
                ),
                "merged_candidate_rows_hash": canonical_hash(merged_rows),
                "merged_result_hash": canonical_hash(merged_result),
            }
            broad_audits.append(
                {
                    **audit_base,
                    "audit_sha256": canonical_hash(audit_base),
                }
            )

        settlement = settle_selected_visual_assignment(
            working_candidates, working_results
        )
        settlement_document = selected_settlement_document(settlement)
        gate_round_history.extend(
            dict(row) for row in settlement_document["rounds"]
        )
        resolved = settlement_document["status"] == "passed"
        rejected_rows = [
            dict(gate_row["candidate_binding"]["range"])
            for round_row in settlement_document["rounds"]
            for gate_row in round_row.get("gate_rows") or ()
            if gate_row.get("status") == "rejected"
        ]
        if broad_audits:
            rejected_rows.extend(
                canonical_candidate_range(candidate)
                for candidate, gate_row in zip(
                    broad_audits[-1]["broad_candidate_rows"],
                    broad_audits[-1]["broad_gate_rows"],
                )
                if gate_row.get("status") == "rejected"
            )
        receipt_base = {
            "contract": SELECTED_VISUAL_REPAIR_ATTEMPT_CONTRACT,
            "policy": SELECTED_VISUAL_REPAIR_POLICY_VERSION,
            "target_rule": SELECTED_VISUAL_REPAIR_TARGET_RULE_VERSION,
            "attempt": attempt,
            "gate_contract": SELECTED_VISUAL_GATE_RECEIPT_CONTRACT,
            "gate_version": SELECTED_VISUAL_GATE_VERSION,
            "transform_contract": SELECTED_VISUAL_TRANSFORM_CONTRACT,
            "cache_replay_contract": SELECTED_VISUAL_CACHE_REPLAY_CONTRACT,
            "exact_render_span_contract": (
                EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION
            ),
            "gate_workers": selected_visual_gate_workers,
            "gate_max_decode_bytes": selected_visual_gate_max_decode_bytes,
            "effective_candidate_rows_hash": canonical_hash(
                working_candidates
            ),
            "effective_results_hash": canonical_hash(working_results),
            "broad_attempt_audits_hash": canonical_hash(broad_audits),
            "gate_round_history_hash": canonical_hash(gate_round_history),
            "selected_visual_settlement_hash": canonical_hash(
                settlement_document
            ),
            "resolved": resolved,
        }
        receipt = {
            **receipt_base,
            "receipt_sha256": canonical_hash(receipt_base),
        }
        return {
            "status": "resolved" if resolved else "unresolved",
            "strategy": (
                "selected_visual_existing_candidates"
                if attempt == 1
                else "selected_visual_broad_repair"
            ),
            "reason": (
                "every globally selected exact range passed the planned-render gate"
                if resolved
                else "selected visual assignment still needs a new exact range"
            ),
            "effective_candidate_rows": working_candidates,
            "effective_results": working_results,
            "selected_visual_settlement": settlement_document,
            "broad_attempt_audits": broad_audits,
            "gate_round_history": gate_round_history,
            "rejected_ranges": rejected_rows,
            "repair_receipt": receipt,
        }

    def durable_selected_visual_repair_handler(
        attempt: int,
        banned: tuple[Mapping[str, Any], ...],
    ) -> Mapping[str, Any]:
        result = dict(selected_visual_repair_handler(attempt, banned))
        durable_candidates = [
            repair_sheet_candidates[artifact_id]
            for artifact_id in sorted(repair_sheet_candidates)
        ]
        durable_hash = canonical_hash(durable_candidates)
        receipt = result.get("repair_receipt")
        if not isinstance(receipt, Mapping):
            raise ValueError("selected visual repair result has no receipt")
        receipt_base = {
            key: value
            for key, value in receipt.items()
            if key != "receipt_sha256"
        }
        receipt_base["repair_sheet_candidates_hash"] = durable_hash
        result["repair_receipt"] = {
            **receipt_base,
            "receipt_sha256": canonical_hash(receipt_base),
        }
        result["repair_sheet_candidates"] = durable_candidates
        result["repair_sheet_candidates_hash"] = durable_hash
        return result

    try:
        selected_result = selected_controller.repair(
            context.run_id,
            selected_request,
            durable_selected_visual_repair_handler,
        )
    except RepairExhausted as exc:
        failed_attempts = selected_controller.attempts(
            context.run_id, SELECTED_VISUAL_REPAIR_NAMESPACE
        )
        completed_failed_attempts = [
            row
            for row in failed_attempts
            if isinstance(row.get("result"), Mapping)
        ]
        if not completed_failed_attempts:
            raise ValueError("selected visual repair exhaustion lost its audit") from exc
        last_completed_attempt = completed_failed_attempts[-1]
        (
            effective_candidates,
            results,
            failed_settlement,
            failed_broad_audits,
        ) = validate_selected_visual_attempt_result(
            last_completed_attempt["result"],
            expected_attempt=int(last_completed_attempt["attempt"]),
            base_candidate_matrix=selected_visual_base_candidates,
            base_result_matrix=selected_visual_base_results,
        )
        remaining_eligible, recomputed_failed_diagnostic = (
            diagnose_collision_assignment(
                effective_candidates,
                results,
                allow_empty=True,
            )
        )
        if recomputed_failed_diagnostic != failed_settlement["diagnostic"]:
            raise ValueError(
                "selected visual exhausted diagnostic changed on blocker"
            ) from exc
        payload = blocked_payload([])
        remaining_indices = sorted(
            {
                *[
                    index
                    for index, rows in enumerate(remaining_eligible)
                    if not rows
                ],
                *[
                    int(index)
                    for index in failed_settlement["diagnostic"].get(
                        "irreducible_core_indices"
                    )
                    or ()
                ],
            }
        )
        blocked_slots = [
            str(effective_beats[index].get("slot_id") or "")
            for index in remaining_indices
        ]
        broad_attempts_used = len(failed_broad_audits)
        payload.update(
            {
                "status": "unresolved",
                "unresolved_slots": blocked_slots,
                "global_assignment_status": "selected_visual_repair_exhausted",
                "global_assignment_policy": (
                    SELECTED_VISUAL_REPAIR_POLICY_VERSION
                ),
                "global_assignment_diagnostic": failed_settlement[
                    "diagnostic"
                ],
                "global_assignment_diagnostic_hash": canonical_hash(
                    failed_settlement["diagnostic"]
                ),
                "collision_repair_provenance": collision_provenance,
                "collision_repair_provenance_hash": canonical_hash(
                    collision_provenance
                ),
                "selected_visual_broad_attempt_audits": failed_broad_audits,
                "selected_visual_failed_attempts": failed_attempts,
                "selected_visual_broad_attempts_used": broad_attempts_used,
                "selected_visual_broad_attempt_limit": (
                    selected_visual_repair_max_attempts
                ),
                "selected_visual_remaining_slot_ids": blocked_slots,
                "selected_visual_remaining_slot_ids_hash": canonical_hash(
                    blocked_slots
                ),
            }
        )
        target = _write_json(
            context, "semantic_judge.needs_selected_visual_repair.json", payload
        )
        raise StageBlocked(
            "needs_selected_visual_repair",
            "bounded selected visual repair exhausted after "
            f"{broad_attempts_used}/{selected_visual_repair_max_attempts} broad "
            f"attempts; remaining slots: {', '.join(blocked_slots)}; {exc}",
            result={
                "status": "unresolved",
                "unresolved_slots": blocked_slots,
                "broad_attempts_used": broad_attempts_used,
                "broad_attempt_limit": selected_visual_repair_max_attempts,
                "artifacts": semantic_stage_artifacts(target),
            },
        ) from exc

    selected_attempt = int(selected_result.get("attempt") or 0)
    (
        effective_candidates,
        results,
        selected_settlement,
        selected_broad_audits,
    ) = validate_selected_visual_attempt_result(
        selected_result,
        expected_attempt=selected_attempt,
        base_candidate_matrix=selected_visual_base_candidates,
        base_result_matrix=selected_visual_base_results,
    )
    if selected_settlement.get("status") != "passed":
        raise ValueError("selected visual controller resolved without a passed gate")
    global_diagnostic = dict(selected_settlement["diagnostic"])
    assignment_materialization = dict(
        selected_settlement["assignment_materialization"]
    )
    final_gate_rows = [
        dict(row) for row in selected_settlement["final_gate_rows"]
    ]
    selected_attempts = selected_controller.attempts(
        context.run_id, SELECTED_VISUAL_REPAIR_NAMESPACE
    )
    selected_repair_provenance = [
        {
            **dict(row["result"].get("repair_provenance") or {}),
            "receipt": dict(row["result"]["repair_receipt"]),
        }
        for row in selected_attempts
        if isinstance(row.get("result"), Mapping)
    ]
    acceptance_base = {
        "schema_version": 1,
        "contract": SELECTED_VISUAL_ACCEPTANCE_CONTRACT,
        "policy": SELECTED_VISUAL_REPAIR_POLICY_VERSION,
        "status": "passed",
        "gate_contract": SELECTED_VISUAL_GATE_RECEIPT_CONTRACT,
        "gate_version": SELECTED_VISUAL_GATE_VERSION,
        "transform_contract": SELECTED_VISUAL_TRANSFORM_CONTRACT,
        "cache_replay_contract": SELECTED_VISUAL_CACHE_REPLAY_CONTRACT,
        "exact_render_span_contract": (
            EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION
        ),
        "selection_input_hash": _semantic_selection_input_hash(
            effective_beats,
            effective_briefs,
            effective_candidates,
            results,
        ),
        "assignment_materialization_hash": canonical_hash(
            assignment_materialization
        ),
        "settlement_rounds": selected_result["gate_round_history"],
        "settlement_rounds_hash": canonical_hash(
            selected_result["gate_round_history"]
        ),
        "final_gate_rows": final_gate_rows,
        "final_gate_rows_hash": canonical_hash(final_gate_rows),
        "planned_timeline_hash": str(
            selected_settlement["planned_timeline_hash"]
        ),
        "broad_attempt_audits": selected_broad_audits,
        "broad_attempt_audits_hash": canonical_hash(selected_broad_audits),
        "repair_provenance": selected_repair_provenance,
        "repair_provenance_hash": canonical_hash(selected_repair_provenance),
    }
    selected_visual_acceptance = {
        **acceptance_base,
        "receipt_sha256": canonical_hash(acceptance_base),
    }

    payload = blocked_payload([])
    payload.update(
        {
            "global_assignment_status": "feasible",
            "global_assignment_policy": GLOBAL_COLLISION_REPAIR_POLICY_VERSION,
            "global_assignment_diagnostic": global_diagnostic,
            "global_assignment_diagnostic_hash": canonical_hash(global_diagnostic),
            "collision_repair_provenance": collision_provenance,
            "collision_repair_provenance_hash": canonical_hash(
                collision_provenance
            ),
            "global_assignment_materialization": assignment_materialization,
            "global_assignment_materialization_hash": canonical_hash(
                assignment_materialization
            ),
            "selected_visual_acceptance": selected_visual_acceptance,
            "selected_visual_acceptance_hash": canonical_hash(
                selected_visual_acceptance
            ),
        }
    )
    target = _write_json(context, "semantic_judge.json", payload)
    return {
        "status": "resolved",
        "effective_beats_hash": payload["effective_beats_hash"],
        "artifacts": semantic_stage_artifacts(target),
    }


def _selected_visual_planned_timeline_hash(
    timeline: Mapping[str, Any],
) -> str:
    """Rebuild the pre-acceptance timeline that the gate receipt authenticates."""

    planned = json.loads(json.dumps(dict(timeline)))
    # ``stage_global_selection`` authenticates the visual settlement before it
    # attaches the remaining global-selection audit envelope.  Those fields
    # have their own hashes and are not part of the timeline decoded by the
    # selected-span gate, so strip them when reproducing the stored gate hash.
    for key in (
        "selected_visual_acceptance",
        "selected_visual_acceptance_hash",
        "original_beats_hash",
        "effective_beats_hash",
        "selection_input_hash",
        "repair_provenance",
        "global_assignment_policy",
        "global_assignment_diagnostic",
        "global_assignment_diagnostic_hash",
        "collision_repair_provenance",
        "collision_repair_provenance_hash",
        "global_assignment_materialization",
        "global_assignment_materialization_hash",
    ):
        planned.pop(key, None)
    slots = planned.get("slots")
    if not isinstance(slots, list):
        raise ValueError("selected visual planned timeline slots are malformed")
    provenance_keys = {
        "selected_visual_gate_version",
        "selected_visual_gate_receipt_hash",
        "selected_visual_transform_binding_hash",
    }
    for raw_slot in slots:
        if not isinstance(raw_slot, dict):
            raise ValueError("selected visual planned timeline slot is malformed")
        raw_slot.pop("selected_visual_gate", None)
        provenance = raw_slot.get("selection_provenance")
        if isinstance(provenance, dict):
            for key in provenance_keys:
                provenance.pop(key, None)
    return canonical_hash(planned)


def _apply_selected_visual_acceptance(
    timeline: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    *,
    expected_selection_input_hash: str,
    expected_assignment_materialization_hash: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Authenticate final gate receipts and persist their exact render transforms."""

    from .render import (
        DETERMINISTIC_TRANSFORM_POLICY_VERSION,
        build_portable_transform_identity,
        deterministic_transform,
    )
    from .selected_visual_gate import (
        SELECTED_VISUAL_GATE_RECEIPT_CONTRACT,
        SELECTED_VISUAL_GATE_VERSION,
        SelectedVisualGateError,
        validate_selected_visual_gate_receipt,
    )

    output = json.loads(json.dumps(dict(timeline)))
    receipt = json.loads(json.dumps(dict(acceptance)))
    unsigned = dict(receipt)
    receipt_sha256 = str(unsigned.pop("receipt_sha256", "") or "")
    final_gate_rows = receipt.get("final_gate_rows")
    repair_provenance = receipt.get("repair_provenance")
    settlement_rounds = receipt.get("settlement_rounds")
    broad_audits = receipt.get("broad_attempt_audits")
    slots = output.get("slots")
    if (
        receipt_sha256 != canonical_hash(unsigned)
        or receipt.get("schema_version") != 1
        or receipt.get("contract") != SELECTED_VISUAL_ACCEPTANCE_CONTRACT
        or receipt.get("policy") != SELECTED_VISUAL_REPAIR_POLICY_VERSION
        or receipt.get("cache_replay_contract")
        != SELECTED_VISUAL_CACHE_REPLAY_CONTRACT
        or receipt.get("exact_render_span_contract")
        != EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION
        or receipt.get("status") != "passed"
        or receipt.get("gate_contract")
        != SELECTED_VISUAL_GATE_RECEIPT_CONTRACT
        or receipt.get("gate_version") != SELECTED_VISUAL_GATE_VERSION
        or receipt.get("transform_contract")
        != DETERMINISTIC_TRANSFORM_POLICY_VERSION
        or receipt.get("transform_contract")
        != SELECTED_VISUAL_TRANSFORM_CONTRACT
        or str(receipt.get("selection_input_hash") or "")
        != expected_selection_input_hash
        or str(receipt.get("assignment_materialization_hash") or "")
        != expected_assignment_materialization_hash
        or not isinstance(final_gate_rows, list)
        or not isinstance(repair_provenance, list)
        or not isinstance(settlement_rounds, list)
        or not isinstance(broad_audits, list)
        or str(receipt.get("final_gate_rows_hash") or "")
        != canonical_hash(final_gate_rows)
        or str(receipt.get("repair_provenance_hash") or "")
        != canonical_hash(repair_provenance)
        or str(receipt.get("settlement_rounds_hash") or "")
        != canonical_hash(settlement_rounds)
        or str(receipt.get("broad_attempt_audits_hash") or "")
        != canonical_hash(broad_audits)
        or not isinstance(slots, list)
        or len(slots) != len(final_gate_rows)
    ):
        raise ValueError("semantic selected visual acceptance receipt is invalid")
    fps = float(output.get("fps") or 0.0)
    if not math.isfinite(fps) or fps <= 0.0:
        raise ValueError("selected visual acceptance timeline fps is invalid")
    authenticated_rows: list[dict[str, Any]] = []
    for slot, raw_gate_row in zip(slots, final_gate_rows):
        if not isinstance(slot, Mapping) or not isinstance(raw_gate_row, Mapping):
            raise ValueError("selected visual acceptance slot row is malformed")
        gate_row = json.loads(json.dumps(dict(raw_gate_row)))
        gate_unsigned = dict(gate_row)
        gate_row_hash = str(gate_unsigned.pop("row_hash", "") or "")
        selected = slot.get("selected_clip")
        slot_id = str(slot.get("slot_id") or "")
        if not isinstance(selected, Mapping):
            raise ValueError("selected visual acceptance lost selected_clip")
        expected_candidate_binding = _exact_repair_candidate_binding(selected)
        portable_slot = {
            "slot_id": slot_id,
            "selected_clip": dict(selected),
        }
        identity = build_portable_transform_identity(portable_slot, fps=fps)
        transform = deterministic_transform(portable_slot, fps=fps)
        transform_binding_base = {
            "contract": SELECTED_VISUAL_TRANSFORM_CONTRACT,
            "identity": identity,
            "identity_hash": canonical_hash(identity),
            "render_transform": transform,
            "render_transform_hash": canonical_hash(transform),
        }
        transform_binding = {
            **transform_binding_base,
            "binding_sha256": canonical_hash(transform_binding_base),
        }
        raw_gate_receipt = gate_row.get("gate_receipt")
        if not isinstance(raw_gate_receipt, Mapping):
            raise ValueError("selected visual acceptance gate receipt is missing")
        try:
            validated_gate_receipt = validate_selected_visual_gate_receipt(
                raw_gate_receipt,
                expected_cache_key=str(raw_gate_receipt.get("cache_key") or ""),
            )
        except SelectedVisualGateError as exc:
            raise ValueError("selected visual acceptance gate receipt is invalid") from exc
        _validate_selected_visual_gate_candidate_binding(
            validated_gate_receipt, selected
        )
        geometry = (validated_gate_receipt.get("binding") or {}).get(
            "render_geometry_proxy"
        )
        if (
            gate_row_hash != canonical_hash(gate_unsigned)
            or gate_row.get("contract") != SELECTED_VISUAL_ACCEPTANCE_CONTRACT
            or str(gate_row.get("slot_id") or "") != slot_id
            or gate_row.get("candidate_binding") != expected_candidate_binding
            or str(gate_row.get("candidate_binding_hash") or "")
            != canonical_hash(expected_candidate_binding)
            or gate_row.get("render_transform") != transform
            or str(gate_row.get("render_transform_hash") or "")
            != canonical_hash(transform)
            or gate_row.get("transform_binding") != transform_binding
            or str(gate_row.get("transform_binding_hash") or "")
            != canonical_hash(transform_binding)
            or gate_row.get("gate_receipt") != validated_gate_receipt
            or str(gate_row.get("gate_receipt_hash") or "")
            != canonical_hash(validated_gate_receipt)
            or gate_row.get("status") != "passed"
            or gate_row.get("rejection_codes") != []
            or validated_gate_receipt.get("status") != "passed"
            or not isinstance(geometry, Mapping)
            or geometry.get("render_transform") != transform
            or not isinstance(geometry.get("crop_margins"), Mapping)
        ):
            raise ValueError("selected visual acceptance gate binding is stale")
        slot["transform"] = dict(transform)
        exact_render_span = _validated_selected_visual_gate_exact_render_span(
            validated_gate_receipt, selected
        )
        slot["selected_clip"] = {
            **dict(selected),
            "transform": dict(transform),
            "exact_render_span": exact_render_span,
        }
        authenticated_rows.append(gate_row)
    if str(receipt.get("planned_timeline_hash") or "") != (
        _selected_visual_planned_timeline_hash(output)
    ):
        raise ValueError("selected visual acceptance planned timeline is stale")
    for slot, gate_row in zip(slots, authenticated_rows):
        slot["selected_visual_gate"] = gate_row
        provenance = dict(slot.get("selection_provenance") or {})
        provenance.update(
            {
                "selected_visual_gate_version": SELECTED_VISUAL_GATE_VERSION,
                "selected_visual_gate_receipt_hash": str(
                    gate_row["gate_receipt_hash"]
                ),
                "selected_visual_transform_binding_hash": str(
                    gate_row["transform_binding_hash"]
                ),
            }
        )
        slot["selection_provenance"] = provenance
    return output, receipt


def stage_global_selection(context: StageContext) -> Mapping[str, Any]:
    fixture = _fixture_stage(context)
    if fixture is not None:
        return fixture

    original_beats = _json(_artifact_path(context, "beats", "beats"))["beats"]
    retrieval = _json(_artifact_path(context, "temporal_retrieval", "retrieval"))
    judge_document = _json(_artifact_path(context, "semantic_judge", "judge"))
    if str(judge_document.get("visual_brief_contract_version") or "") != (
        VISUAL_BRIEF_CONTRACT_VERSION
    ):
        raise ValueError("semantic judge visual brief contract is stale")
    original_beats_hash = canonical_hash(original_beats)
    if str(judge_document.get("original_beats_hash") or "") != original_beats_hash:
        raise ValueError("semantic judge is bound to different original beats")
    if str(retrieval.get("original_beats_hash") or "") != original_beats_hash:
        raise ValueError("temporal retrieval is bound to different original beats")
    beats = judge_document.get("effective_beats")
    if not isinstance(beats, list) or canonical_hash(beats) != str(
        judge_document.get("effective_beats_hash") or ""
    ):
        raise ValueError("semantic judge effective_beats hash is invalid")
    briefs = judge_document.get("effective_briefs")
    candidates = judge_document.get("effective_ranked_candidates")
    judge = judge_document.get("results")
    if not (
        isinstance(briefs, list)
        and isinstance(candidates, list)
        and isinstance(judge, list)
    ):
        raise ValueError("semantic judge selection arrays are missing")
    selection_input_hash = _semantic_selection_input_hash(
        beats, briefs, candidates, judge
    )
    if selection_input_hash != str(judge_document.get("selection_input_hash") or ""):
        raise ValueError("semantic judge selection input hash is invalid")
    global_assignment_diagnostic = judge_document.get(
        "global_assignment_diagnostic"
    )
    collision_repair_provenance = judge_document.get(
        "collision_repair_provenance"
    )
    assignment_materialization = judge_document.get(
        "global_assignment_materialization"
    )
    selected_visual_acceptance = judge_document.get(
        "selected_visual_acceptance"
    )
    if (
        str(judge_document.get("global_assignment_status") or "") != "feasible"
        or str(judge_document.get("global_assignment_policy") or "")
        != GLOBAL_COLLISION_REPAIR_POLICY_VERSION
        or not isinstance(global_assignment_diagnostic, Mapping)
        or str(global_assignment_diagnostic.get("status") or "") != "feasible"
        or str(judge_document.get("global_assignment_diagnostic_hash") or "")
        != canonical_hash(global_assignment_diagnostic)
        or not isinstance(collision_repair_provenance, list)
        or str(judge_document.get("collision_repair_provenance_hash") or "")
        != canonical_hash(collision_repair_provenance)
        or not isinstance(assignment_materialization, Mapping)
        or str(assignment_materialization.get("contract") or "")
        != GLOBAL_ASSIGNMENT_MATERIALIZATION_POLICY_VERSION
        or str(assignment_materialization.get("semantic_stage_version") or "")
        != SEMANTIC_JUDGE_STAGE_VERSION
        or str(
            judge_document.get("global_assignment_materialization_hash") or ""
        )
        != canonical_hash(assignment_materialization)
        or (
            selected_visual_acceptance is not None
            and (
                not isinstance(selected_visual_acceptance, Mapping)
                or str(
                    judge_document.get("selected_visual_acceptance_hash") or ""
                )
                != canonical_hash(selected_visual_acceptance)
            )
        )
        or (
            selected_visual_acceptance is None
            and judge_document.get("selected_visual_acceptance_hash") is not None
        )
    ):
        raise ValueError("semantic judge global assignment receipt is invalid")
    timeline, validated_materialization = _materialize_global_assignment(
        beats,
        briefs,
        candidates,
        judge,
        global_assignment_diagnostic,
        output_fps=float(context.project.settings.get("fps") or 25.0),
        configured_beam_width=int(
            _settings(context).get("selection_beam_width") or 256
        ),
        expected_receipt=assignment_materialization,
    )
    if validated_materialization != dict(assignment_materialization):
        raise ValueError("global assignment materialization changed on cached apply")
    validated_selected_visual_acceptance: dict[str, Any] | None = None
    if isinstance(selected_visual_acceptance, Mapping):
        timeline, validated_selected_visual_acceptance = (
            _apply_selected_visual_acceptance(
                timeline,
                selected_visual_acceptance,
                expected_selection_input_hash=selection_input_hash,
                expected_assignment_materialization_hash=canonical_hash(
                    assignment_materialization
                ),
            )
        )
    for slot in timeline["slots"]:
        clip = slot["selected_clip"]
        source_asset_id = str(clip.get("source_asset_id") or "")
        if not source_asset_id:
            raise ValueError("selected candidate lost source_asset_id")
        _resolve_asset(context, source_asset_id)
        slot["selected_clip"] = _portable_candidate(clip, source_asset_id)
    timeline["original_beats_hash"] = original_beats_hash
    timeline["effective_beats_hash"] = str(judge_document["effective_beats_hash"])
    timeline["selection_input_hash"] = selection_input_hash
    timeline["repair_provenance"] = list(
        judge_document.get("repair_provenance") or ()
    )
    timeline["global_assignment_policy"] = GLOBAL_COLLISION_REPAIR_POLICY_VERSION
    timeline["global_assignment_diagnostic"] = json.loads(
        json.dumps(global_assignment_diagnostic)
    )
    timeline["global_assignment_diagnostic_hash"] = canonical_hash(
        global_assignment_diagnostic
    )
    timeline["collision_repair_provenance"] = json.loads(
        json.dumps(collision_repair_provenance)
    )
    timeline["collision_repair_provenance_hash"] = canonical_hash(
        collision_repair_provenance
    )
    timeline["global_assignment_materialization"] = json.loads(
        json.dumps(validated_materialization)
    )
    timeline["global_assignment_materialization_hash"] = canonical_hash(
        validated_materialization
    )
    if validated_selected_visual_acceptance is not None:
        timeline["selected_visual_acceptance"] = (
            validated_selected_visual_acceptance
        )
        timeline["selected_visual_acceptance_hash"] = canonical_hash(
            validated_selected_visual_acceptance
        )
    target = _write_json(context, "timeline.json", timeline)
    return {
        "status": "resolved",
        "slot_count": len(timeline["slots"]),
        "artifacts": [_artifact("timeline", target)],
    }


def _hydrate_timeline(context: StageContext, timeline: Mapping[str, Any]) -> dict[str, Any]:
    output = json.loads(json.dumps(timeline))
    for slot in output.get("slots") or ():
        slot["selected_clip"] = _hydrate_candidate(context, slot["selected_clip"])
    return output


def stage_acoustic_plan(context: StageContext) -> Mapping[str, Any]:
    fixture = _fixture_stage(context)
    if fixture is not None:
        return fixture
    from .sound import build_sound_plan_v2
    from .sound_planner import (
        ACOUSTIC_RAW_CONTRACT,
        ACOUSTIC_RAW_CONTRACT_VERSION,
        DEFAULT_ACOUSTIC_PLANNER_VERSION,
        make_acoustic_evidence,
        normalize_continuous_sound_lanes_raw,
        plan_music_segments,
        request_acoustic_scenes_raw,
    )

    timeline = _json(_artifact_path(context, "global_selection", "timeline"))
    runtime_timeline = _hydrate_timeline(context, timeline)
    evidence = make_acoustic_evidence(
        runtime_timeline,
        context.attempt_root / "acoustic_evidence",
        workers=int(_settings(context).get("acoustic_evidence_workers") or 6),
    )
    evidence_hashes = {
        key: {
            "contact_sheet_sha256": value["contact_sheet_sha256"],
            "motion_preview_sha256": value["motion_preview_sha256"],
        }
        for key, value in evidence.items()
    }
    try:
        music_model = str(_settings(context).get("music_planner_model") or "gpt-5-nano")
        music = _paid_call(
            context,
            provider="semantic_llm",
            operation_kind="music_mood_plan",
            request={"model": music_model, "timeline": timeline},
            call=lambda: plan_music_segments(timeline, model=music_model),
        )
        acoustic_model = str(_settings(context).get("acoustic_planner_model") or "gpt-5-mini")
        acoustic_batch_size = 6
        acoustic_planner_version = DEFAULT_ACOUSTIC_PLANNER_VERSION
        raw_acoustic = _paid_call(
            context,
            provider="semantic_llm",
            operation_kind="visual_acoustic_plan",
            request={
                "model": acoustic_model,
                "timeline": timeline,
                "evidence_hashes": evidence_hashes,
                "raw_contract": ACOUSTIC_RAW_CONTRACT,
                "raw_contract_version": ACOUSTIC_RAW_CONTRACT_VERSION,
                "planner_version": acoustic_planner_version,
                "batch_size": acoustic_batch_size,
            },
            call=lambda: request_acoustic_scenes_raw(
                timeline,
                evidence,
                model=acoustic_model,
                batch_size=acoustic_batch_size,
                planner_version=acoustic_planner_version,
            ),
        )
    except Exception as exc:
        raise StageBlocked("needs_acoustic_provider", f"acoustic planner failed: {exc}") from exc
    try:
        lanes = normalize_continuous_sound_lanes_raw(
            timeline,
            evidence,
            raw_acoustic,
        )
    except Exception as exc:
        raise StageBlocked(
            "needs_acoustic_plan_validation",
            f"completed acoustic provider result failed local validation: {exc}",
        ) from exc
    plan = build_sound_plan_v2(
        timeline,
        music,
        lanes["ambience_regions"],
        lanes["presence_layers"],
        lanes["event_cues"],
        acoustic_scenes=lanes["acoustic_scenes"],
        status="needs_sound",
    )
    # Absolute donor evidence paths are runtime-only. Hashes/evidence keys are durable.
    for scene in plan.get("acoustic_scenes") or ():
        artefact = scene.get("evidence_artifact")
        if isinstance(artefact, dict):
            artefact.pop("source_path", None)
    evidence_artifacts = []
    for slot_id, row in evidence.items():
        for kind in ("contact_sheet", "motion_preview_path"):
            path = Path(str(row[kind]))
            evidence_artifacts.append(
                _artifact(f"acoustic-{slot_id}-{kind}", path, slot_id=slot_id, kind=kind)
            )
    target = _write_json(context, "sound_plan.planned.json", plan)
    return {
        "status": "needs_sound",
        "artifacts": [_artifact("sound_plan", target), *evidence_artifacts],
    }


def _portable_sound_plan(plan: Mapping[str, Any]) -> None:
    for lane in ("music_segments", "ambience_regions", "presence_layers", "event_cues"):
        for row in plan.get(lane) or ():
            asset = row.get("asset")
            if isinstance(asset, Mapping) and Path(str(asset.get("path") or "x")).is_absolute():
                raise ValueError(f"portable sound plan {lane} contains an absolute asset path")


def _sound_resolution_block(
    context: StageContext,
    detail: str,
    *,
    diagnostics: Mapping[str, Any],
    plan: Mapping[str, Any] | None = None,
) -> None:
    """Persist portable miss evidence and stop at one explicit terminal."""

    portable_diagnostics = json.loads(json.dumps(dict(diagnostics)))
    report = {
        "schema_version": 1,
        "status": "needs_sound",
        "detail": str(detail),
        "diagnostics": portable_diagnostics,
    }
    artifacts = [
        _artifact(
            "sound_resolution_diagnostics",
            _write_json(context, "sound_resolution.diagnostics.json", report),
        )
    ]
    if plan is not None:
        _portable_sound_plan(plan)
        artifacts.insert(
            0,
            _artifact(
                "sound_plan",
                _write_json(context, "sound_plan.needs_sound.json", plan),
            ),
        )
    raise StageBlocked(
        "needs_sound",
        detail,
        result={
            "status": "needs_sound",
            "diagnostics": portable_diagnostics,
            "artifacts": artifacts,
        },
    )


def _catalog_rows(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = catalog.get("assets")
    if isinstance(raw, Mapping):
        rows = []
        for key, value in raw.items():
            if isinstance(value, Mapping):
                row = dict(value)
                row.setdefault("asset_id", str(key))
                rows.append(row)
        return rows
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [dict(row) for row in raw if isinstance(row, Mapping)]
    return []


def _catalog_identity_keys(row: Mapping[str, Any]) -> set[str]:
    provenance = row.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    return {
        value
        for value in (
            str(row.get("asset_id") or "").strip(),
            str(row.get("canonical_id") or "").strip(),
            str(row.get("sha256") or "").strip().casefold(),
            str(provenance.get("provider_asset_id") or "").strip(),
            str(provenance.get("asset_id") or "").strip(),
        )
        if value
    }


def _catalog_row_sha256(row: Mapping[str, Any]) -> str:
    digest = str(row.get("sha256") or "").strip().casefold()
    if not digest:
        canonical_id = str(row.get("canonical_id") or "").strip().casefold()
        if canonical_id.startswith("sha256:"):
            digest = canonical_id.split(":", 1)[1]
    return digest


def _valid_sha256(value: object) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("mapping sha256 must contain 64 hexadecimal characters")
    return digest


def _runtime_catalog_mapping(
    context: StageContext,
    catalog: Mapping[str, Any],
    catalog_path: Path,
    mapping_document: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Validate a portable mapping, then add verified paths to a runtime copy."""

    if mapping_document.get("schema_version") != 1:
        raise ValueError("audio catalog mapping schema_version must be 1")
    catalog_id = str(catalog.get("catalog_id") or "").strip()
    binding = mapping_document.get("catalog_binding")
    if isinstance(binding, Mapping):
        mapping_catalog_id = str(binding.get("catalog_id") or "").strip()
        mapping_catalog_sha256 = binding.get("portable_catalog_sha256")
    else:
        # Backward-compatible input for early hand-written catalog mappings.
        mapping_catalog_id = str(mapping_document.get("catalog_id") or "").strip()
        mapping_catalog_sha256 = mapping_document.get("catalog_sha256")
    if not catalog_id or mapping_catalog_id != catalog_id:
        raise ValueError("audio catalog mapping catalog_id does not match the catalog")
    if _valid_sha256(mapping_catalog_sha256) != sha256_file(catalog_path):
        raise ValueError("audio catalog mapping is bound to a different catalog hash")
    raw_mapping = mapping_document.get("assets")
    if not isinstance(raw_mapping, Mapping):
        raise ValueError("audio catalog mapping assets must be an object")

    rows = _catalog_rows(catalog)
    runtime: dict[str, dict[str, Any]] = {}
    for raw_identity, raw_override in raw_mapping.items():
        identity = str(raw_identity).strip()
        if not identity or not isinstance(raw_override, Mapping):
            raise ValueError("each audio catalog mapping entry must be an object")
        override = dict(raw_override)
        if any(key in override for key in ("path", "relative_path", "canonical_path")):
            raise ValueError("audio catalog mapping entries must not contain paths")
        asset_id = str(override.get("asset_id") or "").strip()
        if not asset_id:
            raise ValueError(f"audio catalog mapping {identity!r} has no project asset_id")
        expected_sha256 = _valid_sha256(override.get("sha256"))
        matches = [row for row in rows if identity in _catalog_identity_keys(row)]
        if len(matches) != 1:
            raise ValueError(
                f"audio catalog mapping identity {identity!r} is missing or ambiguous"
            )
        if _valid_sha256(_catalog_row_sha256(matches[0])) != expected_sha256:
            raise ValueError(f"audio catalog mapping {identity!r} hash disagrees with catalog")
        project_matches = [row for row in context.project.assets if row.asset_id == asset_id]
        if len(project_matches) != 1 or project_matches[0].sha256 != expected_sha256:
            raise ValueError(
                f"audio catalog mapping {identity!r} hash disagrees with project asset"
            )
        runtime_path = _resolve_asset(context, asset_id)
        runtime[identity] = {**override, "path": str(runtime_path)}
    return runtime


def stage_sound_resolution(context: StageContext) -> Mapping[str, Any]:
    fixture = _fixture_stage(context)
    if fixture is not None:
        return fixture
    from .catalog_resolver import resolve_sound_plan_from_catalog
    from .sound import SoundPlanError, validate_sound_plan

    settings = _settings(context)
    ready_id = str(settings.get("ready_sound_plan_asset_id") or "").strip()
    catalog_asset_id = str(settings.get("audio_catalog_asset_id") or "").strip()
    source = "ready_sound_plan"
    try:
        if ready_id:
            # Explicit manual imports remain a supported override for already
            # resolved projects and old checkpoints.
            plan = _json(_resolve_asset(context, ready_id))
        elif catalog_asset_id:
            source = "audio_catalog"
            planned = _json(_artifact_path(context, "acoustic_plan", "sound_plan"))
            catalog_path = _resolve_asset(context, catalog_asset_id)
            catalog = _json(catalog_path)
            mapping_asset_id = str(
                settings.get("audio_catalog_mapping_asset_id") or ""
            ).strip()
            runtime_mapping: Mapping[str, Any] = {}
            if mapping_asset_id:
                mapping_document = _json(_resolve_asset(context, mapping_asset_id))
                runtime_mapping = _runtime_catalog_mapping(
                    context, catalog, catalog_path, mapping_document
                )
            plan = resolve_sound_plan_from_catalog(
                planned,
                catalog,
                catalog_root=catalog_path.parent,
                asset_mapping=runtime_mapping,
            )
        else:
            _sound_resolution_block(
                context,
                "no resolved sound plan or portable audio catalog is configured",
                diagnostics={
                    "source": "configuration",
                    "needs_sound": [
                        {
                            "reason_code": "sound_source_not_configured",
                            "required_one_of": [
                                "settings.production.audio_catalog_asset_id",
                                "settings.production.ready_sound_plan_asset_id",
                            ],
                        }
                    ],
                },
            )
    except StageBlocked as exc:
        if exc.status == "needs_sound":
            raise
        _sound_resolution_block(
            context,
            exc.detail,
            diagnostics={
                "source": source,
                "needs_sound": [
                    {"reason_code": "project_sound_asset_unavailable", "detail": exc.detail}
                ],
            },
        )
    except (SoundPlanError, ValueError) as exc:
        _sound_resolution_block(
            context,
            f"sound catalog resolution failed: {exc}",
            diagnostics={
                "source": source,
                "needs_sound": [
                    {"reason_code": "sound_catalog_resolution_failed", "detail": str(exc)}
                ],
            },
        )

    try:
        _portable_sound_plan(plan)
    except ValueError as exc:
        _sound_resolution_block(
            context,
            str(exc),
            diagnostics={
                "source": source,
                "needs_sound": [
                    {"reason_code": "nonportable_sound_plan", "detail": str(exc)}
                ],
            },
        )
    if plan.get("status") != "ready":
        resolution_diagnostics = plan.get("resolution_diagnostics")
        diagnostics = (
            dict(resolution_diagnostics)
            if isinstance(resolution_diagnostics, Mapping)
            else {
                "source": source,
                "needs_sound": [{"reason_code": "sound_plan_not_ready"}],
            }
        )
        _sound_resolution_block(
            context,
            "sound resolution is incomplete; see needs_sound diagnostics",
            diagnostics=diagnostics,
            plan=plan,
        )

    timeline = _json(_artifact_path(context, "global_selection", "timeline"))
    try:
        validate_sound_plan(
            _hydrate_sound_plan(context, plan),
            # Sound provenance needs runtime audio paths, while acoustic-scene
            # evidence is deliberately bound to the portable donor identity.
            timeline,
            require_resolved_assets=True,
            require_production_v2=True,
            require_causal_compilation=True,
        )
    except StageBlocked as exc:
        _sound_resolution_block(
            context,
            exc.detail,
            diagnostics={
                "source": source,
                "needs_sound": [
                    {"reason_code": "resolved_asset_unavailable", "detail": exc.detail}
                ],
            },
            plan=plan,
        )
    except SoundPlanError as exc:
        _sound_resolution_block(
            context,
            f"resolved sound plan failed validation: {exc}",
            diagnostics={
                "source": source,
                "needs_sound": [
                    {"reason_code": "resolved_plan_invalid", "detail": str(exc)}
                ],
            },
            plan=plan,
        )
    target = _write_json(context, "sound_plan.ready.json", plan)
    return {"status": "ready", "artifacts": [_artifact("sound_plan", target)]}


def _refresh_runtime_sound_plan_key(plan: dict[str, Any]) -> None:
    """Bind a transient hydrated plan to its transient paths without persisting it."""

    schema_version = plan.get("schema_version")
    if isinstance(schema_version, bool) or schema_version not in (1, 2):
        return
    from .sound import stable_cache_key

    core = json.loads(json.dumps(plan))
    core.pop("plan_cache_key", None)
    plan["plan_cache_key"] = stable_cache_key(
        "sound_plan", core, version=schema_version
    )


def _hydrate_sound_plan(context: StageContext, plan: Mapping[str, Any]) -> dict[str, Any]:
    output = json.loads(json.dumps(plan))
    for lane in ("music_segments", "ambience_regions", "presence_layers", "event_cues"):
        for row in output.get(lane) or ():
            asset = row.get("asset")
            if not isinstance(asset, dict) or not asset.get("asset_id"):
                raise StageBlocked("needs_sound_assets", f"{lane} item has no portable asset_id")
            asset["path"] = str(_resolve_asset(context, str(asset["asset_id"])))
    _refresh_runtime_sound_plan_key(output)
    return output


def _runtime_audio_duration(probe: Mapping[str, Any], label: str) -> float:
    streams = [
        row
        for row in probe.get("streams") or ()
        if isinstance(row, Mapping) and row.get("codec_type") == "audio"
    ]
    if not streams:
        raise StageBlocked("needs_sound_assets", f"{label} has no audio stream")
    values: list[float] = []
    for row in streams:
        try:
            values.append(float(row.get("duration")))
        except (TypeError, ValueError):
            pass
    fmt = probe.get("format")
    if isinstance(fmt, Mapping):
        try:
            values.append(float(fmt.get("duration")))
        except (TypeError, ValueError):
            pass
    duration = max((value for value in values if value > 0.0), default=0.0)
    if duration <= 0.0:
        raise StageBlocked(
            "needs_sound_assets", f"{label} has no trustworthy positive duration"
        )
    return duration


def _verified_runtime_sound_plan(
    context: StageContext,
    plan: Mapping[str, Any],
    *,
    ffprobe: str,
) -> dict[str, Any]:
    """Hydrate and verify every paid/licensed asset before FFmpeg sees it."""

    from .render import probe_media

    runtime = _hydrate_sound_plan(context, plan)
    verified_by_path: dict[str, tuple[str, float]] = {}
    for lane in ("music_segments", "ambience_regions", "presence_layers", "event_cues"):
        for index, row in enumerate(runtime.get(lane) or ()):
            asset = row.get("asset")
            if not isinstance(asset, dict):
                raise StageBlocked(
                    "needs_sound_assets", f"{lane}[{index}] has no resolved asset"
                )
            path = str(asset.get("path") or "")
            provenance = asset.get("provenance")
            expected = (
                str(provenance.get("sha256") or "").casefold()
                if isinstance(provenance, Mapping)
                else ""
            )
            if path not in verified_by_path:
                actual = sha256_file(path)
                if not expected or actual != expected:
                    raise StageBlocked(
                        "needs_sound_asset_reconciliation",
                        f"{lane}[{index}] asset hash disagrees with its license receipt",
                    )
                duration = _runtime_audio_duration(
                    probe_media(path, ffprobe=ffprobe), f"{lane}[{index}]"
                )
                verified_by_path[path] = (actual, duration)
            actual, duration = verified_by_path[path]
            if expected != actual:
                raise StageBlocked(
                    "needs_sound_asset_reconciliation",
                    f"{lane}[{index}] reuses a path with conflicting provenance",
                )
            asset["duration"] = duration
            source_offset = float(row.get("source_offset", asset.get("source_offset", 0.0)) or 0.0)
            if source_offset < 0.0 or source_offset >= duration:
                raise StageBlocked(
                    "needs_sound_asset_reconciliation",
                    f"{lane}[{index}] source_offset lies outside the verified asset",
                )
    _refresh_runtime_sound_plan_key(runtime)
    return runtime


_PORTABLE_DROP = object()


def _portable_runtime_value(value: Any, *, key: str = "") -> Any:
    """Strip runtime paths recursively from a durable manifest value."""

    path_keys = {
        "argv",
        "command",
        "concat_file",
        "path",
        "source_path",
        "output_path",
        "input_path",
        "video_path",
        "narration_path",
        "music_path",
        "ambience_path",
        "presence_path",
        "events_path",
        "premaster_path",
    }
    if key.casefold() in path_keys or key.casefold().endswith("_path"):
        return _PORTABLE_DROP
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        return _PORTABLE_DROP if Path(value).is_absolute() else value
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for child_key, child_value in value.items():
            portable = _portable_runtime_value(child_value, key=str(child_key))
            if portable is not _PORTABLE_DROP:
                output[str(child_key)] = portable
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        output_list = []
        for child in value:
            portable = _portable_runtime_value(child)
            if portable is not _PORTABLE_DROP:
                output_list.append(portable)
        return output_list
    return value


def _portable_render_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    output = _portable_runtime_value(manifest)
    if not isinstance(output, dict):
        raise RuntimeError("render manifest did not reduce to a portable object")
    if isinstance(output.get("output"), dict):
        output["output"]["artifact_id"] = "master"
    output.pop("timeline", None)
    encoded = json.dumps(output, ensure_ascii=False, sort_keys=True)
    for token in encoded.replace('\\\\', '/').split('"'):
        if token.startswith("/"):
            raise RuntimeError("portable render manifest still contains an absolute path")
    return output


def _validated_explicit_render_plan(
    timeline: Mapping[str, Any], *, fps: float, require_selected_acceptance: bool = False
) -> list[dict[str, Any]]:
    """Authenticate the path-free transform/crop contract before FFmpeg runs."""

    from .render import (
        DETERMINISTIC_TRANSFORM_POLICY_VERSION,
        RenderError,
        build_portable_transform_identity,
        deterministic_transform,
    )
    from .selected_visual_gate import (
        SELECTED_VISUAL_GATE_RECEIPT_CONTRACT,
        SELECTED_VISUAL_GATE_VERSION,
        SelectedVisualGateError,
        validate_selected_visual_gate_receipt,
    )

    raw_slots = timeline.get("slots")
    if not isinstance(raw_slots, list) or not raw_slots:
        raise StageBlocked(
            "needs_selected_visual_repair",
            "render timeline has no selected slots",
        )
    transform_keys = {
        "preset",
        "seed",
        "hflip",
        "scale",
        "x_offset",
        "y_offset",
        "contrast",
        "saturation",
        "brightness",
        "grain",
        "transform_version",
    }
    acceptance = timeline.get("selected_visual_acceptance")
    has_acceptance = isinstance(acceptance, Mapping)
    if require_selected_acceptance and not has_acceptance:
        raise StageBlocked(
            "needs_selected_visual_repair",
            "production render requires authenticated selected visual acceptance",
        )
    acceptance_rows: list[Any] = []
    if has_acceptance:
        assert isinstance(acceptance, Mapping)
        try:
            planned_timeline_hash = _selected_visual_planned_timeline_hash(
                timeline
            )
        except ValueError as exc:
            raise StageBlocked(
                "needs_selected_visual_repair",
                f"production render planned timeline is malformed: {exc}",
            ) from exc
        acceptance_unsigned = json.loads(json.dumps(dict(acceptance)))
        acceptance_sha256 = str(
            acceptance_unsigned.pop("receipt_sha256", "") or ""
        )
        raw_acceptance_rows = acceptance.get("final_gate_rows")
        if (
            acceptance_sha256 != canonical_hash(acceptance_unsigned)
            or acceptance.get("schema_version") != 1
            or timeline.get("selected_visual_acceptance_hash")
            != canonical_hash(acceptance)
            or acceptance.get("contract")
            != SELECTED_VISUAL_ACCEPTANCE_CONTRACT
            or acceptance.get("policy")
            != SELECTED_VISUAL_REPAIR_POLICY_VERSION
            or acceptance.get("cache_replay_contract")
            != SELECTED_VISUAL_CACHE_REPLAY_CONTRACT
            or acceptance.get("exact_render_span_contract")
            != EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION
            or acceptance.get("status") != "passed"
            or acceptance.get("gate_contract")
            != SELECTED_VISUAL_GATE_RECEIPT_CONTRACT
            or acceptance.get("gate_version") != SELECTED_VISUAL_GATE_VERSION
            or acceptance.get("transform_contract")
            != DETERMINISTIC_TRANSFORM_POLICY_VERSION
            or acceptance.get("transform_contract")
            != SELECTED_VISUAL_TRANSFORM_CONTRACT
            or str(acceptance.get("planned_timeline_hash") or "")
            != planned_timeline_hash
            or not isinstance(raw_acceptance_rows, list)
            or len(raw_acceptance_rows) != len(raw_slots)
            or str(acceptance.get("final_gate_rows_hash") or "")
            != canonical_hash(raw_acceptance_rows or [])
        ):
            raise StageBlocked(
                "needs_selected_visual_repair",
                "production render selected visual acceptance is stale",
            )
        acceptance_rows = list(raw_acceptance_rows)
    rows: list[dict[str, Any]] = []
    for index, raw_slot in enumerate(raw_slots):
        if not isinstance(raw_slot, Mapping):
            raise StageBlocked(
                "needs_selected_visual_repair",
                f"render slot {index} is malformed",
            )
        slot = dict(raw_slot)
        slot_id = str(slot.get("slot_id") or "").strip()
        selected = slot.get("selected_clip")
        transform = slot.get("transform")
        if (
            not slot_id
            or not isinstance(selected, Mapping)
            or not isinstance(transform, Mapping)
            or set(transform) != transform_keys
            or selected.get("transform") != transform
        ):
            raise StageBlocked(
                "needs_selected_visual_repair",
                f"slot {slot_id or index} lacks one explicit selected transform",
            )
        try:
            normalized_transform = deterministic_transform(
                slot, transform, fps=fps
            )
        except (RenderError, TypeError, ValueError, OverflowError) as exc:
            raise StageBlocked(
                "needs_selected_visual_repair",
                f"slot {slot_id} explicit transform is invalid: {exc}",
            ) from exc
        if (
            normalized_transform != transform
            or transform.get("transform_version")
            != DETERMINISTIC_TRANSFORM_POLICY_VERSION
        ):
            raise StageBlocked(
                "needs_selected_visual_repair",
                f"slot {slot_id} explicit transform is not portable/canonical",
            )

        gate_row = slot.get("selected_visual_gate")
        expected_crop: dict[str, Any] | None = None
        expected_exact_span: dict[str, Any] | None = None
        if has_acceptance:
            if (
                not isinstance(gate_row, Mapping)
                or gate_row != acceptance_rows[index]
            ):
                raise StageBlocked(
                    "needs_selected_visual_repair",
                    f"slot {slot_id} selected gate binding is missing",
                )
            portable_slot = {
                "slot_id": slot_id,
                "selected_clip": dict(selected),
            }
            try:
                portable_identity = build_portable_transform_identity(
                    portable_slot, fps=fps
                )
                portable_transform = deterministic_transform(
                    portable_slot, fps=fps
                )
            except (RenderError, TypeError, ValueError, OverflowError) as exc:
                raise StageBlocked(
                    "needs_selected_visual_repair",
                    f"slot {slot_id} portable transform identity is invalid: {exc}",
                ) from exc
            transform_binding_base = {
                "contract": SELECTED_VISUAL_TRANSFORM_CONTRACT,
                "identity": portable_identity,
                "identity_hash": canonical_hash(portable_identity),
                "render_transform": portable_transform,
                "render_transform_hash": canonical_hash(portable_transform),
            }
            expected_transform_binding = {
                **transform_binding_base,
                "binding_sha256": canonical_hash(transform_binding_base),
            }
            expected_candidate_binding = _exact_repair_candidate_binding(
                selected
            )
            gate_unsigned = json.loads(json.dumps(dict(gate_row)))
            gate_row_hash = str(gate_unsigned.pop("row_hash", "") or "")
            gate_receipt = gate_row.get("gate_receipt")
            if not isinstance(gate_receipt, Mapping):
                raise StageBlocked(
                    "needs_selected_visual_repair",
                    f"slot {slot_id} selected gate receipt is missing",
                )
            try:
                validated_gate_receipt = validate_selected_visual_gate_receipt(
                    gate_receipt,
                    expected_cache_key=str(gate_receipt.get("cache_key") or ""),
                )
                _validate_selected_visual_gate_candidate_binding(
                    validated_gate_receipt, selected
                )
                expected_exact_span = (
                    _validated_selected_visual_gate_exact_render_span(
                        validated_gate_receipt, selected
                    )
                )
                detector_crop = _selected_visual_candidate_crop_margins(
                    selected
                )
            except (SelectedVisualGateError, TypeError, ValueError) as exc:
                raise StageBlocked(
                    "needs_selected_visual_repair",
                    f"slot {slot_id} selected gate receipt is invalid: {exc}",
                ) from exc
            geometry = (
                (validated_gate_receipt.get("binding") or {}).get(
                    "render_geometry_proxy"
                )
            )
            if (
                gate_row_hash != canonical_hash(gate_unsigned)
                or gate_row.get("contract")
                != SELECTED_VISUAL_ACCEPTANCE_CONTRACT
                or str(gate_row.get("slot_id") or "") != slot_id
                or gate_row.get("candidate_binding")
                != expected_candidate_binding
                or str(gate_row.get("candidate_binding_hash") or "")
                != canonical_hash(expected_candidate_binding)
                or portable_transform != transform
                or gate_row.get("render_transform") != transform
                or str(gate_row.get("render_transform_hash") or "")
                != canonical_hash(transform)
                or gate_row.get("transform_binding")
                != expected_transform_binding
                or str(gate_row.get("transform_binding_hash") or "")
                != canonical_hash(expected_transform_binding)
                or gate_row.get("gate_receipt") != validated_gate_receipt
                or str(gate_row.get("gate_receipt_hash") or "")
                != canonical_hash(validated_gate_receipt)
                or gate_row.get("status") != "passed"
                or gate_row.get("rejection_codes") != []
                or validated_gate_receipt.get("status") != "passed"
                or validated_gate_receipt.get("rejection_codes") != []
                or not isinstance(geometry, Mapping)
                or geometry.get("render_transform") != transform
                or not isinstance(geometry.get("crop_margins"), Mapping)
            ):
                raise StageBlocked(
                    "needs_selected_visual_repair",
                    f"slot {slot_id} render transform/crop differs from selected gate",
                )
            expected_crop = dict(geometry["crop_margins"])
            if expected_crop != detector_crop:
                raise StageBlocked(
                    "needs_selected_visual_repair",
                    f"slot {slot_id} detector crop differs from selected gate",
                )
            if selected.get("exact_render_span") != expected_exact_span:
                raise StageBlocked(
                    "needs_selected_visual_repair",
                    f"slot {slot_id} exact PTS range differs from selected gate",
                )

        def exact_int(value: Any, field: str) -> int:
            if isinstance(value, bool):
                raise ValueError(field)
            parsed = int(value)
            numeric = float(value)
            if not math.isfinite(numeric) or numeric != parsed:
                raise ValueError(field)
            return parsed

        try:
            render_start_frame = exact_int(
                slot.get("render_start_frame"), "render_start_frame"
            )
            render_end_frame = exact_int(
                slot.get("render_end_frame"), "render_end_frame"
            )
            render_frames = exact_int(slot.get("render_frames"), "render_frames")
            selected_frames = exact_int(
                selected.get("render_frames"), "selected_clip.render_frames"
            )
            source_in = float(selected["source_in"])
            source_out = float(selected["source_out"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise StageBlocked(
                "needs_selected_visual_repair",
                f"slot {slot_id} exact render span is malformed",
            ) from exc
        if (
            render_start_frame < 0
            or render_frames < 1
            or render_end_frame - render_start_frame != render_frames
            or selected_frames != render_frames
            or not math.isfinite(source_in)
            or not math.isfinite(source_out)
            or source_out <= source_in
        ):
            raise StageBlocked(
                "needs_selected_visual_repair",
                f"slot {slot_id} exact render span is inconsistent",
            )
        rows.append(
            {
                "slot_id": slot_id,
                "source_asset_id": str(selected.get("source_asset_id") or ""),
                "source_in": source_in,
                "source_out": source_out,
                "render_start_frame": render_start_frame,
                "render_end_frame": render_end_frame,
                "render_frames": render_frames,
                "transform": dict(transform),
                "edge_cleanup_margins": expected_crop,
                "exact_render_span": expected_exact_span,
            }
        )
    for previous, current in zip(rows, rows[1:]):
        if current["render_start_frame"] != previous["render_end_frame"]:
            raise StageBlocked(
                "needs_selected_visual_repair",
                "selected render frame spans are not contiguous",
            )
    if rows[0]["render_start_frame"] != 0:
        raise StageBlocked(
            "needs_selected_visual_repair",
            "selected render frame spans do not start at frame zero",
        )
    return rows


def _bind_rendered_clip_facts(
    video_manifest: Mapping[str, Any],
    expected_rows: Sequence[Mapping[str, Any]],
) -> None:
    """Fail closed unless FFmpeg facts match the persisted selected plan."""

    raw_clips = video_manifest.get("clips")
    if not isinstance(raw_clips, list) or len(raw_clips) != len(expected_rows):
        raise StageBlocked(
            "needs_selected_visual_repair",
            "rendered clip count differs from the selected visual plan",
        )
    for index, expected in enumerate(expected_rows):
        facts = raw_clips[index]
        if not isinstance(facts, dict):
            raise StageBlocked(
                "needs_selected_visual_repair",
                f"render facts for slot {expected['slot_id']} are malformed",
            )
        actual = {
            "slot_id": str(facts.get("slot_id") or ""),
            "source_in": facts.get("source_in"),
            "source_out": facts.get("source_out"),
            "render_frames": facts.get("render_frames"),
            "transform": facts.get("transform"),
        }
        wanted = {key: expected[key] for key in actual}
        if actual != wanted:
            raise StageBlocked(
                "needs_selected_visual_repair",
                f"render facts for slot {expected['slot_id']} differ from the selected plan",
            )
        expected_crop = expected.get("edge_cleanup_margins")
        if (
            expected_crop is not None
            and facts.get("edge_cleanup_margins") != expected_crop
        ):
            raise StageBlocked(
                "needs_selected_visual_repair",
                f"render crop for slot {expected['slot_id']} differs from the selected gate",
            )
        exact_span = expected.get("exact_render_span")
        expected_fill_strategy = "none"
        if facts.get("production_strict") is not True or str(
            facts.get("fill_strategy") or ""
        ) != expected_fill_strategy:
            raise StageBlocked(
                "needs_selected_visual_repair",
                f"slot {expected['slot_id']} was not materialized with strict no-fill",
            )
        if exact_span is not None:
            if not isinstance(exact_span, Mapping):
                raise StageBlocked(
                    "needs_selected_visual_repair",
                    f"slot {expected['slot_id']} selected exact PTS span is malformed",
                )
            clean = facts.get("clean_frame_contract")
            if not isinstance(clean, Mapping):
                raise StageBlocked(
                    "needs_selected_visual_repair",
                    f"slot {expected['slot_id']} render lost exact clean-frame facts",
                )
            exact_keys = (
                "contract",
                "source_in",
                "source_out",
                "source_frame_start",
                "source_frame_end",
                "source_frames",
                "start_pts",
                "end_pts",
                "time_base",
                "exact_pts_hash",
            )
            expected_exact = {key: exact_span.get(key) for key in exact_keys}
            observed_exact = {key: clean.get(key) for key in exact_keys}
            time_base = exact_span.get("time_base")
            if not isinstance(time_base, Mapping):
                raise StageBlocked(
                    "needs_selected_visual_repair",
                    f"slot {expected['slot_id']} exact PTS time base is malformed",
                )
            try:
                numerator = int(time_base["numerator"])
                denominator = int(time_base["denominator"])
                if denominator <= 0:
                    raise ValueError("time base denominator")
                expected_decode_in = float(
                    Fraction(int(exact_span["start_pts"]) * numerator, denominator)
                )
                expected_decode_out = float(
                    Fraction(int(exact_span["end_pts"]) * numerator, denominator)
                )
                expected_decode_duration = float(
                    Fraction(
                        (
                            int(exact_span["end_pts"])
                            - int(exact_span["start_pts"])
                        )
                        * numerator,
                        denominator,
                    )
                )
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                raise StageBlocked(
                    "needs_selected_visual_repair",
                    f"slot {expected['slot_id']} exact PTS decode bounds are malformed",
                ) from exc
            if (
                observed_exact != expected_exact
                or clean.get("selection_mode") != "persisted_exact_pts"
                or clean.get("clean_frame_start")
                != exact_span.get("source_frame_start")
                or clean.get("clean_frame_end")
                != exact_span.get("source_frame_end")
                or clean.get("source_frames_used")
                != exact_span.get("source_frames")
                or clean.get("decode_source_in") != expected_decode_in
                or clean.get("decode_source_out") != expected_decode_out
                or clean.get("decode_duration") != expected_decode_duration
                or facts.get("decode_source_in") != expected_decode_in
                or facts.get("decode_source_out") != expected_decode_out
                or facts.get("decode_duration") != expected_decode_duration
                or facts.get("copyts") is not True
                or facts.get("input_seek")
                != max(0.0, expected_decode_in - 2.0)
                or facts.get("input_duration") is not None
            ):
                raise StageBlocked(
                    "needs_selected_visual_repair",
                    f"render exact PTS facts for slot {expected['slot_id']} "
                    "differ from the selected gate",
                )
            facts["selected_gate_exact_render_span"] = dict(exact_span)
            facts["selected_gate_exact_render_span_hash"] = canonical_hash(
                exact_span
            )
        facts["source_asset_id"] = expected["source_asset_id"]
        facts["render_start_frame"] = expected["render_start_frame"]
        facts["render_end_frame"] = expected["render_end_frame"]


def stage_render(context: StageContext) -> Mapping[str, Any]:
    fixture = _fixture_stage(context)
    if fixture is not None:
        return fixture
    from .audio import (
        AudioMixConfig,
        AudioPlanError,
        build_ambience_stem_command,
        build_documentary_master_command,
        build_documentary_premaster_command,
        build_event_stem_command,
        build_loudnorm_analysis_command,
        build_presence_stem_command,
        build_segmented_music_stem_command,
        command_maps_input_audio,
        derive_ambience_energy_floor_adjustments,
        finalize_ambience_energy_floor_facts,
        parse_loudnorm_measurements,
    )
    from .audio_qa import validate_pcm_stem
    from .render import (
        command_sha256,
        render_video_only_timeline,
        run_command,
        verify_documentary_master,
    )
    from .sound_qa import (
        SoundQAError,
        bind_required_cue_ab_preview,
        bind_required_cue_combined_stem,
        derive_required_cue_static_gain,
        finalize_required_cue_static_gain,
        measure_required_cue_ab_preview,
        required_cue_preview_layer,
    )

    timeline = _json(_artifact_path(context, "global_selection", "timeline"))
    sound_plan = _json(_artifact_path(context, "sound_resolution", "sound_plan"))
    narration, _words = _tts_paths(context)
    production = _settings(context)
    ffmpeg = str(production.get("ffmpeg") or "ffmpeg")
    ffprobe = str(production.get("ffprobe") or "ffprobe")
    fps = float(context.project.settings.get("fps") or 25.0)
    duration = float(timeline["narration_duration"])
    explicit_render_plan = _validated_explicit_render_plan(
        timeline,
        fps=fps,
        require_selected_acceptance=True,
    )
    render_config = {
        **dict(production.get("render_config") or {}),
        "fps": fps,
        "production_strict": True,
    }
    mix_config = AudioMixConfig.from_mapping(
        dict(production.get("audio_config") or {})
    )
    mix_config.validate()
    runtime_timeline = _hydrate_timeline(context, timeline)
    runtime_plan = _verified_runtime_sound_plan(
        context, sound_plan, ffprobe=ffprobe
    )

    work = context.attempt_root / "render_work"
    work.mkdir(parents=True, exist_ok=True)
    video_only = context.attempt_root / "timeline_video_only.mp4"
    video_manifest = render_video_only_timeline(
        runtime_timeline,
        video_only,
        work / "video",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        render_config=render_config,
    )
    _bind_rendered_clip_facts(video_manifest, explicit_render_plan)

    music_rows: list[dict[str, Any]] = []
    transition_seconds: list[float] = []
    for segment in runtime_plan.get("music_segments") or ():
        asset = segment["asset"]
        gain = segment.get("gain", asset.get("gain", 1.0))
        if segment.get("gain_db") is not None:
            gain = 10.0 ** (float(segment["gain_db"]) / 20.0)
        music_rows.append(
            {
                "path": asset["path"],
                "start": segment["start"],
                "end": segment["end"],
                "source_offset": segment.get(
                    "source_offset", asset.get("source_offset", 0.0)
                ),
                "gain": gain,
                "segment_id": segment.get("segment_id"),
            }
        )
        transition_seconds.append(float((segment.get("transition") or {}).get("seconds") or 0.0))
    if len(music_rows) <= 1:
        music_crossfade = 0.0
    else:
        music_crossfade = transition_seconds[0]
        if not 2.0 <= music_crossfade <= 5.0 or any(
            abs(value - music_crossfade) > 0.001 for value in transition_seconds[1:]
        ):
            raise StageBlocked(
                "needs_sound_plan_fix",
                "all adjacent production music transitions need one common 2-5s crossfade",
            )

    stem_paths = {
        "music": work / "music_pcm.wav",
        "ambience": work / "ambience_pcm.wav",
        "presence": work / "presence_pcm.wav",
        "events": work / "events_pcm.wav",
    }
    ambience_provisional = work / "ambience_pcm.provisional.wav"
    ambience_regions = runtime_plan.get("ambience_regions") or ()
    runtime_presence_layers = [
        copy.deepcopy(dict(row)) for row in runtime_plan.get("presence_layers") or ()
    ]
    runtime_event_cues = [
        copy.deepcopy(dict(row)) for row in runtime_plan.get("event_cues") or ()
    ]
    required_presence = [
        row
        for row in runtime_presence_layers
        if row.get("required") is True
        and isinstance(row.get("physical_foley_obligation"), Mapping)
    ]
    optional_presence = [
        row
        for row in runtime_presence_layers
        if not (
            row.get("required") is True
            and isinstance(row.get("physical_foley_obligation"), Mapping)
        )
    ]
    required_events = [
        row
        for row in runtime_event_cues
        if row.get("required") is True
        and isinstance(row.get("physical_foley_obligation"), Mapping)
    ]
    if required_events:
        raise StageBlocked(
            "needs_audio_render_repair",
            "required physical foley needs an isolated linear presence lane",
        )
    if required_presence:
        if optional_presence:
            raise StageBlocked(
                "needs_audio_render_repair",
                "required physical foley needs an isolated linear premaster lane",
            )
        mix_config = replace(
            mix_config,
            presence_gain=REQUIRED_PHYSICAL_FOLEY_PREMASTER_GAIN,
            presence_duck_ratio=REQUIRED_PHYSICAL_FOLEY_PREMASTER_DUCK_RATIO,
        )
        mix_config.validate()
    required_preview_builds: list[dict[str, Any]] = []
    for lane_name, rows, builder in (
        ("presence", runtime_presence_layers, build_presence_stem_command),
        ("events", runtime_event_cues, build_event_stem_command),
    ):
        for index, layer in enumerate(rows):
            if not (
                layer.get("required") is True
                and isinstance(layer.get("physical_foley_obligation"), Mapping)
            ):
                continue
            try:
                preview_layer, preview_duration = required_cue_preview_layer(layer)
                provisional_path = (
                    work / f"required_{lane_name}_{index:03d}.provisional.wav"
                )
                provisional_argv, provisional_facts = builder(
                    [preview_layer],
                    preview_duration,
                    provisional_path,
                    ffmpeg=ffmpeg,
                    config=mix_config,
                )
            except (AudioPlanError, SoundQAError) as exc:
                raise StageBlocked("needs_audio_render_repair", str(exc)) from exc
            required_preview_builds.append(
                {
                    "lane": lane_name,
                    "index": index,
                    "builder": builder,
                    "preview_duration": preview_duration,
                    "provisional_path": provisional_path,
                    "provisional_argv": provisional_argv,
                    "provisional_facts": provisional_facts,
                }
            )
    try:
        initial_stem_builds = {
            "music": build_segmented_music_stem_command(
                music_rows,
                duration,
                stem_paths["music"],
                crossfade_seconds=music_crossfade,
                ffmpeg=ffmpeg,
                config=mix_config,
            ),
            "ambience_provisional": build_ambience_stem_command(
                ambience_regions,
                duration,
                ambience_provisional,
                ffmpeg=ffmpeg,
                config=mix_config,
            ),
        }
        for row in required_preview_builds:
            initial_stem_builds[
                f"required_{row['lane']}_{row['index']:03d}_provisional"
            ] = (row["provisional_argv"], row["provisional_facts"])
    except AudioPlanError as exc:
        raise StageBlocked("needs_sound_plan_fix", str(exc)) from exc

    timeout = float(render_config.get("final_timeout_seconds") or 3_600.0)
    workers = min(
        max(1, int(production.get("audio_render_workers") or 4)),
        len(initial_stem_builds),
    )
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="semantic-audio") as pool:
        futures = {
            pool.submit(run_command, argv, timeout=timeout): name
            for name, (argv, _facts) in initial_stem_builds.items()
        }
        for future in as_completed(futures):
            future.result()

    required_isolated_facts: list[dict[str, Any]] = []
    required_final_builds: list[dict[str, Any]] = []
    try:
        for row in required_preview_builds:
            rows = (
                runtime_presence_layers
                if row["lane"] == "presence"
                else runtime_event_cues
            )
            adjusted_layer, adjustment_facts = derive_required_cue_static_gain(
                row["provisional_path"],
                rows[row["index"]],
                premaster_presence_gain=mix_config.presence_gain,
                premaster_presence_duck_ratio=mix_config.presence_duck_ratio,
            )
            rows[row["index"]] = adjusted_layer
            adjusted_preview, preview_duration = required_cue_preview_layer(
                adjusted_layer
            )
            final_path = work / f"required_{row['lane']}_{row['index']:03d}.wav"
            final_argv, final_command_facts = row["builder"](
                [adjusted_preview],
                preview_duration,
                final_path,
                ffmpeg=ffmpeg,
                config=mix_config,
            )
            required_final_builds.append(
                {
                    **row,
                    "adjusted_layer": adjusted_layer,
                    "adjustment_facts": adjustment_facts,
                    "final_path": final_path,
                    "final_argv": final_argv,
                    "final_command_facts": final_command_facts,
                }
            )
        with ThreadPoolExecutor(
            max_workers=min(max(1, workers), max(1, len(required_final_builds))),
            thread_name_prefix="semantic-required-cue",
        ) as pool:
            futures = {
                pool.submit(run_command, row["final_argv"], timeout=timeout): row
                for row in required_final_builds
            }
            for future in as_completed(futures):
                future.result()
        for row in required_final_builds:
            required_isolated_facts.append(
                finalize_required_cue_static_gain(
                    row["final_path"],
                    row["adjusted_layer"],
                    row["adjustment_facts"],
                )
            )
    except (AudioPlanError, SoundQAError) as exc:
        raise StageBlocked("needs_audio_render_repair", str(exc)) from exc

    try:
        adjusted_ambience_regions, ambience_adjustment_facts = (
            derive_ambience_energy_floor_adjustments(
                ambience_provisional,
                ambience_regions,
            )
        )
        ambience_final_argv, ambience_final_facts = build_ambience_stem_command(
            adjusted_ambience_regions,
            duration,
            stem_paths["ambience"],
            ffmpeg=ffmpeg,
            config=mix_config,
            energy_floor_facts=ambience_adjustment_facts,
        )
        run_command(ambience_final_argv, timeout=timeout)
        verified_ambience_energy_floor = finalize_ambience_energy_floor_facts(
            stem_paths["ambience"],
            ambience_adjustment_facts,
            ambience_final_facts["boundaries"],
        )
        ambience_final_facts = {
            **ambience_final_facts,
            "energy_floor": verified_ambience_energy_floor,
        }
    except AudioPlanError as exc:
        raise StageBlocked("needs_audio_render_repair", str(exc)) from exc

    try:
        presence_build = build_presence_stem_command(
            runtime_presence_layers,
            duration,
            stem_paths["presence"],
            ffmpeg=ffmpeg,
            config=mix_config,
        )
        events_build = build_event_stem_command(
            runtime_event_cues,
            duration,
            stem_paths["events"],
            ffmpeg=ffmpeg,
            config=mix_config,
        )
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="semantic-foley") as pool:
            futures = [
                pool.submit(run_command, argv, timeout=timeout)
                for argv, _facts in (presence_build, events_build)
            ]
            for future in as_completed(futures):
                future.result()
    except AudioPlanError as exc:
        raise StageBlocked("needs_audio_render_repair", str(exc)) from exc

    stem_builds = {
        "music": initial_stem_builds["music"],
        "ambience": (ambience_final_argv, ambience_final_facts),
        "presence": presence_build,
        "events": events_build,
    }

    stem_qa = {
        name: validate_pcm_stem(path, expected_duration=duration)
        for name, path in stem_paths.items()
    }
    invalid_stems = [name for name, report in stem_qa.items() if not report["passed"]]
    if invalid_stems:
        raise StageBlocked(
            "needs_audio_render_repair",
            "invalid PCM stems: " + ", ".join(invalid_stems),
        )

    required_render_facts: list[dict[str, Any]] = []
    try:
        for facts in required_isolated_facts:
            lane_name = str((facts.get("identity") or {}).get("lane") or "")
            if lane_name not in {"presence", "events"}:
                raise SoundQAError("required cue render lane is invalid")
            combined_argv = stem_builds[lane_name][0]
            required_render_facts.append(
                bind_required_cue_combined_stem(
                    facts,
                    combined_stem_sha256=sha256_file(stem_paths[lane_name]),
                    combined_command_sha256=command_sha256(combined_argv),
                )
            )
    except SoundQAError as exc:
        raise StageBlocked("needs_audio_render_repair", str(exc)) from exc

    premaster = work / "premaster_pcm.wav"
    premaster_argv, premaster_facts = build_documentary_premaster_command(
        narration,
        stem_paths["music"],
        stem_paths["ambience"],
        stem_paths["presence"],
        stem_paths["events"],
        premaster,
        duration,
        ffmpeg=ffmpeg,
        config=mix_config,
    )
    run_command(premaster_argv, timeout=timeout)
    premaster_qa = validate_pcm_stem(premaster, expected_duration=duration)
    if not premaster_qa["passed"]:
        raise StageBlocked(
            "needs_audio_render_repair",
            "premaster PCM failed exact-duration/format QA: "
            + "; ".join(premaster_qa.get("errors") or ()),
        )

    required_ab_builds: list[dict[str, Any]] = []
    try:
        for receipt_index, (row, combined_facts) in enumerate(
            zip(required_final_builds, required_render_facts, strict=True)
        ):
            lane_name = row["lane"]
            all_rows = (
                runtime_presence_layers
                if lane_name == "presence"
                else runtime_event_cues
            )
            muted_rows = [
                copy.deepcopy(dict(layer))
                for index, layer in enumerate(all_rows)
                if index != row["index"]
            ]
            muted_lane = work / f"required_{lane_name}_{row['index']:03d}.muted.wav"
            muted_lane_argv, muted_lane_facts = row["builder"](
                muted_rows,
                duration,
                muted_lane,
                ffmpeg=ffmpeg,
                config=mix_config,
            )
            run_command(muted_lane_argv, timeout=timeout)
            muted_premaster = (
                work / f"required_{lane_name}_{row['index']:03d}.muted.premaster.wav"
            )
            muted_premaster_argv, muted_premaster_facts = (
                build_documentary_premaster_command(
                    narration,
                    stem_paths["music"],
                    stem_paths["ambience"],
                    muted_lane if lane_name == "presence" else stem_paths["presence"],
                    muted_lane if lane_name == "events" else stem_paths["events"],
                    muted_premaster,
                    duration,
                    ffmpeg=ffmpeg,
                    config=mix_config,
                )
            )
            run_command(muted_premaster_argv, timeout=timeout)

            adjusted_layer = row["adjusted_layer"]
            start_frame = int(round(float(adjusted_layer["start"]) * 48_000))
            end_frame = int(round(float(adjusted_layer["end"]) * 48_000))
            if end_frame - start_frame < 19_200:
                raise SoundQAError("required cue is shorter than one A/B window")
            on_preview = work / f"required_{lane_name}_{row['index']:03d}.on.preview.wav"
            muted_preview = (
                work / f"required_{lane_name}_{row['index']:03d}.muted.preview.wav"
            )

            def preview_command(source: Path, output_path: Path) -> list[str]:
                return [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-af",
                    (
                        f"atrim=start_sample={start_frame}:end_sample={end_frame},"
                        "asetpts=PTS-STARTPTS"
                    ),
                    "-vn",
                    "-sn",
                    "-dn",
                    "-c:a",
                    "pcm_s24le",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    str(output_path),
                ]

            on_preview_argv = preview_command(premaster, on_preview)
            muted_preview_argv = preview_command(muted_premaster, muted_preview)
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="semantic-cue-ab") as pool:
                futures = [
                    pool.submit(run_command, argv, timeout=timeout)
                    for argv in (on_preview_argv, muted_preview_argv)
                ]
                for future in as_completed(futures):
                    future.result()
            cue_duration = (end_frame - start_frame) / 48_000.0
            ab_facts = measure_required_cue_ab_preview(
                on_preview,
                muted_preview,
                cue_start=0.0,
                cue_end=cue_duration,
            )
            ab_unsigned = copy.deepcopy(dict(ab_facts))
            ab_unsigned.pop("receipt_sha256", None)
            ab_unsigned["timeline_interval"] = {
                "start_frame": start_frame,
                "end_frame": end_frame,
                "sample_rate": 48_000,
            }
            ab_unsigned["source_premaster_sha256"] = sha256_file(premaster)
            ab_unsigned["muted_premaster_sha256"] = sha256_file(muted_premaster)
            ab_unsigned["receipt_sha256"] = canonical_hash(ab_unsigned)
            required_render_facts[receipt_index] = bind_required_cue_ab_preview(
                combined_facts,
                ab_unsigned,
            )
            required_ab_builds.append(
                {
                    "lane": lane_name,
                    "index": row["index"],
                    "muted_lane_argv": muted_lane_argv,
                    "muted_lane_facts": muted_lane_facts,
                    "muted_premaster_argv": muted_premaster_argv,
                    "muted_premaster_facts": muted_premaster_facts,
                    "on_preview_argv": on_preview_argv,
                    "muted_preview_argv": muted_preview_argv,
                }
            )
            for temporary in (
                muted_lane,
                muted_premaster,
                on_preview,
                muted_preview,
            ):
                temporary.unlink(missing_ok=True)
    except (AudioPlanError, SoundQAError) as exc:
        raise StageBlocked("needs_audio_render_repair", str(exc)) from exc

    analysis_argv, analysis_facts = build_loudnorm_analysis_command(
        premaster,
        duration,
        ffmpeg=ffmpeg,
        config=mix_config,
    )
    analysis = run_command(analysis_argv, timeout=timeout)
    try:
        measurements = parse_loudnorm_measurements(analysis.stderr)
    except AudioPlanError as exc:
        raise StageBlocked("needs_audio_render_repair", str(exc)) from exc

    output = context.attempt_root / "master.mp4"
    master_argv, master_facts = build_documentary_master_command(
        video_only,
        premaster,
        output,
        duration,
        measurements,
        ffmpeg=ffmpeg,
        config=mix_config,
    )
    if command_maps_input_audio(master_argv, 0):
        raise RuntimeError("documentary master command maps donor/video input audio")
    run_command(master_argv, timeout=timeout)
    final = verify_documentary_master(
        output,
        expected_duration=duration,
        fps=fps,
        ffprobe=ffprobe,
    )

    command_records = list(video_manifest["commands"])
    provisional_argv, provisional_facts = initial_stem_builds[
        "ambience_provisional"
    ]
    command_records.append(
        {
            "stage": "ambience_provisional_pcm",
            "sha256": command_sha256(provisional_argv),
            "facts": provisional_facts,
        }
    )
    for row in required_final_builds:
        command_records.extend(
            [
                {
                    "stage": (
                        f"required_{row['lane']}_{row['index']:03d}_provisional"
                    ),
                    "sha256": command_sha256(row["provisional_argv"]),
                    "facts": row["provisional_facts"],
                },
                {
                    "stage": f"required_{row['lane']}_{row['index']:03d}_isolated",
                    "sha256": command_sha256(row["final_argv"]),
                    "facts": row["final_command_facts"],
                },
            ]
        )
    for row in required_ab_builds:
        command_records.extend(
            [
                {
                    "stage": f"required_{row['lane']}_{row['index']:03d}_muted_lane",
                    "sha256": command_sha256(row["muted_lane_argv"]),
                    "facts": row["muted_lane_facts"],
                },
                {
                    "stage": f"required_{row['lane']}_{row['index']:03d}_muted_premaster",
                    "sha256": command_sha256(row["muted_premaster_argv"]),
                    "facts": row["muted_premaster_facts"],
                },
                {
                    "stage": f"required_{row['lane']}_{row['index']:03d}_cue_on_preview",
                    "sha256": command_sha256(row["on_preview_argv"]),
                    "facts": {"kind": "required_cue_on_ab_preview_v1"},
                },
                {
                    "stage": f"required_{row['lane']}_{row['index']:03d}_cue_muted_preview",
                    "sha256": command_sha256(row["muted_preview_argv"]),
                    "facts": {"kind": "required_cue_muted_ab_preview_v1"},
                },
            ]
        )
    for name, (argv, facts) in stem_builds.items():
        command_records.append(
            {"stage": f"{name}_pcm", "sha256": command_sha256(argv), "facts": facts}
        )
    for name, argv, facts in (
        ("premaster_pcm", premaster_argv, premaster_facts),
        ("loudnorm_analysis", analysis_argv, analysis_facts),
        ("documentary_master", master_argv, master_facts),
    ):
        command_records.append(
            {"stage": name, "sha256": command_sha256(argv), "facts": facts}
        )
    manifest = {
        "schema_version": 2,
        "status": "completed",
        "output": {
            "path": str(output),
            "size_bytes": output.stat().st_size,
            "sha256": sha256_file(output),
        },
        "clips": video_manifest["clips"],
        "render_config": video_manifest["render_config"],
        "commands": command_records,
        "donor_audio_proof": {
            **dict(video_manifest["donor_audio_proof"]),
            "final_video_input_audio_map": False,
            "aac_encode_count": int(master_facts["aac_encode_count"]),
            "passed": (
                bool(video_manifest["donor_audio_proof"].get("passed"))
                and not bool(master_facts.get("donor_audio_mapping"))
                and int(master_facts["aac_encode_count"]) == 1
            ),
        },
        "audio": {
            "mode": "production_v2_five_lane_ambience_energy_floor_two_pass",
            "sound_plan_sha256": sha256_file(
                _artifact_path(context, "sound_resolution", "sound_plan")
            ),
            "ambience_energy_floor": verified_ambience_energy_floor,
            "physical_foley_audibility": required_render_facts,
            "stems": {
                name: {
                    "artifact_id": f"{name}_pcm",
                    "sha256": sha256_file(path),
                    "qa": stem_qa[name],
                }
                for name, path in stem_paths.items()
            },
            "premaster": {
                "artifact_id": "premaster_pcm",
                "sha256": sha256_file(premaster),
                "qa": premaster_qa,
            },
            "loudnorm": {
                "mode": "two_pass_measured",
                "measurements": measurements,
            },
            "aac_encode_count": 1,
        },
        "probe": final["probe"],
        "qa": final["qa"],
    }
    portable = _portable_render_manifest(manifest)
    manifest_path = _write_json(context, "render_manifest.json", portable)
    return {
        "status": "rendered",
        "artifacts": [
            _artifact("master", output, media_type="video/mp4"),
            _artifact("video_only", video_only, media_type="video/mp4"),
            _artifact("music_pcm", stem_paths["music"], media_type="audio/wav"),
            _artifact("ambience_pcm", stem_paths["ambience"], media_type="audio/wav"),
            _artifact("presence_pcm", stem_paths["presence"], media_type="audio/wav"),
            _artifact("events_pcm", stem_paths["events"], media_type="audio/wav"),
            _artifact("premaster_pcm", premaster, media_type="audio/wav"),
            _artifact("render_manifest", manifest_path),
        ],
    }


def _visual_qa_provenance_rows(
    timeline: Mapping[str, Any],
    render_manifest: Mapping[str, Any],
    *,
    master_sha256: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Bind each rendered source range to its already-paid semantic verdict.

    Final visual QA is deliberately provider-free.  The semantic stage already
    judged the exact half-open source range selected into the timeline; the
    final master only needs to prove that render materialization consumed that
    same range and frame count.  Treating the five time samples as independent
    judge candidates would both repay for semantics and misread a 1x5 strip as
    a panorama.
    """

    from .render import (
        DETERMINISTIC_TRANSFORM_POLICY_VERSION,
        RenderError,
        build_portable_transform_identity,
        deterministic_transform,
    )
    from .selected_visual_gate import (
        SELECTED_VISUAL_GATE_RECEIPT_CONTRACT,
        SELECTED_VISUAL_GATE_VERSION,
        SelectedVisualGateError,
        validate_selected_visual_gate_receipt,
    )

    raw_slots = timeline.get("slots")
    raw_clips = render_manifest.get("clips")
    errors: list[str] = []
    if not isinstance(raw_slots, list) or not raw_slots:
        return [], ["timeline has no provenance-verifiable slots"]
    if not isinstance(raw_clips, list):
        return [], ["render manifest has no clip provenance"]
    manifest_output = render_manifest.get("output")
    manifest_master_sha256 = (
        str(manifest_output.get("sha256") or "")
        if isinstance(manifest_output, Mapping)
        else ""
    )
    if master_sha256 is not None and manifest_master_sha256 != master_sha256:
        errors.append("render manifest master hash differs from master")
    acceptance = timeline.get("selected_visual_acceptance")
    acceptance_rows: list[Any] = []
    if not isinstance(acceptance, Mapping):
        errors.append("timeline selected visual acceptance is missing")
    else:
        try:
            planned_timeline_hash = _selected_visual_planned_timeline_hash(
                timeline
            )
        except ValueError:
            planned_timeline_hash = ""
            errors.append("timeline selected visual planned timeline is malformed")
        acceptance_unsigned = json.loads(json.dumps(dict(acceptance)))
        acceptance_hash = str(
            acceptance_unsigned.pop("receipt_sha256", "") or ""
        )
        raw_acceptance_rows = acceptance.get("final_gate_rows")
        if (
            acceptance_hash != canonical_hash(acceptance_unsigned)
            or acceptance.get("schema_version") != 1
            or timeline.get("selected_visual_acceptance_hash")
            != canonical_hash(acceptance)
            or acceptance.get("contract") != SELECTED_VISUAL_ACCEPTANCE_CONTRACT
            or acceptance.get("policy")
            != SELECTED_VISUAL_REPAIR_POLICY_VERSION
            or acceptance.get("cache_replay_contract")
            != SELECTED_VISUAL_CACHE_REPLAY_CONTRACT
            or acceptance.get("exact_render_span_contract")
            != EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION
            or acceptance.get("status") != "passed"
            or acceptance.get("gate_contract")
            != SELECTED_VISUAL_GATE_RECEIPT_CONTRACT
            or acceptance.get("gate_version") != SELECTED_VISUAL_GATE_VERSION
            or acceptance.get("transform_contract")
            != DETERMINISTIC_TRANSFORM_POLICY_VERSION
            or acceptance.get("transform_contract")
            != SELECTED_VISUAL_TRANSFORM_CONTRACT
            or str(acceptance.get("planned_timeline_hash") or "")
            != planned_timeline_hash
            or not isinstance(raw_acceptance_rows, list)
            or str(acceptance.get("final_gate_rows_hash") or "")
            != canonical_hash(raw_acceptance_rows or [])
        ):
            errors.append("timeline selected visual acceptance is stale")
        elif len(raw_acceptance_rows) != len(raw_slots):
            errors.append("timeline selected visual gate row count is stale")
        else:
            acceptance_rows = list(raw_acceptance_rows)
    if len(raw_slots) != len(raw_clips):
        errors.append(
            "render manifest clip count differs from timeline: "
            f"{len(raw_clips)} != {len(raw_slots)}"
        )

    slot_ids = [
        str(row.get("slot_id") or "") if isinstance(row, Mapping) else ""
        for row in raw_slots
    ]
    if any(not slot_id for slot_id in slot_ids):
        errors.append("timeline contains an empty slot_id")
    if len(set(slot_ids)) != len(slot_ids):
        errors.append("timeline contains duplicate slot_id values")

    rows: list[dict[str, Any]] = []
    cumulative_render_frame = 0
    for index, raw_slot in enumerate(raw_slots):
        slot_errors: list[str] = []
        slot = dict(raw_slot) if isinstance(raw_slot, Mapping) else {}
        rendered = (
            dict(raw_clips[index])
            if index < len(raw_clips) and isinstance(raw_clips[index], Mapping)
            else {}
        )
        slot_id = str(slot.get("slot_id") or f"index-{index}")
        selected = slot.get("selected_clip")
        if not isinstance(selected, Mapping):
            selected = {}
            slot_errors.append("timeline selected_clip is missing")

        verdict = str(slot.get("verdict") or "").casefold()
        selected_verdict = str(selected.get("judge_verdict") or "").casefold()
        if slot.get("status") != "resolved":
            slot_errors.append("timeline semantic status is not resolved")
        if verdict not in {"exact", "relevant"}:
            slot_errors.append("timeline semantic verdict is not exact/relevant")
        if selected_verdict != verdict:
            slot_errors.append("selected clip verdict differs from timeline verdict")
        if not isinstance(slot.get("selection_provenance"), Mapping):
            slot_errors.append("timeline selection provenance is missing")

        gate_row = slot.get("selected_visual_gate")
        validated_gate_receipt: dict[str, Any] | None = None
        gate_receipt_hash = ""
        planned_transform: dict[str, Any] | None = None
        planned_crop_margins: dict[str, Any] | None = None
        planned_exact_render_span: dict[str, Any] | None = None
        transform_binding: dict[str, Any] | None = None
        if not isinstance(gate_row, Mapping):
            slot_errors.append("timeline selected visual gate row is missing")
        else:
            gate_row = json.loads(json.dumps(dict(gate_row)))
            gate_unsigned = dict(gate_row)
            gate_row_hash = str(gate_unsigned.pop("row_hash", "") or "")
            if (
                index >= len(acceptance_rows)
                or gate_row != acceptance_rows[index]
            ):
                slot_errors.append("slot gate row differs from acceptance receipt")
            raw_gate_receipt = gate_row.get("gate_receipt")
            if not isinstance(raw_gate_receipt, Mapping):
                slot_errors.append("selected visual gate receipt is missing")
            else:
                try:
                    validated_gate_receipt = (
                        validate_selected_visual_gate_receipt(
                            raw_gate_receipt,
                            expected_cache_key=str(
                                raw_gate_receipt.get("cache_key") or ""
                            ),
                        )
                    )
                    _validate_selected_visual_gate_candidate_binding(
                        validated_gate_receipt, selected
                    )
                    planned_exact_render_span = (
                        _validated_selected_visual_gate_exact_render_span(
                            validated_gate_receipt, selected
                        )
                    )
                except (SelectedVisualGateError, ValueError):
                    slot_errors.append("selected visual gate receipt is invalid")
            if (
                gate_row_hash != canonical_hash(gate_unsigned)
                or gate_row.get("contract")
                != SELECTED_VISUAL_ACCEPTANCE_CONTRACT
                or str(gate_row.get("slot_id") or "") != slot_id
                or gate_row.get("status") != "passed"
                or gate_row.get("rejection_codes") != []
            ):
                slot_errors.append("selected visual gate row is stale or rejected")
            if validated_gate_receipt is not None:
                gate_receipt_hash = canonical_hash(validated_gate_receipt)
                geometry = (validated_gate_receipt.get("binding") or {}).get(
                    "render_geometry_proxy"
                )
                if (
                    validated_gate_receipt.get("status") != "passed"
                    or gate_row.get("gate_receipt") != validated_gate_receipt
                    or str(gate_row.get("gate_receipt_hash") or "")
                    != gate_receipt_hash
                    or not isinstance(geometry, Mapping)
                ):
                    slot_errors.append("selected visual gate receipt binding is stale")
                else:
                    if isinstance(geometry.get("render_transform"), Mapping):
                        planned_transform = dict(geometry["render_transform"])
                    else:
                        slot_errors.append("selected visual gate transform is missing")
                    if isinstance(geometry.get("crop_margins"), Mapping):
                        planned_crop_margins = dict(geometry["crop_margins"])
                    else:
                        slot_errors.append("selected visual gate crop is missing")

        if isinstance(selected, Mapping) and selected:
            try:
                fps = float(timeline.get("fps") or 0.0)
                portable_slot = {
                    "slot_id": slot_id,
                    "selected_clip": dict(selected),
                }
                identity = build_portable_transform_identity(
                    portable_slot, fps=fps
                )
                expected_transform = deterministic_transform(
                    portable_slot, fps=fps
                )
                transform_base = {
                    "contract": SELECTED_VISUAL_TRANSFORM_CONTRACT,
                    "identity": identity,
                    "identity_hash": canonical_hash(identity),
                    "render_transform": expected_transform,
                    "render_transform_hash": canonical_hash(expected_transform),
                }
                transform_binding = {
                    **transform_base,
                    "binding_sha256": canonical_hash(transform_base),
                }
            except (RenderError, TypeError, ValueError, OverflowError):
                expected_transform = None
                slot_errors.append("selected visual portable transform is invalid")
            if expected_transform is not None:
                if (
                    slot.get("transform") != expected_transform
                    or selected.get("transform") != expected_transform
                    or not isinstance(gate_row, Mapping)
                    or gate_row.get("render_transform") != expected_transform
                    or gate_row.get("transform_binding") != transform_binding
                    or str(gate_row.get("transform_binding_hash") or "")
                    != canonical_hash(transform_binding)
                    or planned_transform != expected_transform
                ):
                    slot_errors.append("planned render transform binding changed")
            if planned_crop_margins is not None:
                try:
                    selected_crop = _selected_visual_candidate_crop_margins(
                        selected
                    )
                except ValueError:
                    selected_crop = None
                    slot_errors.append("selected detector crop binding is invalid")
                if (
                    selected_crop is not None
                    and selected_crop != planned_crop_margins
                ):
                    slot_errors.append(
                        "selected detector crop differs from selected gate"
                    )
            if (
                planned_exact_render_span is not None
                and selected.get("exact_render_span")
                != planned_exact_render_span
            ):
                slot_errors.append("selected exact PTS range differs from selected gate")

        def exact_positive_int(value: Any, label: str) -> int | None:
            if isinstance(value, bool):
                slot_errors.append(f"{label} is not an integer")
                return None
            try:
                parsed = int(value)
                numeric = float(value)
            except (TypeError, ValueError, OverflowError):
                slot_errors.append(f"{label} is not an integer")
                return None
            if not math.isfinite(numeric) or numeric != parsed or parsed < 1:
                slot_errors.append(f"{label} is not a positive integer")
                return None
            return parsed

        slot_frames = exact_positive_int(slot.get("render_frames"), "slot render_frames")
        selected_frames = exact_positive_int(
            selected.get("render_frames"), "selected clip render_frames"
        )
        rendered_frames = exact_positive_int(
            rendered.get("render_frames"), "render manifest render_frames"
        )
        def exact_nonnegative_int(value: Any, label: str) -> int | None:
            if isinstance(value, bool):
                slot_errors.append(f"{label} is not an integer")
                return None
            try:
                parsed = int(value)
                numeric = float(value)
            except (TypeError, ValueError, OverflowError):
                slot_errors.append(f"{label} is not an integer")
                return None
            if not math.isfinite(numeric) or numeric != parsed or parsed < 0:
                slot_errors.append(f"{label} is not a non-negative integer")
                return None
            return parsed

        slot_start_frame = exact_nonnegative_int(
            slot.get("render_start_frame"), "slot render_start_frame"
        )
        slot_end_frame = exact_nonnegative_int(
            slot.get("render_end_frame"), "slot render_end_frame"
        )
        rendered_start_frame = exact_nonnegative_int(
            rendered.get("render_start_frame"),
            "render manifest render_start_frame",
        )
        rendered_end_frame = exact_nonnegative_int(
            rendered.get("render_end_frame"),
            "render manifest render_end_frame",
        )
        if (
            slot_frames is not None
            and selected_frames is not None
            and slot_frames != selected_frames
        ):
            slot_errors.append("selected clip frame count differs from timeline")
        if (
            slot_frames is not None
            and rendered_frames is not None
            and slot_frames != rendered_frames
        ):
            slot_errors.append("rendered frame count differs from timeline")
        expected_start_frame = cumulative_render_frame
        expected_end_frame = (
            expected_start_frame + slot_frames
            if slot_frames is not None
            else None
        )
        if (
            slot_start_frame != expected_start_frame
            or rendered_start_frame != expected_start_frame
        ):
            slot_errors.append("rendered slot start frame differs from exact timeline")
        if expected_end_frame is not None and (
            slot_end_frame != expected_end_frame
            or rendered_end_frame != expected_end_frame
        ):
            slot_errors.append("rendered slot end frame differs from exact timeline")
        if slot_frames is not None:
            cumulative_render_frame += slot_frames

        def exact_range(row: Mapping[str, Any], label: str) -> dict[str, Any] | None:
            source_asset_id = str(row.get("source_asset_id") or "").strip()
            try:
                source_in = float(row["source_in"])
                source_out = float(row["source_out"])
            except (KeyError, TypeError, ValueError, OverflowError):
                slot_errors.append(f"{label} source range is malformed")
                return None
            if (
                not source_asset_id
                or not math.isfinite(source_in)
                or not math.isfinite(source_out)
                or source_out <= source_in
            ):
                slot_errors.append(f"{label} source range is malformed")
                return None
            return {
                "source_asset_id": source_asset_id,
                "source_in": source_in,
                "source_out": source_out,
            }

        selected_range = exact_range(selected, "selected clip")
        rendered_range = exact_range(rendered, "render manifest")
        if selected_range is not None and rendered_range is not None:
            if selected_range != rendered_range:
                slot_errors.append("rendered source range differs from selected clip")
        if str(rendered.get("slot_id") or "") != slot_id:
            slot_errors.append("render manifest slot_id differs from timeline")
        if planned_transform is not None and rendered.get("transform") != (
            planned_transform
        ):
            slot_errors.append("render manifest transform differs from selected gate")
        if planned_crop_margins is not None and rendered.get(
            "edge_cleanup_margins"
        ) != planned_crop_margins:
            slot_errors.append("render manifest crop differs from selected gate")
        if rendered.get("production_strict") is not True:
            slot_errors.append("render manifest clip is not production-strict")
        expected_fill_strategy = "none"
        if str(rendered.get("fill_strategy") or "") != expected_fill_strategy:
            slot_errors.append("render manifest clip used a fill strategy")
        if planned_exact_render_span is not None:
            clean = rendered.get("clean_frame_contract")
            exact_keys = (
                "contract",
                "source_in",
                "source_out",
                "source_frame_start",
                "source_frame_end",
                "source_frames",
                "start_pts",
                "end_pts",
                "time_base",
                "exact_pts_hash",
            )
            if not isinstance(clean, Mapping):
                slot_errors.append("render manifest lost exact clean-frame facts")
            else:
                expected_exact = {
                    key: planned_exact_render_span.get(key)
                    for key in exact_keys
                }
                observed_exact = {key: clean.get(key) for key in exact_keys}
                time_base = planned_exact_render_span.get("time_base")
                try:
                    if not isinstance(time_base, Mapping):
                        raise ValueError("time base")
                    numerator = int(time_base["numerator"])
                    denominator = int(time_base["denominator"])
                    if denominator <= 0:
                        raise ValueError("time base")
                    start_pts = int(planned_exact_render_span["start_pts"])
                    end_pts = int(planned_exact_render_span["end_pts"])
                    expected_decode_in = float(
                        Fraction(start_pts * numerator, denominator)
                    )
                    expected_decode_out = float(
                        Fraction(end_pts * numerator, denominator)
                    )
                    expected_decode_duration = float(
                        Fraction(
                            (end_pts - start_pts) * numerator,
                            denominator,
                        )
                    )
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    expected_decode_in = None
                    expected_decode_out = None
                    expected_decode_duration = None
                    slot_errors.append("selected exact PTS decode bounds are malformed")
                if (
                    observed_exact != expected_exact
                    or clean.get("selection_mode") != "persisted_exact_pts"
                    or clean.get("clean_frame_start")
                    != planned_exact_render_span.get("source_frame_start")
                    or clean.get("clean_frame_end")
                    != planned_exact_render_span.get("source_frame_end")
                    or clean.get("source_frames_used")
                    != planned_exact_render_span.get("source_frames")
                    or clean.get("decode_source_in") != expected_decode_in
                    or clean.get("decode_source_out") != expected_decode_out
                    or clean.get("decode_duration") != expected_decode_duration
                    or rendered.get("decode_source_in") != expected_decode_in
                    or rendered.get("decode_source_out") != expected_decode_out
                    or rendered.get("decode_duration")
                    != expected_decode_duration
                    or rendered.get("copyts") is not True
                    or rendered.get("input_seek")
                    != (
                        max(0.0, expected_decode_in - 2.0)
                        if expected_decode_in is not None
                        else None
                    )
                    or rendered.get("input_duration") is not None
                    or rendered.get("selected_gate_exact_render_span")
                    != planned_exact_render_span
                    or str(
                        rendered.get("selected_gate_exact_render_span_hash")
                        or ""
                    )
                    != canonical_hash(planned_exact_render_span)
                ):
                    slot_errors.append(
                        "render exact PTS facts differ from selected gate"
                    )

        if isinstance(gate_row, Mapping) and isinstance(selected, Mapping):
            try:
                expected_gate_candidate_binding = (
                    _exact_repair_candidate_binding(selected)
                )
            except (TypeError, ValueError, KeyError):
                expected_gate_candidate_binding = None
                slot_errors.append("selected gate candidate binding is malformed")
            if expected_gate_candidate_binding is not None and (
                gate_row.get("candidate_binding")
                != expected_gate_candidate_binding
                or str(gate_row.get("candidate_binding_hash") or "")
                != canonical_hash(expected_gate_candidate_binding)
            ):
                slot_errors.append("selected gate candidate range differs from timeline")

        selected_binding = {
            "slot_id": slot_id,
            "range": selected_range,
            "candidate_range_hash": str(
                selected.get("candidate_range_hash") or ""
            ),
            "strict_variant_key": str(selected.get("strict_variant_key") or ""),
            "frame_contract_hash": canonical_hash(
                selected.get("frame_contract")
            ),
            "render_frames": selected_frames,
            "render_transform_hash": canonical_hash(
                selected.get("transform")
            ),
            "selected_visual_gate_receipt_hash": gate_receipt_hash,
            "exact_render_span_contract": (
                EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION
            ),
            "exact_render_span_hash": canonical_hash(
                selected.get("exact_render_span")
            ),
        }
        rendered_binding = {
            "slot_id": str(rendered.get("slot_id") or ""),
            "range": rendered_range,
            "render_frames": rendered_frames,
            "production_strict": rendered.get("production_strict") is True,
            "fill_strategy": str(rendered.get("fill_strategy") or ""),
            "render_transform_hash": canonical_hash(rendered.get("transform")),
            "edge_cleanup_margins_hash": canonical_hash(
                rendered.get("edge_cleanup_margins")
            ),
            "clean_frame_contract_hash": canonical_hash(
                rendered.get("clean_frame_contract")
            ),
            "selected_gate_exact_render_span_hash": str(
                rendered.get("selected_gate_exact_render_span_hash") or ""
            ),
        }
        master_render_binding = {
            "master_sha256": master_sha256,
            "manifest_master_sha256": manifest_master_sha256,
            "render_start_frame": rendered_start_frame,
            "render_end_frame": rendered_end_frame,
            "render_frames": rendered_frames,
        }
        receipt_base = {
            "schema_version": 1,
            "contract": VISUAL_QA_PROVENANCE_CONTRACT,
            "slot_index": index,
            "slot_id": slot_id,
            "timeline_slot_hash": canonical_hash(slot),
            "semantic_verdict": verdict,
            "semantic_confidence": slot.get("confidence"),
            "selected_clip_binding": selected_binding,
            "selected_clip_binding_hash": canonical_hash(selected_binding),
            "render_clip_binding": rendered_binding,
            "render_clip_binding_hash": canonical_hash(rendered_binding),
            "master_render_binding": master_render_binding,
            "master_render_binding_hash": canonical_hash(master_render_binding),
            "selected_visual_gate_receipt": validated_gate_receipt,
            "selected_visual_gate_receipt_hash": gate_receipt_hash,
            "exact_render_span_contract": (
                EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION
            ),
            "selected_gate_exact_render_span": planned_exact_render_span,
            "selected_gate_exact_render_span_hash": canonical_hash(
                planned_exact_render_span
            ),
            "rendered_clean_frame_contract_hash": canonical_hash(
                rendered.get("clean_frame_contract")
            ),
            "selected_visual_transform_binding": transform_binding,
            "selected_visual_transform_binding_hash": canonical_hash(
                transform_binding
            ),
            "passed": not slot_errors,
            "errors": slot_errors,
        }
        receipt = {
            **receipt_base,
            "receipt_sha256": canonical_hash(receipt_base),
        }
        rows.append(
            {
                "slot_id": slot_id,
                "timeline_slot_hash": canonical_hash(slot),
                "verdict": verdict if not slot_errors else "wrong",
                "confidence": (
                    float(slot.get("confidence") or 0.0)
                    if not slot_errors
                    else 0.0
                ),
                "reason": (
                    "timeline semantic verdict and selected render range are hash-bound"
                    if not slot_errors
                    else "; ".join(slot_errors)
                ),
                "provenance_receipt": receipt,
            }
        )
    return rows, errors


def stage_visual_qa(context: StageContext) -> Mapping[str, Any]:
    fixture = _fixture_stage(context)
    if fixture is not None:
        return fixture
    from .selected_visual_gate import (
        SELECTED_VISUAL_GATE_RECEIPT_CONTRACT,
        SELECTED_VISUAL_GATE_VERSION,
    )
    from .visual_qa import run_visual_qa

    master = _artifact_path(context, "render", "master")
    render_manifest_path = _artifact_path(context, "render", "render_manifest")
    timeline_path = _artifact_path(context, "global_selection", "timeline")
    timeline = _json(timeline_path)
    render_manifest = _json(render_manifest_path)
    proof_root = context.attempt_root / "proofs"
    master_sha256 = sha256_file(master)
    provenance_rows, provenance_errors = _visual_qa_provenance_rows(
        timeline, render_manifest, master_sha256=master_sha256
    )
    semantic_by_slot_hash = {
        (str(row["slot_id"]), str(row["timeline_slot_hash"])): dict(row)
        for row in provenance_rows
    }

    def semantic_judge(slot: Mapping[str, Any], sheet: Path) -> Mapping[str, Any]:
        key = (str(slot.get("slot_id") or ""), canonical_hash(slot))
        evaluated = semantic_by_slot_hash.get(key)
        if evaluated is None:
            return {
                "verdict": "wrong",
                "confidence": 0.0,
                "reason": "decoded slot differs from the bound timeline slot",
                "provenance_receipt": None,
            }
        receipt = dict(evaluated["provenance_receipt"])
        receipt_base = {
            **receipt,
            "proof_sheet_sha256": sha256_file(sheet),
        }
        receipt_base.pop("receipt_sha256", None)
        return {
            **evaluated,
            "proof_sheet_sha256": receipt_base["proof_sheet_sha256"],
            "provenance_receipt": {
                **receipt_base,
                "receipt_sha256": canonical_hash(receipt_base),
            },
        }

    def transition_judge(slot: Mapping[str, Any], _sheet: Path) -> Mapping[str, Any]:
        candidate = slot.get("candidate")
        if not isinstance(candidate, Mapping):
            return {
                "verdict": "continuous_motion",
                "confidence": 0.0,
                "reason": "transition candidate is malformed; no positive defect proof",
            }
        try:
            mae = float(candidate.get("mae") or 0.0)
            hsv_distance = float(candidate.get("hsv_distance") or 0.0)
        except (TypeError, ValueError):
            mae = hsv_distance = 0.0
        if mae >= 15.0 and hsv_distance >= 0.15:
            return {
                "verdict": "unplanned_transition",
                "confidence": 1.0,
                "reason": "joint hard MAE and HSV discontinuity is positive cut evidence",
            }
        return {
            "verdict": "continuous_motion",
            "confidence": 0.0,
            "reason": (
                "subtle pixel spike has no independent optical-flow/cluster "
                "confirmation and remains diagnostic-only"
            ),
        }

    def overlay_analyzer(
        slot: Mapping[str, Any], _frames: list[np.ndarray]
    ) -> Mapping[str, Any]:
        key = (str(slot.get("slot_id") or ""), canonical_hash(slot))
        evaluated = semantic_by_slot_hash.get(key)
        provenance = (
            evaluated.get("provenance_receipt")
            if isinstance(evaluated, Mapping)
            else None
        )
        gate_receipt = (
            provenance.get("selected_visual_gate_receipt")
            if isinstance(provenance, Mapping)
            else None
        )
        passed = (
            isinstance(gate_receipt, Mapping)
            and gate_receipt.get("status") == "passed"
            and not gate_receipt.get("rejection_codes")
        )
        geometry = (
            (gate_receipt.get("evidence") or {}).get("render_geometry_proxy")
            if isinstance(gate_receipt, Mapping)
            else None
        )
        return {
            "contract": SELECTED_VISUAL_GATE_RECEIPT_CONTRACT,
            "gate_version": SELECTED_VISUAL_GATE_VERSION,
            "action": "none" if passed else "reject",
            "excluded_ranges": [],
            "inpaint_markers": [],
            "passed": passed,
            "positive_evidence_policy": (
                "stable_ocr_or_logo_or_known_profile_projection_v1"
            ),
            "gate_receipt_hash": (
                canonical_hash(gate_receipt)
                if isinstance(gate_receipt, Mapping)
                else None
            ),
            "render_geometry_proxy": geometry,
            "persistent_pixel_region_policy": "diagnostic_only",
        }

    combined = run_visual_qa(
        master_path=master,
        timeline_path=timeline_path,
        timeline=timeline,
        proof_root=proof_root,
        semantic_judge=semantic_judge,
        transition_judge=transition_judge,
        overlay_analyzer=overlay_analyzer,
    )

    structural_errors: list[str] = list(provenance_errors)
    manifest_output = render_manifest.get("output")
    timeline_sha256 = sha256_file(timeline_path)
    combined_input_hashes = combined.get("input_hashes")
    if (
        not isinstance(manifest_output, Mapping)
        or str(manifest_output.get("sha256") or "") != master_sha256
    ):
        structural_errors.append("render manifest master hash differs from master")
    if (
        not isinstance(combined_input_hashes, Mapping)
        or str(combined_input_hashes.get("master") or "") != master_sha256
        or str(combined_input_hashes.get("timeline") or "") != timeline_sha256
    ):
        structural_errors.append("decoded visual QA inputs differ from bound artifacts")
    if combined.get("decoded_frames") != combined.get("expected_frames"):
        structural_errors.append("master frame count differs from the exact timeline")
    if int(combined.get("slot_count") or 0) != len(timeline.get("slots") or ()):
        structural_errors.append("not every timeline slot was decoded")
    semantic_errors = list(structural_errors)
    boundary_errors = list(structural_errors)
    overlay_errors = list(structural_errors)
    for row in combined.get("slots") or ():
        slot_id = str(row.get("slot_id"))
        if (row.get("semantic") or {}).get("verdict") not in {"exact", "relevant"}:
            semantic_errors.append(f"semantic mismatch in {slot_id}")
        if row.get("unplanned_transitions"):
            boundary_errors.append(f"unplanned transition in {slot_id}")
        if not bool((row.get("overlay") or {}).get("passed")):
            overlay_errors.append(f"residual overlay in {slot_id}")
    bindings = {
        "master": master_sha256,
        "timeline": timeline_sha256,
        "render_manifest": sha256_file(render_manifest_path),
    }
    semantic_receipts = [
        dict((row.get("semantic") or {}).get("provenance_receipt") or {})
        for row in combined.get("slots") or ()
    ]
    selected_gate_master_bindings = [
        {
            "slot_id": str(receipt.get("slot_id") or ""),
            "master_render_binding": dict(
                receipt.get("master_render_binding") or {}
            ),
            "selected_visual_gate_receipt_hash": str(
                receipt.get("selected_visual_gate_receipt_hash") or ""
            ),
            "exact_render_span_contract": str(
                receipt.get("exact_render_span_contract") or ""
            ),
            "selected_gate_exact_render_span_hash": str(
                receipt.get("selected_gate_exact_render_span_hash") or ""
            ),
        }
        for receipt in semantic_receipts
    ]
    reports = {
        "visual_qa": {
            "schema_version": 3,
            "contract": VISUAL_QA_PROVENANCE_CONTRACT,
            "stage_version": VISUAL_QA_STAGE_VERSION,
            "assurance_scope": (
                "provider-free provenance verifier bound to the decoded master; "
                "pixel acceptance is inherited from the selected-span gate under "
                "the exact persisted crop and transform"
            ),
            "passed": not semantic_errors,
            "errors": semantic_errors,
            "input_hashes": bindings,
            "semantic_provenance_receipts": semantic_receipts,
            "semantic_provenance_receipts_hash": canonical_hash(
                semantic_receipts
            ),
            "slots": [
                {"slot_id": row["slot_id"], "semantic": row["semantic"]}
                for row in combined.get("slots") or ()
            ],
        },
        "boundary_qa": {
            "schema_version": 3,
            "stage_version": VISUAL_QA_STAGE_VERSION,
            "passed": not boundary_errors,
            "errors": boundary_errors,
            "input_hashes": bindings,
            "slots": [
                {
                    "slot_id": row["slot_id"],
                    "unplanned_transitions": row["unplanned_transitions"],
                }
                for row in combined.get("slots") or ()
            ],
        },
        "overlay_qa": {
            "schema_version": 3,
            "stage_version": VISUAL_QA_STAGE_VERSION,
            "assurance_scope": (
                "selected-span gate provenance under the exact persisted crop, "
                "transform and decoded-master frame span; this report does not "
                "claim an independent full-master pixel classifier"
            ),
            "passed": not overlay_errors,
            "errors": overlay_errors,
            "input_hashes": bindings,
            "selected_gate_master_bindings": selected_gate_master_bindings,
            "selected_gate_master_bindings_hash": canonical_hash(
                selected_gate_master_bindings
            ),
            "slots": [
                {"slot_id": row["slot_id"], "overlay": row["overlay"]}
                for row in combined.get("slots") or ()
            ],
        },
    }
    report_artifacts: list[dict[str, Any]] = []
    for name, report in reports.items():
        report_artifacts.append(_artifact(name, _write_json(context, f"{name}.json", report)))
    proof_artifacts = [
        _artifact(
            "proof-" + hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:20],
            path,
            kind="visual_qa_proof",
        )
        for path in sorted(proof_root.glob("*.jpg"))
    ]
    result = {
        "status": "passed" if all(row["passed"] for row in reports.values()) else "failed",
        "artifacts": [*report_artifacts, *proof_artifacts],
    }
    failures = [name for name, row in reports.items() if not row["passed"]]
    if failures:
        raise StageBlocked(
            "needs_visual_repair",
            f"automatic visual QA failed: {', '.join(failures)}",
            result=result,
        )
    return result


def stage_audio_qa(context: StageContext) -> Mapping[str, Any]:
    fixture = _fixture_stage(context)
    if fixture is not None:
        return fixture
    from .audio_qa import run_diegetic_audio_qa
    from .sound_qa import run_sound_qa

    try:
        ambience = _artifact_path(context, "render", "ambience_pcm")
        presence = _artifact_path(context, "render", "presence_pcm")
        events = _artifact_path(context, "render", "events_pcm")
        premaster = _artifact_path(context, "render", "premaster_pcm")
    except RuntimeError as exc:
        raise StageBlocked(
            "needs_audio_qa_stems",
            "render must expose exact ambience/presence/events PCM artifacts",
        ) from exc
    timeline_path = _artifact_path(context, "global_selection", "timeline")
    sound_path = _artifact_path(context, "sound_resolution", "sound_plan")
    render_manifest_path = _artifact_path(context, "render", "render_manifest")
    master = _artifact_path(context, "render", "master")
    timeline = _json(timeline_path)
    portable_plan = _json(sound_path)
    runtime_timeline = _hydrate_timeline(context, timeline)
    runtime_plan = _hydrate_sound_plan(context, portable_plan)
    render_manifest = _json(render_manifest_path)
    manifest_audio = render_manifest.get("audio")
    physical_foley_render_facts = (
        manifest_audio.get("physical_foley_audibility")
        if isinstance(manifest_audio, Mapping)
        else None
    )
    if not isinstance(physical_foley_render_facts, list):
        raise StageBlocked(
            "needs_audio_repair",
            "render manifest lacks required physical foley audibility receipts",
        )
    premaster_sha256 = sha256_file(premaster)
    for row in physical_foley_render_facts:
        ab_preview = row.get("ab_preview") if isinstance(row, Mapping) else None
        if (
            not isinstance(ab_preview, Mapping)
            or ab_preview.get("source_premaster_sha256") != premaster_sha256
        ):
            raise StageBlocked(
                "needs_audio_repair",
                "required cue A/B preview is not bound to the rendered premaster",
            )
    structural = run_sound_qa(
        runtime_plan,
        runtime_timeline,
        ambience_wav=ambience,
        presence_wav=presence,
        events_wav=events,
        physical_foley_render_facts=physical_foley_render_facts,
        release_mode=True,
        verify_hashes=True,
        require_causal_compilation=True,
    )
    contradictions = sum(
        len(structural.get(name) or ())
        for name in (
            "wrong_species",
            "wrong_action",
            "wrong_source_kind",
            "hard_negative_violations",
        )
    )
    notable = structural.get("notable_source_coverage") or {}
    semantic = {
        "passed": structural.get("verdict") == "PASS",
        "acoustic_contradictions": contradictions,
        "notable_source_coverage": float(notable.get("weighted_ratio") or 0.0),
        "sound_qa": structural,
    }
    probabilities = {"speech_probability": 0.0, "music_probability": 0.0}
    for lane in ("ambience_regions", "presence_layers", "event_cues"):
        for row in runtime_plan.get(lane) or ():
            quality = ((row.get("asset") or {}).get("quality") or {})
            for key in probabilities:
                probabilities[key] = max(
                    probabilities[key], float(quality.get(key) or 0.0)
                )
    boundaries = [float(row["end"]) for row in timeline["slots"][:-1]]
    report = run_diegetic_audio_qa(
        ambience_path=ambience,
        presence_path=presence,
        events_path=events,
        expected_duration=float(timeline["narration_duration"]),
        boundaries=boundaries,
        semantic_report=semantic,
        contamination_report=probabilities,
    )
    report.update(
        {
            "schema_version": 2,
            "input_hashes": {
                "master": sha256_file(master),
                "timeline": sha256_file(timeline_path),
                "sound_plan": sha256_file(sound_path),
            },
            "stem_hashes": {
                "ambience": sha256_file(ambience),
                "presence": sha256_file(presence),
                "events": sha256_file(events),
            },
        }
    )
    target = _write_json(context, "audio_qa.json", report)
    result = {
        "status": "passed" if report["passed"] else "failed",
        "artifacts": [_artifact("audio_qa", target)],
    }
    if not report["passed"]:
        raise StageBlocked(
            "needs_audio_repair",
            "; ".join(report.get("errors") or ["diegetic audio QA failed"]),
            result=result,
        )
    return result


def stage_release_qa(context: StageContext) -> Mapping[str, Any]:
    fixture = _fixture_stage(context)
    if fixture is not None:
        return fixture
    from .release_gate import ReleaseInputs, write_release_report

    master = _artifact_path(context, "render", "master")
    timeline = _artifact_path(context, "global_selection", "timeline")
    sound = _artifact_path(context, "sound_resolution", "sound_plan")
    manifest = _artifact_path(context, "render", "render_manifest")
    visual = _artifact_path(context, "visual_qa", "visual_qa")
    boundary = _artifact_path(context, "visual_qa", "boundary_qa")
    overlay = _artifact_path(context, "visual_qa", "overlay_qa")
    audio = _artifact_path(context, "audio_qa", "audio_qa")
    target = context.attempt_root / "release_report.json"
    write_release_report(
        ReleaseInputs(master, timeline, sound, manifest, visual, boundary, overlay, audio),
        target,
    )
    return {"status": "released", "artifacts": [_artifact("release_report", target)]}


def build_production_stage_registry() -> StageRegistry:
    """Return the complete explicit production DAG used by the CLI."""

    registry = StageRegistry()
    definitions: list[tuple[str, Callable[[StageContext], Mapping[str, Any]], tuple[str, ...]]] = [
        ("script_qa", stage_script_qa, ()),
        ("tts_or_narration_import", stage_tts_or_narration_import, ("script_qa",)),
        ("beats", stage_beats, ("tts_or_narration_import",)),
        ("donor_index", stage_donor_index, ()),
        ("overlay_analysis", stage_overlay_analysis, ("donor_index",)),
        (
            "temporal_retrieval",
            stage_temporal_retrieval,
            ("beats", "donor_index", "overlay_analysis"),
        ),
        (
            "semantic_judge",
            stage_semantic_judge,
            (
                "beats",
                "tts_or_narration_import",
                "temporal_retrieval",
                "donor_index",
                "overlay_analysis",
            ),
        ),
        ("global_selection", stage_global_selection, ("beats", "temporal_retrieval", "semantic_judge")),
        ("acoustic_plan", stage_acoustic_plan, ("global_selection",)),
        ("sound_resolution", stage_sound_resolution, ("acoustic_plan", "global_selection")),
        ("render", stage_render, ("tts_or_narration_import", "global_selection", "sound_resolution")),
        ("visual_qa", stage_visual_qa, ("render", "global_selection")),
        ("audio_qa", stage_audio_qa, ("render", "sound_resolution", "global_selection")),
        ("release_qa", stage_release_qa, ("render", "global_selection", "sound_resolution", "visual_qa", "audio_qa")),
    ]
    for name, handler, dependencies in definitions:
        registry.register(
            StageDefinition(
                name,
                handler,
                dependencies=dependencies,
                version=(
                    TEMPORAL_RETRIEVAL_STAGE_VERSION
                    if name == "temporal_retrieval"
                    else SEMANTIC_JUDGE_STAGE_VERSION
                    if name == "semantic_judge"
                    else ACOUSTIC_PLAN_STAGE_VERSION
                    if name == "acoustic_plan"
                    else SOUND_RESOLUTION_STAGE_VERSION
                    if name == "sound_resolution"
                    else RENDER_STAGE_VERSION
                    if name == "render"
                    else VISUAL_QA_STAGE_VERSION
                    if name == "visual_qa"
                    else PRODUCTION_STAGE_VERSION
                ),
                paid=name in {"temporal_retrieval", "semantic_judge", "acoustic_plan"},
            )
        )
    return registry


__all__ = [
    "ACOUSTIC_PLAN_STAGE_VERSION",
    "PRODUCTION_STAGE_VERSION",
    "RENDER_STAGE_VERSION",
    "SOUND_RESOLUTION_STAGE_VERSION",
    "POST_RESEG_SEMANTIC_REPAIR_POLICY_VERSION",
    "EXACT_SEMANTIC_REPAIR_CONTRACT_VERSION",
    "GLOBAL_ASSIGNMENT_EXACT_WITNESS_CONTRACT",
    "GLOBAL_ASSIGNMENT_MATERIALIZATION_POLICY_VERSION",
    "GLOBAL_COLLISION_REPAIR_NAMESPACE",
    "GLOBAL_COLLISION_REPAIR_POLICY_VERSION",
    "GLOBAL_COLLISION_REPAIR_STAGE_VERSION",
    "GLOBAL_COLLISION_TARGET_RULE_VERSION",
    "SEMANTIC_JUDGE_STAGE_VERSION",
    "TEMPORAL_RETRIEVAL_STAGE_VERSION",
    "VISUAL_QA_PROVENANCE_CONTRACT",
    "VISUAL_QA_STAGE_VERSION",
    "build_production_stage_registry",
]
