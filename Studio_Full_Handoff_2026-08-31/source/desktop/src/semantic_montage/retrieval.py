"""Pareto cross-modal retrieval and contact-sheet materialization."""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import math
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .clip_model import embed_images, embed_text


class SelectionOptimizationError(ValueError):
    """Raised when bounded global selection cannot return a valid assignment.

    ``code`` and ``diagnostic`` let production distinguish a proven collision
    contradiction from ordinary beam exhaustion without breaking callers which
    already catch this class (or its :class:`ValueError` base).
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "selection_optimization_error",
        diagnostic: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.reason = self.code
        self.diagnostic = dict(diagnostic) if diagnostic is not None else None


def _clip_id(clip: dict) -> str:
    return str(clip.get("clip_id") or f"{clip.get('source_id')}:{float(clip['source_in']):.3f}-{float(clip['source_out']):.3f}")


def extract_thumbnail(clip: dict, path: str | os.PathLike[str], ffmpeg: str = "ffmpeg") -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 1024:
        return str(target)
    middle = (float(clip["source_in"]) + float(clip["source_out"])) / 2.0
    temp = target.with_name(f".{target.stem}.{os.getpid()}.tmp.jpg")
    process = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{middle:.3f}",
         "-i", str(clip["source_path"]), "-frames:v", "1", "-an",
         "-vf", "scale=384:-2", "-q:v", "3", "-y", str(temp)],
        capture_output=True,
        timeout=90,
    )
    if process.returncode or not temp.is_file():
        raise RuntimeError((process.stderr or b"thumbnail extraction failed")[-1000:].decode("utf-8", "replace"))
    os.replace(temp, target)
    return str(target)


def materialize_thumbnails(
    clips: list[dict], output_dir: str | os.PathLike[str], *, workers: int = 8
) -> list[dict]:
    root = Path(output_dir)
    rows: list[dict | None] = [None] * len(clips)

    def work(index: int, clip: dict):
        target = root / f"{_clip_id(clip).replace(':', '_').replace('/', '_')}.jpg"
        value = dict(clip)
        value["thumbnail"] = extract_thumbnail(clip, target)
        value["clip_id"] = _clip_id(clip)
        return index, value

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(work, index, clip) for index, clip in enumerate(clips)]
        for future in cf.as_completed(futures):
            index, value = future.result()
            rows[index] = value
    return [row for row in rows if row is not None]


def embed_clip_rows(clips: list[dict], *, batch_size: int = 192) -> np.ndarray:
    vectors: list[np.ndarray] = []
    for start in range(0, len(clips), max(1, batch_size)):
        batch = clips[start:start + batch_size]
        paths = [str(row["thumbnail"]) for row in batch]
        embeddings = embed_images(paths)
        for path in paths:
            if path not in embeddings:
                raise ValueError(f"CLIP could not embed {path}")
            vectors.append(embeddings[path])
    if not vectors:
        return np.empty((0, 0), dtype=np.float32)
    return np.stack(vectors).astype(np.float32)


def embed_contact_sheet_rows(clips: list[dict], *, batch_size: int = 64) -> np.ndarray:
    """Embed all four temporal cells of each 2x2 contact sheet.

    The legacy index embeds only the midpoint.  Production retrieval averages
    normalized CLIP vectors from 10/35/65/90% so a good midpoint cannot hide bad
    heads or tails.  Contact sheets are split locally; no additional decode is needed.
    """

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency preflight
        raise RuntimeError("Pillow is required for temporal CLIP indexing") from exc
    import tempfile

    if not clips:
        return np.empty((0, 0), dtype=np.float32)
    vectors: list[np.ndarray] = []
    with tempfile.TemporaryDirectory(prefix="semantic-temporal-embed-") as temp:
        root = Path(temp)
        for start in range(0, len(clips), max(1, batch_size)):
            batch = clips[start:start + batch_size]
            cell_paths: list[str] = []
            rows: list[list[str]] = []
            for row_index, row in enumerate(batch):
                sheet_path = str(row.get("contact_sheet") or "")
                if not sheet_path:
                    raise ValueError("contact_sheet is required for temporal embedding")
                with Image.open(sheet_path) as image:
                    image = image.convert("RGB")
                    width, height = image.size
                    if width < 4 or height < 4:
                        raise ValueError(f"invalid contact sheet: {sheet_path}")
                    x = width // 2
                    y = height // 2
                    boxes = ((0, 0, x, y), (x, 0, width, y),
                             (0, y, x, height), (x, y, width, height))
                    current: list[str] = []
                    for cell_index, box in enumerate(boxes):
                        target = root / f"{start + row_index:08d}_{cell_index}.jpg"
                        image.crop(box).save(target, format="JPEG", quality=92)
                        current.append(str(target))
                        cell_paths.append(str(target))
                    rows.append(current)
            embedded = embed_images(cell_paths)
            for current in rows:
                cell_vectors = []
                for path in current:
                    if path not in embedded:
                        raise ValueError(f"CLIP could not embed temporal cell {path}")
                    vector = np.asarray(embedded[path], dtype=np.float32)
                    norm = float(np.linalg.norm(vector))
                    if not np.isfinite(norm) or norm <= 0:
                        raise ValueError(f"invalid temporal embedding for {path}")
                    cell_vectors.append(vector / norm)
                pooled = np.mean(np.stack(cell_vectors), axis=0)
                pooled_norm = float(np.linalg.norm(pooled))
                if pooled_norm <= 0:
                    raise ValueError("temporal embedding collapsed to zero")
                vectors.append((pooled / pooled_norm).astype(np.float32))
    return np.stack(vectors).astype(np.float32)


def save_index(path: str | os.PathLike[str], clips: list[dict], vectors: np.ndarray) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp.npz")
    np.savez_compressed(temp, vectors=vectors, clips=json.dumps(clips, ensure_ascii=False))
    os.replace(temp, target)


def load_index(path: str | os.PathLike[str]) -> tuple[list[dict], np.ndarray]:
    with np.load(path, allow_pickle=False) as data:
        clips = json.loads(str(data["clips"]))
        vectors = data["vectors"].astype(np.float32)
    return clips, vectors


def rank_for_query(query: str, clips: list[dict], vectors: np.ndarray, *, top_k: int = 20) -> list[dict]:
    if len(clips) != len(vectors):
        raise ValueError("clip/vector count mismatch")
    text_vector = embed_text(query)
    scores = vectors @ text_vector
    order = np.argsort(-scores)[:max(1, top_k)]
    return [{**clips[int(index)], "retrieval_score": float(scores[int(index)])} for index in order]


def ranges_overlap(a: dict, b: dict, tolerance: float = 0.08) -> bool:
    if str(a.get("source_id")) != str(b.get("source_id")):
        return False
    return min(float(a["source_out"]), float(b["source_out"])) - max(float(a["source_in"]), float(b["source_in"])) > tolerance


def choose_non_overlapping(
    ranked_per_slot: list[list[dict]], *,
    same_donor_penalty: float = 0.025,
    continuity_bonus: float = 0.035,
) -> list[list[dict]]:
    """Reorder each shortlist using already selected ranges and soft continuity.

    The first element of every returned shortlist is the global greedy pick; remaining
    entries stay available for the temporal judge.
    """
    used: list[dict] = []
    previous: dict | None = None
    output: list[list[dict]] = []
    for candidates in ranked_per_slot:
        scored = []
        for candidate in candidates:
            if any(ranges_overlap(candidate, old) for old in used):
                continue
            score = float(candidate.get("retrieval_score") or 0.0)
            if previous and candidate.get("source_id") == previous.get("source_id"):
                score -= same_donor_penalty
                delta = float(candidate["source_in"]) - float(previous["source_out"])
                if -0.2 <= delta <= 120.0:
                    score += continuity_bonus * math.exp(-max(0.0, delta) / 120.0)
            scored.append(({**candidate, "selection_score": score}, score))
        ordered = [row for row, _ in sorted(scored, key=lambda pair: -pair[1])]
        if ordered:
            used.append(ordered[0])
            previous = ordered[0]
        output.append(ordered)
    return output


def _candidate_global_score(candidate: dict) -> float:
    score = float(candidate.get("retrieval_score") or 0.0)
    score += 0.20 * float(candidate.get("quality_score") or 0.0)
    score += 0.16 * float(candidate.get("judge_confidence") or 0.0)
    score += 0.08 * float(candidate.get("temporal_consistency") or 0.0)
    score -= 0.20 * float(candidate.get("overlay_risk") or 0.0)
    return score


_COLLISION_DIAGNOSTIC_VERSION = "global-collision-feasibility-v1"


class _CollisionDiagnosticNodeLimit(RuntimeError):
    pass


class _CollisionDiagnosticBudget:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.used = 0

    def step(self) -> None:
        if self.used >= self.maximum:
            raise _CollisionDiagnosticNodeLimit
        self.used += 1


def _canonical_diagnostic_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _finish_collision_diagnostic_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    output = dict(receipt)
    output["receipt_sha256"] = _canonical_diagnostic_hash(output)
    return output


def _positive_diagnostic_limit(value: int, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if normalized < 1 or normalized != value:
        raise ValueError(f"{name} must be a positive integer")
    return normalized


def _collision_candidate_descriptor(
    row: Mapping[str, Any], *, slot_index: int, candidate_index: int
) -> tuple[dict[str, Any], str]:
    if "source_id" not in row or "source_in" not in row or "source_out" not in row:
        raise ValueError(
            "collision diagnostic candidate "
            f"{slot_index}:{candidate_index} lacks source_id/source_in/source_out"
        )
    try:
        source_in = float(row["source_in"])
        source_out = float(row["source_out"])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"collision diagnostic candidate {slot_index}:{candidate_index} has invalid range"
        ) from exc
    if not math.isfinite(source_in) or not math.isfinite(source_out) or source_out <= source_in:
        raise ValueError(
            f"collision diagnostic candidate {slot_index}:{candidate_index} has invalid range"
        )
    # Avoid two JSON spellings for the same zero while otherwise preserving the
    # exact floats used by ranges_overlap.
    if source_in == 0.0:
        source_in = 0.0
    if source_out == 0.0:
        source_out = 0.0
    descriptor = {
        "source_id": str(row["source_id"]),
        "source_in": source_in,
        "source_out": source_out,
    }
    fingerprint = _canonical_diagnostic_hash(
        {
            "schema_version": 1,
            "collision_descriptor": descriptor,
        }
    )
    return descriptor, fingerprint


def _normalized_collision_slot_ids(
    slot_count: int, slot_ids: Sequence[str] | None
) -> list[str]:
    if slot_ids is None:
        return [str(index) for index in range(slot_count)]
    normalized = [str(value) for value in slot_ids]
    if len(normalized) != slot_count:
        raise ValueError("slot_ids length must equal eligible_per_slot length")
    if any(not value for value in normalized):
        raise ValueError("slot_ids must be non-empty")
    if len(set(normalized)) != len(normalized):
        raise ValueError("slot_ids must be unique")
    return normalized


def _collision_ordered_input_hash(
    slot_ids: Sequence[str], fingerprint_rows: Sequence[Sequence[str]]
) -> str:
    return _canonical_diagnostic_hash(
        {
            "slot_ids": list(slot_ids),
            "ordered_candidate_fingerprint_sha256_by_slot": [
                list(row) for row in fingerprint_rows
            ],
        }
    )


def validate_global_collision_feasibility_witness(
    eligible_per_slot: Sequence[Sequence[Mapping[str, Any]]],
    diagnostic: Mapping[str, Any],
    *,
    slot_ids: Sequence[str] | None = None,
) -> list[int]:
    """Validate and resolve an exact feasible diagnostic witness.

    The returned integers index the caller's *same ordered eligible matrix*.
    Validation is independent of the search: it checks the receipt checksum,
    canonical and order-sensitive matrix bindings, the fingerprint at every
    selected index, and all pairwise collision constraints.  A stale, reordered,
    or tampered matrix/receipt raises :class:`ValueError` instead of selecting a
    potentially different candidate.
    """

    if not isinstance(diagnostic, Mapping):
        raise ValueError("collision diagnostic must be a mapping")
    unsigned = dict(diagnostic)
    receipt_sha256 = unsigned.pop("receipt_sha256", None)
    if not isinstance(receipt_sha256, str) or len(receipt_sha256) != 64:
        raise ValueError("collision diagnostic receipt_sha256 is missing or invalid")
    try:
        expected_receipt_sha256 = _canonical_diagnostic_hash(unsigned)
    except (TypeError, ValueError) as exc:
        raise ValueError("collision diagnostic receipt is not canonical JSON") from exc
    if receipt_sha256 != expected_receipt_sha256:
        raise ValueError("collision diagnostic receipt_sha256 mismatch")
    if type(diagnostic.get("schema_version")) is not int or diagnostic.get(
        "schema_version"
    ) != 1:
        raise ValueError("unsupported collision diagnostic schema_version")
    if diagnostic.get("diagnostic_version") != _COLLISION_DIAGNOSTIC_VERSION:
        raise ValueError("unsupported collision diagnostic version")
    if diagnostic.get("status") != "feasible":
        raise ValueError("collision diagnostic does not contain a feasible witness")

    try:
        raw_slots = [list(rows) for rows in eligible_per_slot]
    except TypeError as exc:
        raise ValueError(
            "eligible_per_slot must be a sequence of candidate sequences"
        ) from exc
    normalized_slot_ids = _normalized_collision_slot_ids(len(raw_slots), slot_ids)
    if diagnostic.get("slot_ids") != normalized_slot_ids:
        raise ValueError("collision diagnostic slot_ids do not match eligible matrix")

    ordered_descriptors: list[list[dict[str, Any]]] = []
    ordered_fingerprint_rows: list[list[str]] = []
    canonical_fingerprint_rows: list[list[str]] = []
    for slot_index, raw_candidates in enumerate(raw_slots):
        descriptor_row: list[dict[str, Any]] = []
        fingerprint_row: list[str] = []
        for candidate_index, raw in enumerate(raw_candidates):
            if not isinstance(raw, Mapping):
                raise ValueError(
                    "collision diagnostic candidate "
                    f"{slot_index}:{candidate_index} is not a mapping"
                )
            descriptor, fingerprint = _collision_candidate_descriptor(
                raw,
                slot_index=slot_index,
                candidate_index=candidate_index,
            )
            descriptor_row.append(descriptor)
            fingerprint_row.append(fingerprint)
        ordered_descriptors.append(descriptor_row)
        ordered_fingerprint_rows.append(fingerprint_row)
        canonical_fingerprint_rows.append(sorted(set(fingerprint_row)))

    candidate_counts = [len(rows) for rows in raw_slots]
    if type(diagnostic.get("slot_count")) is not int or diagnostic.get(
        "slot_count"
    ) != len(raw_slots):
        raise ValueError("collision diagnostic slot_count does not match eligible matrix")
    if (
        not isinstance(diagnostic.get("candidate_counts"), list)
        or any(type(value) is not int for value in diagnostic["candidate_counts"])
        or diagnostic["candidate_counts"] != candidate_counts
    ):
        raise ValueError("collision diagnostic candidate_counts do not match eligible matrix")
    if type(diagnostic.get("candidate_count")) is not int or diagnostic.get(
        "candidate_count"
    ) != sum(len(row) for row in canonical_fingerprint_rows):
        raise ValueError("collision diagnostic candidate_count does not match eligible matrix")
    if (
        diagnostic.get("candidate_fingerprint_sha256_by_slot")
        != canonical_fingerprint_rows
    ):
        raise ValueError("collision diagnostic candidate fingerprints do not match eligible matrix")
    canonical_input_sha256 = _canonical_diagnostic_hash(
        {
            "slot_ids": normalized_slot_ids,
            "candidate_fingerprint_sha256_by_slot": canonical_fingerprint_rows,
        }
    )
    if diagnostic.get("input_sha256") != canonical_input_sha256:
        raise ValueError("collision diagnostic input_sha256 does not match eligible matrix")
    ordered_input_sha256 = _collision_ordered_input_hash(
        normalized_slot_ids, ordered_fingerprint_rows
    )
    if diagnostic.get("feasible_witness_input_sha256") != ordered_input_sha256:
        raise ValueError(
            "collision diagnostic feasible witness does not match ordered eligible matrix"
        )

    witness_indices = diagnostic.get("feasible_witness_candidate_indices")
    witness_fingerprints = diagnostic.get("feasible_witness_candidate_fingerprints")
    if not isinstance(witness_indices, list) or len(witness_indices) != len(raw_slots):
        raise ValueError("collision diagnostic witness indices have invalid length")
    if not isinstance(witness_fingerprints, list) or len(witness_fingerprints) != len(
        raw_slots
    ):
        raise ValueError("collision diagnostic witness fingerprints have invalid length")

    selected_descriptors: list[dict[str, Any]] = []
    validated_indices: list[int] = []
    for slot_index, candidate_index in enumerate(witness_indices):
        if isinstance(candidate_index, bool) or not isinstance(candidate_index, int):
            raise ValueError("collision diagnostic witness index must be an integer")
        if not 0 <= candidate_index < len(raw_slots[slot_index]):
            raise ValueError("collision diagnostic witness index is out of bounds")
        actual_fingerprint = ordered_fingerprint_rows[slot_index][candidate_index]
        if witness_fingerprints[slot_index] != actual_fingerprint:
            raise ValueError(
                "collision diagnostic witness fingerprint does not match candidate index"
            )
        selected_descriptors.append(ordered_descriptors[slot_index][candidate_index])
        validated_indices.append(candidate_index)

    for later_index, later in enumerate(selected_descriptors):
        for earlier in selected_descriptors[:later_index]:
            if ranges_overlap(later, earlier):
                raise ValueError("collision diagnostic witness contains overlapping ranges")
    return validated_indices


def diagnose_global_collision_feasibility(
    eligible_per_slot: Sequence[Sequence[Mapping[str, Any]]],
    *,
    slot_ids: Sequence[str] | None = None,
    max_slots: int = 128,
    max_candidates_per_slot: int = 64,
    max_nodes: int = 1_000_000,
) -> dict[str, Any]:
    """Prove collision feasibility independently of heuristic beam pruning.

    The input must already contain only eligible, frame-exact candidates.  This
    routine deliberately ignores scores and every path-bearing/media field: the
    portable receipt binds only logical slot IDs plus SHA-256 fingerprints of
    the exact ``source_id/source_in/source_out`` collision descriptors.

    ``true_infeasible`` is emitted only after exhaustive bounded search.  The
    earliest failing prefix is minimized to a deterministic irreducible slot
    core.  ``single_slot_augmentation_indices`` contains core slots for which a
    new globally unique range would make the *full* instance feasible (the test
    is equivalent to removing that one slot).  If any configured bound prevents
    a proof, the status is ``limit_exceeded`` and callers must fail closed.
    """

    slot_limit = _positive_diagnostic_limit(max_slots, "max_slots")
    candidate_limit = _positive_diagnostic_limit(
        max_candidates_per_slot, "max_candidates_per_slot"
    )
    node_limit = _positive_diagnostic_limit(max_nodes, "max_nodes")

    try:
        raw_slots = [list(rows) for rows in eligible_per_slot]
    except TypeError as exc:
        raise ValueError("eligible_per_slot must be a sequence of candidate sequences") from exc
    slot_count = len(raw_slots)
    normalized_slot_ids = _normalized_collision_slot_ids(slot_count, slot_ids)

    candidate_counts = [len(rows) for rows in raw_slots]
    limits = {
        "max_slots": slot_limit,
        "max_candidates_per_slot": candidate_limit,
        "max_nodes": node_limit,
    }
    preflight_binding = {
        "slot_ids": normalized_slot_ids,
        "candidate_counts": candidate_counts,
    }
    common: dict[str, Any] = {
        "schema_version": 1,
        "diagnostic_version": _COLLISION_DIAGNOSTIC_VERSION,
        "slot_count": slot_count,
        "candidate_count": sum(candidate_counts),
        "limits": limits,
        "slot_ids": normalized_slot_ids,
        "candidate_counts": candidate_counts,
        "input_sha256": _canonical_diagnostic_hash(preflight_binding),
        "nodes_explored": 0,
        "earliest_frontier_index": None,
        "irreducible_core_indices": [],
        "irreducible_core_slot_ids": [],
        "single_slot_augmentation_indices": [],
        "single_slot_augmentation_slot_ids": [],
        "candidate_fingerprint_sha256_by_slot": [],
        "feasible_witness_candidate_fingerprints": [],
        "feasible_witness_candidate_indices": [],
        "feasible_witness_input_sha256": None,
    }
    if slot_count > slot_limit:
        return _finish_collision_diagnostic_receipt(
            {
                **common,
                "status": "limit_exceeded",
                "limit_phase": "preflight",
                "limit_kind": "max_slots",
            }
        )
    oversized_slot = next(
        (index for index, count in enumerate(candidate_counts) if count > candidate_limit),
        None,
    )
    if oversized_slot is not None:
        return _finish_collision_diagnostic_receipt(
            {
                **common,
                "status": "limit_exceeded",
                "limit_phase": "preflight",
                "limit_kind": "max_candidates_per_slot",
                "limit_slot_index": oversized_slot,
            }
        )

    normalized_slots: list[list[dict[str, Any]]] = []
    fingerprint_rows: list[list[str]] = []
    ordered_fingerprint_rows: list[list[str]] = []
    for slot_index, raw_candidates in enumerate(raw_slots):
        unique: dict[str, dict[str, Any]] = {}
        ordered_fingerprints: list[str] = []
        for candidate_index, raw in enumerate(raw_candidates):
            if not isinstance(raw, Mapping):
                raise ValueError(
                    f"collision diagnostic candidate {slot_index}:{candidate_index} is not a mapping"
                )
            descriptor, fingerprint = _collision_candidate_descriptor(
                raw,
                slot_index=slot_index,
                candidate_index=candidate_index,
            )
            ordered_fingerprints.append(fingerprint)
            unique.setdefault(
                fingerprint,
                {
                    **descriptor,
                    "fingerprint_sha256": fingerprint,
                    "original_candidate_index": candidate_index,
                },
            )
        ordered = [unique[key] for key in sorted(unique)]
        normalized_slots.append(ordered)
        fingerprint_rows.append([row["fingerprint_sha256"] for row in ordered])
        ordered_fingerprint_rows.append(ordered_fingerprints)

    input_binding = {
        "slot_ids": normalized_slot_ids,
        "candidate_fingerprint_sha256_by_slot": fingerprint_rows,
    }
    common.update(
        {
            "candidate_count": sum(len(rows) for rows in normalized_slots),
            "input_sha256": _canonical_diagnostic_hash(input_binding),
            "candidate_fingerprint_sha256_by_slot": fingerprint_rows,
        }
    )
    budget = _CollisionDiagnosticBudget(node_limit)
    overlap_cache: dict[tuple[int, int, int, int], bool] = {}

    def overlaps(slot_a: int, candidate_a: int, slot_b: int, candidate_b: int) -> bool:
        if slot_a < slot_b:
            key = (slot_a, candidate_a, slot_b, candidate_b)
        else:
            key = (slot_b, candidate_b, slot_a, candidate_a)
        cached = overlap_cache.get(key)
        if cached is not None:
            return cached
        value = ranges_overlap(
            normalized_slots[slot_a][candidate_a],
            normalized_slots[slot_b][candidate_b],
        )
        overlap_cache[key] = value
        return value

    def solve(active_slot_indices: Sequence[int]) -> dict[int, int] | None:
        domains: dict[int, tuple[int, ...]] = {
            index: tuple(range(len(normalized_slots[index])))
            for index in active_slot_indices
        }
        failed_states: set[tuple[tuple[int, tuple[int, ...]], ...]] = set()

        def search(
            remaining: dict[int, tuple[int, ...]], assignment: dict[int, int]
        ) -> dict[int, int] | None:
            budget.step()
            if not remaining:
                return dict(assignment)
            signature = tuple(sorted(remaining.items()))
            if signature in failed_states:
                return None
            slot_index = min(remaining, key=lambda index: (len(remaining[index]), index))
            choices = remaining[slot_index]
            if not choices:
                failed_states.add(signature)
                return None
            other_domains = {
                index: domain for index, domain in remaining.items() if index != slot_index
            }
            for candidate_index in choices:
                budget.step()
                reduced: dict[int, tuple[int, ...]] = {}
                viable = True
                for other_index in sorted(other_domains):
                    compatible = tuple(
                        other_candidate
                        for other_candidate in other_domains[other_index]
                        if not overlaps(
                            slot_index,
                            candidate_index,
                            other_index,
                            other_candidate,
                        )
                    )
                    if not compatible:
                        viable = False
                        break
                    reduced[other_index] = compatible
                if not viable:
                    continue
                assignment[slot_index] = candidate_index
                witness = search(reduced, assignment)
                assignment.pop(slot_index, None)
                if witness is not None:
                    return witness
            failed_states.add(signature)
            return None

        return search(domains, {})

    phase = "full_feasibility"
    proven_frontier: int | None = None
    completed_core: list[int] = []
    try:
        full_witness = solve(tuple(range(slot_count)))
        if full_witness is not None:
            witness_fingerprints = [
                normalized_slots[index][full_witness[index]]["fingerprint_sha256"]
                for index in range(slot_count)
            ]
            witness_candidate_indices = [
                normalized_slots[index][full_witness[index]]["original_candidate_index"]
                for index in range(slot_count)
            ]
            receipt = _finish_collision_diagnostic_receipt(
                {
                    **common,
                    "status": "feasible",
                    "nodes_explored": budget.used,
                    "feasible_witness_candidate_fingerprints": witness_fingerprints,
                    "feasible_witness_candidate_indices": witness_candidate_indices,
                    "feasible_witness_input_sha256": _collision_ordered_input_hash(
                        normalized_slot_ids, ordered_fingerprint_rows
                    ),
                }
            )
            validate_global_collision_feasibility_witness(
                raw_slots,
                receipt,
                slot_ids=normalized_slot_ids,
            )
            return receipt

        # Infeasibility of prefixes is monotone, so binary search returns the
        # deterministic earliest inclusive frontier with far fewer repeated
        # exact solves than a linear prefix scan.
        phase = "earliest_frontier"
        low = 0
        high = slot_count - 1
        while low < high:
            middle = (low + high) // 2
            if solve(tuple(range(middle + 1))) is None:
                high = middle
            else:
                proven_frontier = middle
                low = middle + 1
        earliest_frontier = low

        phase = "irreducible_core"
        core = list(range(earliest_frontier + 1))
        for slot_index in tuple(core):
            trial = [index for index in core if index != slot_index]
            if solve(trial) is None:
                core = trial
        completed_core = list(core)

        phase = "single_slot_augmentation"
        augmentation_indices = []
        all_indices = tuple(range(slot_count))
        for slot_index in core:
            without_target = tuple(index for index in all_indices if index != slot_index)
            if solve(without_target) is not None:
                augmentation_indices.append(slot_index)

        return _finish_collision_diagnostic_receipt(
            {
                **common,
                "status": "true_infeasible",
                "nodes_explored": budget.used,
                "earliest_frontier_index": earliest_frontier,
                "last_proven_feasible_frontier_index": (
                    earliest_frontier - 1 if earliest_frontier > 0 else None
                ),
                "irreducible_core_indices": core,
                "irreducible_core_slot_ids": [normalized_slot_ids[index] for index in core],
                "single_slot_augmentation_indices": augmentation_indices,
                "single_slot_augmentation_slot_ids": [
                    normalized_slot_ids[index] for index in augmentation_indices
                ],
            }
        )
    except _CollisionDiagnosticNodeLimit:
        return _finish_collision_diagnostic_receipt(
            {
                **common,
                "status": "limit_exceeded",
                "nodes_explored": budget.used,
                "limit_phase": phase,
                "limit_kind": "max_nodes",
                "last_proven_feasible_frontier_index": proven_frontier,
                "irreducible_core_indices": completed_core,
                "irreducible_core_slot_ids": [
                    normalized_slot_ids[index] for index in completed_core
                ],
            }
        )


def global_constrained_select(
    ranked_per_slot: list[list[dict]],
    *,
    beam_width: int = 256,
    candidates_per_slot: int = 20,
    same_donor_penalty: float = 0.025,
    continuity_bonus: float = 0.035,
    require_judged: bool = True,
    accepted_verdicts: frozenset[str] = frozenset({"exact", "relevant"}),
    slot_ids: Sequence[str] | None = None,
    diagnostic_max_slots: int = 128,
    diagnostic_max_candidates_per_slot: int = 64,
    diagnostic_max_nodes: int = 1_000_000,
) -> list[dict]:
    """Choose the best collision-free assignment across the whole timeline.

    Unlike the legacy greedy function, this never inserts a duplicate or
    unjudged candidate when a slot becomes difficult.  Absence of a complete
    assignment is terminal and must enter the bounded repair loop.
    """

    if beam_width < 1 or candidates_per_slot < 1:
        raise ValueError("beam_width and candidates_per_slot must be positive")
    eligible_per_slot: list[list[dict]] = []
    for raw_candidates in ranked_per_slot:
        eligible: list[dict] = []
        for row in raw_candidates[:candidates_per_slot]:
            candidate = dict(row)
            if candidate.get("overlay_blocked") or candidate.get("unresolved_overlay"):
                continue
            if require_judged:
                verdict = str(candidate.get("judge_verdict") or "").lower()
                if verdict not in accepted_verdicts:
                    continue
                if float(candidate.get("judge_confidence") or 0.0) <= 0.0:
                    continue
            eligible.append(candidate)
        eligible_per_slot.append(eligible)

    def raise_exhausted(slot_index: int) -> None:
        diagnostic = diagnose_global_collision_feasibility(
            eligible_per_slot,
            slot_ids=slot_ids,
            max_slots=diagnostic_max_slots,
            max_candidates_per_slot=diagnostic_max_candidates_per_slot,
            max_nodes=diagnostic_max_nodes,
        )
        status = str(diagnostic["status"])
        if status == "true_infeasible":
            frontier = diagnostic.get("earliest_frontier_index")
            core = diagnostic.get("irreducible_core_indices") or []
            raise SelectionOptimizationError(
                "no global collision-free assignment; "
                f"true infeasibility begins at slot {frontier}, core slots={core}",
                code="collision_infeasible",
                diagnostic=diagnostic,
            )
        if status == "feasible":
            raise SelectionOptimizationError(
                "global collision beam exhausted through slot "
                f"{slot_index}, but exact diagnostic found a feasible assignment",
                code="beam_exhausted",
                diagnostic=diagnostic,
            )
        raise SelectionOptimizationError(
            "global collision beam exhausted through slot "
            f"{slot_index}; exact diagnostic limit was exceeded",
            code="collision_diagnostic_limit_exceeded",
            diagnostic=diagnostic,
        )

    # (score, picks)
    beam: list[tuple[float, tuple[dict, ...]]] = [(0.0, ())]
    for slot_index, candidates in enumerate(eligible_per_slot):
        if not candidates:
            raise_exhausted(slot_index)
        expanded: list[tuple[float, tuple[dict, ...]]] = []
        for base_score, picks in beam:
            previous = picks[-1] if picks else None
            for candidate in candidates:
                if any(ranges_overlap(candidate, old) for old in picks):
                    continue
                score = base_score + _candidate_global_score(candidate)
                if previous and candidate.get("source_id") == previous.get("source_id"):
                    score -= same_donor_penalty
                    delta = float(candidate["source_in"]) - float(previous["source_out"])
                    if -0.2 <= delta <= 120.0:
                        score += continuity_bonus * math.exp(-max(0.0, delta) / 120.0)
                selected = dict(candidate)
                selected["global_candidate_score"] = score - base_score
                expanded.append((score, picks + (selected,)))
        if not expanded:
            raise_exhausted(slot_index)
        expanded.sort(key=lambda item: (-item[0], tuple(_clip_id(row) for row in item[1])))
        beam = expanded[:beam_width]
    if not beam:
        raise SelectionOptimizationError("no global assignment")
    return list(beam[0][1])


def make_contact_sheet(clip: dict, path: str | os.PathLike[str], ffmpeg: str = "ffmpeg") -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 2048:
        return str(target)
    start = float(clip["source_in"])
    duration = float(clip["source_out"]) - start
    if duration <= 0:
        raise ValueError("clip duration must be positive")
    # One FFmpeg decode, four temporal samples. The 10/35/65/90 positions avoid
    # transition frames at the exact ends and expose motion better than a single
    # thumbnail to the final vision judge.
    offsets = [duration * fraction for fraction in (0.10, 0.35, 0.65, 0.90)]
    source_filters = ["setpts=PTS-STARTPTS"]
    overlay = clip.get("overlay_analysis")
    if isinstance(overlay, dict) and overlay.get("action") in {
        "crop",
        "crop_and_exclude",
        "crop_and_inpaint",
    }:
        if not bool(overlay.get("crop_safe")):
            raise ValueError("overlay crop is required but is not marked crop_safe")
        margins = overlay.get("crop_margins") or {}
        left = float(margins.get("left") or 0.0)
        right = float(margins.get("right") or 0.0)
        top = float(margins.get("top") or 0.0)
        bottom = float(margins.get("bottom") or 0.0)
        if any(not 0.0 <= value < 0.5 for value in (left, right, top, bottom)):
            raise ValueError("overlay crop margins must be in [0, 0.5)")
        if left + right >= 0.5 or top + bottom >= 0.5:
            raise ValueError("overlay crop removes too much of the frame")
        source_filters.append(
            "crop="
            f"trunc(iw*{1.0-left-right:.9f}/2)*2:"
            f"trunc(ih*{1.0-top-bottom:.9f}/2)*2:"
            f"trunc(iw*{left:.9f}/2)*2:"
            f"trunc(ih*{top:.9f}/2)*2"
        )
    branches = [f"[0:v]{','.join(source_filters)},split=4[v0][v1][v2][v3]"]
    for index, offset in enumerate(offsets):
        branches.append(
            f"[v{index}]trim=start={offset:.6f}:duration=0.200000,"
            "setpts=PTS-STARTPTS,"
            "scale=480:270:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad=480:270:(ow-iw)/2:(oh-ih)/2[s{index}]"
        )
    branches.append(
        "[s0][s1][s2][s3]xstack=inputs=4:"
        "layout=0_0|w0_0|0_h0|w0_h0:fill=black[sheet]"
    )
    temp = target.with_name(f".{target.stem}.{os.getpid()}.tmp.jpg")
    process = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}",
         "-t", f"{duration:.3f}", "-i", str(clip["source_path"]), "-an",
         "-filter_complex", ";".join(branches), "-map", "[sheet]",
         "-frames:v", "1", "-q:v", "3", "-y", str(temp)],
        capture_output=True,
        timeout=120,
    )
    if process.returncode or not temp.is_file() or temp.stat().st_size < 2048:
        temp.unlink(missing_ok=True)
        raise RuntimeError((process.stderr or b"contact sheet extraction failed")[-1000:].decode("utf-8", "replace"))
    os.replace(temp, target)
    return str(target)


def materialize_contact_sheets(
    ranked_per_slot: list[list[dict]], output_dir: str | os.PathLike[str], *,
    candidates_per_slot: int = 3, workers: int = 8,
) -> list[list[dict]]:
    root = Path(output_dir)
    unique: dict[str, dict] = {}
    for candidates in ranked_per_slot:
        for candidate in candidates[:candidates_per_slot]:
            unique[_clip_id(candidate)] = candidate

    def work(item):
        clip_id, clip = item
        target = root / f"{clip_id.replace(':', '_').replace('/', '_')}.jpg"
        return clip_id, make_contact_sheet(clip, target)

    sheets: dict[str, str] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        for clip_id, path in executor.map(work, unique.items()):
            sheets[clip_id] = path
    return [
        [{**candidate, "contact_sheet": sheets[_clip_id(candidate)]}
         for candidate in candidates[:candidates_per_slot]]
        for candidates in ranked_per_slot
    ]
