"""Portable broad retrieval and lazy strict-validation foundation.

This module is deliberately not wired into the production DAG yet.  It owns
only deterministic, fail-closed contracts which a later stage adapter can use:

* convert complete raw donor indexes into a path-free broad catalog;
* bind portable CLIP vectors to that catalog and rank bounded pages;
* deduplicate the cross-slot top union and plan only those strict windows;
* preserve exact integer frame PTS for VFR-safe window addressing; and
* turn donor-level overlay evidence into a candidate-specific crop/reject
  decision without fixed margins, inpainting, padding, holding, or retiming.

The functions never open or modify donor media.  Exact PTS and visual evidence
are supplied by callers after their own read-only decode/probe operations.
"""
from __future__ import annotations

import bisect
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import numpy as np

from .common import atomic_write_json, canonical_hash


BROAD_CATALOG_CONTRACT = "portable_broad_catalog_v1"
BROAD_EMBEDDING_CONTRACT = "portable_broad_embeddings_v2"
BROAD_EMBEDDING_REPRESENTATION = "four_temporal_cells_mean_normalized"
BROAD_RANKING_CONTRACT = "broad_ranked_pages_v1"
BROAD_UNION_CONTRACT = "broad_ranked_union_v1"
LAZY_PLAN_CONTRACT = "lazy_strict_window_plan_v2"
EXACT_PTS_CONTRACT = "exact_integer_pts_window_v1"
DONOR_OVERLAY_PROFILE_CONTRACT = "donor_overlay_profile_v1"
CANDIDATE_OVERLAY_PROFILE_CONTRACT = "candidate_overlay_profile_v1"
STRICT_DETECTOR_POLICY_RESOLUTION_CONTRACT = (
    "strict_detector_policy_from_overlay_profile_v1"
)

MIN_CLIP_SECONDS = 4.0
MAX_CLIP_SECONDS = 8.0
_TIME_EPSILON = 1e-5
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_WINDOW_KEY_PREFIX = "strict-window-v1-"

DEFAULT_BROAD_POLICY: dict[str, Any] = {
    "policy_version": "broad-virtual-clips-v1",
    "min_clip_seconds": MIN_CLIP_SECONDS,
    "max_clip_seconds": MAX_CLIP_SECONDS,
    "sample_fractions": [0.10, 0.35, 0.65, 0.90],
    "full_source_coverage": True,
    "allow_duration_cap": False,
    "allow_manual_selection": False,
}

DEFAULT_OVERLAY_POLICY: dict[str, Any] = {
    "policy_version": "candidate-overlay-profile-v1",
    "min_logo_contexts": 2,
    "min_logo_persistence": 0.70,
    "min_logo_confidence": 0.80,
    "edge_anchor_fraction": 0.12,
    "crop_padding_fraction": 0.015,
    "max_crop_fraction": 0.25,
    "min_retained_area": 0.72,
    "allow_inpaint": False,
    "allow_manual_crop": False,
}

_PROFILE_TRACK_CLASSES = {
    "screen_fixed_edge_logo",
    "natural_static_edge",
    "central_title",
    "transient_title",
}
_CANDIDATE_OBSERVATION_KINDS = {
    "profile_track",
    "natural_static_edge",
    "central_title",
    "transient_title",
    "unclassified_overlay",
}


class BroadLazyError(ValueError):
    """A broad/lazy artifact cannot satisfy the production contract."""


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BroadLazyError(f"{name} must be an object")
    return value


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise BroadLazyError(f"{name} must be an array")
    return value


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BroadLazyError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise BroadLazyError(f"{name} must be finite")
    return number


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise BroadLazyError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BroadLazyError(f"{name} must be a positive integer") from exc
    if parsed <= 0 or parsed != value:
        raise BroadLazyError(f"{name} must be a positive integer")
    return parsed


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise BroadLazyError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BroadLazyError(f"{name} must be an integer") from exc
    if parsed != value:
        raise BroadLazyError(f"{name} must be an integer")
    return parsed


def _sha256(value: Any, name: str) -> str:
    digest = str(value or "").strip().casefold()
    if not _SHA256_RE.fullmatch(digest):
        raise BroadLazyError(f"{name} must be a lowercase SHA-256")
    return digest


def _portable_id(value: Any, name: str) -> str:
    identifier = str(value or "").strip()
    if not _PORTABLE_ID_RE.fullmatch(identifier):
        raise BroadLazyError(f"{name} must be a portable identifier")
    return identifier


def _json_clone(value: Any, name: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise BroadLazyError(f"{name} must be finite JSON data") from exc


def _host_path(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if stripped.startswith(("/", "~/", "file://")):
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]", stripped))


def _assert_portable(value: Any, name: str = "artifact") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).casefold()
            if lowered in {
                "path",
                "source_path",
                "absolute_path",
                "original_location",
                "original_locations",
            }:
                raise BroadLazyError(f"{name} contains forbidden path field {key!r}")
            _assert_portable(item, f"{name}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _assert_portable(item, f"{name}[{index}]")
    elif isinstance(value, str) and _host_path(value):
        raise BroadLazyError(f"{name} contains an absolute host path")


def _content_document(base: Mapping[str, Any], *, id_field: str, id_prefix: str) -> dict[str, Any]:
    portable = _json_clone(dict(base), "artifact")
    _assert_portable(portable)
    digest = canonical_hash(portable)
    return {
        **portable,
        "content_hash": digest,
        id_field: f"{id_prefix}{digest[:24]}",
    }


def _validate_content_document(
    document: Mapping[str, Any],
    *,
    contract: str,
    id_field: str,
    id_prefix: str,
) -> dict[str, Any]:
    row = _json_clone(_mapping(document, "document"), "document")
    if row.get("schema_version") != 1 or row.get("contract") != contract:
        raise BroadLazyError(f"expected {contract} schema v1")
    supplied_hash = _sha256(row.get("content_hash"), "document.content_hash")
    supplied_id = str(row.get(id_field) or "")
    base = {
        key: value
        for key, value in row.items()
        if key not in {"content_hash", id_field}
    }
    actual_hash = canonical_hash(base)
    if actual_hash != supplied_hash:
        raise BroadLazyError(f"{contract} content hash mismatch")
    if supplied_id != f"{id_prefix}{actual_hash[:24]}":
        raise BroadLazyError(f"{contract} content id mismatch")
    _assert_portable(row)
    return row


def _policy(
    supplied: Mapping[str, Any] | None,
    default: Mapping[str, Any],
    name: str,
) -> dict[str, Any]:
    output = deepcopy(dict(default))
    if supplied is not None:
        output.update(_json_clone(_mapping(supplied, name), name))
    _assert_portable(output, name)
    return output


def _broad_policy(supplied: Mapping[str, Any] | None) -> dict[str, Any]:
    policy = _policy(supplied, DEFAULT_BROAD_POLICY, "broad_policy")
    if abs(_finite(policy.get("min_clip_seconds"), "min_clip_seconds") - 4.0) > 1e-9:
        raise BroadLazyError("broad policy minimum clip duration must remain 4 seconds")
    if abs(_finite(policy.get("max_clip_seconds"), "max_clip_seconds") - 8.0) > 1e-9:
        raise BroadLazyError("broad policy maximum clip duration must remain 8 seconds")
    if policy.get("full_source_coverage") is not True:
        raise BroadLazyError("broad policy must require full source coverage")
    if policy.get("allow_duration_cap") is not False:
        raise BroadLazyError("broad policy cannot allow duration caps")
    if policy.get("allow_manual_selection") is not False:
        raise BroadLazyError("broad policy cannot allow manual selection")
    fractions = [_finite(value, "sample_fractions") for value in _sequence(
        policy.get("sample_fractions"), "sample_fractions"
    )]
    if fractions != [0.10, 0.35, 0.65, 0.90]:
        raise BroadLazyError("broad policy requires 10/35/65/90 percent samples")
    policy["sample_fractions"] = fractions
    policy["min_clip_seconds"] = 4.0
    policy["max_clip_seconds"] = 8.0
    return policy


def _overlay_policy(supplied: Mapping[str, Any] | None) -> dict[str, Any]:
    policy = _policy(supplied, DEFAULT_OVERLAY_POLICY, "overlay_policy")
    for field in (
        "min_logo_persistence",
        "min_logo_confidence",
        "edge_anchor_fraction",
        "crop_padding_fraction",
        "max_crop_fraction",
        "min_retained_area",
    ):
        policy[field] = _finite(policy.get(field), f"overlay_policy.{field}")
    policy["min_logo_contexts"] = _positive_int(
        policy.get("min_logo_contexts"), "overlay_policy.min_logo_contexts"
    )
    if not 0.0 <= policy["min_logo_persistence"] <= 1.0:
        raise BroadLazyError("min_logo_persistence must be in [0, 1]")
    if not 0.0 <= policy["min_logo_confidence"] <= 1.0:
        raise BroadLazyError("min_logo_confidence must be in [0, 1]")
    if not 0.0 < policy["edge_anchor_fraction"] < 0.5:
        raise BroadLazyError("edge_anchor_fraction must be in (0, 0.5)")
    if not 0.0 <= policy["crop_padding_fraction"] < 0.1:
        raise BroadLazyError("crop_padding_fraction must be in [0, 0.1)")
    if not 0.0 < policy["max_crop_fraction"] < 0.5:
        raise BroadLazyError("max_crop_fraction must be in (0, 0.5)")
    if not 0.5 <= policy["min_retained_area"] <= 1.0:
        raise BroadLazyError("min_retained_area must be in [0.5, 1]")
    if policy.get("allow_inpaint") is not False or policy.get("allow_manual_crop") is not False:
        raise BroadLazyError("overlay policy cannot enable inpaint or manual crop")
    return policy


def broad_catalog_cache_key(
    donor_sha256s: Sequence[str], policy: Mapping[str, Any]
) -> str:
    """Return a mount-independent cache key for a complete broad catalog."""

    digests = sorted({_sha256(value, "donor_sha256") for value in donor_sha256s})
    if not digests:
        raise BroadLazyError("at least one donor SHA-256 is required")
    normalized_policy = _broad_policy(policy)
    return "broad-catalog-v1-" + canonical_hash(
        {"donor_sha256s": digests, "policy": normalized_policy}
    )


def _resolve_asset_ids(
    indexes: Sequence[Mapping[str, Any]],
    source_asset_ids: Sequence[str] | Mapping[str, str] | None,
) -> list[str]:
    if source_asset_ids is None:
        output = []
        for index, raw in enumerate(indexes):
            media = _mapping(raw.get("media"), f"donor_indexes[{index}].media")
            output.append(
                _portable_id(
                    media.get("asset_id") or raw.get("source_asset_id"),
                    f"donor_indexes[{index}].source_asset_id",
                )
            )
        return output
    if isinstance(source_asset_ids, Mapping):
        output = []
        for index, raw in enumerate(indexes):
            media = _mapping(raw.get("media"), f"donor_indexes[{index}].media")
            digest = _sha256(media.get("sha256"), f"donor_indexes[{index}].media.sha256")
            output.append(
                _portable_id(
                    source_asset_ids.get(digest), f"source_asset_ids[{digest}]"
                )
            )
        return output
    values = list(_sequence(source_asset_ids, "source_asset_ids"))
    if len(values) != len(indexes):
        raise BroadLazyError("source_asset_ids must align with donor_indexes")
    return [_portable_id(value, f"source_asset_ids[{index}]") for index, value in enumerate(values)]


def _portable_samples(raw: Any, start: float, end: float, name: str) -> list[float]:
    samples = [_finite(value, name) for value in _sequence(raw, name)]
    if len(samples) != 4 or any(
        point <= start + _TIME_EPSILON or point >= end - _TIME_EPSILON
        for point in samples
    ) or any(right <= left for left, right in zip(samples, samples[1:])):
        raise BroadLazyError(f"{name} must contain four ordered interior samples")
    return [round(value, 9) for value in samples]


def build_portable_broad_catalog(
    donor_indexes: Sequence[Mapping[str, Any]],
    *,
    source_asset_ids: Sequence[str] | Mapping[str, str] | None = None,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a full-coverage, path-free catalog from raw donor index documents.

    Capped indexes, gaps, overlaps, non-4--8-second clips, and source hash
    disagreements are rejected.  Only a whitelist of retrieval fields is
    copied, so absolute donor and thumbnail locations cannot leak into output.
    """

    indexes = list(_sequence(donor_indexes, "donor_indexes"))
    if not indexes:
        raise BroadLazyError("donor_indexes cannot be empty")
    for index, raw in enumerate(indexes):
        _mapping(raw, f"donor_indexes[{index}]")
    asset_ids = _resolve_asset_ids(indexes, source_asset_ids)
    if len(set(asset_ids)) != len(asset_ids):
        raise BroadLazyError("source asset ids must be unique")
    normalized_policy = _broad_policy(policy)
    policy_hash = canonical_hash(normalized_policy)

    donors: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, (raw_value, asset_id) in enumerate(zip(indexes, asset_ids)):
        raw = _mapping(raw_value, f"donor_indexes[{index}]")
        media = _mapping(raw.get("media"), f"donor_indexes[{index}].media")
        digest = _sha256(media.get("sha256"), f"donor_indexes[{index}].media.sha256")
        duration = _finite(media.get("duration"), f"donor_indexes[{index}].media.duration")
        fps = _finite(media.get("fps"), f"donor_indexes[{index}].media.fps")
        if duration < MIN_CLIP_SECONDS or fps <= 0:
            raise BroadLazyError("donor duration and fps must be positive")
        range_start = _finite(raw.get("range_start"), f"donor_indexes[{index}].range_start")
        range_end = _finite(raw.get("range_end"), f"donor_indexes[{index}].range_end")
        if abs(range_start) > _TIME_EPSILON or abs(range_end - duration) > _TIME_EPSILON:
            raise BroadLazyError(
                f"donor {asset_id} is capped; broad catalogs require [0, duration]"
            )
        raw_clips = list(_sequence(raw.get("clips"), f"donor_indexes[{index}].clips"))
        if not raw_clips:
            raise BroadLazyError(f"donor {asset_id} has no broad clips")

        donor_candidates: list[dict[str, Any]] = []
        prior_end = 0.0
        for clip_index, clip_value in enumerate(raw_clips):
            clip = _mapping(clip_value, f"donor_indexes[{index}].clips[{clip_index}]")
            source_id = str(clip.get("source_id") or digest).strip().casefold()
            if source_id != digest:
                raise BroadLazyError(f"donor {asset_id} clip source_id disagrees with SHA-256")
            source_in = _finite(clip.get("source_in"), "clip.source_in")
            source_out = _finite(clip.get("source_out"), "clip.source_out")
            clip_duration = source_out - source_in
            if source_in < -_TIME_EPSILON or source_out > duration + _TIME_EPSILON:
                raise BroadLazyError(f"donor {asset_id} clip falls outside source duration")
            if not MIN_CLIP_SECONDS - _TIME_EPSILON <= clip_duration <= MAX_CLIP_SECONDS + _TIME_EPSILON:
                raise BroadLazyError(f"donor {asset_id} clip is outside the 4--8 second contract")
            if abs(source_in - prior_end) > _TIME_EPSILON:
                relation = "overlap" if source_in < prior_end else "gap"
                raise BroadLazyError(f"donor {asset_id} broad clips contain a {relation}")
            samples = _portable_samples(
                clip.get("sample_times"), source_in, source_out, "clip.sample_times"
            )
            shot_cuts = sorted(
                _finite(value, "clip.shot_cuts")
                for value in _sequence(clip.get("shot_cuts") or (), "clip.shot_cuts")
            )
            if any(not source_in < cut < source_out for cut in shot_cuts):
                raise BroadLazyError("clip.shot_cuts must be strictly inside the clip")
            identity = {
                "source_sha256": digest,
                "source_in": round(source_in, 9),
                "source_out": round(source_out, 9),
                "policy_hash": policy_hash,
            }
            candidate_hash = canonical_hash(identity)
            clip_id = f"broad-{candidate_hash[:24]}"
            if clip_id in seen_ids:
                raise BroadLazyError("broad clip ids must be unique")
            seen_ids.add(clip_id)
            candidate = {
                "clip_id": clip_id,
                "candidate_key": candidate_hash,
                "source_asset_id": asset_id,
                "source_sha256": digest,
                "source_in": round(source_in, 9),
                "source_out": round(source_out, 9),
                "duration": round(clip_duration, 9),
                "sample_times": samples,
                "shot_cuts": [round(value, 9) for value in shot_cuts],
                "retrieval_only": True,
                "renderable": False,
            }
            donor_candidates.append(candidate)
            prior_end = source_out
        if abs(prior_end - duration) > _TIME_EPSILON:
            raise BroadLazyError(f"donor {asset_id} broad clips lose the source tail")

        donor = {
            "source_asset_id": asset_id,
            "source_sha256": digest,
            "duration": round(duration, 9),
            "fps_advisory": round(fps, 9),
            "width": _positive_int(media.get("width"), "media.width"),
            "height": _positive_int(media.get("height"), "media.height"),
            "video_codec": str(media.get("video_codec") or "unknown"),
            "has_audio": bool(media.get("has_audio")),
            "candidate_count": len(donor_candidates),
            "covered_seconds": round(sum(row["duration"] for row in donor_candidates), 9),
        }
        donors.append(donor)
        candidates.extend(donor_candidates)

    donors.sort(key=lambda row: (row["source_asset_id"], row["source_sha256"]))
    candidates.sort(
        key=lambda row: (row["source_asset_id"], row["source_in"], row["source_out"])
    )
    donor_digests = [row["source_sha256"] for row in donors]
    base = {
        "schema_version": 1,
        "contract": BROAD_CATALOG_CONTRACT,
        "policy": normalized_policy,
        "policy_hash": policy_hash,
        "cache_key": broad_catalog_cache_key(donor_digests, normalized_policy),
        "donors": donors,
        "candidates": candidates,
        "donor_count": len(donors),
        "candidate_count": len(candidates),
        "covered_seconds": round(sum(row["duration"] for row in donors), 9),
    }
    return _content_document(base, id_field="catalog_id", id_prefix="broad-catalog-")


def validate_broad_catalog(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate content binding and all portable broad-catalog invariants."""

    catalog = _validate_content_document(
        document,
        contract=BROAD_CATALOG_CONTRACT,
        id_field="catalog_id",
        id_prefix="broad-catalog-",
    )
    rebuilt = build_portable_broad_catalog(
        [
            {
                "media": {
                    "asset_id": donor["source_asset_id"],
                    "sha256": donor["source_sha256"],
                    "duration": donor["duration"],
                    "fps": donor["fps_advisory"],
                    "width": donor["width"],
                    "height": donor["height"],
                    "video_codec": donor.get("video_codec"),
                    "has_audio": donor.get("has_audio"),
                },
                "range_start": 0.0,
                "range_end": donor["duration"],
                "clips": [
                    {
                        "source_id": row["source_sha256"],
                        "source_in": row["source_in"],
                        "source_out": row["source_out"],
                        "sample_times": row["sample_times"],
                        "shot_cuts": row.get("shot_cuts") or (),
                    }
                    for row in catalog.get("candidates") or ()
                    if row.get("source_asset_id") == donor.get("source_asset_id")
                ],
            }
            for donor in _sequence(catalog.get("donors"), "catalog.donors")
        ],
        policy=_mapping(catalog.get("policy"), "catalog.policy"),
    )
    if rebuilt != catalog:
        raise BroadLazyError("broad catalog fields disagree with canonical reconstruction")
    return catalog


def save_broad_catalog(
    catalog: Mapping[str, Any], cache_directory: str | os.PathLike[str]
) -> Path:
    """Atomically save a catalog under its content hash and return its path."""

    validated = validate_broad_catalog(catalog)
    root = Path(cache_directory)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"broad_catalog.{validated['content_hash']}.json"
    if target.exists():
        existing = load_broad_catalog(target, expected_content_hash=validated["content_hash"])
        if existing != validated:
            raise BroadLazyError("content-addressed catalog target contains different data")
        return target
    atomic_write_json(target, validated)
    return target


def load_broad_catalog(
    path: str | os.PathLike[str], *, expected_content_hash: str | None = None
) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BroadLazyError(f"cannot load broad catalog: {exc}") from exc
    validated = validate_broad_catalog(payload)
    if expected_content_hash is not None and validated["content_hash"] != _sha256(
        expected_content_hash, "expected_content_hash"
    ):
        raise BroadLazyError("loaded broad catalog has an unexpected content hash")
    return validated


def _vector_bytes(vectors: np.ndarray) -> bytes:
    little_endian = np.ascontiguousarray(np.asarray(vectors, dtype="<f4"))
    return little_endian.tobytes(order="C")


def _model_contract(model_contract: Mapping[str, Any]) -> dict[str, Any]:
    model = _json_clone(_mapping(model_contract, "model_contract"), "model_contract")
    if not str(model.get("provider") or "").strip():
        raise BroadLazyError("model_contract.provider is required")
    if not str(model.get("model_name") or model.get("model") or "").strip():
        raise BroadLazyError("model_contract.model_name is required")
    model["weights_sha256"] = _sha256(
        model.get("weights_sha256"), "model_contract.weights_sha256"
    )
    _assert_portable(model, "model_contract")
    return model


def build_portable_embedding_contract(
    catalog: Mapping[str, Any],
    vectors: np.ndarray,
    *,
    model_contract: Mapping[str, Any],
    representation: str = BROAD_EMBEDDING_REPRESENTATION,
) -> dict[str, Any]:
    """Bind normalized vectors to a path-free catalog without embedding paths."""

    validated_catalog = validate_broad_catalog(catalog)
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim != 2 or array.shape[0] != validated_catalog["candidate_count"] or array.shape[1] < 1:
        raise BroadLazyError("embedding vectors must be a non-empty N x D catalog matrix")
    if not np.isfinite(array).all():
        raise BroadLazyError("embedding vectors must be finite")
    norms = np.linalg.norm(array, axis=1)
    if not np.allclose(norms, 1.0, rtol=1e-4, atol=1e-4):
        raise BroadLazyError("embedding vectors must be normalized")
    representation = str(representation or "").strip()
    if representation != BROAD_EMBEDDING_REPRESENTATION:
        raise BroadLazyError(
            "broad embeddings must declare four_temporal_cells_mean_normalized "
            "representation"
        )
    model = _model_contract(model_contract)
    clip_ids = [str(row["clip_id"]) for row in validated_catalog["candidates"]]
    vector_digest = hashlib.sha256(
        canonical_hash(clip_ids).encode("ascii") + _vector_bytes(array)
    ).hexdigest()
    model_hash = canonical_hash(model)
    cache_key = "broad-embeddings-v2-" + canonical_hash(
        {
            "catalog_hash": validated_catalog["content_hash"],
            "donor_sha256s": [row["source_sha256"] for row in validated_catalog["donors"]],
            "model_contract_hash": model_hash,
            "representation": representation,
        }
    )
    base = {
        "schema_version": 1,
        "contract": BROAD_EMBEDDING_CONTRACT,
        "catalog_hash": validated_catalog["content_hash"],
        "catalog_cache_key": validated_catalog["cache_key"],
        "clip_ids": clip_ids,
        "clip_ids_hash": canonical_hash(clip_ids),
        "dimensions": int(array.shape[1]),
        "count": int(array.shape[0]),
        "dtype": "float32-le",
        "normalized": True,
        "representation": representation,
        "model_contract": model,
        "model_contract_hash": model_hash,
        "vectors_sha256": vector_digest,
        "cache_key": cache_key,
    }
    return _content_document(base, id_field="embedding_id", id_prefix="broad-emb-")


def validate_embedding_contract(
    contract: Mapping[str, Any], catalog: Mapping[str, Any], vectors: np.ndarray
) -> tuple[dict[str, Any], np.ndarray]:
    validated_catalog = validate_broad_catalog(catalog)
    document = _validate_content_document(
        contract,
        contract=BROAD_EMBEDDING_CONTRACT,
        id_field="embedding_id",
        id_prefix="broad-emb-",
    )
    array = np.asarray(vectors, dtype=np.float32)
    if document.get("catalog_hash") != validated_catalog["content_hash"]:
        raise BroadLazyError("embedding contract is bound to another catalog")
    rebuilt = build_portable_embedding_contract(
        validated_catalog,
        array,
        model_contract=_mapping(document.get("model_contract"), "model_contract"),
        representation=str(document.get("representation") or ""),
    )
    if rebuilt != document:
        raise BroadLazyError("embedding contract fields disagree with vectors/catalog")
    return document, array


def _query_vector_hash(vector: np.ndarray) -> str:
    return hashlib.sha256(_vector_bytes(vector.reshape(1, -1))).hexdigest()


def rank_broad_candidate_pages(
    catalog: Mapping[str, Any],
    embedding_contract: Mapping[str, Any],
    vectors: np.ndarray,
    query_vectors: Mapping[str, Sequence[float] | np.ndarray],
    *,
    page_size: int = 20,
    start_page: int = 0,
    page_count: int = 1,
) -> dict[str, Any]:
    """Rank deterministic bounded pages for each slot from portable vectors."""

    validated_catalog = validate_broad_catalog(catalog)
    embeddings, array = validate_embedding_contract(
        embedding_contract, validated_catalog, vectors
    )
    page_size = _positive_int(page_size, "page_size")
    page_count = _positive_int(page_count, "page_count")
    start_page = _integer(start_page, "start_page")
    if start_page < 0:
        raise BroadLazyError("start_page must be non-negative")
    queries = _mapping(query_vectors, "query_vectors")
    if not queries:
        raise BroadLazyError("query_vectors cannot be empty")
    candidates = validated_catalog["candidates"]
    slots: list[dict[str, Any]] = []
    for raw_slot_id in sorted(queries, key=lambda item: str(item).casefold()):
        slot_id = _portable_id(raw_slot_id, "slot_id")
        query = np.asarray(queries[raw_slot_id], dtype=np.float32)
        if query.ndim != 1 or query.shape[0] != array.shape[1] or not np.isfinite(query).all():
            raise BroadLazyError(f"query vector for {slot_id} has invalid shape/data")
        norm = float(np.linalg.norm(query))
        if norm <= 0:
            raise BroadLazyError(f"query vector for {slot_id} is zero")
        normalized_query = query / norm
        scores = np.asarray(array @ normalized_query, dtype=np.float64)
        order = sorted(
            range(len(candidates)),
            key=lambda index: (-float(scores[index]), str(candidates[index]["clip_id"])),
        )
        pages: list[dict[str, Any]] = []
        for page_index in range(start_page, start_page + page_count):
            left = page_index * page_size
            right = min(len(order), left + page_size)
            if left >= len(order):
                break
            rows = []
            for rank, candidate_index in enumerate(order[left:right], start=left):
                rows.append(
                    {
                        **deepcopy(candidates[candidate_index]),
                        "broad_rank": rank,
                        "retrieval_score": round(float(scores[candidate_index]), 10),
                    }
                )
            pages.append(
                {
                    "page_index": page_index,
                    "page_size": page_size,
                    "candidates": rows,
                }
            )
        if not pages:
            raise BroadLazyError(f"requested ranking page is outside catalog for {slot_id}")
        slots.append(
            {
                "slot_id": slot_id,
                "query_vector_sha256": _query_vector_hash(normalized_query),
                "pages": pages,
            }
        )
    base = {
        "schema_version": 1,
        "contract": BROAD_RANKING_CONTRACT,
        "catalog_hash": validated_catalog["content_hash"],
        "catalog_candidate_count": validated_catalog["candidate_count"],
        "embedding_hash": embeddings["content_hash"],
        "page_size": page_size,
        "start_page": start_page,
        "page_count": page_count,
        "slots": slots,
    }
    return _content_document(base, id_field="ranking_id", id_prefix="broad-rank-")


def _validate_ranking(document: Mapping[str, Any]) -> dict[str, Any]:
    return _validate_content_document(
        document,
        contract=BROAD_RANKING_CONTRACT,
        id_field="ranking_id",
        id_prefix="broad-rank-",
    )


def deduplicate_ranked_union(
    rankings: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the unique cross-slot union of only the supplied ranked pages."""

    documents = (
        [rankings]
        if isinstance(rankings, Mapping)
        else list(_sequence(rankings, "rankings"))
    )
    if not documents:
        raise BroadLazyError("rankings cannot be empty")
    validated = [_validate_ranking(row) for row in documents]
    catalog_hashes = {row.get("catalog_hash") for row in validated}
    embedding_hashes = {row.get("embedding_hash") for row in validated}
    if len(catalog_hashes) != 1 or len(embedding_hashes) != 1:
        raise BroadLazyError("ranked pages must share one catalog and embedding contract")

    identities: dict[str, dict[str, Any]] = {}
    requests: dict[str, list[dict[str, Any]]] = {}
    slot_candidates: dict[str, set[str]] = {}
    for document in validated:
        for slot in _sequence(document.get("slots"), "ranking.slots"):
            slot_row = _mapping(slot, "ranking.slot")
            slot_id = _portable_id(slot_row.get("slot_id"), "ranking.slot_id")
            slot_candidates.setdefault(slot_id, set())
            for page in _sequence(slot_row.get("pages"), "ranking.pages"):
                page_row = _mapping(page, "ranking.page")
                page_index = _integer(page_row.get("page_index"), "page_index")
                for raw_candidate in _sequence(page_row.get("candidates"), "page.candidates"):
                    candidate = dict(_mapping(raw_candidate, "ranked candidate"))
                    clip_id = _portable_id(candidate.get("clip_id"), "candidate.clip_id")
                    request = {
                        "slot_id": slot_id,
                        "page_index": page_index,
                        "broad_rank": _integer(candidate.pop("broad_rank"), "broad_rank"),
                        "retrieval_score": _finite(
                            candidate.pop("retrieval_score"), "retrieval_score"
                        ),
                    }
                    prior = identities.get(clip_id)
                    if prior is not None and prior != candidate:
                        raise BroadLazyError("same clip_id has conflicting ranked identities")
                    identities[clip_id] = candidate
                    slot_candidates[slot_id].add(clip_id)
                    requests.setdefault(clip_id, []).append(request)
    if not identities:
        raise BroadLazyError("ranked union cannot be empty")
    candidates = []
    for clip_id in sorted(identities):
        unique_requests = {
            canonical_hash(row): row for row in requests.get(clip_id, [])
        }
        candidates.append(
            {
                **identities[clip_id],
                "requests": sorted(
                    unique_requests.values(),
                    key=lambda row: (
                        row["slot_id"], row["page_index"], row["broad_rank"]
                    ),
                ),
            }
        )
    slot_candidate_ids = {
        slot_id: sorted(values)
        for slot_id, values in sorted(slot_candidates.items())
    }
    base = {
        "schema_version": 1,
        "contract": BROAD_UNION_CONTRACT,
        "catalog_hash": validated[0]["catalog_hash"],
        "catalog_candidate_count": validated[0]["catalog_candidate_count"],
        "embedding_hash": validated[0]["embedding_hash"],
        "ranking_hashes": sorted(row["content_hash"] for row in validated),
        "slot_candidate_ids": slot_candidate_ids,
        "candidates": candidates,
        "candidate_count": len(candidates),
    }
    return _content_document(base, id_field="union_id", id_prefix="broad-union-")


def _validate_union(document: Mapping[str, Any]) -> dict[str, Any]:
    return _validate_content_document(
        document,
        contract=BROAD_UNION_CONTRACT,
        id_field="union_id",
        id_prefix="broad-union-",
    )


def _normalized_bbox(value: Any, name: str = "bbox") -> list[float]:
    raw = [_finite(item, name) for item in _sequence(value, name)]
    if len(raw) != 4:
        raise BroadLazyError(f"{name} must contain x1,y1,x2,y2")
    x1, y1, x2, y2 = raw
    if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
        raise BroadLazyError(f"{name} must be normalized and non-empty")
    return [round(item, 9) for item in raw]


def _anchored_edges(bbox: Sequence[float], anchor: float) -> list[str]:
    x1, y1, x2, y2 = bbox
    output = []
    if x1 <= anchor:
        output.append("left")
    if 1.0 - x2 <= anchor:
        output.append("right")
    if y1 <= anchor:
        output.append("top")
    if 1.0 - y2 <= anchor:
        output.append("bottom")
    return output


def build_donor_overlay_profile(
    source_asset_id: str,
    source_sha256: str,
    tracks: Sequence[Mapping[str, Any]],
    *,
    detector_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize cross-scene overlay evidence into a portable donor profile.

    A screen-fixed logo is crop-eligible only when it is edge-anchored and is
    independently supported by the configured number of scene contexts.
    Candidate-local static edges alone therefore never become a crop.
    """

    asset_id = _portable_id(source_asset_id, "source_asset_id")
    digest = _sha256(source_sha256, "source_sha256")
    policy = _overlay_policy(detector_policy)
    policy_hash = canonical_hash(policy)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_value in enumerate(_sequence(tracks, "tracks")):
        raw = _mapping(raw_value, f"tracks[{index}]")
        track_id = _portable_id(raw.get("track_id"), f"tracks[{index}].track_id")
        if track_id in seen:
            raise BroadLazyError("overlay profile track ids must be unique")
        seen.add(track_id)
        classification = str(raw.get("classification") or "").strip()
        if classification not in _PROFILE_TRACK_CLASSES:
            raise BroadLazyError(f"unsupported overlay track classification {classification!r}")
        bbox = _normalized_bbox(raw.get("bbox"), f"tracks[{index}].bbox")
        contexts = sorted(
            {
                _portable_id(value, f"tracks[{index}].context_ids")
                for value in _sequence(raw.get("context_ids") or (), "context_ids")
            }
        )
        persistence = _finite(raw.get("persistence"), "track.persistence")
        confidence = _finite(raw.get("confidence"), "track.confidence")
        if not 0.0 <= persistence <= 1.0 or not 0.0 <= confidence <= 1.0:
            raise BroadLazyError("overlay persistence/confidence must be in [0, 1]")
        edges = _anchored_edges(bbox, policy["edge_anchor_fraction"])
        eligible = (
            classification == "screen_fixed_edge_logo"
            and len(contexts) >= policy["min_logo_contexts"]
            and persistence >= policy["min_logo_persistence"]
            and confidence >= policy["min_logo_confidence"]
            and bool(edges)
        )
        if classification == "natural_static_edge":
            decision = "ignore_natural_edge"
        elif classification in {"central_title", "transient_title"}:
            decision = "reject_on_presence"
        elif eligible:
            decision = "detector_crop"
        else:
            decision = "reject_unproven_overlay"
        normalized.append(
            {
                "track_id": track_id,
                "reported_classification": classification,
                "bbox": bbox,
                "context_ids": contexts,
                "support_contexts": len(contexts),
                "persistence": round(persistence, 9),
                "confidence": round(confidence, 9),
                "anchored_edges": edges,
                "crop_eligible": eligible,
                "decision": decision,
            }
        )
    normalized.sort(key=lambda row: row["track_id"])
    base = {
        "schema_version": 1,
        "contract": DONOR_OVERLAY_PROFILE_CONTRACT,
        "source_asset_id": asset_id,
        "source_sha256": digest,
        "detector_policy": policy,
        "detector_policy_hash": policy_hash,
        "tracks": normalized,
    }
    return _content_document(base, id_field="profile_id", id_prefix="donor-overlay-")


def validate_donor_overlay_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    document = _validate_content_document(
        profile,
        contract=DONOR_OVERLAY_PROFILE_CONTRACT,
        id_field="profile_id",
        id_prefix="donor-overlay-",
    )
    rebuilt = build_donor_overlay_profile(
        str(document.get("source_asset_id") or ""),
        str(document.get("source_sha256") or ""),
        [
            {
                "track_id": row.get("track_id"),
                "classification": row.get("reported_classification"),
                "bbox": row.get("bbox"),
                "context_ids": row.get("context_ids"),
                "persistence": row.get("persistence"),
                "confidence": row.get("confidence"),
            }
            for row in _sequence(document.get("tracks"), "profile.tracks")
        ],
        detector_policy=_mapping(document.get("detector_policy"), "detector_policy"),
    )
    if rebuilt != document:
        raise BroadLazyError("donor overlay profile fields are not canonical")
    return document


def resolve_strict_detector_policy(
    profile: Mapping[str, Any],
    requested_policy: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the exact detector policy bound to strict window analysis.

    Runtime callers may supply only overrides while donor overlay profiles
    contain those overrides expanded with deterministic defaults.  Strict
    planning and strict analysis must hash the same effective policy, so the
    canonical profile is the source of truth after every requested field has
    been verified against it.
    """

    validated = validate_donor_overlay_profile(profile)
    if requested_policy is None:
        requested: dict[str, Any] = {}
    else:
        requested = _json_clone(
            _mapping(requested_policy, "requested_detector_policy"),
            "requested_detector_policy",
        )
    effective = _json_clone(
        _mapping(validated.get("detector_policy"), "profile.detector_policy"),
        "profile.detector_policy",
    )
    for key, value in requested.items():
        if key not in effective or canonical_hash(effective[key]) != canonical_hash(value):
            raise BroadLazyError("requested detector policy/profile mismatch")
    return effective


def _crop_edge_for_track(track: Mapping[str, Any], policy: Mapping[str, Any]) -> tuple[str, float]:
    x1, y1, x2, y2 = [float(value) for value in track["bbox"]]
    padding = float(policy["crop_padding_fraction"])
    values = {
        "left": min(1.0, x2 + padding),
        "right": min(1.0, 1.0 - x1 + padding),
        "top": min(1.0, y2 + padding),
        "bottom": min(1.0, 1.0 - y1 + padding),
    }
    edges = list(track.get("anchored_edges") or ())
    if not edges:
        raise BroadLazyError("crop-eligible overlay has no anchored edge")
    priority = {"left": 0, "right": 1, "top": 2, "bottom": 3}
    return min(edges, key=lambda edge: (values[edge], priority[edge])), values[
        min(edges, key=lambda edge: (values[edge], priority[edge]))
    ]


def evaluate_candidate_overlay_profile(
    donor_profile: Mapping[str, Any],
    candidate_id: str,
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve overlay evidence for one candidate or reject it fail-closed."""

    profile = validate_donor_overlay_profile(donor_profile)
    candidate_id = _portable_id(candidate_id, "candidate_id")
    policy = profile["detector_policy"]
    tracks = {row["track_id"]: row for row in profile["tracks"]}
    margins = {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0}
    evidence: list[dict[str, Any]] = []
    rejection_reasons: list[str] = []
    for index, raw_value in enumerate(_sequence(observations, "observations")):
        raw = _mapping(raw_value, f"observations[{index}]")
        kind = str(raw.get("kind") or "").strip()
        if kind not in _CANDIDATE_OBSERVATION_KINDS:
            rejection_reasons.append(f"unknown_observation:{kind or 'missing'}")
            continue
        observation_id = _portable_id(
            raw.get("observation_id") or f"observation-{index:04d}",
            "observation_id",
        )
        if kind == "natural_static_edge":
            evidence.append(
                {
                    "observation_id": observation_id,
                    "kind": kind,
                    "decision": "ignored_not_overlay",
                }
            )
            continue
        if kind in {"central_title", "transient_title", "unclassified_overlay"}:
            rejection_reasons.append(kind)
            evidence.append(
                {
                    "observation_id": observation_id,
                    "kind": kind,
                    "decision": "reject",
                }
            )
            continue
        track_id = str(raw.get("profile_track_id") or "").strip()
        track = tracks.get(track_id)
        if track is None:
            rejection_reasons.append("unknown_profile_track")
            evidence.append(
                {
                    "observation_id": observation_id,
                    "kind": kind,
                    "profile_track_id": track_id,
                    "decision": "reject",
                }
            )
            continue
        decision = str(track["decision"])
        evidence.append(
            {
                "observation_id": observation_id,
                "kind": kind,
                "profile_track_id": track_id,
                "profile_track_decision": decision,
            }
        )
        if decision == "ignore_natural_edge":
            continue
        if decision != "detector_crop" or not bool(track.get("crop_eligible")):
            rejection_reasons.append(decision)
            continue
        edge, value = _crop_edge_for_track(track, policy)
        margins[edge] = max(margins[edge], value)

    margins = {key: round(value, 9) for key, value in margins.items()}
    retained_area = (1.0 - margins["left"] - margins["right"]) * (
        1.0 - margins["top"] - margins["bottom"]
    )
    unsafe_crop = (
        any(value > policy["max_crop_fraction"] for value in margins.values())
        or margins["left"] + margins["right"] >= 1.0
        or margins["top"] + margins["bottom"] >= 1.0
        or retained_area < policy["min_retained_area"]
    )
    if unsafe_crop and any(margins.values()):
        rejection_reasons.append("detector_crop_unsafe")
    rejected = bool(rejection_reasons)
    action = "reject" if rejected else "crop" if any(margins.values()) else "none"
    base = {
        "schema_version": 1,
        "contract": CANDIDATE_OVERLAY_PROFILE_CONTRACT,
        "candidate_id": candidate_id,
        "source_asset_id": profile["source_asset_id"],
        "source_sha256": profile["source_sha256"],
        "donor_profile_hash": profile["content_hash"],
        "detector_policy_hash": profile["detector_policy_hash"],
        "observations_hash": canonical_hash(_json_clone(list(observations), "observations")),
        "status": "rejected" if rejected else "resolved",
        "action": action,
        "crop_safe": action == "crop",
        "crop_margins": margins if action == "crop" else {
            "left": 0.0,
            "right": 0.0,
            "top": 0.0,
            "bottom": 0.0,
        },
        "retained_area": round(retained_area if action == "crop" else 1.0, 9),
        "rejection_reasons": sorted(set(rejection_reasons)),
        "evidence": evidence,
        "render_constraints": {
            "fill_strategy": "none",
            "retime_factor": 1.0,
            "allow_hold": False,
            "allow_inpaint": False,
            "allow_manual_crop": False,
        },
    }
    return _content_document(base, id_field="profile_id", id_prefix="candidate-overlay-")


def strict_window_cache_key(
    *,
    donor_sha256: str,
    clip_id: str,
    source_in: float,
    source_out: float,
    window_start: float,
    window_end: float,
    model_contract_hash: str,
    detector_policy: Mapping[str, Any],
    overlay_profile_hash: str,
) -> str:
    """Return a deterministic cache key with no mount or source path fields."""

    digest = _sha256(donor_sha256, "donor_sha256")
    clip_id = _portable_id(clip_id, "clip_id")
    model_hash = _sha256(model_contract_hash, "model_contract_hash")
    overlay_hash = _sha256(overlay_profile_hash, "overlay_profile_hash")
    values = {
        "source_in": _finite(source_in, "source_in"),
        "source_out": _finite(source_out, "source_out"),
        "window_start": _finite(window_start, "window_start"),
        "window_end": _finite(window_end, "window_end"),
    }
    if not 0 <= values["window_start"] <= values["source_in"] < values["source_out"] <= values["window_end"]:
        raise BroadLazyError("strict window bounds do not contain the candidate")
    detector = _json_clone(_mapping(detector_policy, "detector_policy"), "detector_policy")
    _assert_portable(detector, "detector_policy")
    return _WINDOW_KEY_PREFIX + canonical_hash(
        {
            "donor_sha256": digest,
            "clip_id": clip_id,
            **{key: round(value, 9) for key, value in values.items()},
            "model_contract_hash": model_hash,
            "detector_policy": detector,
            "overlay_profile_hash": overlay_hash,
            "exact_pts_contract": EXACT_PTS_CONTRACT,
        }
    )


def plan_lazy_strict_windows(
    catalog: Mapping[str, Any],
    ranked_union: Mapping[str, Any],
    embedding_contract: Mapping[str, Any],
    *,
    slot_render_frames: Mapping[str, int],
    detector_policy: Mapping[str, Any],
    overlay_profiles: Mapping[str, Mapping[str, Any]],
    search_seconds: float = 4.0,
) -> dict[str, Any]:
    """Plan strict work for the ranked top union, never the whole catalog."""

    validated_catalog = validate_broad_catalog(catalog)
    union = _validate_union(ranked_union)
    embeddings = _validate_content_document(
        embedding_contract,
        contract=BROAD_EMBEDDING_CONTRACT,
        id_field="embedding_id",
        id_prefix="broad-emb-",
    )
    if union.get("catalog_hash") != validated_catalog["content_hash"]:
        raise BroadLazyError("ranked union is bound to another catalog")
    if union.get("embedding_hash") != embeddings["content_hash"]:
        raise BroadLazyError("ranked union is bound to another embedding contract")
    if embeddings.get("catalog_hash") != validated_catalog["content_hash"]:
        raise BroadLazyError("embedding contract is bound to another catalog")
    search_seconds = _finite(search_seconds, "search_seconds")
    if not 0.0 <= search_seconds <= 30.0:
        raise BroadLazyError("search_seconds must be in [0, 30]")
    render_frames = {
        _portable_id(slot_id, "slot_render_frames.slot_id"): _positive_int(value, "render_frames")
        for slot_id, value in _mapping(slot_render_frames, "slot_render_frames").items()
    }
    detector = _json_clone(_mapping(detector_policy, "detector_policy"), "detector_policy")
    _assert_portable(detector, "detector_policy")
    detector_hash = canonical_hash(detector)
    donors = {row["source_asset_id"]: row for row in validated_catalog["donors"]}
    catalog_candidates = {row["clip_id"]: row for row in validated_catalog["candidates"]}
    profiles = {
        _portable_id(asset_id, "overlay_profiles.asset_id"): validate_donor_overlay_profile(profile)
        for asset_id, profile in _mapping(overlay_profiles, "overlay_profiles").items()
    }
    windows: list[dict[str, Any]] = []
    effective_detector_policy_hashes: set[str] = set()
    for raw in _sequence(union.get("candidates"), "ranked_union.candidates"):
        candidate = _mapping(raw, "ranked_union.candidate")
        clip_id = _portable_id(candidate.get("clip_id"), "candidate.clip_id")
        canonical_candidate = catalog_candidates.get(clip_id)
        candidate_without_requests = {
            key: value for key, value in candidate.items() if key != "requests"
        }
        if canonical_candidate is None or candidate_without_requests != canonical_candidate:
            raise BroadLazyError("ranked union candidate disagrees with broad catalog")
        asset_id = str(candidate["source_asset_id"])
        donor = donors.get(asset_id)
        profile = profiles.get(asset_id)
        if donor is None or profile is None:
            raise BroadLazyError(f"missing donor/overlay profile for {asset_id}")
        if profile["source_sha256"] != donor["source_sha256"]:
            raise BroadLazyError("overlay profile donor SHA-256 mismatch")
        effective_detector_policy = resolve_strict_detector_policy(profile, detector)
        effective_detector_policy_hash = canonical_hash(effective_detector_policy)
        if effective_detector_policy_hash != profile["detector_policy_hash"]:
            raise BroadLazyError("overlay profile detector policy hash mismatch")
        effective_detector_policy_hashes.add(effective_detector_policy_hash)
        source_in = float(candidate["source_in"])
        source_out = float(candidate["source_out"])
        window_start = round(max(0.0, source_in - search_seconds), 9)
        window_end = round(min(float(donor["duration"]), source_out + search_seconds), 9)
        requested: dict[str, int] = {}
        for request in _sequence(candidate.get("requests"), "candidate.requests"):
            slot_id = _portable_id(_mapping(request, "request").get("slot_id"), "request.slot_id")
            if slot_id not in render_frames:
                raise BroadLazyError(f"missing render frame count for slot {slot_id}")
            requested[slot_id] = render_frames[slot_id]
        if not requested:
            raise BroadLazyError("strict window has no requesting slots")
        windows.append(
            {
                "clip_id": clip_id,
                "candidate_key": candidate["candidate_key"],
                "source_asset_id": asset_id,
                "source_sha256": donor["source_sha256"],
                "candidate_source_in": source_in,
                "candidate_source_out": source_out,
                "requested_window_start": window_start,
                "requested_window_end": window_end,
                "requested_slots": [
                    {"slot_id": slot_id, "render_frames": requested[slot_id]}
                    for slot_id in sorted(requested)
                ],
                "overlay_profile_hash": profile["content_hash"],
                "detector_policy_hash": effective_detector_policy_hash,
                "detector_policy_resolution_contract": (
                    STRICT_DETECTOR_POLICY_RESOLUTION_CONTRACT
                ),
                "cache_key": strict_window_cache_key(
                    donor_sha256=donor["source_sha256"],
                    clip_id=clip_id,
                    source_in=source_in,
                    source_out=source_out,
                    window_start=window_start,
                    window_end=window_end,
                    model_contract_hash=embeddings["model_contract_hash"],
                    detector_policy=effective_detector_policy,
                    overlay_profile_hash=profile["content_hash"],
                ),
                "status": "planned_not_renderable",
                "requires_exact_pts_upgrade": True,
            }
        )
    windows.sort(key=lambda row: row["clip_id"])
    if len(windows) != int(union.get("candidate_count") or -1):
        raise BroadLazyError("lazy plan did not preserve ranked union cardinality")
    base = {
        "schema_version": 1,
        "contract": LAZY_PLAN_CONTRACT,
        "catalog_hash": validated_catalog["content_hash"],
        "embedding_hash": embeddings["content_hash"],
        "ranked_union_hash": union["content_hash"],
        "model_contract_hash": embeddings["model_contract_hash"],
        "detector_policy": detector,
        "detector_policy_hash": detector_hash,
        "detector_policy_resolution_contract": (
            STRICT_DETECTOR_POLICY_RESOLUTION_CONTRACT
        ),
        "effective_detector_policy_hashes": sorted(
            effective_detector_policy_hashes
        ),
        "search_seconds": round(search_seconds, 9),
        "broad_candidate_count": validated_catalog["candidate_count"],
        "planned_window_count": len(windows),
        "windows": windows,
        "render_constraints": {
            "fill_strategy": "none",
            "retime_factor": 1.0,
            "allow_hold": False,
            "allow_tpad": False,
            "allow_manual_selection": False,
        },
    }
    return _content_document(base, id_field="plan_id", id_prefix="lazy-plan-")


def _time_base(value: Any) -> Fraction:
    try:
        if isinstance(value, Mapping):
            numerator = _integer(
                value.get("numerator", value.get("num")), "time_base.numerator"
            )
            denominator = _integer(
                value.get("denominator", value.get("den")), "time_base.denominator"
            )
            result = Fraction(numerator, denominator)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            values = list(value)
            if len(values) != 2:
                raise BroadLazyError("time_base sequence must contain two integers")
            result = Fraction(_integer(values[0], "time_base[0]"), _integer(values[1], "time_base[1]"))
        else:
            result = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise BroadLazyError("time_base must be a positive rational") from exc
    if result <= 0:
        raise BroadLazyError("time_base must be a positive rational")
    return result


def _fraction_document(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def build_exact_pts_window_contract(
    frame_pts: Sequence[int],
    time_base: Any,
    *,
    start_pts: int,
    end_pts: int,
    source_end_pts: int | None = None,
) -> dict[str, Any]:
    """Slice exact frame PTS at real frame boundaries without FPS inference.

    ``start_pts`` must equal an observed frame PTS.  ``end_pts`` must equal a
    later observed frame PTS, or the explicitly probed terminal source PTS.
    The helper never derives cadence from average FPS, median deltas, or packet
    count, making the resulting frame addressing safe for VFR inputs.
    """

    points = [_integer(value, "frame_pts") for value in _sequence(frame_pts, "frame_pts")]
    if not points or any(right <= left for left, right in zip(points, points[1:])):
        raise BroadLazyError("frame_pts must be a non-empty strictly increasing sequence")
    start = _integer(start_pts, "start_pts")
    end = _integer(end_pts, "end_pts")
    if end <= start:
        raise BroadLazyError("exact PTS window must satisfy start_pts < end_pts")
    left = bisect.bisect_left(points, start)
    if left >= len(points) or points[left] != start:
        raise BroadLazyError("start_pts is not an observed frame boundary")
    right = bisect.bisect_left(points, end)
    if right < len(points) and points[right] == end:
        pass
    else:
        terminal = (
            _integer(source_end_pts, "source_end_pts")
            if source_end_pts is not None
            else None
        )
        if terminal is None or end != terminal or terminal <= points[-1]:
            raise BroadLazyError("end_pts is not an observed or explicit terminal boundary")
        right = len(points)
    if right <= left:
        raise BroadLazyError("exact PTS window contains no frames")
    tb = _time_base(time_base)
    selected = points[left:right]
    duration = Fraction(end - start) * tb
    base = {
        "schema_version": 1,
        "contract": EXACT_PTS_CONTRACT,
        "time_base": {"numerator": tb.numerator, "denominator": tb.denominator},
        "source_frame_start": left,
        "source_frame_end": right,
        "frame_count": len(selected),
        "frame_pts": selected,
        "start_pts": start,
        "end_pts": end,
        "start_time": _fraction_document(Fraction(start) * tb),
        "end_time": _fraction_document(Fraction(end) * tb),
        "duration": _fraction_document(duration),
        "addressing": "integer_pts_half_open",
        "fps_inference_used": False,
    }
    return _content_document(base, id_field="window_id", id_prefix="pts-window-")


__all__ = [
    "BROAD_CATALOG_CONTRACT",
    "BROAD_EMBEDDING_CONTRACT",
    "BROAD_EMBEDDING_REPRESENTATION",
    "BROAD_RANKING_CONTRACT",
    "BROAD_UNION_CONTRACT",
    "LAZY_PLAN_CONTRACT",
    "EXACT_PTS_CONTRACT",
    "DONOR_OVERLAY_PROFILE_CONTRACT",
    "CANDIDATE_OVERLAY_PROFILE_CONTRACT",
    "STRICT_DETECTOR_POLICY_RESOLUTION_CONTRACT",
    "DEFAULT_BROAD_POLICY",
    "DEFAULT_OVERLAY_POLICY",
    "BroadLazyError",
    "broad_catalog_cache_key",
    "build_portable_broad_catalog",
    "validate_broad_catalog",
    "save_broad_catalog",
    "load_broad_catalog",
    "build_portable_embedding_contract",
    "validate_embedding_contract",
    "rank_broad_candidate_pages",
    "deduplicate_ranked_union",
    "build_donor_overlay_profile",
    "validate_donor_overlay_profile",
    "resolve_strict_detector_policy",
    "evaluate_candidate_overlay_profile",
    "strict_window_cache_key",
    "plan_lazy_strict_windows",
    "build_exact_pts_window_contract",
]
