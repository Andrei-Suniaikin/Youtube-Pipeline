"""Stage-agnostic bridge from broad/lazy retrieval to temporal schema v2.

The broad catalog is useful for cheap paging, but its four-frame sheets describe
coarse 4--8 second ranges.  A strict upgrade may re-anchor either edge.  This
adapter therefore treats every strict result as a new, exact media artifact:
it materializes a fresh four-frame sheet from the strict bounds, embeds those
sheets, and only then builds the retrieval document consumed by the temporal
judge and semantic repair controller.

Host paths are runtime-only.  The returned payload is safe to persist or move;
all paths are returned in a separate mapping for the caller's artifact registry.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
import re
from typing import Any

import numpy as np

from .broad_lazy_runtime import (
    DEFAULT_STRICT_CALLBACK_CONTRACT,
    BroadLazyRuntimeBlocked,
    BroadLazyRuntimeError,
    materialize_broad_embeddings,
    prepare_runtime_candidates,
    run_lazy_strict_upgrade,
)
from .clip_model import embed_text as _embed_text
from .common import atomic_write_json, canonical_hash, interval_frame_span
from .retrieval import embed_contact_sheet_rows as _embed_contact_sheet_rows
from .retrieval import make_contact_sheet as _make_contact_sheet
from .semantic import (
    VISUAL_BRIEF_CONTRACT_VERSION,
    validate_visual_brief_contract,
)


TEMPORAL_RETRIEVAL_SCHEMA_VERSION = 2
EXACT_SHEET_CONTRACT = "strict_exact_range_four_frame_v1"
EXACT_EMBEDDING_CONTRACT = "four_temporal_cells_mean_normalized"

_PATH_FIELD_NAMES = {
    "path",
    "source_path",
    "absolute_path",
    "contact_sheet",
    "thumbnail",
    "original_location",
    "original_locations",
}
_ABSOLUTE_PATH = re.compile(r"^(?:/|~/|file://|[A-Za-z]:[\\/])")
_SLOT_LINEAGE_FIELDS = {
    "slot_id",
    "broad_rank",
    "retrieval_score",
    "ranking_page_index",
    "strict_task_key",
    "strict_window_cache_key",
}


class BroadLazyAdapterError(ValueError):
    """The adapter cannot produce a complete temporal retrieval contract."""


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
        raise BroadLazyAdapterError(f"{name} must be finite JSON data") from exc


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise BroadLazyAdapterError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise BroadLazyAdapterError(f"{name} must be a positive integer") from exc
    if result <= 0 or result != value:
        raise BroadLazyAdapterError(f"{name} must be a positive integer")
    return result


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise BroadLazyAdapterError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise BroadLazyAdapterError(f"{name} must be an integer") from exc
    if result != value:
        raise BroadLazyAdapterError(f"{name} must be an integer")
    return result


def _positive_rational(value: Any, name: str) -> Fraction:
    if not isinstance(value, Mapping):
        raise BroadLazyAdapterError(f"{name} must be a rational object")
    try:
        numerator = _integer(value.get("numerator"), f"{name}.numerator")
        denominator = _integer(value.get("denominator"), f"{name}.denominator")
        result = Fraction(numerator, denominator)
    except ZeroDivisionError as exc:
        raise BroadLazyAdapterError(f"{name} denominator cannot be zero") from exc
    if result <= 0:
        raise BroadLazyAdapterError(f"{name} must be positive")
    if dict(value) != {
        "numerator": result.numerator,
        "denominator": result.denominator,
    }:
        raise BroadLazyAdapterError(f"{name} must be canonical")
    return result


def _output_fps_rational(output_fps: float) -> Fraction:
    if not math.isfinite(output_fps) or output_fps <= 0:
        raise BroadLazyAdapterError("output_fps must be positive and finite")
    # Recover standard rates such as 24000/1001 from their float API value.
    rate = Fraction(str(output_fps)).limit_denominator(1_000_000)
    if abs(float(rate) - output_fps) > 1e-9:
        raise BroadLazyAdapterError("output_fps cannot be represented rationally")
    return rate


def _selected_exact_clean_span(
    candidate: Mapping[str, Any],
    *,
    clean_start: int,
    clean_end: int,
) -> tuple[dict[str, Any], Fraction, Fraction]:
    raw_spans = candidate.get("clean_spans")
    if not isinstance(raw_spans, Sequence) or isinstance(
        raw_spans, (str, bytes, bytearray)
    ):
        raise BroadLazyAdapterError("strict candidate has no exact clean spans")
    matches: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_spans):
        if not isinstance(raw, Mapping):
            raise BroadLazyAdapterError(
                f"strict candidate clean_spans[{index}] must be an object"
            )
        try:
            span_start = _integer(
                raw.get("start_frame"), f"clean_spans[{index}].start_frame"
            )
            span_end = _integer(
                raw.get("end_frame"), f"clean_spans[{index}].end_frame"
            )
        except BroadLazyAdapterError:
            raise
        if span_start == clean_start and span_end == clean_end:
            matches.append(dict(raw))
    if len(matches) != 1:
        raise BroadLazyAdapterError(
            "top-level clean frame bounds must match exactly one clean span"
        )
    span = matches[0]
    frame_count = clean_end - clean_start
    if _integer(span.get("frame_count"), "selected_clean_span.frame_count") != frame_count:
        raise BroadLazyAdapterError("selected clean span frame_count is inconsistent")
    start_pts = _integer(span.get("start_pts"), "selected_clean_span.start_pts")
    end_pts = _integer(span.get("end_pts"), "selected_clean_span.end_pts")
    if end_pts <= start_pts:
        raise BroadLazyAdapterError("selected clean span has invalid integer PTS bounds")
    time_base = _positive_rational(
        span.get("time_base"), "selected_clean_span.time_base"
    )
    top_time_base = _positive_rational(candidate.get("time_base"), "candidate.time_base")
    if time_base != top_time_base:
        raise BroadLazyAdapterError(
            "selected clean span time base disagrees with the candidate"
        )
    frame_pts = span.get("frame_pts")
    if frame_pts is not None:
        if not isinstance(frame_pts, Sequence) or isinstance(
            frame_pts, (str, bytes, bytearray)
        ):
            raise BroadLazyAdapterError("selected clean span frame_pts must be an array")
        try:
            points = [
                _integer(value, "selected_clean_span.frame_pts") for value in frame_pts
            ]
        except BroadLazyAdapterError:
            raise
        if (
            len(points) != frame_count
            or not points
            or points[0] != start_pts
            or points[-1] >= end_pts
            or any(right <= left for left, right in zip(points, points[1:]))
        ):
            raise BroadLazyAdapterError(
                "selected clean span frame_pts disagree with its exact bounds"
            )
    exact_start = Fraction(start_pts) * time_base
    exact_end = Fraction(end_pts) * time_base
    for label, supplied, expected in (
        ("clean_source_in", candidate.get("clean_source_in"), exact_start),
        ("clean_source_out", candidate.get("clean_source_out"), exact_end),
        ("clean_span.start_time", span.get("start_time"), exact_start),
        ("clean_span.end_time", span.get("end_time"), exact_end),
    ):
        try:
            supplied_float = float(supplied)
        except (TypeError, ValueError) as exc:
            raise BroadLazyAdapterError(f"{label} must be finite") from exc
        if not math.isfinite(supplied_float) or abs(supplied_float - float(expected)) > 1e-9:
            raise BroadLazyAdapterError(f"{label} disagrees with exact integer PTS")
    try:
        source_in = float(candidate.get("source_in"))
        source_out = float(candidate.get("source_out"))
    except (TypeError, ValueError) as exc:
        raise BroadLazyAdapterError("strict candidate source bounds are malformed") from exc
    if (
        source_in < float(exact_start) - 1e-9
        or source_out > float(exact_end) + 1e-9
    ):
        raise BroadLazyAdapterError("strict source range escapes its selected clean span")
    return span, time_base, exact_end - exact_start


def _assert_path_free(value: Any, name: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in _PATH_FIELD_NAMES:
                raise BroadLazyAdapterError(
                    f"{name} contains forbidden runtime path field {key!r}"
                )
            _assert_path_free(item, f"{name}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for index, item in enumerate(value):
            _assert_path_free(item, f"{name}[{index}]")
        return
    if isinstance(value, str) and _ABSOLUTE_PATH.match(value.strip()):
        raise BroadLazyAdapterError(f"{name} contains an absolute host path")


def _validate_inputs(
    beats: Sequence[Mapping[str, Any]],
    briefs: Sequence[Mapping[str, Any]],
    slot_render_frames: Mapping[str, int],
    *,
    output_fps: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    if not math.isfinite(output_fps) or output_fps <= 0:
        raise BroadLazyAdapterError("output_fps must be positive and finite")
    if (
        not isinstance(beats, Sequence)
        or isinstance(beats, (str, bytes))
        or not beats
    ):
        raise BroadLazyAdapterError("beats must be a non-empty array")
    if (
        not isinstance(briefs, Sequence)
        or isinstance(briefs, (str, bytes))
        or len(briefs) != len(beats)
    ):
        raise BroadLazyAdapterError("briefs must align one-to-one with beats")
    if not isinstance(slot_render_frames, Mapping):
        raise BroadLazyAdapterError("slot_render_frames must be an object")

    normalized_beats: list[dict[str, Any]] = []
    normalized_briefs: list[dict[str, Any]] = []
    normalized_frames: dict[str, int] = {}
    seen: set[str] = set()
    for index, (raw_beat, raw_brief) in enumerate(zip(beats, briefs)):
        if not isinstance(raw_beat, Mapping) or not isinstance(raw_brief, Mapping):
            raise BroadLazyAdapterError(f"slot {index} beat/brief must be objects")
        beat = _clone(dict(raw_beat), f"beats[{index}]")
        slot_id = str(beat.get("slot_id") or "").strip()
        if not slot_id or _ABSOLUTE_PATH.match(slot_id):
            raise BroadLazyAdapterError(f"beats[{index}] has no portable slot_id")
        if slot_id in seen:
            raise BroadLazyAdapterError(f"duplicate slot_id: {slot_id}")
        seen.add(slot_id)
        try:
            start = float(beat["start"])
            end = float(beat["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BroadLazyAdapterError(f"beat {slot_id} has invalid timing") from exc
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise BroadLazyAdapterError(f"beat {slot_id} has an empty timing range")
        if slot_id not in slot_render_frames:
            raise BroadLazyAdapterError(
                f"slot_render_frames is missing beat {slot_id}"
            )
        render_frames = _positive_int(
            slot_render_frames[slot_id], f"slot_render_frames[{slot_id}]"
        )
        try:
            _start_frame, _end_frame, expected_frames = interval_frame_span(
                start, end, output_fps
            )
        except ValueError as exc:
            raise BroadLazyAdapterError(
                f"beat {slot_id} has an invalid output-frame interval"
            ) from exc
        if render_frames != expected_frames:
            raise BroadLazyAdapterError(
                f"beat {slot_id} timing requires {expected_frames} frames, "
                f"not {render_frames}"
            )
        try:
            brief = validate_visual_brief_contract(beat, dict(raw_brief))
        except (TypeError, ValueError) as exc:
            raise BroadLazyAdapterError(
                f"visual brief contract failed for {slot_id}: {exc}"
            ) from exc
        normalized_beats.append(beat)
        normalized_briefs.append(brief)
        normalized_frames[slot_id] = render_frames

    if set(str(key) for key in slot_render_frames) != seen:
        raise BroadLazyAdapterError(
            "slot_render_frames must exactly cover the beat/brief slots"
        )
    return normalized_beats, normalized_briefs, normalized_frames


def _normalized_overlay(candidate: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(candidate)
    overlay = row.get("overlay_analysis")
    if isinstance(overlay, Mapping):
        normalized = dict(overlay)
        if not str(normalized.get("status") or "").strip():
            normalized["status"] = (
                "unresolved"
                if normalized.get("action")
                in {"inpaint_required", "crop_and_inpaint"}
                else "resolved"
            )
        row["overlay_analysis"] = normalized
    return row


def _assert_strict_candidate_contract(
    candidate: Mapping[str, Any],
    *,
    output_fps: float,
    required_render_frames: int | None = None,
) -> tuple[int, float]:
    try:
        source_in = float(candidate["source_in"])
        source_out = float(candidate["source_out"])
        source_fps = float(candidate["fps"])
        clean_start = int(candidate["clean_frame_start"])
        clean_end = int(candidate["clean_frame_end"])
        clean_source_in = float(candidate["clean_source_in"])
        clean_source_out = float(candidate["clean_source_out"])
        render_frames = int(candidate["render_frames"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BroadLazyAdapterError(
            "strict candidate lacks exact source/clean-frame capacity"
        ) from exc
    if not all(
        math.isfinite(value)
        for value in (
            source_in,
            source_out,
            source_fps,
            clean_source_in,
            clean_source_out,
        )
    ):
        raise BroadLazyAdapterError("strict candidate has non-finite media facts")
    if source_out <= source_in or source_fps + 1e-6 < output_fps:
        raise BroadLazyAdapterError("strict candidate cannot support output timing")
    if clean_end <= clean_start or render_frames <= 0:
        raise BroadLazyAdapterError("strict candidate has an empty clean frame span")
    if clean_source_out <= clean_source_in:
        raise BroadLazyAdapterError("strict candidate has an empty clean source span")
    if required_render_frames is not None and render_frames != required_render_frames:
        raise BroadLazyAdapterError(
            "strict candidate render_frames does not match its requested slot"
        )
    if not isinstance(candidate.get("frame_contract"), Mapping):
        raise BroadLazyAdapterError("strict candidate has no frame_contract")
    if str(candidate.get("fill_strategy") or "") != "none":
        raise BroadLazyAdapterError("strict candidate requests a fill strategy")
    try:
        retime = float(candidate.get("retime_factor"))
    except (TypeError, ValueError) as exc:
        raise BroadLazyAdapterError("strict candidate has invalid retime_factor") from exc
    if abs(retime - 1.0) > 1e-9:
        raise BroadLazyAdapterError("strict candidate requests retiming")
    if not str(candidate.get("selection_contract") or "").startswith(
        "frame_exact"
    ):
        raise BroadLazyAdapterError("strict candidate is not frame-exact")
    if any(
        bool(candidate.get(field))
        for field in (
            "allow_hold",
            "allow_tpad",
            "manual_selection",
            "held_tail_seconds",
            "overlay_blocked",
            "unresolved_overlay",
        )
    ):
        raise BroadLazyAdapterError(
            "strict candidate contains manual/fill/unresolved lineage"
        )
    overlay = candidate.get("overlay_analysis")
    if not isinstance(overlay, Mapping) or str(overlay.get("status") or "") not in {
        "clean",
        "resolved",
    }:
        raise BroadLazyAdapterError(
            "strict candidate lacks resolved overlay evidence"
        )
    _span, _time_base, exact_clean_duration = _selected_exact_clean_span(
        candidate,
        clean_start=clean_start,
        clean_end=clean_end,
    )
    try:
        source_frame_start = _integer(
            candidate.get("source_frame_start"), "source_frame_start"
        )
        source_frame_end = _integer(
            candidate.get("source_frame_end"), "source_frame_end"
        )
    except BroadLazyAdapterError:
        raise
    if not (
        clean_start
        <= source_frame_start
        < source_frame_end
        <= clean_end
    ):
        raise BroadLazyAdapterError(
            "strict source frame range escapes its selected clean span"
        )
    output_rate = _output_fps_rational(output_fps)
    selected_source_frames = source_frame_end - source_frame_start
    span_points = _span.get("frame_pts")
    if span_points is not None:
        points = [
            _integer(value, "selected_clean_span.frame_pts")
            for value in span_points
        ]
        start_offset = source_frame_start - clean_start
        end_offset = source_frame_end - clean_start
        selected_start_pts = points[start_offset]
        selected_end_pts = (
            points[end_offset]
            if end_offset < len(points)
            else _integer(_span.get("end_pts"), "selected_clean_span.end_pts")
        )
        exact_source_in = Fraction(selected_start_pts) * _time_base
        exact_source_out = Fraction(selected_end_pts) * _time_base
        if (
            abs(source_in - float(exact_source_in)) > 1e-9
            or abs(source_out - float(exact_source_out)) > 1e-9
        ):
            raise BroadLazyAdapterError(
                "strict source bounds disagree with selected integer PTS"
            )
        exact_source_duration = exact_source_out - exact_source_in
    else:
        # Compatibility for synthetic contracts that predate per-span vectors.
        # Real strict results always take the exact integer-PTS branch above.
        exact_source_duration = Fraction(str(source_out)) - Fraction(str(source_in))
    required_duration = Fraction(render_frames, 1) / output_rate
    if (
        selected_source_frames < render_frames
        or exact_source_duration < required_duration
    ):
        raise BroadLazyAdapterError(
            "strict selected source range is shorter than render_frames"
        )
    duration_capacity = exact_clean_duration * output_rate
    capacity_frames = min(
        clean_end - clean_start,
        duration_capacity.numerator // duration_capacity.denominator,
    )
    if capacity_frames < render_frames:
        raise BroadLazyAdapterError(
            "strict candidate clean span is shorter than render_frames"
        )
    capacity_seconds = Fraction(capacity_frames, 1) / output_rate
    return capacity_frames, float(capacity_seconds)


def validate_exact_candidate_contract(
    candidate: Mapping[str, Any],
    *,
    output_fps: float,
    required_render_frames: int | None = None,
) -> tuple[int, float]:
    """Validate one judged exact range and return its clean-span capacity."""

    return _assert_strict_candidate_contract(
        candidate,
        output_fps=output_fps,
        required_render_frames=required_render_frames,
    )


def _exact_range_identity(
    row: Mapping[str, Any], *, parent_clip_id: str
) -> dict[str, Any]:
    """Project only stable, render-affecting exact-range identity fields."""

    overlay = row.get("overlay_analysis")
    if not isinstance(overlay, Mapping) or str(overlay.get("status") or "") not in {
        "clean",
        "resolved",
    }:
        raise BroadLazyAdapterError(
            "strict candidate lacks resolved overlay evidence"
        )
    try:
        clean_start = _integer(row.get("clean_frame_start"), "clean_frame_start")
        clean_end = _integer(row.get("clean_frame_end"), "clean_frame_end")
        source_frame_start = _integer(
            row.get("source_frame_start"), "source_frame_start"
        )
        source_frame_end = _integer(row.get("source_frame_end"), "source_frame_end")
        render_frames = _positive_int(row.get("render_frames"), "render_frames")
        source_in = float(row.get("source_in"))
        source_out = float(row.get("source_out"))
    except (TypeError, ValueError) as exc:
        raise BroadLazyAdapterError(
            "strict candidate exact range identity is malformed"
        ) from exc
    if (
        clean_end <= clean_start
        or source_frame_end <= source_frame_start
        or not math.isfinite(source_in)
        or not math.isfinite(source_out)
        or source_out <= source_in
    ):
        raise BroadLazyAdapterError("strict candidate exact range identity is invalid")
    span, time_base, _duration = _selected_exact_clean_span(
        row,
        clean_start=clean_start,
        clean_end=clean_end,
    )
    return {
        "contract": "strict_exact_candidate_range_v1",
        "parent_clip_id": parent_clip_id,
        "source_asset_id": str(row.get("source_asset_id") or ""),
        "source_sha256": str(row.get("source_sha256") or ""),
        "source_frame_start": source_frame_start,
        "source_frame_end": source_frame_end,
        "source_in": round(source_in, 9),
        "source_out": round(source_out, 9),
        "render_frames": render_frames,
        "clean_frame_start": clean_start,
        "clean_frame_end": clean_end,
        "clean_start_pts": _integer(span.get("start_pts"), "clean_start_pts"),
        "clean_end_pts": _integer(span.get("end_pts"), "clean_end_pts"),
        "time_base": {
            "numerator": time_base.numerator,
            "denominator": time_base.denominator,
        },
        "selection_contract": str(row.get("selection_contract") or ""),
        "fill_strategy": str(row.get("fill_strategy") or ""),
        "retime_factor": float(row.get("retime_factor")),
        "overlay_render": {
            "action": str(overlay.get("action") or ""),
            "crop_safe": bool(overlay.get("crop_safe")),
            "crop_margins": _clone(
                overlay.get("crop_margins") or {}, "overlay.crop_margins"
            ),
            "crop_provenance": _clone(
                overlay.get("crop_provenance"), "overlay.crop_provenance"
            ),
        },
    }


def _exact_candidate(raw: Mapping[str, Any]) -> dict[str, Any]:
    row = _normalized_overlay(raw)
    parent_clip_id = str(row.get("clip_id") or "").strip()
    if not parent_clip_id:
        raise BroadLazyAdapterError("strict candidate has no clip_id")
    if not str(row.get("source_asset_id") or "").strip():
        raise BroadLazyAdapterError("strict candidate lost source_asset_id")
    if not str(row.get("source_sha256") or "").strip():
        raise BroadLazyAdapterError("strict candidate lost source_sha256")
    identity = _exact_range_identity(row, parent_clip_id=parent_clip_id)
    exact_hash = canonical_hash(identity)
    exact_clip_id = f"{parent_clip_id}__strict_{exact_hash[:16]}"
    exact = {
        **row,
        "parent_clip_id": parent_clip_id,
        "clip_id": exact_clip_id,
        "candidate_range_hash": exact_hash,
        "exact_sheet_contract": EXACT_SHEET_CONTRACT,
    }
    if "retrieval_score" in exact:
        exact["broad_retrieval_score"] = float(exact["retrieval_score"])
    return exact


def _lineage_assignment(candidate: Mapping[str, Any]) -> dict[str, Any]:
    assignment = {
        key: candidate[key]
        for key in sorted(_SLOT_LINEAGE_FIELDS)
        if key in candidate
    }
    if "retrieval_score" in assignment:
        assignment["retrieval_score"] = float(assignment["retrieval_score"])
    return assignment


def _deduplicate_strict_union(
    strict_arrays: Sequence[Sequence[Mapping[str, Any]]],
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]], int]:
    per_slot: list[list[dict[str, Any]]] = []
    universe_by_id: dict[str, dict[str, Any]] = {}
    input_count = 0
    for slot_rows in strict_arrays:
        current: list[dict[str, Any]] = []
        for raw in slot_rows:
            input_count += 1
            exact = _exact_candidate(raw)
            clip_id = str(exact["clip_id"])
            current.append(exact)
            assignment = _lineage_assignment(exact)
            existing = universe_by_id.get(clip_id)
            if existing is None:
                portable = {
                    key: value
                    for key, value in exact.items()
                    if key not in _SLOT_LINEAGE_FIELDS
                }
                portable["strict_lineage_assignments"] = [assignment]
                universe_by_id[clip_id] = portable
            else:
                assignments = existing["strict_lineage_assignments"]
                if assignment not in assignments:
                    assignments.append(assignment)
        per_slot.append(current)
    universe = [universe_by_id[key] for key in sorted(universe_by_id)]
    for candidate in universe:
        candidate["strict_lineage_assignments"].sort(
            key=lambda row: (
                str(row.get("slot_id") or ""),
                int(row.get("broad_rank") or 0),
                str(row.get("strict_task_key") or ""),
            )
        )
    return per_slot, universe, input_count


def _materialize_exact_sheets(
    candidates: Sequence[Mapping[str, Any]],
    asset_resolver: Mapping[str, str | os.PathLike[str]],
    directory: Path,
    *,
    workers: int,
    ffmpeg: str,
    sheet_maker: Callable,
    artifact_prefix: str = "sheet",
) -> tuple[list[dict[str, Any]], dict[str, Path], dict[str, int]]:
    runtime = prepare_runtime_candidates(candidates, asset_resolver)
    sheets: list[dict[str, Any] | None] = [None] * len(runtime)
    artifact_paths: dict[str, Path] = {}

    def make(
        index: int, candidate: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any], str, Path, bool]:
        exact_hash = str(candidate.get("candidate_range_hash") or "")
        if len(exact_hash) != 64:
            raise BroadLazyAdapterError("exact candidate has no range hash")
        artifact_id = f"{artifact_prefix}-{exact_hash[:20]}"
        target = directory / f"{artifact_prefix}.{exact_hash}.jpg"
        hit = target.is_file() and target.stat().st_size > 2048
        if not hit:
            sheet_maker(dict(candidate), target, ffmpeg=ffmpeg)
        if not target.is_file() or target.stat().st_size <= 2048:
            raise BroadLazyAdapterError(
                f"exact four-frame sheet is missing for {candidate['clip_id']}"
            )
        row = {
            **dict(candidate),
            "contact_sheet": str(target),
            "contact_sheet_artifact_id": artifact_id,
        }
        return index, row, artifact_id, target, hit

    hits = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(make, index, candidate)
            for index, candidate in enumerate(runtime)
        ]
        for future in cf.as_completed(futures):
            index, row, artifact_id, target, hit = future.result()
            sheets[index] = row
            artifact_paths[artifact_id] = target
            hits += int(hit)
    ordered = [row for row in sheets if row is not None]
    if len(ordered) != len(candidates) or len(artifact_paths) != len(candidates):
        raise BroadLazyAdapterError("exact sheet materialization lost candidates")
    return ordered, dict(sorted(artifact_paths.items())), {
        "sheet_cache_hits": hits,
        "sheet_materialized": len(ordered) - hits,
    }


def _normalize_vectors(raw: Any, *, expected_rows: int) -> np.ndarray:
    matrix = np.asarray(raw, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] != expected_rows or matrix.shape[1] < 1:
        raise BroadLazyAdapterError("exact embedding matrix has invalid shape")
    if not np.isfinite(matrix).all():
        raise BroadLazyAdapterError("exact embedding matrix contains non-finite values")
    norms = np.linalg.norm(matrix, axis=1)
    if np.any(~np.isfinite(norms)) or np.any(norms <= 0):
        raise BroadLazyAdapterError("exact embedding matrix contains zero vectors")
    return (matrix / norms[:, None]).astype(np.float32)


def _query_vector(query: str, *, embedder: Callable, dimensions: int) -> np.ndarray:
    vector = np.asarray(embedder(query), dtype=np.float32)
    if vector.ndim != 1 or vector.size != dimensions or not np.isfinite(vector).all():
        raise BroadLazyAdapterError("query embedding disagrees with exact embeddings")
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or norm <= 0:
        raise BroadLazyAdapterError("query embedding is empty")
    return vector / norm


def _portable_candidate(
    raw: Mapping[str, Any], *, output_fps: float
) -> dict[str, Any]:
    row = dict(raw)
    row.pop("source_path", None)
    row.pop("contact_sheet", None)
    capacity_frames, capacity_seconds = _assert_strict_candidate_contract(
        row, output_fps=output_fps
    )
    row["max_render_frames"] = capacity_frames
    row["max_render_seconds"] = round(capacity_seconds, 9)
    _assert_path_free(row, "candidate")
    return row


def materialize_exact_repair_candidate(
    candidate: Mapping[str, Any],
    *,
    asset_resolver: Mapping[str, str | os.PathLike[str]],
    cache_directory: str | os.PathLike[str],
    output_fps: float,
    ffmpeg: str = "ffmpeg",
    sheet_maker: Callable = _make_contact_sheet,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create/cache a fresh four-frame sheet for one rematerialized range.

    The first return value is path-free and safe to persist. The second is a
    runtime-only artifact record whose path stays under ``cache_directory``.
    Existing exact candidates retain their broad ``parent_clip_id`` so a new
    range receives a stable sibling identity instead of a nested clip ID.
    """

    if not isinstance(candidate, Mapping):
        raise BroadLazyAdapterError("repair candidate must be an object")
    identity_input = dict(candidate)
    parent_clip_id = str(
        identity_input.get("parent_clip_id") or identity_input.get("clip_id") or ""
    ).strip()
    if not parent_clip_id:
        raise BroadLazyAdapterError("repair candidate has no parent clip identity")
    identity_input["clip_id"] = parent_clip_id
    for stale in (
        "candidate_range_hash",
        "contact_sheet_artifact_id",
        "exact_sheet_contract",
        "max_render_frames",
        "max_render_seconds",
    ):
        identity_input.pop(stale, None)
    exact = _exact_candidate(identity_input)
    render_frames = _positive_int(exact.get("render_frames"), "render_frames")
    validate_exact_candidate_contract(
        exact,
        output_fps=output_fps,
        required_render_frames=render_frames,
    )

    cache_root = Path(cache_directory).expanduser().resolve()
    sheet_root = cache_root / "exact_repair_contact_sheets"
    rows, artifact_paths, stats = _materialize_exact_sheets(
        [exact],
        asset_resolver,
        sheet_root,
        workers=1,
        ffmpeg=ffmpeg,
        sheet_maker=sheet_maker,
        artifact_prefix="repair-sheet",
    )
    if len(rows) != 1 or len(artifact_paths) != 1:
        raise BroadLazyAdapterError("repair sheet materialization lost its candidate")
    runtime_row = rows[0]
    portable = _portable_candidate(runtime_row, output_fps=output_fps)
    artifact_id = str(portable.get("contact_sheet_artifact_id") or "")
    path = artifact_paths.get(artifact_id)
    if not artifact_id.startswith("repair-sheet-") or path is None:
        raise BroadLazyAdapterError("repair sheet artifact identity is invalid")
    try:
        path.relative_to(cache_root)
    except ValueError as exc:
        raise BroadLazyAdapterError("repair sheet escaped its cache directory") from exc
    artifact = {
        "artifact_id": artifact_id,
        "path": str(path),
        "cache_hit": stats["sheet_cache_hits"] == 1,
        "clip_id": str(portable["clip_id"]),
        "candidate_range_hash": str(portable["candidate_range_hash"]),
    }
    return portable, artifact


def _rank_rows(
    candidates: Sequence[Mapping[str, Any]],
    *,
    query_vector: np.ndarray,
    vector_by_id: Mapping[str, np.ndarray],
    output_fps: float,
    limit: int,
    required_render_frames: int | None = None,
    minimum_render_frames: int | None = None,
) -> list[dict[str, Any]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for raw in candidates:
        row = _portable_candidate(raw, output_fps=output_fps)
        if required_render_frames is not None:
            _assert_strict_candidate_contract(
                row,
                output_fps=output_fps,
                required_render_frames=required_render_frames,
            )
        if minimum_render_frames is not None and int(
            row.get("max_render_frames") or 0
        ) < minimum_render_frames:
            continue
        clip_id = str(row.get("clip_id") or "")
        if clip_id not in vector_by_id:
            raise BroadLazyAdapterError("candidate is missing from exact embeddings")
        score = float(np.dot(vector_by_id[clip_id], query_vector))
        if not math.isfinite(score):
            raise BroadLazyAdapterError("exact retrieval produced a non-finite score")
        if "retrieval_score" in row and "broad_retrieval_score" not in row:
            row["broad_retrieval_score"] = float(row["retrieval_score"])
        row["retrieval_score"] = score
        row["exact_retrieval_score"] = score
        scored.append((score, row))
    scored.sort(
        key=lambda item: (
            -item[0],
            int(item[1].get("broad_rank") or 0),
            str(item[1].get("clip_id") or ""),
        )
    )
    return [row for _score, row in scored[:limit]]


def _write_exact_embeddings(
    directory: Path,
    *,
    vectors: np.ndarray,
    clip_ids: Sequence[str],
    model_contract: Mapping[str, Any],
) -> tuple[Path, str, str]:
    clip_ids_hash = canonical_hash(list(clip_ids))
    vector_sha256 = hashlib.sha256(
        np.asarray(vectors, dtype="<f4").tobytes(order="C")
    ).hexdigest()
    artifact_hash = canonical_hash(
        {
            "embedding_contract": EXACT_EMBEDDING_CONTRACT,
            "exact_sheet_contract": EXACT_SHEET_CONTRACT,
            "clip_ids_hash": clip_ids_hash,
            "vector_sha256": vector_sha256,
            "model_contract": _clone(dict(model_contract), "model_contract"),
        }
    )
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"temporal_embeddings.{artifact_hash}.npz"
    if target.exists():
        try:
            with np.load(target, allow_pickle=False) as data:
                stored_vectors = np.asarray(data["vectors"], dtype=np.float32)
                stored_ids = [str(value) for value in data["clip_ids"].tolist()]
        except (OSError, KeyError, ValueError) as exc:
            raise BroadLazyAdapterError(
                "existing exact embedding artifact is unreadable"
            ) from exc
        if stored_ids != list(clip_ids) or not np.array_equal(
            stored_vectors, np.asarray(vectors, dtype=np.float32)
        ):
            raise BroadLazyAdapterError(
                "content-addressed exact embedding artifact disagrees with its hash"
            )
        return target, clip_ids_hash, vector_sha256
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.tmp"
    )
    try:
        with temporary.open("wb") as output:
            np.savez_compressed(
                output,
                vectors=np.asarray(vectors, dtype=np.float32),
                clip_ids=np.asarray(list(clip_ids), dtype=np.str_),
            )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target, clip_ids_hash, vector_sha256


def _write_retrieval_payload(directory: Path, payload: Mapping[str, Any]) -> Path:
    document = _clone(dict(payload), "temporal retrieval payload")
    _assert_path_free(document)
    payload_hash = canonical_hash(document)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"temporal_retrieval.{payload_hash}.json"
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BroadLazyAdapterError(
                "existing temporal retrieval artifact is unreadable"
            ) from exc
        if existing != document:
            raise BroadLazyAdapterError(
                "content-addressed temporal retrieval artifact has different content"
            )
        return target
    atomic_write_json(target, document)
    return target


def materialize_temporal_retrieval_v2(
    catalog: Mapping[str, Any],
    beats: Sequence[Mapping[str, Any]],
    briefs: Sequence[Mapping[str, Any]],
    *,
    slot_render_frames: Mapping[str, int],
    asset_resolver: Mapping[str, str | os.PathLike[str]],
    cache_directory: str | os.PathLike[str],
    artifact_directory: str | os.PathLike[str],
    model_contract: Mapping[str, Any],
    strict_upgrade_fn: Callable,
    detector_policy: Mapping[str, Any],
    output_fps: float = 25.0,
    survivor_target: int = 12,
    repair_pool_size: int = 60,
    page_size: int = 20,
    broad_sheet_workers: int = 4,
    strict_workers: int = 4,
    exact_sheet_workers: int = 4,
    embedding_batch_size: int = 64,
    search_seconds: float = 4.0,
    overlay_profiles: Mapping[str, Mapping[str, Any]] | None = None,
    callback_contract: str = DEFAULT_STRICT_CALLBACK_CONTRACT,
    ffmpeg: str = "ffmpeg",
    query_embedder: Callable = _embed_text,
    broad_sheet_maker: Callable = _make_contact_sheet,
    broad_embedding_fn: Callable = _embed_contact_sheet_rows,
    exact_sheet_maker: Callable = _make_contact_sheet,
    exact_embedding_fn: Callable = _embed_contact_sheet_rows,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create temporal retrieval schema v2 from broad and lazy-strict APIs.

    ``payload`` (the first return item) contains no host path.  ``paths`` (the
    second return item) binds durable artifact IDs to local files and is meant
    for the caller's runtime artifact registry, never for JSON persistence.

    ``BroadLazyRuntimeBlocked(code='needs_clean_donor_ranges')`` is deliberately
    propagated when paging cannot reach ``survivor_target`` for every slot.
    """

    target = _positive_int(survivor_target, "survivor_target")
    repair_size = _positive_int(repair_pool_size, "repair_pool_size")
    broad_workers = _positive_int(broad_sheet_workers, "broad_sheet_workers")
    strict_worker_count = _positive_int(strict_workers, "strict_workers")
    exact_workers = _positive_int(exact_sheet_workers, "exact_sheet_workers")
    batch_size = _positive_int(embedding_batch_size, "embedding_batch_size")
    page_size = _positive_int(page_size, "page_size")
    normalized_beats, normalized_briefs, normalized_frames = _validate_inputs(
        beats, briefs, slot_render_frames, output_fps=output_fps
    )
    cache_root = Path(cache_directory)
    artifact_root = Path(artifact_directory)
    broad_artifact_root = artifact_root / "broad_runtime"

    broad_embedding, broad_vectors, broad_stats = materialize_broad_embeddings(
        catalog,
        asset_resolver,
        cache_root,
        model_contract=model_contract,
        sheet_workers=broad_workers,
        embedding_batch_size=batch_size,
        ffmpeg=ffmpeg,
        sheet_maker=broad_sheet_maker,
        embedding_fn=broad_embedding_fn,
    )
    strict_result = run_lazy_strict_upgrade(
        catalog,
        broad_embedding,
        broad_vectors,
        normalized_briefs,
        slot_render_frames=normalized_frames,
        asset_resolver=asset_resolver,
        strict_upgrade_fn=strict_upgrade_fn,
        cache_directory=cache_root,
        detector_policy=detector_policy,
        overlay_profiles=overlay_profiles,
        strict_survivors_per_slot=target,
        page_size=page_size,
        max_workers=strict_worker_count,
        search_seconds=search_seconds,
        query_embedder=query_embedder,
        callback_contract=callback_contract,
        artifact_directory=broad_artifact_root,
    )
    if strict_result.get("status") != "ready":
        raise BroadLazyAdapterError("lazy strict upgrade did not finish ready")
    slot_ids = [str(row["slot_id"]) for row in normalized_beats]
    if strict_result.get("slot_ids") != slot_ids:
        raise BroadLazyAdapterError("strict result slot order is not beat-aligned")
    strict_arrays = strict_result.get("strict_candidates")
    if not isinstance(strict_arrays, list) or len(strict_arrays) != len(slot_ids):
        raise BroadLazyAdapterError("strict result candidate arrays are invalid")
    if any(not isinstance(rows, list) or len(rows) < target for rows in strict_arrays):
        raise BroadLazyAdapterError("strict result missed its survivor target")

    slot_exact, universe, strict_input_count = _deduplicate_strict_union(
        strict_arrays
    )
    if not universe:
        raise BroadLazyAdapterError("strict survivor union is empty")
    for slot_id, render_frames, rows in zip(
        slot_ids, (normalized_frames[key] for key in slot_ids), slot_exact
    ):
        if len(rows) < target:
            raise BroadLazyAdapterError(f"strict survivor array is short for {slot_id}")
        for row in rows:
            _assert_strict_candidate_contract(
                row,
                output_fps=output_fps,
                required_render_frames=render_frames,
            )

    exact_sheets, sheet_paths, sheet_stats = _materialize_exact_sheets(
        universe,
        asset_resolver,
        artifact_root / "exact_contact_sheets",
        workers=exact_workers,
        ffmpeg=ffmpeg,
        sheet_maker=exact_sheet_maker,
    )
    try:
        raw_exact_vectors = exact_embedding_fn(
            exact_sheets, batch_size=batch_size
        )
    except Exception as exc:
        raise BroadLazyAdapterError(
            f"exact four-frame embedding failed: {exc}"
        ) from exc
    exact_vectors = _normalize_vectors(
        raw_exact_vectors, expected_rows=len(exact_sheets)
    )
    clip_ids = [str(row["clip_id"]) for row in exact_sheets]
    if len(set(clip_ids)) != len(clip_ids):
        raise BroadLazyAdapterError("candidate universe is not deduplicated")
    vector_by_id = {
        clip_id: exact_vectors[index] for index, clip_id in enumerate(clip_ids)
    }
    sheet_by_id = {str(row["clip_id"]): row for row in exact_sheets}

    ranked_candidates: list[list[dict[str, Any]]] = []
    repair_candidates: list[list[dict[str, Any]]] = []
    minimum_repair_frames = int(math.ceil(4.0 * output_fps - 1e-9))
    for slot_id, brief, rows in zip(slot_ids, normalized_briefs, slot_exact):
        query = str(brief.get("query_en") or "").strip()
        query_vector = _query_vector(
            query, embedder=query_embedder, dimensions=exact_vectors.shape[1]
        )
        runtime_rows = []
        for row in rows:
            sheet = sheet_by_id.get(str(row["clip_id"]))
            if sheet is None:
                raise BroadLazyAdapterError(
                    f"strict slot {slot_id} lost its exact contact sheet"
                )
            runtime_rows.append(
                {
                    **row,
                    "contact_sheet_artifact_id": sheet[
                        "contact_sheet_artifact_id"
                    ],
                }
            )
        ranked = _rank_rows(
            runtime_rows,
            query_vector=query_vector,
            vector_by_id=vector_by_id,
            output_fps=output_fps,
            limit=target,
            required_render_frames=normalized_frames[slot_id],
        )
        if len(ranked) < target:
            raise BroadLazyAdapterError(
                f"exact retrieval left too few strict candidates for {slot_id}"
            )
        ranked_candidates.append(ranked)

        repair_rows = _rank_rows(
            exact_sheets,
            query_vector=query_vector,
            vector_by_id=vector_by_id,
            output_fps=output_fps,
            limit=repair_size,
            minimum_render_frames=minimum_repair_frames,
        )
        if not repair_rows:
            raise BroadLazyAdapterError(
                f"exact retrieval left no clean repair candidate for {slot_id}"
            )
        repair_candidates.append(repair_rows)

    candidate_universe = [
        _portable_candidate(row, output_fps=output_fps) for row in exact_sheets
    ]
    embeddings_path, clip_ids_hash, vector_sha256 = _write_exact_embeddings(
        artifact_root,
        vectors=exact_vectors,
        clip_ids=clip_ids,
        model_contract=model_contract,
    )
    original_beats_hash = canonical_hash(normalized_beats)
    diagnostics = {
        "broad_embedding": {
            **{key: int(value) for key, value in sorted(broad_stats.items())},
            "contract_hash": str(broad_embedding.get("content_hash") or ""),
        },
        "strict_upgrade": {
            "result_hash": str(strict_result.get("content_hash") or ""),
            "survivor_target": target,
            "candidate_counts": list(strict_result.get("strict_candidate_counts") or ()),
            "provenance": _clone(
                strict_result.get("provenance") or {}, "strict provenance"
            ),
            "lineage_policy": _clone(
                strict_result.get("lineage_policy") or {}, "strict lineage policy"
            ),
        },
        "exact_range": {
            "sheet_contract": EXACT_SHEET_CONTRACT,
            "strict_survivor_rows": strict_input_count,
            "deduplicated_candidate_count": len(candidate_universe),
            "deduplicated_rows": strict_input_count - len(candidate_universe),
            **sheet_stats,
            "embedding_rows": int(exact_vectors.shape[0]),
            "embedding_dimensions": int(exact_vectors.shape[1]),
            "embedding_vector_sha256": vector_sha256,
            "clip_ids_hash": clip_ids_hash,
        },
        "repair_pool_size": repair_size,
    }
    payload = {
        "schema_version": TEMPORAL_RETRIEVAL_SCHEMA_VERSION,
        "original_beats_hash": original_beats_hash,
        "effective_beats": _clone(normalized_beats, "effective_beats"),
        "effective_beats_hash": original_beats_hash,
        "visual_brief_contract_version": VISUAL_BRIEF_CONTRACT_VERSION,
        "briefs": _clone(normalized_briefs, "briefs"),
        "ranked_candidates": ranked_candidates,
        "repair_candidates": repair_candidates,
        "candidate_universe": candidate_universe,
        "embedding_artifact_id": "temporal_embeddings",
        "embedding_clip_ids_hash": clip_ids_hash,
        "embedding_contract": EXACT_EMBEDDING_CONTRACT,
        "diagnostics": diagnostics,
    }
    _assert_path_free(payload)
    retrieval_path = _write_retrieval_payload(artifact_root, payload)
    paths = {
        "retrieval": retrieval_path,
        "temporal_embeddings": embeddings_path,
        "contact_sheets": sheet_paths,
        "broad_runtime_artifacts": broad_artifact_root,
    }
    return _clone(payload, "payload"), paths


__all__ = [
    "TEMPORAL_RETRIEVAL_SCHEMA_VERSION",
    "EXACT_SHEET_CONTRACT",
    "EXACT_EMBEDDING_CONTRACT",
    "BroadLazyAdapterError",
    "BroadLazyRuntimeBlocked",
    "BroadLazyRuntimeError",
    "materialize_exact_repair_candidate",
    "materialize_temporal_retrieval_v2",
    "validate_exact_candidate_contract",
]
