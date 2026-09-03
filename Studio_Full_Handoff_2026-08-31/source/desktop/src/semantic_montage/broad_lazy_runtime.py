"""Runnable, path-free orchestration for broad retrieval and lazy strict upgrade.

The broad catalog and every durable JSON artifact contain only asset IDs,
content hashes, and source-relative timing.  Host paths are injected from a
runtime resolver immediately before media access and are stripped before a
strict result is cached or returned.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import math
import os
from pathlib import Path
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

from .broad_lazy import (
    BroadLazyError,
    build_donor_overlay_profile,
    build_portable_broad_catalog,
    build_portable_embedding_contract,
    deduplicate_ranked_union,
    plan_lazy_strict_windows,
    rank_broad_candidate_pages,
    validate_broad_catalog,
    validate_embedding_contract,
)
from .clip_model import embed_text as _embed_text
from .common import atomic_write_json, canonical_hash
from .donor import index_donor as _index_donor
from .retrieval import embed_contact_sheet_rows as _embed_contact_sheet_rows
from .retrieval import make_contact_sheet as _make_contact_sheet


PATH_FREE_ARTIFACT_CONTRACT = "broad_lazy_path_free_artifact_v1"
EMBEDDING_CACHE_CONTRACT = "broad_lazy_embedding_cache_v2"
STRICT_CACHE_CONTRACT = "broad_lazy_strict_cache_v2"
STRICT_RESULT_CONTRACT = "broad_lazy_strict_result_v1"
FOUR_FRAME_EMBEDDING_METHOD = "four_frame_contact_sheet_pooled_v2"
DEFAULT_STRICT_CALLBACK_CONTRACT = "strict_upgrade_callback_v1"
STRICT_VARIANTS_FIELD = "strict_variants"
MAX_STRICT_VARIANTS_PER_TASK = 16

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


class BroadLazyRuntimeError(ValueError):
    """Runtime orchestration or a durable cache artifact is invalid."""


class BroadLazyRuntimeBlocked(RuntimeError):
    """Lazy paging exhausted the broad catalog without enough strict ranges."""

    def __init__(self, code: str, message: str, artifact: Mapping[str, Any]):
        super().__init__(message)
        self.code = str(code)
        self.artifact = dict(artifact)


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
        raise BroadLazyRuntimeError(f"{name} must be finite JSON data") from exc


def _assert_path_free(value: Any, name: str = "artifact") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in _PATH_FIELDS:
                raise BroadLazyRuntimeError(
                    f"{name} contains forbidden path field {key!r}"
                )
            _assert_path_free(item, f"{name}.{key}")
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for index, item in enumerate(value):
            _assert_path_free(item, f"{name}[{index}]")
    elif isinstance(value, str) and _ABSOLUTE_PATH.match(value.strip()):
        raise BroadLazyRuntimeError(f"{name} contains an absolute host path")


def _sealed(base: Mapping[str, Any]) -> dict[str, Any]:
    document = _clone(dict(base), "document")
    _assert_path_free(document)
    return {**document, "content_hash": canonical_hash(document)}


def _validate_sealed(
    document: Mapping[str, Any], *, contract: str | None = None
) -> dict[str, Any]:
    row = _clone(dict(document), "document")
    supplied = str(row.pop("content_hash", ""))
    if len(supplied) != 64 or canonical_hash(row) != supplied:
        raise BroadLazyRuntimeError("durable artifact content hash mismatch")
    if contract is not None and row.get("contract") != contract:
        raise BroadLazyRuntimeError(f"expected durable contract {contract}")
    validated = {**row, "content_hash": supplied}
    _assert_path_free(validated)
    return validated


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise BroadLazyRuntimeError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BroadLazyRuntimeError(f"{name} must be a positive integer") from exc
    if parsed <= 0 or parsed != value:
        raise BroadLazyRuntimeError(f"{name} must be a positive integer")
    return parsed


def _portable_id(value: Any, name: str) -> str:
    identifier = str(value or "").strip()
    if not identifier or _ABSOLUTE_PATH.match(identifier):
        raise BroadLazyRuntimeError(f"{name} must be a portable identifier")
    return identifier


def save_path_free_artifact(
    document: Mapping[str, Any],
    directory: str | os.PathLike[str],
    *,
    kind: str,
) -> Path:
    """Atomically save arbitrary portable JSON in a content-addressed envelope."""

    payload = _clone(dict(document), "payload")
    _assert_path_free(payload, "payload")
    kind = _portable_id(kind, "kind")
    envelope = _sealed(
        {
            "schema_version": 1,
            "contract": PATH_FREE_ARTIFACT_CONTRACT,
            "kind": kind,
            "payload": payload,
        }
    )
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{kind}.{envelope['content_hash']}.json"
    if target.exists():
        existing = load_path_free_artifact(target, expected_kind=kind)
        if existing != payload:
            raise BroadLazyRuntimeError(
                "content-addressed artifact contains different payload"
            )
        return target
    atomic_write_json(target, envelope)
    return target


def load_path_free_artifact(
    path: str | os.PathLike[str], *, expected_kind: str | None = None
) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BroadLazyRuntimeError(f"cannot load path-free artifact: {exc}") from exc
    envelope = _validate_sealed(raw, contract=PATH_FREE_ARTIFACT_CONTRACT)
    if expected_kind is not None and envelope.get("kind") != expected_kind:
        raise BroadLazyRuntimeError("path-free artifact kind mismatch")
    payload = _clone(envelope.get("payload"), "payload")
    if not isinstance(payload, dict):
        raise BroadLazyRuntimeError("path-free artifact payload must be an object")
    _assert_path_free(payload, "payload")
    return payload


def build_complete_broad_catalog(
    asset_paths: Mapping[str, str | os.PathLike[str]],
    *,
    index_workers: int = 2,
    indexer: Callable = _index_donor,
    ffprobe_bin: str = "ffprobe",
    probe_timeout: float = 90.0,
    detector_threshold: float = 22.0,
    min_scene_len_frames: int = 15,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Index every donor completely, then discard all runtime paths.

    ``max_seconds=None`` and ``frame_contract=None`` are explicit callback
    arguments.  A strict/capped index returned despite those arguments is
    rejected before portable conversion.
    """

    if not isinstance(asset_paths, Mapping) or not asset_paths:
        raise BroadLazyRuntimeError("asset_paths must be a non-empty mapping")
    workers = _positive_int(index_workers, "index_workers")
    items: list[tuple[str, Path]] = []
    seen_paths: set[str] = set()
    for raw_asset_id in sorted(asset_paths, key=lambda value: str(value).casefold()):
        asset_id = _portable_id(raw_asset_id, "asset_id")
        try:
            path = Path(asset_paths[raw_asset_id]).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise BroadLazyRuntimeError(f"donor asset is unavailable: {asset_id}") from exc
        if not path.is_file():
            raise BroadLazyRuntimeError(f"donor asset is not a file: {asset_id}")
        if str(path) in seen_paths:
            raise BroadLazyRuntimeError("two asset IDs cannot resolve to the same donor")
        seen_paths.add(str(path))
        items.append((asset_id, path))

    def work(item: tuple[str, Path]) -> tuple[str, dict[str, Any]]:
        asset_id, path = item
        indexed = indexer(
            str(path),
            ffprobe_bin=ffprobe_bin,
            probe_timeout=probe_timeout,
            detector_threshold=detector_threshold,
            min_scene_len_frames=min_scene_len_frames,
            start_seconds=0.0,
            max_seconds=None,
            frame_contract=None,
        )
        if hasattr(indexed, "to_dict"):
            raw = indexed.to_dict()
        elif isinstance(indexed, Mapping):
            raw = dict(indexed)
        else:
            raise BroadLazyRuntimeError(
                f"indexer returned an unsupported result for {asset_id}"
            )
        if raw.get("frame_contract") is not None or any(
            isinstance(row, Mapping) and row.get("frame_contract") is not None
            for row in raw.get("clips") or ()
        ):
            raise BroadLazyRuntimeError(
                f"broad donor index unexpectedly contains strict frame data: {asset_id}"
            )
        return asset_id, raw

    indexes_by_id: dict[str, dict[str, Any]] = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(work, item): item[0] for item in items}
        for future in cf.as_completed(futures):
            asset_id, raw = future.result()
            indexes_by_id[asset_id] = raw

    ordered_ids = [asset_id for asset_id, _path in items]
    catalog = build_portable_broad_catalog(
        [indexes_by_id[asset_id] for asset_id in ordered_ids],
        source_asset_ids=ordered_ids,
        policy=policy,
    )
    _assert_path_free(catalog, "broad_catalog")
    return catalog


def prepare_runtime_candidates(
    candidates: Sequence[Mapping[str, Any]],
    asset_resolver: Mapping[str, str | os.PathLike[str]],
) -> list[dict[str, Any]]:
    """Hydrate portable candidates from asset IDs without changing lineage."""

    if not isinstance(asset_resolver, Mapping):
        raise BroadLazyRuntimeError("asset_resolver must be an object")
    resolved: dict[str, str] = {}
    output: list[dict[str, Any]] = []
    for index, raw in enumerate(candidates):
        if not isinstance(raw, Mapping):
            raise BroadLazyRuntimeError(f"candidates[{index}] must be an object")
        asset_id = _portable_id(raw.get("source_asset_id"), "source_asset_id")
        if asset_id not in asset_resolver:
            raise BroadLazyRuntimeError(f"asset resolver is missing {asset_id}")
        if asset_id not in resolved:
            try:
                source = Path(asset_resolver[asset_id]).expanduser().resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise BroadLazyRuntimeError(
                    f"resolved donor is unavailable: {asset_id}"
                ) from exc
            if not source.is_file():
                raise BroadLazyRuntimeError(f"resolved donor is not a file: {asset_id}")
            resolved[asset_id] = str(source)
        row = dict(raw)
        row["source_path"] = resolved[asset_id]
        row["source_id"] = str(row.get("source_sha256") or "")
        output.append(row)
    return output


def _embedding_request_key(
    catalog: Mapping[str, Any], model_contract: Mapping[str, Any]
) -> str:
    validated = validate_broad_catalog(catalog)
    model = _clone(dict(model_contract), "model_contract")
    _assert_path_free(model, "model_contract")
    return canonical_hash(
        {
            "catalog_hash": validated["content_hash"],
            "model_contract": model,
            "embedding_method": FOUR_FRAME_EMBEDDING_METHOD,
        }
    )


def _write_vectors(path: Path, vectors: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("wb") as output:
            np.savez_compressed(output, vectors=np.asarray(vectors, dtype=np.float32))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _load_embedding_cache(
    lookup_path: Path,
    vector_root: Path,
    *,
    catalog: Mapping[str, Any],
    request_key: str,
) -> tuple[dict[str, Any], np.ndarray] | None:
    if not lookup_path.exists():
        return None
    try:
        raw = json.loads(lookup_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BroadLazyRuntimeError(f"cannot load embedding cache: {exc}") from exc
    lookup = _validate_sealed(raw, contract=EMBEDDING_CACHE_CONTRACT)
    if lookup.get("request_key") != request_key:
        raise BroadLazyRuntimeError("embedding cache request key mismatch")
    contract = lookup.get("embedding_contract")
    if not isinstance(contract, Mapping):
        raise BroadLazyRuntimeError("embedding cache has no contract")
    vector_path = vector_root / f"vectors.{contract.get('content_hash')}.npz"
    if not vector_path.is_file():
        raise BroadLazyRuntimeError("embedding cache vector payload is missing")
    try:
        with np.load(vector_path, allow_pickle=False) as data:
            vectors = np.asarray(data["vectors"], dtype=np.float32)
    except (OSError, KeyError, ValueError) as exc:
        raise BroadLazyRuntimeError(f"cannot load cached vectors: {exc}") from exc
    validated, vectors = validate_embedding_contract(contract, catalog, vectors)
    return validated, vectors


def materialize_broad_embeddings(
    catalog: Mapping[str, Any],
    asset_resolver: Mapping[str, str | os.PathLike[str]],
    cache_directory: str | os.PathLike[str],
    *,
    model_contract: Mapping[str, Any],
    sheet_workers: int = 4,
    embedding_batch_size: int = 64,
    ffmpeg: str = "ffmpeg",
    sheet_maker: Callable = _make_contact_sheet,
    embedding_fn: Callable = _embed_contact_sheet_rows,
) -> tuple[dict[str, Any], np.ndarray, dict[str, int]]:
    """Materialize four-frame sheets and a content-addressed vector matrix."""

    validated = validate_broad_catalog(catalog)
    workers = _positive_int(sheet_workers, "sheet_workers")
    batch_size = _positive_int(embedding_batch_size, "embedding_batch_size")
    root = Path(cache_directory)
    lookup_root = root / "embedding_lookup"
    vector_root = root / "embedding_vectors"
    sheet_root = root / "broad_sheets"
    request_key = _embedding_request_key(validated, model_contract)
    lookup_path = lookup_root / f"embedding.{request_key}.json"
    cached = _load_embedding_cache(
        lookup_path, vector_root, catalog=validated, request_key=request_key
    )
    if cached is not None:
        contract, vectors = cached
        return contract, vectors, {
            "embedding_cache_hits": 1,
            "embedding_cache_misses": 0,
            "sheet_cache_hits": int(validated["candidate_count"]),
            "sheet_materialized": 0,
        }

    runtime = prepare_runtime_candidates(validated["candidates"], asset_resolver)
    sheets: list[dict[str, Any] | None] = [None] * len(runtime)

    def make(index: int, candidate: Mapping[str, Any]) -> tuple[int, dict[str, Any], bool]:
        target = sheet_root / f"sheet.{candidate['candidate_key']}.jpg"
        hit = target.is_file() and target.stat().st_size > 2048
        if not hit:
            sheet_maker(dict(candidate), target, ffmpeg=ffmpeg)
        if not target.is_file() or target.stat().st_size <= 2048:
            raise BroadLazyRuntimeError(
                f"four-frame sheet is missing for {candidate['clip_id']}"
            )
        return index, {**dict(candidate), "contact_sheet": str(target)}, hit

    sheet_hits = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(make, index, candidate)
            for index, candidate in enumerate(runtime)
        ]
        for future in cf.as_completed(futures):
            index, row, hit = future.result()
            sheets[index] = row
            sheet_hits += int(hit)
    ordered_sheets = [row for row in sheets if row is not None]
    if len(ordered_sheets) != int(validated["candidate_count"]):
        raise BroadLazyRuntimeError("sheet materialization lost broad candidates")
    vectors = np.asarray(
        embedding_fn(ordered_sheets, batch_size=batch_size), dtype=np.float32
    )
    contract = build_portable_embedding_contract(
        validated,
        vectors,
        model_contract=model_contract,
    )
    vector_path = vector_root / f"vectors.{contract['content_hash']}.npz"
    if not vector_path.exists():
        _write_vectors(vector_path, vectors)
    lookup = _sealed(
        {
            "schema_version": 1,
            "contract": EMBEDDING_CACHE_CONTRACT,
            "request_key": request_key,
            "embedding_method": FOUR_FRAME_EMBEDDING_METHOD,
            "embedding_contract": contract,
        }
    )
    lookup_root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(lookup_path, lookup)
    return contract, vectors, {
        "embedding_cache_hits": 0,
        "embedding_cache_misses": 1,
        "sheet_cache_hits": sheet_hits,
        "sheet_materialized": len(ordered_sheets) - sheet_hits,
    }


def _query_vectors_for_briefs(
    briefs: Sequence[Mapping[str, Any]], query_embedder: Callable
) -> tuple[list[str], dict[str, np.ndarray]]:
    if not isinstance(briefs, Sequence) or isinstance(briefs, (str, bytes)) or not briefs:
        raise BroadLazyRuntimeError("briefs must be a non-empty array")
    slot_ids: list[str] = []
    vectors: dict[str, np.ndarray] = {}
    for index, brief in enumerate(briefs):
        if not isinstance(brief, Mapping):
            raise BroadLazyRuntimeError(f"briefs[{index}] must be an object")
        slot_id = _portable_id(brief.get("slot_id"), "brief.slot_id")
        if slot_id in vectors:
            raise BroadLazyRuntimeError(f"duplicate brief slot_id: {slot_id}")
        query = str(brief.get("query_en") or "").strip()
        if not query:
            raise BroadLazyRuntimeError(f"brief {slot_id} has no query_en")
        vector = np.asarray(query_embedder(query), dtype=np.float32)
        if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
            raise BroadLazyRuntimeError(f"query embedding is invalid for {slot_id}")
        slot_ids.append(slot_id)
        vectors[slot_id] = vector
    return slot_ids, vectors


def rank_broad_pages_for_briefs(
    catalog: Mapping[str, Any],
    embedding_contract: Mapping[str, Any],
    vectors: np.ndarray,
    briefs: Sequence[Mapping[str, Any]],
    *,
    page_size: int = 20,
    start_page: int = 0,
    page_count: int = 1,
    query_embedder: Callable = _embed_text,
) -> dict[str, Any]:
    """Embed each brief query and rank deterministic broad pages."""

    _slot_ids, query_vectors = _query_vectors_for_briefs(briefs, query_embedder)
    return rank_broad_candidate_pages(
        catalog,
        embedding_contract,
        vectors,
        query_vectors,
        page_size=page_size,
        start_page=start_page,
        page_count=page_count,
    )


def deduplicate_top_union(
    rankings: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Expose the portable broad union under the runnable runtime API."""

    return deduplicate_ranked_union(rankings)


def _strip_runtime_paths(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_runtime_paths(item)
            for key, item in value.items()
            if str(key).casefold() not in _PATH_FIELDS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_strip_runtime_paths(item) for item in value]
    return value


def _portable_strict_candidate(
    result: Mapping[str, Any],
    broad_candidate: Mapping[str, Any],
    window: Mapping[str, Any],
    *,
    render_frames: int,
    resolved_source_path: str,
) -> dict[str, Any]:
    row = dict(result)
    if row.get("source_path") is not None:
        try:
            returned_path = str(Path(str(row["source_path"])).expanduser().resolve())
        except (OSError, RuntimeError) as exc:
            raise BroadLazyRuntimeError("strict result source path is invalid") from exc
        if returned_path != resolved_source_path:
            raise BroadLazyRuntimeError("strict result changed donor lineage path")
    for field in ("clip_id", "candidate_key", "source_asset_id", "source_sha256"):
        if str(row.get(field) or "") != str(broad_candidate.get(field) or ""):
            raise BroadLazyRuntimeError(f"strict result changed immutable {field}")
    try:
        source_in = float(row.get("source_in"))
        source_out = float(row.get("source_out"))
    except (TypeError, ValueError) as exc:
        raise BroadLazyRuntimeError("strict result has invalid source bounds") from exc
    if not (
        float(window["requested_window_start"]) - 1e-6
        <= source_in
        < source_out
        <= float(window["requested_window_end"]) + 1e-6
    ):
        raise BroadLazyRuntimeError("strict result escaped its planned source window")
    if int(row.get("render_frames") or 0) != render_frames:
        raise BroadLazyRuntimeError("strict result render_frames mismatch")
    if not isinstance(row.get("frame_contract"), Mapping):
        raise BroadLazyRuntimeError("strict result has no frame contract")
    if str(row.get("fill_strategy") or "") != "none":
        raise BroadLazyRuntimeError("strict result uses a fill strategy")
    if abs(float(row.get("retime_factor") or 0.0) - 1.0) > 1e-9:
        raise BroadLazyRuntimeError("strict result uses retiming")
    if not str(row.get("selection_contract") or "").startswith("frame_exact"):
        raise BroadLazyRuntimeError("strict result is not frame exact")
    if any(
        bool(row.get(field))
        for field in ("allow_hold", "allow_tpad", "manual_selection", "held_tail_seconds")
    ):
        raise BroadLazyRuntimeError("strict result enables manual/fill lineage")
    row.update(
        {
            "retrieval_only": False,
            "renderable": True,
            "strict_window_cache_key": str(window["cache_key"]),
        }
    )
    portable = _clone(_strip_runtime_paths(row), "strict_candidate")
    _assert_path_free(portable, "strict_candidate")
    return portable


def _strict_variant_identity(candidate: Mapping[str, Any]) -> str:
    """Return a range identity without relying on the broad parent clip id."""

    return canonical_hash(
        {
            "source_asset_id": str(candidate.get("source_asset_id") or ""),
            "source_sha256": str(candidate.get("source_sha256") or ""),
            "source_frame_start": int(candidate.get("source_frame_start") or 0),
            "source_frame_end": int(candidate.get("source_frame_end") or 0),
            "source_in": round(float(candidate.get("source_in") or 0.0), 9),
            "source_out": round(float(candidate.get("source_out") or 0.0), 9),
            "render_frames": int(candidate.get("render_frames") or 0),
            "clean_frame_start": int(candidate.get("clean_frame_start") or 0),
            "clean_frame_end": int(candidate.get("clean_frame_end") or 0),
        }
    )


def _portable_strict_variants(
    result: Mapping[str, Any],
    broad_candidate: Mapping[str, Any],
    window: Mapping[str, Any],
    *,
    render_frames: int,
    resolved_source_path: str,
) -> list[dict[str, Any]]:
    """Validate the primary strict result and every clean-span alternative.

    The callback remains backward compatible: a legacy single mapping becomes
    one variant.  New callbacks attach ``strict_variants`` while keeping the
    primary candidate at the top level for existing callers.
    """

    primary = {
        key: value for key, value in result.items() if key != STRICT_VARIANTS_FIELD
    }
    raw_variants = result.get(STRICT_VARIANTS_FIELD)
    if raw_variants is None:
        ordered: list[Mapping[str, Any]] = [primary]
    else:
        if not isinstance(raw_variants, Sequence) or isinstance(
            raw_variants, (str, bytes, bytearray)
        ):
            raise BroadLazyRuntimeError("strict_variants must be an array")
        if len(raw_variants) > MAX_STRICT_VARIANTS_PER_TASK:
            raise BroadLazyRuntimeError("strict_variants exceeds the bounded task limit")
        ordered = [primary]
        for raw in raw_variants:
            if not isinstance(raw, Mapping):
                raise BroadLazyRuntimeError("strict_variants rows must be objects")
            ordered.append(
                {key: value for key, value in raw.items() if key != STRICT_VARIANTS_FIELD}
            )

    portable: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in ordered:
        row = _portable_strict_candidate(
            raw,
            broad_candidate,
            window,
            render_frames=render_frames,
            resolved_source_path=resolved_source_path,
        )
        identity = _strict_variant_identity(row)
        declared = str(row.get("strict_variant_key") or "")
        if declared and declared != identity:
            raise BroadLazyRuntimeError(
                "strict variant key disagrees with its exact source range"
            )
        row["strict_variant_key"] = identity
        if identity in seen:
            continue
        seen.add(identity)
        portable.append(row)
        if len(portable) > MAX_STRICT_VARIANTS_PER_TASK:
            raise BroadLazyRuntimeError(
                "strict callback exceeds the bounded exact-variant limit"
            )
    if not portable:
        raise BroadLazyRuntimeError("strict callback returned no exact variants")
    return portable


def _safe_rejection(exc: BaseException) -> dict[str, str]:
    raw = " ".join(str(exc).split())[:1000]
    reason_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    reason = raw or exc.__class__.__name__
    try:
        _assert_path_free(reason, "strict rejection")
    except BroadLazyRuntimeError:
        reason = "strict upgrade callback rejected candidate"
    if _EMBEDDED_HOST_PATH.search(reason):
        reason = "strict upgrade callback rejected candidate"
    return {
        "reason_code": exc.__class__.__name__,
        "reason": reason,
        "reason_sha256": reason_hash,
    }


def _strict_task_key(
    window: Mapping[str, Any], *, render_frames: int, callback_contract: str
) -> str:
    return "strict-runtime-v2-" + canonical_hash(
        {
            "planned_window_cache_key": str(window.get("cache_key") or ""),
            "render_frames": render_frames,
            "callback_contract": callback_contract,
        }
    )


def _load_strict_cache(path: Path, *, task_key: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BroadLazyRuntimeError(f"cannot load strict cache: {exc}") from exc
    row = _validate_sealed(raw, contract=STRICT_CACHE_CONTRACT)
    if row.get("task_key") != task_key or row.get("status") not in {
        "success",
        "rejected",
    }:
        raise BroadLazyRuntimeError("strict cache identity/status mismatch")
    return row


def _write_strict_cache(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, _sealed(document))


def _strict_result_document(
    *,
    status: str,
    catalog: Mapping[str, Any],
    embedding_contract: Mapping[str, Any],
    slot_ids: Sequence[str],
    survivors: Mapping[str, Sequence[Mapping[str, Any]]],
    survivor_parents: Mapping[str, set[str]],
    strict_survivors_per_slot: int,
    union: Mapping[str, Any],
    counters: Mapping[str, int],
) -> dict[str, Any]:
    arrays = [
        [_clone(dict(row), "strict candidate") for row in survivors[slot_id]]
        for slot_id in slot_ids
    ]
    base = {
        "schema_version": 1,
        "contract": STRICT_RESULT_CONTRACT,
        "status": status,
        "catalog_hash": str(catalog["content_hash"]),
        "embedding_hash": str(embedding_contract["content_hash"]),
        "ranked_union_hash": str(union["content_hash"]),
        "slot_ids": list(slot_ids),
        "strict_survivors_per_slot": strict_survivors_per_slot,
        "strict_candidates": arrays,
        "strict_candidate_counts": [len(rows) for rows in arrays],
        "strict_parent_counts": [len(survivor_parents[slot_id]) for slot_id in slot_ids],
        "provenance": {
            **{key: int(value) for key, value in sorted(counters.items())},
            "broad_catalog_candidate_count": int(catalog["candidate_count"]),
            "ranked_union_candidate_count": int(union["candidate_count"]),
            "manual_selection_count": 0,
            "capped_donor_count": 0,
        },
        "lineage_policy": {
            "asset_resolver_runtime_only": True,
            "manual_selection_allowed": False,
            "duration_caps_allowed": False,
            "fill_allowed": False,
            "retime_allowed": False,
            "clean_span_variants_preserved": True,
            "survivor_target_counts_unique_broad_parents": True,
        },
    }
    result = _sealed(base)
    _assert_path_free(result, "strict_result")
    return result


def run_lazy_strict_upgrade(
    catalog: Mapping[str, Any],
    embedding_contract: Mapping[str, Any],
    vectors: np.ndarray,
    briefs: Sequence[Mapping[str, Any]],
    *,
    slot_render_frames: Mapping[str, int],
    asset_resolver: Mapping[str, str | os.PathLike[str]],
    strict_upgrade_fn: Callable,
    cache_directory: str | os.PathLike[str],
    detector_policy: Mapping[str, Any],
    overlay_profiles: Mapping[str, Mapping[str, Any]] | None = None,
    strict_survivors_per_slot: int = 3,
    page_size: int = 20,
    max_workers: int = 4,
    search_seconds: float = 4.0,
    query_embedder: Callable = _embed_text,
    callback_contract: str = DEFAULT_STRICT_CALLBACK_CONTRACT,
    artifact_directory: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Page broad results until every slot has enough cached strict survivors.

    The dependency-injected callback receives ``(runtime_candidate, *, window,
    render_frames)``.  A mapping with ``status='rejected'`` or any exception is
    a durable candidate rejection; any other mapping must satisfy the current
    frame-exact no-fill candidate contract.
    """

    validated_catalog = validate_broad_catalog(catalog)
    validated_embedding, matrix = validate_embedding_contract(
        embedding_contract, validated_catalog, vectors
    )
    survivor_target = _positive_int(
        strict_survivors_per_slot, "strict_survivors_per_slot"
    )
    page_size = _positive_int(page_size, "page_size")
    workers = _positive_int(max_workers, "max_workers")
    callback_contract = _portable_id(callback_contract, "callback_contract")
    slot_ids, query_vectors = _query_vectors_for_briefs(briefs, query_embedder)
    frames = {
        _portable_id(slot_id, "slot_render_frames.slot_id"): _positive_int(
            value, "render_frames"
        )
        for slot_id, value in slot_render_frames.items()
    }
    if set(frames) != set(slot_ids):
        raise BroadLazyRuntimeError("slot_render_frames must exactly cover briefs")
    profiles = dict(overlay_profiles or {})
    for donor in validated_catalog["donors"]:
        asset_id = str(donor["source_asset_id"])
        if asset_id not in profiles:
            profiles[asset_id] = build_donor_overlay_profile(
                asset_id,
                str(donor["source_sha256"]),
                [],
                detector_policy=detector_policy,
            )
    runtime_candidates = {
        str(row["clip_id"]): row
        for row in prepare_runtime_candidates(
            validated_catalog["candidates"], asset_resolver
        )
    }
    survivors: dict[str, list[dict[str, Any]]] = {
        slot_id: [] for slot_id in slot_ids
    }
    seen_survivors: dict[str, set[str]] = {slot_id: set() for slot_id in slot_ids}
    survivor_parents: dict[str, set[str]] = {
        slot_id: set() for slot_id in slot_ids
    }
    rankings: list[dict[str, Any]] = []
    counters = {
        "paging_rounds": 0,
        "ranking_pages_scanned": 0,
        "ranked_rows_seen": 0,
        "strict_windows_planned": 0,
        "strict_unique_tasks": 0,
        "strict_callback_calls": 0,
        "strict_cache_hit_success": 0,
        "strict_cache_hit_rejected": 0,
        "strict_success_outcomes": 0,
        "strict_rejected_outcomes": 0,
        "strict_variant_candidates": 0,
    }
    root = Path(cache_directory)
    strict_cache_root = root / "strict_upgrade"
    total_pages = int(math.ceil(validated_catalog["candidate_count"] / page_size))

    for page_index in range(total_pages):
        active = [
            slot_id
            for slot_id in slot_ids
            if len(survivor_parents[slot_id]) < survivor_target
        ]
        if not active:
            break
        ranking = rank_broad_candidate_pages(
            validated_catalog,
            validated_embedding,
            matrix,
            {slot_id: query_vectors[slot_id] for slot_id in active},
            page_size=page_size,
            start_page=page_index,
            page_count=1,
        )
        rankings.append(ranking)
        counters["paging_rounds"] += 1
        counters["ranking_pages_scanned"] += len(active)
        counters["ranked_rows_seen"] += sum(
            len(page["candidates"])
            for slot in ranking["slots"]
            for page in slot["pages"]
        )
        if artifact_directory is not None:
            save_path_free_artifact(
                ranking, artifact_directory, kind="broad_ranking"
            )
        current_union = deduplicate_ranked_union(ranking)
        plan = plan_lazy_strict_windows(
            validated_catalog,
            current_union,
            validated_embedding,
            slot_render_frames=frames,
            detector_policy=detector_policy,
            overlay_profiles=profiles,
            search_seconds=search_seconds,
        )
        counters["strict_windows_planned"] += len(plan["windows"])
        union_by_clip = {
            str(row["clip_id"]): row for row in current_union["candidates"]
        }

        tasks: dict[str, dict[str, Any]] = {}
        assignments: dict[str, list[dict[str, Any]]] = {}
        for window in plan["windows"]:
            clip_id = str(window["clip_id"])
            union_candidate = union_by_clip[clip_id]
            broad_candidate = runtime_candidates[clip_id]
            request_by_slot = {
                str(row["slot_id"]): row
                for row in union_candidate.get("requests") or ()
            }
            for request in window["requested_slots"]:
                slot_id = str(request["slot_id"])
                render_frames = int(request["render_frames"])
                task_key = _strict_task_key(
                    window,
                    render_frames=render_frames,
                    callback_contract=callback_contract,
                )
                tasks.setdefault(
                    task_key,
                    {
                        "task_key": task_key,
                        "window": dict(window),
                        "broad_candidate": dict(broad_candidate),
                        "render_frames": render_frames,
                    },
                )
                ranking_request = dict(request_by_slot[slot_id])
                assignments.setdefault(task_key, []).append(
                    {"slot_id": slot_id, "ranking_request": ranking_request}
                )
        counters["strict_unique_tasks"] += len(tasks)

        def execute(task: Mapping[str, Any]) -> dict[str, Any]:
            task_key = str(task["task_key"])
            cache_path = strict_cache_root / f"strict.{task_key}.json"
            cached = _load_strict_cache(cache_path, task_key=task_key)
            if cached is not None:
                window = dict(task["window"])
                candidate = dict(task["broad_candidate"])
                render_frames = int(task["render_frames"])
                if (
                    cached.get("planned_window_cache_key") != window.get("cache_key")
                    or int(cached.get("render_frames") or 0) != render_frames
                    or cached.get("callback_contract") != callback_contract
                ):
                    raise BroadLazyRuntimeError(
                        "strict cache fields disagree with its task identity"
                    )
                if cached["status"] == "success":
                    raw_candidates = cached.get("candidates")
                    if raw_candidates is None:
                        raw_candidate = cached.get("candidate")
                        raw_candidates = (
                            [raw_candidate] if isinstance(raw_candidate, Mapping) else None
                        )
                    if not isinstance(raw_candidates, list) or not raw_candidates:
                        raise BroadLazyRuntimeError(
                            "strict success cache has no candidates"
                        )
                    if len(raw_candidates) > MAX_STRICT_VARIANTS_PER_TASK:
                        raise BroadLazyRuntimeError(
                            "strict success cache exceeds the bounded variant limit"
                        )
                    revalidated_rows = []
                    for raw_candidate in raw_candidates:
                        if not isinstance(raw_candidate, Mapping):
                            raise BroadLazyRuntimeError(
                                "strict success cache candidate is not an object"
                            )
                        revalidated = _portable_strict_candidate(
                            raw_candidate,
                            candidate,
                            window,
                            render_frames=render_frames,
                            resolved_source_path=str(candidate["source_path"]),
                        )
                        if str(revalidated.get("strict_variant_key") or "") != (
                            _strict_variant_identity(revalidated)
                        ):
                            raise BroadLazyRuntimeError(
                                "strict success cache variant identity mismatch"
                            )
                        revalidated_rows.append(revalidated)
                    if revalidated_rows != raw_candidates:
                        raise BroadLazyRuntimeError(
                            "strict success cache is not canonical"
                        )
                    if cached.get("candidate") != raw_candidates[0]:
                        raise BroadLazyRuntimeError(
                            "strict success cache primary candidate mismatch"
                        )
                return {**cached, "cache_hit": True}
            candidate = dict(task["broad_candidate"])
            window = dict(task["window"])
            render_frames = int(task["render_frames"])
            try:
                raw = strict_upgrade_fn(
                    candidate,
                    window=window,
                    render_frames=render_frames,
                )
                if not isinstance(raw, Mapping):
                    raise BroadLazyRuntimeError(
                        "strict upgrade callback must return an object"
                    )
                if str(raw.get("status") or "").casefold() in {
                    "rejected",
                    "failed",
                    "unresolved",
                }:
                    reason = str(raw.get("reason") or "strict callback rejected")
                    raise BroadLazyRuntimeError(reason)
                portable_rows = _portable_strict_variants(
                    raw,
                    candidate,
                    window,
                    render_frames=render_frames,
                    resolved_source_path=str(candidate["source_path"]),
                )
                cache = {
                    "schema_version": 1,
                    "contract": STRICT_CACHE_CONTRACT,
                    "task_key": task_key,
                    "planned_window_cache_key": str(window["cache_key"]),
                    "render_frames": render_frames,
                    "callback_contract": callback_contract,
                    "status": "success",
                    "candidate": portable_rows[0],
                    "candidates": portable_rows,
                }
            except Exception as exc:  # candidate-local strict rejection
                cache = {
                    "schema_version": 1,
                    "contract": STRICT_CACHE_CONTRACT,
                    "task_key": task_key,
                    "planned_window_cache_key": str(window["cache_key"]),
                    "render_frames": render_frames,
                    "callback_contract": callback_contract,
                    "status": "rejected",
                    **_safe_rejection(exc),
                }
            _write_strict_cache(cache_path, cache)
            return {**_validate_sealed(_sealed(cache)), "cache_hit": False}

        outcomes: dict[str, dict[str, Any]] = {}
        with cf.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_key = {
                executor.submit(execute, task): task_key
                for task_key, task in tasks.items()
            }
            for future in cf.as_completed(future_to_key):
                outcomes[future_to_key[future]] = future.result()

        for task_key in sorted(outcomes):
            outcome = outcomes[task_key]
            hit = bool(outcome.pop("cache_hit", False))
            status = str(outcome["status"])
            if hit:
                counters[f"strict_cache_hit_{status}"] += 1
            else:
                counters["strict_callback_calls"] += 1
            counters[f"strict_{status}_outcomes"] += 1
            if status != "success":
                continue
            cached_candidates = [
                dict(row) for row in outcome.get("candidates") or [outcome["candidate"]]
            ]
            counters["strict_variant_candidates"] += len(cached_candidates)
            for assignment in assignments[task_key]:
                slot_id = str(assignment["slot_id"])
                ranking_request = assignment["ranking_request"]
                for cached_candidate in cached_candidates:
                    clip_id = str(cached_candidate["clip_id"])
                    variant_id = _strict_variant_identity(cached_candidate)
                    if variant_id in seen_survivors[slot_id]:
                        continue
                    slot_candidate = {
                        **cached_candidate,
                        "slot_id": slot_id,
                        "broad_rank": int(ranking_request["broad_rank"]),
                        "retrieval_score": float(ranking_request["retrieval_score"]),
                        "ranking_page_index": int(ranking_request["page_index"]),
                        "strict_task_key": task_key,
                    }
                    survivors[slot_id].append(slot_candidate)
                    seen_survivors[slot_id].add(variant_id)
                    survivor_parents[slot_id].add(clip_id)

    if not rankings:
        raise BroadLazyRuntimeError("lazy strict upgrade produced no ranking pages")
    overall_union = deduplicate_ranked_union(rankings)
    for slot_id in slot_ids:
        survivors[slot_id].sort(
            key=lambda row: (
                int(row["broad_rank"]),
                -float(row["retrieval_score"]),
                str(row["clip_id"]),
                float(row["source_in"]),
                str(row.get("strict_variant_key") or ""),
            )
        )
        selected_parents: list[str] = []
        for row in survivors[slot_id]:
            parent = str(row["clip_id"])
            if parent not in selected_parents:
                selected_parents.append(parent)
            if len(selected_parents) >= survivor_target:
                break
        allowed = set(selected_parents)
        survivors[slot_id] = [
            row for row in survivors[slot_id] if str(row["clip_id"]) in allowed
        ]
        survivor_parents[slot_id] = allowed
    complete = all(
        len(survivor_parents[slot_id]) >= survivor_target for slot_id in slot_ids
    )
    status = "ready" if complete else "needs_clean_donor_ranges"
    result = _strict_result_document(
        status=status,
        catalog=validated_catalog,
        embedding_contract=validated_embedding,
        slot_ids=slot_ids,
        survivors=survivors,
        survivor_parents=survivor_parents,
        strict_survivors_per_slot=survivor_target,
        union=overall_union,
        counters=counters,
    )
    if artifact_directory is not None:
        save_path_free_artifact(overall_union, artifact_directory, kind="broad_union")
        save_path_free_artifact(result, artifact_directory, kind="strict_candidates")
    if not complete:
        missing = [
            slot_id
            for slot_id in slot_ids
            if len(survivor_parents[slot_id]) < survivor_target
        ]
        raise BroadLazyRuntimeBlocked(
            "needs_clean_donor_ranges",
            f"broad paging exhausted without enough strict ranges for {missing}",
            result,
        )
    return result


__all__ = [
    "PATH_FREE_ARTIFACT_CONTRACT",
    "EMBEDDING_CACHE_CONTRACT",
    "STRICT_CACHE_CONTRACT",
    "STRICT_RESULT_CONTRACT",
    "FOUR_FRAME_EMBEDDING_METHOD",
    "DEFAULT_STRICT_CALLBACK_CONTRACT",
    "BroadLazyRuntimeError",
    "BroadLazyRuntimeBlocked",
    "save_path_free_artifact",
    "load_path_free_artifact",
    "build_complete_broad_catalog",
    "prepare_runtime_candidates",
    "materialize_broad_embeddings",
    "rank_broad_pages_for_briefs",
    "deduplicate_top_union",
    "run_lazy_strict_upgrade",
]
