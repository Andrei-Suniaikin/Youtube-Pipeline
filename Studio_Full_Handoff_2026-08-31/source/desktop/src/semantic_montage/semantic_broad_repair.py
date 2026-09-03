"""Bounded one-slot semantic repair over the immutable broad catalog.

This module deliberately does not own a production stage.  It exposes a
stage-agnostic helper which accepts an already validated *active fallback*
brief, runs the normal broad/lazy/exact retrieval stack for that one slot, and
converts its winners into the repair-sheet namespace used by semantic repair.

Persisted output is path-free.  Host paths are returned only in a separate
runtime artifact mapping so callers can register them without leaking a mount
layout into receipts or cache identities.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
import re
from typing import Any

from .broad_lazy import BroadLazyError, validate_broad_catalog
from .broad_lazy_adapter import (
    BroadLazyAdapterError,
    _exact_candidate as _adapter_exact_candidate,
    materialize_exact_repair_candidate,
    materialize_temporal_retrieval_v2,
    validate_exact_candidate_contract,
)
from .broad_lazy_runtime import (
    MAX_STRICT_VARIANTS_PER_TASK,
    BroadLazyRuntimeBlocked,
    BroadLazyRuntimeError,
)
from .common import canonical_hash, interval_frame_span, sha256_file
from .semantic import (
    VISUAL_BRIEF_CONTRACT_VERSION,
    validate_visual_brief_contract,
)


SEMANTIC_BROAD_REPAIR_CONTRACT = "semantic_broad_repair_v2"
SEMANTIC_BROAD_REPAIR_BINDING_CONTRACT = "semantic_broad_repair_binding_v2"
SEMANTIC_BROAD_REPAIR_BLOCK_CONTRACT = "semantic_broad_repair_block_v2"
FROZEN_EXACT_FILTER_CONTRACT = "semantic_broad_frozen_exact_filter_v1"

MAX_CATALOG_CANDIDATES = 50_000
MAX_PAGE_SIZE = 64
MAX_SURVIVORS = 16
MAX_WORKERS = 8
MAX_EMBEDDING_BATCH_SIZE = 256
MAX_SEARCH_SECONDS = 30.0
MAX_REPAIR_POOL_SIZE = MAX_SURVIVORS * MAX_STRICT_VARIANTS_PER_TASK

_REPAIR_POLICY = {
    "full_immutable_catalog": True,
    "one_slot_only": True,
    "manual_candidate_or_range_injection": False,
    "fill_allowed": False,
    "retime_allowed": False,
    "inpaint_allowed": False,
    "manual_selection_allowed": False,
    "unique_broad_parent_quota": 1,
    "frozen_exact_filter_before_survivor_quota": True,
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ABSOLUTE_PATH = re.compile(r"^(?:/|~/|file://|[A-Za-z]:[\\/])")
_EMBEDDED_HOST_PATH = re.compile(
    r"(?:file://|[A-Za-z]:[\\/]|/(?:Users|home|tmp|var|opt|Volumes|mnt)/)"
)
_PATH_FIELDS = {
    "path",
    "source_path",
    "absolute_path",
    "contact_sheet",
    "thumbnail",
    "original_location",
    "original_locations",
}


class SemanticBroadRepairError(ValueError):
    """The one-slot broad repair request or its receipt is invalid."""


class SemanticBroadRepairBlocked(RuntimeError):
    """The bounded broad repair produced no usable new exact candidate."""

    def __init__(self, code: str, message: str, artifact: Mapping[str, Any]):
        super().__init__(message)
        self.code = str(code)
        self.artifact = _clone(dict(artifact), "blocked artifact")


def _clone(value: Any, name: str) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise SemanticBroadRepairError(f"{name} must be finite JSON data") from exc


def _assert_path_free(value: Any, name: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if (
                normalized_key in _PATH_FIELDS
                or normalized_key.endswith("_path")
                or normalized_key.endswith("_paths")
            ):
                raise SemanticBroadRepairError(
                    f"{name} contains runtime path field {key!r}"
                )
            _assert_path_free(item, f"{name}.{key}")
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for index, item in enumerate(value):
            _assert_path_free(item, f"{name}[{index}]")
    elif isinstance(value, str) and (
        _ABSOLUTE_PATH.match(value.strip()) or _EMBEDDED_HOST_PATH.search(value)
    ):
        raise SemanticBroadRepairError(f"{name} contains an absolute host path")


def _portable_id(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result or _ABSOLUTE_PATH.match(result):
        raise SemanticBroadRepairError(f"{name} must be a portable identifier")
    return result


def _sha256(value: Any, name: str) -> str:
    digest = str(value or "").strip().casefold()
    if not _SHA256.fullmatch(digest):
        raise SemanticBroadRepairError(f"{name} must be a SHA-256")
    return digest


def _bounded_int(value: Any, name: str, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise SemanticBroadRepairError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SemanticBroadRepairError(
            f"{name} must be a positive integer"
        ) from exc
    if result <= 0 or result != value or result > maximum:
        raise SemanticBroadRepairError(
            f"{name} must be between 1 and {maximum}"
        )
    return result


def _positive_finite(value: Any, name: str, *, maximum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SemanticBroadRepairError(f"{name} must be numeric") from exc
    if not math.isfinite(result) or result <= 0 or (
        maximum is not None and result > maximum
    ):
        suffix = f" and at most {maximum}" if maximum is not None else ""
        raise SemanticBroadRepairError(f"{name} must be positive, finite{suffix}")
    return result


def _fps_document(output_fps: float) -> dict[str, int]:
    value = _positive_finite(output_fps, "output_fps")
    rate = Fraction(str(value)).limit_denominator(1_000_000)
    if abs(float(rate) - value) > 1e-9:
        raise SemanticBroadRepairError(
            "output_fps cannot be represented by a stable rational"
        )
    return {"numerator": rate.numerator, "denominator": rate.denominator}


def _content_document(
    base: Mapping[str, Any], *, identifier_field: str, identifier_prefix: str
) -> dict[str, Any]:
    portable = _clone(dict(base), "portable document")
    _assert_path_free(portable)
    digest = canonical_hash(portable)
    return {
        **portable,
        "content_hash": digest,
        identifier_field: f"{identifier_prefix}{digest[:24]}",
    }


def _validate_content_document(
    document: Mapping[str, Any],
    *,
    contract: str,
    identifier_field: str,
    identifier_prefix: str,
) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise SemanticBroadRepairError("receipt must be an object")
    row = _clone(dict(document), "receipt")
    _assert_path_free(row, "receipt")
    if row.get("schema_version") != 1 or row.get("contract") != contract:
        raise SemanticBroadRepairError(f"expected {contract} schema v1")
    supplied_hash = _sha256(row.get("content_hash"), "receipt.content_hash")
    supplied_id = str(row.get(identifier_field) or "")
    base = {
        key: value
        for key, value in row.items()
        if key not in {"content_hash", identifier_field}
    }
    actual_hash = canonical_hash(base)
    if supplied_hash != actual_hash:
        raise SemanticBroadRepairError("repair receipt content hash mismatch")
    if supplied_id != f"{identifier_prefix}{actual_hash[:24]}":
        raise SemanticBroadRepairError("repair receipt identifier mismatch")
    return row


def _frozen_range_hashes(
    frozen_candidate_universe: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    if frozen_candidate_universe is None:
        return []
    if not isinstance(frozen_candidate_universe, Sequence) or isinstance(
        frozen_candidate_universe, (str, bytes, bytearray)
    ):
        raise SemanticBroadRepairError("frozen_candidate_universe must be an array")
    hashes: set[str] = set()
    for index, raw in enumerate(frozen_candidate_universe):
        if not isinstance(raw, Mapping):
            raise SemanticBroadRepairError(
                f"frozen_candidate_universe[{index}] must be an object"
            )
        row = _clone(dict(raw), f"frozen_candidate_universe[{index}]")
        _assert_path_free(row, f"frozen_candidate_universe[{index}]")
        hashes.add(
            _sha256(
                row.get("candidate_range_hash"),
                f"frozen_candidate_universe[{index}].candidate_range_hash",
            )
        )
    return sorted(hashes)


def _frozen_exact_filter_binding(
    callback_contract: str, frozen_hashes: Sequence[str]
) -> dict[str, Any]:
    row = {
        "contract": FROZEN_EXACT_FILTER_CONTRACT,
        "base_callback_contract": _portable_id(
            callback_contract, "callback_contract"
        ),
        "candidate_range_identity_contract": "strict_exact_candidate_range_v1",
        "frozen_candidate_range_count": len(frozen_hashes),
        "frozen_candidate_range_hashes_hash": canonical_hash(list(frozen_hashes)),
        "applied_before_survivor_quota": True,
    }
    row["effective_callback_contract"] = (
        f"{FROZEN_EXACT_FILTER_CONTRACT}-"
        f"{canonical_hash(row)[:24]}"
    )
    return row


def _validate_frozen_exact_filter_binding(
    value: Any,
    *,
    callback_contract: str,
    frozen_count: int,
    frozen_hashes_hash: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SemanticBroadRepairError("strict frozen-filter binding is missing")
    row = _clone(dict(value), "strict frozen-filter binding")
    effective = str(row.pop("effective_callback_contract", "") or "")
    expected_effective = (
        f"{FROZEN_EXACT_FILTER_CONTRACT}-{canonical_hash(row)[:24]}"
    )
    if (
        row.get("contract") != FROZEN_EXACT_FILTER_CONTRACT
        or row.get("base_callback_contract") != callback_contract
        or row.get("candidate_range_identity_contract")
        != "strict_exact_candidate_range_v1"
        or int(row.get("frozen_candidate_range_count") or 0) != frozen_count
        or row.get("frozen_candidate_range_hashes_hash") != frozen_hashes_hash
        or row.get("applied_before_survivor_quota") is not True
        or effective != expected_effective
    ):
        raise SemanticBroadRepairError("strict frozen-filter binding is stale")
    row["effective_callback_contract"] = effective
    return row


def _frozen_filter_strict_callback(
    strict_upgrade_fn: Callable[..., Any],
    *,
    callback_contract: str,
    frozen_hashes: Sequence[str],
) -> tuple[Callable[..., Any], dict[str, Any]]:
    """Filter frozen exact variants before lazy paging counts survivors.

    The broad runtime counts unique strict parents and stops when its survivor
    target is reached.  Filtering only the final exact rows would therefore
    hide later catalog pages whenever the first survivors were already judged.
    This wrapper projects the same exact-range identity used by the adapter,
    removes frozen variants, and lets the unchanged lazy runtime continue
    paging.  The base strict callback keeps its own content-addressed decode
    cache; the wrapper receives a frozen-universe-specific runtime cache key.
    """

    frozen = set(frozen_hashes)
    filter_binding = _frozen_exact_filter_binding(
        callback_contract, list(frozen_hashes)
    )

    def filtered_strict_upgrade(
        candidate: Mapping[str, Any],
        *,
        window: Mapping[str, Any],
        render_frames: int,
    ) -> Mapping[str, Any]:
        raw = strict_upgrade_fn(
            candidate,
            window=window,
            render_frames=render_frames,
        )
        if not isinstance(raw, Mapping) or not frozen:
            return raw
        if str(raw.get("status") or "").casefold() in {
            "rejected",
            "failed",
            "unresolved",
        }:
            return raw
        primary = {
            key: value for key, value in raw.items() if key != "strict_variants"
        }
        raw_variants = raw.get("strict_variants")
        if raw_variants is None:
            ordered: list[Mapping[str, Any]] = [primary]
        elif isinstance(raw_variants, Sequence) and not isinstance(
            raw_variants, (str, bytes, bytearray)
        ):
            if (
                len(raw_variants) > MAX_STRICT_VARIANTS_PER_TASK
                or any(not isinstance(value, Mapping) for value in raw_variants)
            ):
                return raw
            ordered = [primary]
            ordered.extend(raw_variants)
        else:
            # Preserve the base result so the runtime's canonical validator
            # reports the malformed callback instead of silently weakening it.
            return raw
        remaining: list[dict[str, Any]] = []
        seen_ranges: set[str] = set()
        for raw_variant in ordered:
            variant = {
                key: value
                for key, value in raw_variant.items()
                if key != "strict_variants"
            }
            try:
                exact = _adapter_exact_candidate(variant)
            except BroadLazyAdapterError as exc:
                raise SemanticBroadRepairError(
                    f"strict frozen-range projection failed: {exc}"
                ) from exc
            range_hash = _sha256(
                exact.get("candidate_range_hash"),
                "strict candidate.candidate_range_hash",
            )
            if range_hash in frozen or range_hash in seen_ranges:
                continue
            seen_ranges.add(range_hash)
            remaining.append(variant)
        if not remaining:
            raise SemanticBroadRepairError(
                "all exact variants for this strict candidate are frozen"
            )
        if raw_variants is None:
            return remaining[0]
        return {**remaining[0], "strict_variants": remaining}

    filtered_strict_upgrade.__name__ = "semantic_broad_frozen_exact_filter"
    filtered_strict_upgrade.callback_contract = filter_binding[
        "effective_callback_contract"
    ]
    return filtered_strict_upgrade, filter_binding


def _catalog_exhaustion_evidence(
    artifact: Any,
    *,
    catalog: Mapping[str, Any],
    slot_id: str,
    page_size: int,
    survivor_target: int,
    filter_binding: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(artifact, Mapping):
        raise SemanticBroadRepairError(
            "underlying broad block lacks catalog-exhaustion evidence"
        )
    row = _clone(dict(artifact), "underlying broad block")
    _assert_path_free(row, "underlying broad block")
    supplied_hash = _sha256(
        row.get("content_hash"), "underlying broad block.content_hash"
    )
    actual_hash = canonical_hash(
        {key: value for key, value in row.items() if key != "content_hash"}
    )
    provenance = row.get("provenance")
    expected_pages = math.ceil(int(catalog["candidate_count"]) / page_size)
    if (
        supplied_hash != actual_hash
        or row.get("schema_version") != 1
        or row.get("contract") != "broad_lazy_strict_result_v1"
        or row.get("status") != "needs_clean_donor_ranges"
        or row.get("catalog_hash") != catalog.get("content_hash")
        or row.get("slot_ids") != [slot_id]
        or int(row.get("strict_survivors_per_slot") or 0) != survivor_target
        or not isinstance(row.get("strict_parent_counts"), list)
        or len(row["strict_parent_counts"]) != 1
        or int(row["strict_parent_counts"][0]) >= survivor_target
        or not isinstance(provenance, Mapping)
        or int(provenance.get("broad_catalog_candidate_count") or 0)
        != int(catalog["candidate_count"])
        or int(provenance.get("paging_rounds") or 0) != expected_pages
        or int(provenance.get("ranking_pages_scanned") or 0) != expected_pages
        or int(provenance.get("ranked_rows_seen") or 0)
        != int(catalog["candidate_count"])
    ):
        raise SemanticBroadRepairError(
            "underlying broad block does not prove full catalog exhaustion"
        )
    return {
        "contract": "semantic_broad_catalog_exhaustion_v1",
        "catalog_candidate_count": int(catalog["candidate_count"]),
        "page_size": int(page_size),
        "expected_page_rounds": expected_pages,
        "observed_page_rounds": int(provenance["paging_rounds"]),
        "ranked_rows_seen": int(provenance["ranked_rows_seen"]),
        "strict_success_outcomes": int(
            provenance.get("strict_success_outcomes") or 0
        ),
        "strict_rejected_outcomes": int(
            provenance.get("strict_rejected_outcomes") or 0
        ),
        "strict_filter": _clone(dict(filter_binding), "strict filter"),
        "strict_filter_hash": canonical_hash(filter_binding),
        "underlying_block": row,
        "underlying_block_hash": supplied_hash,
        "catalog_exhausted": True,
    }


def _normalized_context(
    catalog: Mapping[str, Any],
    beat: Mapping[str, Any],
    fallback_brief: Mapping[str, Any],
    *,
    render_frames: int,
    output_fps: float,
    model_contract: Mapping[str, Any],
    callback_contract: str,
    detector_policy: Mapping[str, Any],
    frozen_candidate_universe: Sequence[Mapping[str, Any]] | None,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    list[str],
]:
    if not isinstance(catalog, Mapping):
        raise SemanticBroadRepairError("catalog must be an object")
    try:
        normalized_catalog = validate_broad_catalog(catalog)
    except BroadLazyError as exc:
        raise SemanticBroadRepairError(f"broad catalog is invalid: {exc}") from exc
    candidate_count = int(normalized_catalog.get("candidate_count") or 0)
    if not 1 <= candidate_count <= MAX_CATALOG_CANDIDATES:
        raise SemanticBroadRepairError(
            "broad catalog candidate count exceeds the bounded repair contract"
        )
    if not isinstance(beat, Mapping) or not isinstance(fallback_brief, Mapping):
        raise SemanticBroadRepairError("beat and fallback_brief must be objects")
    normalized_beat = _clone(dict(beat), "beat")
    slot_id = _portable_id(normalized_beat.get("slot_id"), "beat.slot_id")
    try:
        start = float(normalized_beat["start"])
        end = float(normalized_beat["end"])
        _start_frame, _end_frame, expected_frames = interval_frame_span(
            start, end, output_fps
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SemanticBroadRepairError(
            f"beat {slot_id} has invalid output timing"
        ) from exc
    requested_frames = _bounded_int(
        render_frames, "render_frames", maximum=1_000_000
    )
    if requested_frames != expected_frames:
        raise SemanticBroadRepairError(
            f"beat {slot_id} requires {expected_frames} frames, not {requested_frames}"
        )
    try:
        normalized_brief = validate_visual_brief_contract(
            normalized_beat, dict(fallback_brief)
        )
    except (TypeError, ValueError) as exc:
        raise SemanticBroadRepairError(
            f"active fallback brief is invalid for {slot_id}: {exc}"
        ) from exc
    if not isinstance(model_contract, Mapping) or not model_contract:
        raise SemanticBroadRepairError("model_contract must be a non-empty object")
    if not isinstance(detector_policy, Mapping):
        raise SemanticBroadRepairError("detector_policy must be an object")
    normalized_model = _clone(dict(model_contract), "model_contract")
    normalized_detector = _clone(dict(detector_policy), "detector_policy")
    _assert_path_free(normalized_model, "model_contract")
    _assert_path_free(normalized_detector, "detector_policy")
    _portable_id(callback_contract, "callback_contract")
    frozen_hashes = _frozen_range_hashes(frozen_candidate_universe)
    return (
        normalized_catalog,
        normalized_beat,
        normalized_brief,
        normalized_model,
        normalized_detector,
        frozen_hashes,
    )


def build_semantic_broad_repair_binding(
    catalog: Mapping[str, Any],
    beat: Mapping[str, Any],
    fallback_brief: Mapping[str, Any],
    *,
    render_frames: int,
    output_fps: float,
    model_contract: Mapping[str, Any],
    callback_contract: str,
    detector_policy: Mapping[str, Any],
    frozen_candidate_universe: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the exact active-state binding later checked by a cached receipt."""

    (
        normalized_catalog,
        normalized_beat,
        normalized_brief,
        normalized_model,
        normalized_detector,
        frozen_hashes,
    ) = _normalized_context(
        catalog,
        beat,
        fallback_brief,
        render_frames=render_frames,
        output_fps=output_fps,
        model_contract=model_contract,
        callback_contract=callback_contract,
        detector_policy=detector_policy,
        frozen_candidate_universe=frozen_candidate_universe,
    )
    strict_filter = _frozen_exact_filter_binding(
        callback_contract, frozen_hashes
    )
    binding = {
        "schema_version": 1,
        "contract": SEMANTIC_BROAD_REPAIR_BINDING_CONTRACT,
        "catalog_hash": str(normalized_catalog["content_hash"]),
        "catalog_cache_key": str(normalized_catalog["cache_key"]),
        "catalog_candidate_count": int(normalized_catalog["candidate_count"]),
        "slot_id": str(normalized_beat["slot_id"]),
        "beat_hash": canonical_hash(normalized_beat),
        "brief_hash": canonical_hash(normalized_brief),
        "brief_contract_version": VISUAL_BRIEF_CONTRACT_VERSION,
        "render_frames": int(render_frames),
        "output_fps": _fps_document(output_fps),
        "model_contract_hash": canonical_hash(normalized_model),
        "callback_contract": str(callback_contract),
        "detector_policy_hash": canonical_hash(normalized_detector),
        "frozen_candidate_range_count": len(frozen_hashes),
        "frozen_candidate_range_hashes_hash": canonical_hash(frozen_hashes),
        "strict_filter": strict_filter,
        "strict_filter_hash": canonical_hash(strict_filter),
    }
    _assert_path_free(binding, "binding")
    return binding


def _blocked_artifact(
    binding: Mapping[str, Any],
    *,
    reason_code: str,
    underlying: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    base = {
        "schema_version": 1,
        "contract": SEMANTIC_BROAD_REPAIR_BLOCK_CONTRACT,
        "status": "needs_clean_donor_ranges",
        "reason_code": _portable_id(reason_code, "reason_code"),
        "binding": _clone(dict(binding), "binding"),
        "binding_hash": canonical_hash(binding),
        "underlying": _clone(dict(underlying or {}), "underlying block evidence"),
    }
    return _content_document(
        base,
        identifier_field="block_id",
        identifier_prefix="semantic-broad-repair-block-",
    )


def _validate_underlying_output(
    payload: Any,
    paths: Any,
    *,
    beat: Mapping[str, Any],
    brief: Mapping[str, Any],
    render_frames: int,
    output_fps: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    if not isinstance(payload, Mapping) or not isinstance(paths, Mapping):
        raise SemanticBroadRepairError(
            "one-slot temporal materializer returned malformed output"
        )
    portable = _clone(dict(payload), "underlying temporal payload")
    _assert_path_free(portable, "underlying temporal payload")
    if portable.get("schema_version") != 2:
        raise SemanticBroadRepairError("underlying temporal payload is not schema v2")
    if portable.get("effective_beats") != [dict(beat)]:
        raise SemanticBroadRepairError("underlying temporal beat binding is stale")
    if portable.get("briefs") != [dict(brief)]:
        raise SemanticBroadRepairError("underlying temporal brief binding is stale")
    beats_hash = canonical_hash([dict(beat)])
    if portable.get("original_beats_hash") != beats_hash or portable.get(
        "effective_beats_hash"
    ) != beats_hash:
        raise SemanticBroadRepairError("underlying temporal beat hash is stale")
    ranked = portable.get("ranked_candidates")
    repair = portable.get("repair_candidates")
    universe = portable.get("candidate_universe")
    if (
        not isinstance(ranked, list)
        or len(ranked) != 1
        or not isinstance(ranked[0], list)
        or not isinstance(repair, list)
        or len(repair) != 1
        or not isinstance(repair[0], list)
        or not isinstance(universe, list)
    ):
        raise SemanticBroadRepairError(
            "underlying temporal candidate arrays are not one-slot aligned"
        )
    diagnostics = portable.get("diagnostics")
    strict = diagnostics.get("strict_upgrade") if isinstance(diagnostics, Mapping) else None
    provenance = strict.get("provenance") if isinstance(strict, Mapping) else None
    lineage = strict.get("lineage_policy") if isinstance(strict, Mapping) else None
    if not isinstance(provenance, Mapping) or not isinstance(lineage, Mapping):
        raise SemanticBroadRepairError(
            "underlying broad diagnostics lack strict provenance"
        )
    if int(provenance.get("manual_selection_count") or 0) != 0 or int(
        provenance.get("capped_donor_count") or 0
    ) != 0:
        raise SemanticBroadRepairError(
            "underlying broad expansion used caps or manual selection"
        )
    if any(
        lineage.get(key) is not False
        for key in (
            "manual_selection_allowed",
            "duration_caps_allowed",
            "fill_allowed",
            "retime_allowed",
        )
    ) or any(
        lineage.get(key) is not True
        for key in (
            "clean_span_variants_preserved",
            "survivor_target_counts_unique_broad_parents",
        )
    ):
        raise SemanticBroadRepairError(
            "underlying broad lineage policy is weaker than repair policy"
        )
    sheet_paths = paths.get("contact_sheets")
    if not isinstance(sheet_paths, Mapping) or not sheet_paths:
        raise SemanticBroadRepairError(
            "underlying temporal materializer returned no exact sheets"
        )
    underlying_sheet_ids = sorted(str(value) for value in sheet_paths)
    if any(
        not value.startswith("sheet-") or value.startswith("repair-sheet-")
        for value in underlying_sheet_ids
    ):
        raise SemanticBroadRepairError(
            "underlying exact sheet namespace is invalid"
        )

    combined: dict[str, dict[str, Any]] = {}
    for raw in [*repair[0], *ranked[0]]:
        if not isinstance(raw, Mapping):
            raise SemanticBroadRepairError(
                "underlying ranked exact survivor is not an object"
            )
        row = _clone(dict(raw), "underlying ranked exact survivor")
        _assert_path_free(row, "underlying ranked exact survivor")
        try:
            validate_exact_candidate_contract(
                row,
                output_fps=output_fps,
                required_render_frames=render_frames,
            )
        except BroadLazyAdapterError as exc:
            raise SemanticBroadRepairError(
                f"underlying ranked exact survivor is invalid: {exc}"
            ) from exc
        range_hash = _sha256(
            row.get("candidate_range_hash"), "candidate.candidate_range_hash"
        )
        prior = combined.get(range_hash)
        if prior is None or _candidate_rank_key(row) < _candidate_rank_key(prior):
            combined[range_hash] = row
    return portable, list(combined.values()), underlying_sheet_ids


def _candidate_rank_key(candidate: Mapping[str, Any]) -> tuple[float, int, str]:
    try:
        score = float(
            candidate.get("exact_retrieval_score", candidate.get("retrieval_score"))
        )
        rank = int(candidate.get("broad_rank") or 0)
    except (TypeError, ValueError) as exc:
        raise SemanticBroadRepairError(
            "ranked exact survivor has malformed score/rank"
        ) from exc
    if not math.isfinite(score):
        raise SemanticBroadRepairError("ranked exact survivor has non-finite score")
    return (-score, rank, str(candidate.get("clip_id") or ""))


def _assert_candidate_policy(
    candidate: Mapping[str, Any], *, output_fps: float, render_frames: int
) -> None:
    try:
        validate_exact_candidate_contract(
            candidate,
            output_fps=output_fps,
            required_render_frames=render_frames,
        )
    except BroadLazyAdapterError as exc:
        raise SemanticBroadRepairError(
            f"repair candidate failed exact contract: {exc}"
        ) from exc
    if candidate.get("fill_strategy") != "none" or abs(
        float(candidate.get("retime_factor") or 0.0) - 1.0
    ) > 1e-9:
        raise SemanticBroadRepairError("repair candidate enables fill or retiming")
    if any(
        bool(candidate.get(field))
        for field in (
            "manual_selection",
            "manual_crop",
            "allow_hold",
            "allow_tpad",
            "held_tail_seconds",
            "inpaint",
        )
    ):
        raise SemanticBroadRepairError(
            "repair candidate enables manual/fill/inpaint behavior"
        )
    overlay = candidate.get("overlay_analysis")
    if not isinstance(overlay, Mapping):
        raise SemanticBroadRepairError("repair candidate lacks overlay evidence")
    action = str(overlay.get("action") or "")
    if action not in {"none", "crop"} or overlay.get("inpaint_markers"):
        raise SemanticBroadRepairError(
            "repair candidate requires unresolved inpaint/manual overlay work"
        )


def _exact_range_receipt(candidate: Mapping[str, Any]) -> dict[str, Any]:
    try:
        row = {
            "clip_id": _portable_id(candidate.get("clip_id"), "candidate.clip_id"),
            "parent_clip_id": _portable_id(
                candidate.get("parent_clip_id"), "candidate.parent_clip_id"
            ),
            "candidate_range_hash": _sha256(
                candidate.get("candidate_range_hash"),
                "candidate.candidate_range_hash",
            ),
            "source_asset_id": _portable_id(
                candidate.get("source_asset_id"), "candidate.source_asset_id"
            ),
            "source_sha256": _sha256(
                candidate.get("source_sha256"), "candidate.source_sha256"
            ),
            "source_in": round(float(candidate.get("source_in")), 9),
            "source_out": round(float(candidate.get("source_out")), 9),
            "source_frame_start": int(candidate.get("source_frame_start")),
            "source_frame_end": int(candidate.get("source_frame_end")),
            "clean_frame_start": int(candidate.get("clean_frame_start")),
            "clean_frame_end": int(candidate.get("clean_frame_end")),
            "render_frames": int(candidate.get("render_frames")),
            "time_base": _clone(candidate.get("time_base"), "candidate.time_base"),
        }
    except (TypeError, ValueError) as exc:
        raise SemanticBroadRepairError(
            "repair candidate exact range receipt is malformed"
        ) from exc
    _assert_path_free(row, "exact range receipt")
    return row


def _materialize_repair_row(
    row: Mapping[str, Any],
    *,
    asset_resolver: Mapping[str, str | os.PathLike[str]],
    repair_cache_directory: str | os.PathLike[str],
    output_fps: float,
    ffmpeg: str,
    repair_materializer: Callable[..., Any],
    repair_sheet_maker: Callable[..., Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    kwargs: dict[str, Any] = {
        "asset_resolver": asset_resolver,
        "cache_directory": repair_cache_directory,
        "output_fps": output_fps,
        "ffmpeg": ffmpeg,
    }
    if repair_sheet_maker is not None:
        kwargs["sheet_maker"] = repair_sheet_maker
    try:
        result = repair_materializer(row, **kwargs)
    except (BroadLazyAdapterError, BroadLazyRuntimeError) as exc:
        raise SemanticBroadRepairError(
            f"repair-sheet materialization failed: {exc}"
        ) from exc
    if (
        not isinstance(result, tuple)
        or len(result) != 2
        or not isinstance(result[0], Mapping)
        or not isinstance(result[1], Mapping)
    ):
        raise SemanticBroadRepairError(
            "repair materializer returned malformed candidate/artifact"
        )
    return (
        _clone(dict(result[0]), "repair candidate"),
        dict(result[1]),
    )


def materialize_semantic_broad_repair(
    catalog: Mapping[str, Any],
    beat: Mapping[str, Any],
    fallback_brief: Mapping[str, Any],
    *,
    render_frames: int,
    asset_resolver: Mapping[str, str | os.PathLike[str]],
    broad_cache_directory: str | os.PathLike[str],
    repair_cache_directory: str | os.PathLike[str],
    artifact_directory: str | os.PathLike[str],
    model_contract: Mapping[str, Any],
    strict_upgrade_fn: Callable[..., Any],
    detector_policy: Mapping[str, Any],
    output_fps: float,
    callback_contract: str,
    overlay_profiles: Mapping[str, Mapping[str, Any]] | None = None,
    frozen_candidate_universe: Sequence[Mapping[str, Any]] | None = None,
    active_binding: Mapping[str, Any] | None = None,
    survivor_target: int = 6,
    page_size: int = 20,
    max_workers: int = 4,
    embedding_batch_size: int = 64,
    search_seconds: float = 4.0,
    ffmpeg: str = "ffmpeg",
    query_embedder: Callable[..., Any] | None = None,
    broad_sheet_maker: Callable[..., Any] | None = None,
    broad_embedding_fn: Callable[..., Any] | None = None,
    exact_sheet_maker: Callable[..., Any] | None = None,
    exact_embedding_fn: Callable[..., Any] | None = None,
    repair_sheet_maker: Callable[..., Any] | None = None,
    temporal_materializer: Callable[..., Any] = materialize_temporal_retrieval_v2,
    repair_materializer: Callable[..., Any] = materialize_exact_repair_candidate,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Discover and materialize bounded new exact candidates for one slot.

    No candidate ID or source range can be supplied to this API.  Discovery is
    performed from the validated full broad catalog and the active fallback
    query.  The returned receipt is sealed/path-free; ``runtime_artifacts`` is
    the only return value that contains local paths.  Broad retrieval state
    and final repair sheets deliberately use separate cache roots.
    """

    target = _bounded_int(
        survivor_target, "survivor_target", maximum=MAX_SURVIVORS
    )
    page = _bounded_int(page_size, "page_size", maximum=MAX_PAGE_SIZE)
    workers = _bounded_int(max_workers, "max_workers", maximum=MAX_WORKERS)
    batch_size = _bounded_int(
        embedding_batch_size,
        "embedding_batch_size",
        maximum=MAX_EMBEDDING_BATCH_SIZE,
    )
    search = _positive_finite(
        search_seconds, "search_seconds", maximum=MAX_SEARCH_SECONDS
    )
    (
        normalized_catalog,
        normalized_beat,
        normalized_brief,
        normalized_model,
        normalized_detector,
        frozen_hashes,
    ) = _normalized_context(
        catalog,
        beat,
        fallback_brief,
        render_frames=render_frames,
        output_fps=output_fps,
        model_contract=model_contract,
        callback_contract=callback_contract,
        detector_policy=detector_policy,
        frozen_candidate_universe=frozen_candidate_universe,
    )
    binding = build_semantic_broad_repair_binding(
        normalized_catalog,
        normalized_beat,
        normalized_brief,
        render_frames=render_frames,
        output_fps=output_fps,
        model_contract=normalized_model,
        callback_contract=callback_contract,
        detector_policy=normalized_detector,
        frozen_candidate_universe=frozen_candidate_universe,
    )
    if active_binding is not None:
        expected = _clone(dict(active_binding), "active_binding")
        _assert_path_free(expected, "active_binding")
        if expected != binding:
            raise SemanticBroadRepairError("active fallback repair binding is stale")
    declared_callback = str(
        getattr(strict_upgrade_fn, "callback_contract", "") or ""
    )
    if declared_callback and declared_callback != str(callback_contract):
        raise SemanticBroadRepairError(
            "strict callback declaration disagrees with callback_contract"
        )
    filtered_strict_upgrade, strict_filter = _frozen_filter_strict_callback(
        strict_upgrade_fn,
        callback_contract=str(callback_contract),
        frozen_hashes=frozen_hashes,
    )
    if (
        binding.get("strict_filter") != strict_filter
        or binding.get("strict_filter_hash") != canonical_hash(strict_filter)
    ):
        raise SemanticBroadRepairError("strict frozen-filter binding is stale")

    temporal_kwargs: dict[str, Any] = {
        "slot_render_frames": {str(normalized_beat["slot_id"]): int(render_frames)},
        "asset_resolver": asset_resolver,
        "cache_directory": broad_cache_directory,
        "artifact_directory": artifact_directory,
        "model_contract": normalized_model,
        "strict_upgrade_fn": filtered_strict_upgrade,
        "detector_policy": normalized_detector,
        "output_fps": output_fps,
        "survivor_target": target,
        "repair_pool_size": min(
            MAX_REPAIR_POOL_SIZE, target * MAX_STRICT_VARIANTS_PER_TASK
        ),
        "page_size": page,
        "broad_sheet_workers": workers,
        "strict_workers": workers,
        "exact_sheet_workers": workers,
        "embedding_batch_size": batch_size,
        "search_seconds": search,
        "overlay_profiles": overlay_profiles,
        "callback_contract": strict_filter["effective_callback_contract"],
        "ffmpeg": ffmpeg,
    }
    for key, value in (
        ("query_embedder", query_embedder),
        ("broad_sheet_maker", broad_sheet_maker),
        ("broad_embedding_fn", broad_embedding_fn),
        ("exact_sheet_maker", exact_sheet_maker),
        ("exact_embedding_fn", exact_embedding_fn),
    ):
        if value is not None:
            temporal_kwargs[key] = value
    try:
        temporal_result = temporal_materializer(
            normalized_catalog,
            [normalized_beat],
            [normalized_brief],
            **temporal_kwargs,
        )
    except BroadLazyRuntimeBlocked as exc:
        exhaustion = _catalog_exhaustion_evidence(
            exc.artifact,
            catalog=normalized_catalog,
            slot_id=str(normalized_beat["slot_id"]),
            page_size=page,
            survivor_target=target,
            filter_binding=strict_filter,
        )
        reason_code = (
            "no_new_unique_exact_candidates"
            if frozen_hashes
            else "underlying_broad_catalog_exhausted"
        )
        artifact = _blocked_artifact(
            binding,
            reason_code=reason_code,
            underlying=exhaustion,
        )
        raise SemanticBroadRepairBlocked(
            "needs_clean_donor_ranges", str(exc), artifact
        ) from exc
    except (BroadLazyAdapterError, BroadLazyRuntimeError, BroadLazyError) as exc:
        raise SemanticBroadRepairError(
            f"one-slot broad expansion failed: {exc}"
        ) from exc
    if not isinstance(temporal_result, tuple) or len(temporal_result) != 2:
        raise SemanticBroadRepairError(
            "temporal materializer must return (payload, paths)"
        )
    temporal_payload, ranked_rows, underlying_sheet_ids = _validate_underlying_output(
        temporal_result[0],
        temporal_result[1],
        beat=normalized_beat,
        brief=normalized_brief,
        render_frames=int(render_frames),
        output_fps=output_fps,
    )

    frozen = set(frozen_hashes)
    selected: list[dict[str, Any]] = []
    seen_parents: set[str] = set()
    for row in sorted(ranked_rows, key=_candidate_rank_key):
        range_hash = _sha256(
            row.get("candidate_range_hash"), "candidate.candidate_range_hash"
        )
        if range_hash in frozen:
            continue
        parent = _portable_id(
            row.get("parent_clip_id"), "candidate.parent_clip_id"
        )
        if parent in seen_parents:
            continue
        seen_parents.add(parent)
        selected.append(row)
        if len(selected) >= target:
            break
    if not selected:
        raise SemanticBroadRepairError(
            "ready broad expansion returned no candidate after its "
            "pre-quota frozen exact filter"
        )

    materialized: list[tuple[dict[str, Any], dict[str, Any]] | None] = [
        None
    ] * len(selected)
    with cf.ThreadPoolExecutor(max_workers=min(workers, len(selected))) as executor:
        futures = {
            executor.submit(
                _materialize_repair_row,
                row,
                asset_resolver=asset_resolver,
                repair_cache_directory=repair_cache_directory,
                output_fps=output_fps,
                ffmpeg=ffmpeg,
                repair_materializer=repair_materializer,
                repair_sheet_maker=repair_sheet_maker,
            ): index
            for index, row in enumerate(selected)
        }
        for future in cf.as_completed(futures):
            materialized[futures[future]] = future.result()

    repair_cache_root = Path(repair_cache_directory).expanduser().resolve()
    portable_candidates: list[dict[str, Any]] = []
    portable_artifacts: list[dict[str, Any]] = []
    runtime_artifacts: dict[str, dict[str, Any]] = {}
    selected_by_hash = {
        str(row["candidate_range_hash"]): row for row in selected
    }
    for result in materialized:
        if result is None:
            raise SemanticBroadRepairError("repair materialization lost a candidate")
        candidate, runtime = result
        _assert_path_free(candidate, "repair candidate")
        _assert_candidate_policy(
            candidate, output_fps=output_fps, render_frames=int(render_frames)
        )
        range_hash = _sha256(
            candidate.get("candidate_range_hash"),
            "repair candidate.candidate_range_hash",
        )
        source_row = selected_by_hash.get(range_hash)
        if source_row is None or str(candidate.get("parent_clip_id") or "") != str(
            source_row.get("parent_clip_id") or ""
        ):
            raise SemanticBroadRepairError(
                "repair materializer changed the selected exact-range lineage"
            )
        artifact_id = _portable_id(runtime.get("artifact_id"), "artifact_id")
        if not artifact_id.startswith("repair-sheet-"):
            raise SemanticBroadRepairError(
                "semantic broad repair artifact is outside repair-sheet namespace"
            )
        if artifact_id in underlying_sheet_ids:
            raise SemanticBroadRepairError(
                "repair-sheet artifact collides with underlying sheet namespace"
            )
        if str(candidate.get("contact_sheet_artifact_id") or "") != artifact_id:
            raise SemanticBroadRepairError(
                "repair candidate/artifact sheet identity mismatch"
            )
        if str(runtime.get("candidate_range_hash") or "") != range_hash or str(
            runtime.get("clip_id") or ""
        ) != str(candidate.get("clip_id") or ""):
            raise SemanticBroadRepairError(
                "repair runtime artifact exact-range binding is stale"
            )
        try:
            path = Path(str(runtime.get("path") or "")).expanduser().resolve(
                strict=True
            )
            path.relative_to(repair_cache_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SemanticBroadRepairError(
                "repair-sheet path escaped or is missing from repair_cache_directory"
            ) from exc
        size = path.stat().st_size
        if size <= 2048:
            raise SemanticBroadRepairError("repair-sheet artifact is truncated")
        digest = sha256_file(path)
        artifact_receipt = {
            "artifact_id": artifact_id,
            "clip_id": str(candidate["clip_id"]),
            "parent_clip_id": str(candidate["parent_clip_id"]),
            "candidate_range_hash": range_hash,
            "sha256": digest,
            "bytes": size,
        }
        if artifact_id in runtime_artifacts:
            raise SemanticBroadRepairError("duplicate repair-sheet artifact identity")
        portable_candidates.append(candidate)
        portable_artifacts.append(artifact_receipt)
        runtime_artifacts[artifact_id] = {
            **artifact_receipt,
            "path": str(path),
            "cache_hit": bool(runtime.get("cache_hit")),
        }

    parent_ids = [str(row["parent_clip_id"]) for row in portable_candidates]
    range_hashes = [str(row["candidate_range_hash"]) for row in portable_candidates]
    if len(set(parent_ids)) != len(parent_ids) or len(set(range_hashes)) != len(
        range_hashes
    ):
        raise SemanticBroadRepairError(
            "semantic broad repair violated unique parent/range quota"
        )
    exact_ranges = [_exact_range_receipt(row) for row in portable_candidates]
    diagnostics = temporal_payload.get("diagnostics")
    receipt_base = {
        "schema_version": 1,
        "contract": SEMANTIC_BROAD_REPAIR_CONTRACT,
        "status": "ready",
        "binding": binding,
        "binding_hash": canonical_hash(binding),
        "slot_id": str(normalized_beat["slot_id"]),
        "candidate_count": len(portable_candidates),
        "unique_broad_parent_count": len(parent_ids),
        "candidates": portable_candidates,
        "candidate_range_hashes": range_hashes,
        "candidate_range_hashes_hash": canonical_hash(range_hashes),
        "exact_ranges": exact_ranges,
        "exact_ranges_hash": canonical_hash(exact_ranges),
        "artifacts": portable_artifacts,
        "artifacts_hash": canonical_hash(portable_artifacts),
        "underlying": {
            "strict_filter": strict_filter,
            "strict_filter_hash": canonical_hash(strict_filter),
            "temporal_payload_hash": canonical_hash(temporal_payload),
            "diagnostics": _clone(diagnostics, "underlying diagnostics"),
            "diagnostics_hash": canonical_hash(diagnostics),
            "exact_sheet_artifact_ids": underlying_sheet_ids,
            "exact_sheet_artifact_ids_hash": canonical_hash(
                underlying_sheet_ids
            ),
            "candidate_universe_count": len(
                temporal_payload.get("candidate_universe") or []
            ),
            "ranked_exact_survivor_count": len(ranked_rows),
        },
        "policy": dict(_REPAIR_POLICY),
        "limits": {
            "catalog_candidate_count": int(
                normalized_catalog["candidate_count"]
            ),
            "maximum_page_rounds": int(
                math.ceil(int(normalized_catalog["candidate_count"]) / page)
            ),
            "page_size": page,
            "survivor_target": target,
            "repair_pool_size": min(
                MAX_REPAIR_POOL_SIZE, target * MAX_STRICT_VARIANTS_PER_TASK
            ),
            "max_workers": workers,
            "embedding_batch_size": batch_size,
            "search_seconds": search,
        },
    }
    receipt = _content_document(
        receipt_base,
        identifier_field="receipt_id",
        identifier_prefix="semantic-broad-repair-",
    )
    return receipt, dict(sorted(runtime_artifacts.items()))


def validate_semantic_broad_repair_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_binding: Mapping[str, Any],
    runtime_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
    repair_cache_directory: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Validate a persisted receipt against the current active-state binding.

    When ``runtime_artifacts`` is supplied, ``repair_cache_directory`` is
    required and every registered repair sheet is re-bound by ID, exact range,
    byte size, SHA-256 and containment in that repair-only cache root.
    """

    row = _validate_content_document(
        receipt,
        contract=SEMANTIC_BROAD_REPAIR_CONTRACT,
        identifier_field="receipt_id",
        identifier_prefix="semantic-broad-repair-",
    )
    expected = _clone(dict(expected_binding), "expected_binding")
    _assert_path_free(expected, "expected_binding")
    if expected.get("contract") != SEMANTIC_BROAD_REPAIR_BINDING_CONTRACT:
        raise SemanticBroadRepairError("expected binding contract is invalid")
    expected_filter = _validate_frozen_exact_filter_binding(
        expected.get("strict_filter"),
        callback_contract=str(expected.get("callback_contract") or ""),
        frozen_count=int(expected.get("frozen_candidate_range_count") or 0),
        frozen_hashes_hash=str(
            expected.get("frozen_candidate_range_hashes_hash") or ""
        ),
    )
    if expected.get("strict_filter_hash") != canonical_hash(expected_filter):
        raise SemanticBroadRepairError("expected strict frozen-filter hash is stale")
    if row.get("binding") != expected or row.get("binding_hash") != canonical_hash(
        expected
    ):
        raise SemanticBroadRepairError("semantic broad repair receipt is stale")
    if row.get("status") != "ready" or row.get("slot_id") != expected.get("slot_id"):
        raise SemanticBroadRepairError("semantic broad repair receipt status/slot is stale")
    if row.get("policy") != _REPAIR_POLICY:
        raise SemanticBroadRepairError("semantic broad repair policy is invalid")
    limits = row.get("limits")
    if not isinstance(limits, Mapping):
        raise SemanticBroadRepairError("semantic broad repair limits are missing")
    try:
        catalog_count = int(limits.get("catalog_candidate_count"))
        page_size = int(limits.get("page_size"))
        survivor_target = int(limits.get("survivor_target"))
        repair_pool_size = int(limits.get("repair_pool_size"))
        workers = int(limits.get("max_workers"))
        embedding_batch = int(limits.get("embedding_batch_size"))
        search_seconds = float(limits.get("search_seconds"))
        maximum_page_rounds = int(limits.get("maximum_page_rounds"))
    except (TypeError, ValueError) as exc:
        raise SemanticBroadRepairError("semantic broad repair limits are malformed") from exc
    if (
        catalog_count != int(expected.get("catalog_candidate_count") or 0)
        or not 1 <= catalog_count <= MAX_CATALOG_CANDIDATES
        or not 1 <= page_size <= MAX_PAGE_SIZE
        or not 1 <= survivor_target <= MAX_SURVIVORS
        or repair_pool_size
        != min(
            MAX_REPAIR_POOL_SIZE,
            survivor_target * MAX_STRICT_VARIANTS_PER_TASK,
        )
        or not 1 <= workers <= MAX_WORKERS
        or not 1 <= embedding_batch <= MAX_EMBEDDING_BATCH_SIZE
        or not 0 < search_seconds <= MAX_SEARCH_SECONDS
        or maximum_page_rounds != math.ceil(catalog_count / page_size)
    ):
        raise SemanticBroadRepairError("semantic broad repair limits are invalid")
    underlying = row.get("underlying")
    if not isinstance(underlying, Mapping):
        raise SemanticBroadRepairError("underlying broad diagnostics are missing")
    diagnostics = underlying.get("diagnostics")
    internal_sheet_ids = underlying.get("exact_sheet_artifact_ids")
    if (
        underlying.get("strict_filter") != expected_filter
        or underlying.get("strict_filter_hash") != canonical_hash(expected_filter)
        or not isinstance(diagnostics, Mapping)
        or underlying.get("diagnostics_hash") != canonical_hash(diagnostics)
        or not isinstance(internal_sheet_ids, list)
        or not internal_sheet_ids
        or underlying.get("exact_sheet_artifact_ids_hash")
        != canonical_hash(internal_sheet_ids)
        or any(
            not isinstance(value, str)
            or not value.startswith("sheet-")
            or value.startswith("repair-sheet-")
            for value in internal_sheet_ids
        )
    ):
        raise SemanticBroadRepairError("underlying broad diagnostics are invalid")
    candidates = row.get("candidates")
    artifacts = row.get("artifacts")
    exact_ranges = row.get("exact_ranges")
    if (
        not isinstance(candidates, list)
        or not candidates
        or not isinstance(artifacts, list)
        or not isinstance(exact_ranges, list)
        or len(candidates) != len(artifacts)
        or len(candidates) != len(exact_ranges)
        or int(row.get("candidate_count") or 0) != len(candidates)
        or int(row.get("unique_broad_parent_count") or 0) != len(candidates)
    ):
        raise SemanticBroadRepairError("repair receipt candidate arrays are invalid")
    fps_doc = expected.get("output_fps")
    if not isinstance(fps_doc, Mapping):
        raise SemanticBroadRepairError("repair binding output_fps is invalid")
    try:
        fps = float(
            Fraction(int(fps_doc["numerator"]), int(fps_doc["denominator"]))
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise SemanticBroadRepairError("repair binding output_fps is invalid") from exc
    render_frames = int(expected.get("render_frames") or 0)
    parents: list[str] = []
    ranges: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise SemanticBroadRepairError("repair receipt candidate is not an object")
        _assert_candidate_policy(
            candidate, output_fps=fps, render_frames=render_frames
        )
        _assert_path_free(candidate, "repair receipt candidate")
        parents.append(
            _portable_id(candidate.get("parent_clip_id"), "candidate.parent_clip_id")
        )
        ranges.append(
            _sha256(
                candidate.get("candidate_range_hash"),
                "candidate.candidate_range_hash",
            )
        )
    if len(set(parents)) != len(parents) or len(set(ranges)) != len(ranges):
        raise SemanticBroadRepairError("repair receipt has duplicate parents/ranges")
    if row.get("candidate_range_hashes") != ranges or row.get(
        "candidate_range_hashes_hash"
    ) != canonical_hash(ranges):
        raise SemanticBroadRepairError("repair receipt range sequence is stale")
    expected_ranges = [_exact_range_receipt(candidate) for candidate in candidates]
    if exact_ranges != expected_ranges or row.get("exact_ranges_hash") != canonical_hash(
        expected_ranges
    ):
        raise SemanticBroadRepairError("repair receipt exact-range binding is stale")
    if row.get("artifacts_hash") != canonical_hash(artifacts):
        raise SemanticBroadRepairError("repair receipt artifact hash is stale")
    artifact_by_id: dict[str, dict[str, Any]] = {}
    for candidate, artifact in zip(candidates, artifacts):
        if not isinstance(artifact, Mapping):
            raise SemanticBroadRepairError("repair receipt artifact is not an object")
        artifact_row = dict(artifact)
        artifact_id = _portable_id(artifact_row.get("artifact_id"), "artifact_id")
        if not artifact_id.startswith("repair-sheet-"):
            raise SemanticBroadRepairError("repair receipt artifact namespace is invalid")
        if artifact_id in internal_sheet_ids:
            raise SemanticBroadRepairError(
                "repair receipt collides with underlying sheet namespace"
            )
        if artifact_id in artifact_by_id:
            raise SemanticBroadRepairError("repair receipt repeats an artifact ID")
        if (
            str(candidate.get("contact_sheet_artifact_id") or "") != artifact_id
            or str(candidate.get("clip_id") or "")
            != str(artifact_row.get("clip_id") or "")
            or str(candidate.get("parent_clip_id") or "")
            != str(artifact_row.get("parent_clip_id") or "")
            or str(candidate.get("candidate_range_hash") or "")
            != str(artifact_row.get("candidate_range_hash") or "")
        ):
            raise SemanticBroadRepairError(
                "repair receipt candidate/artifact binding is stale"
            )
        _sha256(artifact_row.get("sha256"), "artifact.sha256")
        try:
            size = int(artifact_row.get("bytes"))
        except (TypeError, ValueError) as exc:
            raise SemanticBroadRepairError("artifact bytes is invalid") from exc
        if size <= 2048:
            raise SemanticBroadRepairError("repair receipt artifact is truncated")
        artifact_by_id[artifact_id] = artifact_row

    if runtime_artifacts is not None:
        if repair_cache_directory is None:
            raise SemanticBroadRepairError(
                "repair_cache_directory is required with runtime_artifacts"
            )
        if not isinstance(runtime_artifacts, Mapping) or set(runtime_artifacts) != set(
            artifact_by_id
        ):
            raise SemanticBroadRepairError(
                "runtime repair-sheet registry disagrees with receipt"
            )
        repair_cache_root = Path(repair_cache_directory).expanduser().resolve()
        for artifact_id, portable in artifact_by_id.items():
            runtime = runtime_artifacts.get(artifact_id)
            if not isinstance(runtime, Mapping):
                raise SemanticBroadRepairError(
                    "runtime repair-sheet record is malformed"
                )
            if any(
                str(runtime.get(key) or "") != str(portable.get(key) or "")
                for key in (
                    "artifact_id",
                    "clip_id",
                    "parent_clip_id",
                    "candidate_range_hash",
                    "sha256",
                    "bytes",
                )
            ):
                raise SemanticBroadRepairError(
                    "runtime repair-sheet binding disagrees with receipt"
                )
            try:
                path = Path(str(runtime.get("path") or "")).expanduser().resolve(
                    strict=True
                )
                path.relative_to(repair_cache_root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise SemanticBroadRepairError(
                    "runtime repair-sheet path is missing or outside cache"
                ) from exc
            if path.stat().st_size != int(portable["bytes"]) or sha256_file(path) != str(
                portable["sha256"]
            ):
                raise SemanticBroadRepairError(
                    "runtime repair-sheet content disagrees with receipt"
                )
    return row


def validate_semantic_broad_repair_block(
    block: Mapping[str, Any],
    *,
    expected_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a terminal block and its proof of full catalog exhaustion."""

    row = _validate_content_document(
        block,
        contract=SEMANTIC_BROAD_REPAIR_BLOCK_CONTRACT,
        identifier_field="block_id",
        identifier_prefix="semantic-broad-repair-block-",
    )
    expected = _clone(dict(expected_binding), "expected_binding")
    _assert_path_free(expected, "expected_binding")
    if expected.get("contract") != SEMANTIC_BROAD_REPAIR_BINDING_CONTRACT:
        raise SemanticBroadRepairError("expected binding contract is invalid")
    expected_filter = _validate_frozen_exact_filter_binding(
        expected.get("strict_filter"),
        callback_contract=str(expected.get("callback_contract") or ""),
        frozen_count=int(expected.get("frozen_candidate_range_count") or 0),
        frozen_hashes_hash=str(
            expected.get("frozen_candidate_range_hashes_hash") or ""
        ),
    )
    if (
        expected.get("strict_filter_hash") != canonical_hash(expected_filter)
        or row.get("status") != "needs_clean_donor_ranges"
        or row.get("binding") != expected
        or row.get("binding_hash") != canonical_hash(expected)
    ):
        raise SemanticBroadRepairError("semantic broad repair block is stale")
    frozen_count = int(expected.get("frozen_candidate_range_count") or 0)
    expected_reason = (
        "no_new_unique_exact_candidates"
        if frozen_count
        else "underlying_broad_catalog_exhausted"
    )
    exhaustion = row.get("underlying")
    if not isinstance(exhaustion, Mapping):
        raise SemanticBroadRepairError("catalog-exhaustion evidence is missing")
    underlying_block = exhaustion.get("underlying_block")
    try:
        catalog_count = int(exhaustion.get("catalog_candidate_count"))
        page_size = int(exhaustion.get("page_size"))
        expected_rounds = int(exhaustion.get("expected_page_rounds"))
        observed_rounds = int(exhaustion.get("observed_page_rounds"))
        ranked_rows_seen = int(exhaustion.get("ranked_rows_seen"))
        strict_successes = int(exhaustion.get("strict_success_outcomes"))
        strict_rejections = int(exhaustion.get("strict_rejected_outcomes"))
    except (TypeError, ValueError) as exc:
        raise SemanticBroadRepairError(
            "catalog-exhaustion counters are malformed"
        ) from exc
    if (
        row.get("reason_code") != expected_reason
        or exhaustion.get("contract")
        != "semantic_broad_catalog_exhaustion_v1"
        or exhaustion.get("catalog_exhausted") is not True
        or catalog_count != int(expected.get("catalog_candidate_count") or 0)
        or not 1 <= page_size <= MAX_PAGE_SIZE
        or expected_rounds != math.ceil(catalog_count / page_size)
        or observed_rounds != expected_rounds
        or ranked_rows_seen != catalog_count
        or strict_successes < 0
        or strict_rejections < 0
        or exhaustion.get("strict_filter") != expected_filter
        or exhaustion.get("strict_filter_hash") != canonical_hash(expected_filter)
        or not isinstance(underlying_block, Mapping)
        or exhaustion.get("underlying_block_hash")
        != underlying_block.get("content_hash")
        or underlying_block.get("content_hash")
        != canonical_hash(
            {
                key: value
                for key, value in underlying_block.items()
                if key != "content_hash"
            }
        )
    ):
        raise SemanticBroadRepairError(
            "semantic broad repair block lacks a valid full-catalog proof"
        )
    return row


__all__ = [
    "SEMANTIC_BROAD_REPAIR_BINDING_CONTRACT",
    "SEMANTIC_BROAD_REPAIR_BLOCK_CONTRACT",
    "SEMANTIC_BROAD_REPAIR_CONTRACT",
    "SemanticBroadRepairBlocked",
    "SemanticBroadRepairError",
    "build_semantic_broad_repair_binding",
    "materialize_semantic_broad_repair",
    "validate_semantic_broad_repair_block",
    "validate_semantic_broad_repair_receipt",
]
