"""Semantic selection glue for the donor-montage canary.

The expensive stages stay deliberately small: CLIP retrieves a shortlist and a
temporal vision judge sees only the top contact sheets.  This module turns those
results into a gap-free timeline while enforcing the hard 4--8 second and
no-reused-source-range rules.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np

from .broad_lazy_adapter import (
    BroadLazyAdapterError,
    validate_exact_candidate_contract,
)
from .common import interval_frame_span
from .retrieval import (
    SelectionOptimizationError,
    choose_non_overlapping,
    global_constrained_select,
    ranges_overlap,
    rank_for_query,
)


class SelectionError(ValueError):
    """Raised when a valid renderable timeline cannot be assembled."""


_MICRO_TAIL_SECONDS = 0.8
_SHOT_END_GUARD_FRAMES = 2


def _safe_source_end(
    candidate: Mapping[str, Any],
    *,
    source_in: float,
    source_out: float,
    fps: float,
) -> dict[str, Any]:
    """Describe a safe end when a used range barely enters the next shot.

    A 4--8 second virtual clip may legitimately merge short source shots.  If
    the requested range ends less than half a second after an internal hard
    cut, that last fragment reads as an accidental flash.  Keep the semantic
    selection intact, stop two frames before the cut and let the renderer hold
    the last clean frame for the small remainder.
    """

    if fps <= 0:
        raise SelectionError("fps must be positive")
    raw_cuts = candidate.get("shot_cuts") or ()
    cuts: list[float] = []
    if isinstance(raw_cuts, Sequence) and not isinstance(raw_cuts, (str, bytes)):
        for raw in raw_cuts:
            try:
                cut = float(raw)
            except (TypeError, ValueError):
                continue
            if source_in < cut <= source_out:
                cuts.append(cut)
    if not cuts:
        return {}
    shot_end = max(cuts)
    tail = source_out - shot_end
    if tail >= _MICRO_TAIL_SECONDS:
        return {}
    safe_end = shot_end - (_SHOT_END_GUARD_FRAMES / fps)
    if safe_end <= source_in:
        return {}
    return {
        "safe_source_out": round(safe_end, 6),
        "shot_end": round(shot_end, 6),
        "safe_end_reason": "micro_tail_after_shot_cut",
        "held_tail_seconds": round(source_out - safe_end, 6),
    }


def rank_beats(
    beats: Sequence[Mapping[str, Any]],
    briefs: Sequence[Mapping[str, Any]],
    clips: list[dict],
    vectors: np.ndarray,
    *,
    top_k: int = 30,
    duration_tolerance: float = 1.0 / 25.0,
    production: bool = False,
    output_fps: float = 25.0,
) -> list[list[dict]]:
    """Retrieve duration-compatible candidates for every narration beat."""

    if len(beats) != len(briefs):
        raise SelectionError("beat/brief count mismatch")
    output: list[list[dict]] = []
    for beat, brief in zip(beats, briefs):
        if str(beat.get("slot_id")) != str(brief.get("slot_id")):
            raise SelectionError("beat/brief slot order mismatch")
        duration = float(beat["end"]) - float(beat["start"])
        try:
            _render_start, _render_end, render_frames = interval_frame_span(
                float(beat["start"]), float(beat["end"]), output_fps
            )
        except ValueError as exc:
            raise SelectionError(f"invalid frame interval for {beat.get('slot_id')}") from exc
        query = str(brief.get("query_en") or "").strip()
        if not query:
            raise SelectionError(f"empty visual query for {beat.get('slot_id')}")
        # Ask for a wider pool before the duration filter. Some detected shots
        # are valid 4s clips but cannot cover a beat near the 8s ceiling.
        ranked = rank_for_query(
            query, clips, vectors, top_k=min(len(clips), max(top_k * 3, top_k))
        )
        compatible = []
        for row in ranked:
            if bool(row.get("overlay_blocked")) or bool(row.get("unresolved_overlay")):
                continue
            if production:
                try:
                    source_fps = float(row["fps"])
                    clean_start = int(row["clean_frame_start"])
                    clean_end = int(row["clean_frame_end"])
                except (KeyError, TypeError, ValueError):
                    continue
                if source_fps + 1e-6 < output_fps:
                    continue
                required_source_frames = int(
                    np.ceil(render_frames * source_fps / output_fps - 1e-9)
                )
                if clean_end - clean_start < required_source_frames:
                    continue
                analysis = row.get("overlay_analysis")
                if not isinstance(analysis, Mapping) or analysis.get("status") not in {
                    "clean", "resolved"
                }:
                    continue
            elif (
                float(row["source_out"]) - float(row["source_in"])
                < duration - duration_tolerance
            ):
                continue
            compatible.append(row)
            if len(compatible) >= top_k:
                break
        if not compatible:
            raise SelectionError(
                f"no duration-compatible donor clip for {beat.get('slot_id')} ({duration:.3f}s)"
            )
        output.append(compatible)
    return output if production else choose_non_overlapping(output)


def build_judge_jobs(
    beats: Sequence[Mapping[str, Any]],
    briefs: Sequence[Mapping[str, Any]],
    candidates: Sequence[Sequence[Mapping[str, Any]]],
) -> list[tuple[dict, dict, list[dict]]]:
    if not (len(beats) == len(briefs) == len(candidates)):
        raise SelectionError("judge input count mismatch")
    return [
        (dict(beat), dict(brief), [dict(row) for row in rows])
        for beat, brief, rows in zip(beats, briefs, candidates)
    ]


def _candidate_order(candidates: Sequence[Mapping[str, Any]], chosen_index: Any) -> list[dict]:
    try:
        chosen = int(chosen_index)
    except (TypeError, ValueError):
        chosen = 0
    indices = list(range(len(candidates)))
    if 0 <= chosen < len(indices):
        indices.remove(chosen)
        indices.insert(0, chosen)
    return [dict(candidates[index]) for index in indices]


def assemble_timeline(
    beats: Sequence[Mapping[str, Any]],
    briefs: Sequence[Mapping[str, Any]],
    candidates: Sequence[Sequence[Mapping[str, Any]]],
    judge_results: Sequence[Mapping[str, Any]],
    *,
    fps: float = 25.0,
    production: bool = False,
    beam_width: int = 256,
) -> dict[str, Any]:
    """Resolve judge choices globally and return a renderer-compatible timeline."""

    if not (len(beats) == len(briefs) == len(candidates) == len(judge_results)):
        raise SelectionError("selection input count mismatch")
    if production:
        return _assemble_production_timeline(
            beats,
            briefs,
            candidates,
            judge_results,
            fps=fps,
            beam_width=beam_width,
        )
    used_full_ranges: list[dict] = []
    slots: list[dict[str, Any]] = []
    for beat, brief, rows, judge in zip(beats, briefs, candidates, judge_results):
        duration = float(beat["end"]) - float(beat["start"])
        selected: dict | None = None
        selected_rank = -1
        for rank, candidate in enumerate(_candidate_order(rows, judge.get("chosen_index"))):
            if bool(candidate.get("overlay_blocked")):
                continue
            available = float(candidate["source_out"]) - float(candidate["source_in"])
            if available + 1.0 / fps < duration:
                continue
            if any(ranges_overlap(candidate, old) for old in used_full_ranges):
                continue
            selected = candidate
            selected_rank = rank
            break
        if selected is None:
            raise SelectionError(f"all candidates collide for {beat.get('slot_id')}")

        # The renderer consumes exactly the beat duration from source_in. Store
        # that exact used interval, while retaining the detected parent range for
        # provenance and stricter duplicate accounting.
        used_full_ranges.append(dict(selected))
        source_in = float(selected["source_in"])
        selected_clip = dict(selected)
        selected_clip["detected_source_out"] = float(selected["source_out"])
        selected_clip["source_in"] = round(source_in, 6)
        selected_clip["source_out"] = round(source_in + duration, 6)
        selected_clip.update(
            _safe_source_end(
                selected,
                source_in=source_in,
                source_out=float(selected_clip["source_out"]),
                fps=float(fps),
            )
        )

        status = str(judge.get("status") or "needs_review")
        verdict = str(judge.get("verdict") or "generic")
        reason = str(judge.get("reason") or "")
        if selected_rank:
            status = "needs_review" if status == "resolved" else status
            reason = (reason + "; global no-overlap fallback").strip("; ")
        slots.append(
            {
                "slot_id": str(beat["slot_id"]),
                "start": round(float(beat["start"]), 6),
                "end": round(float(beat["end"]), 6),
                "duration": round(duration, 6),
                "text": str(beat.get("text") or ""),
                "visual_brief": dict(brief),
                "selected_clip": selected_clip,
                "verdict": verdict,
                "confidence": float(judge.get("confidence") or 0.0),
                "reason": reason,
                "status": status,
                "judge_error": judge.get("judge_error"),
            }
        )

    return {
        "schema_version": 1,
        "fps": float(fps),
        "narration_duration": round(float(beats[-1]["end"]), 6),
        "slots": slots,
    }


def _assemble_production_timeline(
    beats: Sequence[Mapping[str, Any]],
    briefs: Sequence[Mapping[str, Any]],
    candidates: Sequence[Sequence[Mapping[str, Any]]],
    judge_results: Sequence[Mapping[str, Any]],
    *,
    fps: float,
    beam_width: int,
) -> dict[str, Any]:
    """Build schema v2 without unjudged fallbacks, holds or implicit retiming."""

    if fps <= 0:
        raise SelectionError("fps must be positive")
    enriched_per_slot: list[list[dict]] = []
    for beat, rows, judge in zip(beats, candidates, judge_results):
        if judge.get("status") == "unresolved" or judge.get("judge_error"):
            raise SelectionError(f"strict judge unresolved for {beat.get('slot_id')}")
        evaluations = judge.get("evaluations")
        if not isinstance(evaluations, list) or len(evaluations) != len(rows):
            raise SelectionError(f"strict judge audit missing for {beat.get('slot_id')}")
        beat_start = float(beat["start"])
        beat_end = float(beat["end"])
        duration = beat_end - beat_start
        try:
            render_start_frame, render_end_frame, render_frames = interval_frame_span(
                beat_start, beat_end, fps
            )
        except ValueError as exc:
            raise SelectionError(f"non-frame-aligned beat {beat.get('slot_id')}") from exc
        if render_frames <= 0 or abs(render_frames / fps - duration) > 1.0 / fps + 1e-9:
            raise SelectionError(f"non-frame-aligned beat {beat.get('slot_id')}")
        enriched: list[dict] = []
        for index, (raw, evaluation) in enumerate(zip(rows, evaluations)):
            if int(evaluation.get("index", -1)) != index:
                raise SelectionError(f"judge candidate order mismatch for {beat.get('slot_id')}")
            candidate = dict(raw)
            candidate["judge_verdict"] = str(evaluation.get("verdict") or "wrong")
            candidate["judge_confidence"] = float(evaluation.get("confidence") or 0.0)
            candidate["judge_reason"] = str(evaluation.get("reason") or "")[:500]
            try:
                candidate_render_frames = int(candidate["render_frames"])
            except (KeyError, TypeError, ValueError):
                continue
            if candidate_render_frames != render_frames:
                continue
            try:
                validate_exact_candidate_contract(
                    candidate,
                    output_fps=fps,
                    required_render_frames=render_frames,
                )
            except BroadLazyAdapterError:
                continue
            # The vision judge saw this exact half-open source range. Never
            # reset it to the wider clean-span start during timeline assembly.
            candidate.update(
                {
                    "render_start_frame": render_start_frame,
                    "render_end_frame": render_end_frame,
                    "fill_strategy": "none",
                    "retime_factor": 1.0,
                }
            )
            enriched.append(candidate)
        if not enriched:
            raise SelectionError(f"no frame-exact judged candidate for {beat.get('slot_id')}")
        enriched_per_slot.append(enriched)
    try:
        selected = global_constrained_select(
            enriched_per_slot,
            beam_width=beam_width,
            require_judged=True,
        )
    except SelectionOptimizationError as exc:
        raise SelectionError(str(exc)) from exc

    slots: list[dict[str, Any]] = []
    for beat, brief, selected_clip, all_candidates in zip(
        beats, briefs, selected, enriched_per_slot
    ):
        duration = float(beat["end"]) - float(beat["start"])
        slots.append(
            {
                "slot_id": str(beat["slot_id"]),
                "start": round(float(beat["start"]), 6),
                "end": round(float(beat["end"]), 6),
                "duration": round(duration, 6),
                "render_frames": int(selected_clip["render_frames"]),
                "render_start_frame": int(selected_clip["render_start_frame"]),
                "render_end_frame": int(selected_clip["render_end_frame"]),
                "text": str(beat.get("text") or ""),
                "visual_brief": dict(brief),
                "selected_clip": dict(selected_clip),
                "verdict": selected_clip["judge_verdict"],
                "confidence": selected_clip["judge_confidence"],
                "reason": selected_clip.get("judge_reason", ""),
                "status": "resolved",
                "selection_provenance": {
                    "method": "global_constrained_beam_v2",
                    "candidate_ids": [str(row.get("clip_id")) for row in all_candidates],
                    "candidate_scores": [
                        {
                            "clip_id": str(row.get("clip_id")),
                            "retrieval": float(row.get("retrieval_score") or 0.0),
                            "judge_verdict": row.get("judge_verdict"),
                            "judge_confidence": float(row.get("judge_confidence") or 0.0),
                        }
                        for row in all_candidates
                    ],
                },
            }
        )
    return {
        "schema_version": 2,
        "contract": "production_frame_exact",
        "fps": float(fps),
        "narration_duration": round(float(beats[-1]["end"]), 6),
        "slots": slots,
    }


__all__ = [
    "SelectionError",
    "assemble_timeline",
    "build_judge_jobs",
    "rank_beats",
]
