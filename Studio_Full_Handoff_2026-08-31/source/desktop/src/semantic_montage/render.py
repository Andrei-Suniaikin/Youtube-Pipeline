"""Video-only FFmpeg render foundation for semantic donor montage.

The render lane has one non-negotiable invariant: donor audio never enters the
graph.  Every selected source range is materialized with an explicit video map
and ``-an``.  The concat file is probed to prove that it is video-only, then the
separate narration/background lane from :mod:`semantic_montage.audio` creates
the sole final audio stream.

Inputs and results are JSON-friendly dictionaries.  The accepted timeline is
compatible with ``contracts/timeline.schema.json`` from the implementation
bundle and tolerates direct ``slot.transform`` objects for early MVP callers.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .audio import (
    AudioMixConfig,
    AudioPlanError,
    build_final_mix_command,
    build_layered_final_mix_command,
    build_looped_background_command,
    build_segmented_music_stem_command,
    build_sfx_stem_command,
    command_maps_input_audio,
)
from .sound import SoundPlanError, validate_sound_plan
from .common import canonical_hash, interval_frame_span


DETERMINISTIC_TRANSFORM_POLICY_VERSION = "portable-deterministic-transform-v1"
EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION = "exact-selected-pts-render-v1"
_EXACT_PTS_CONTRACT = "exact_integer_pts_window_v1"
_PORTABLE_TRANSFORM_MARKERS = (
    "source_asset_id",
    "source_sha256",
    "candidate_range_hash",
)


class RenderError(RuntimeError):
    """Base error for a rejected or failed semantic-montage render."""


class CommandExecutionError(RenderError):
    """An FFmpeg/ffprobe process returned a non-zero exit status."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str):
        self.argv = list(argv)
        self.returncode = int(returncode)
        self.stderr = stderr
        tail = "\n".join((stderr or "").strip().splitlines()[-20:])
        super().__init__(f"Command failed with exit code {returncode}: {tail}")


@dataclass(frozen=True)
class RenderConfig:
    width: int = 1920
    height: int = 1080
    fps: float = 25.0
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = 20
    max_workers: int = 4
    # Remove broadcaster/UI overlays before any scale/pan transform.  The
    # asymmetric default is measured from the supplied donors: their overlays
    # occupy the upper band, so keeping the bottom avoids needless content
    # loss.  ``edge_cleanup_percent`` remains a uniform compatibility override
    # (and setting it to 0 disables cleanup completely).
    edge_cleanup_percent: float | None = None
    edge_cleanup_side_percent: float = 0.108
    edge_cleanup_top_percent: float = 0.215
    edge_cleanup_bottom_percent: float = 0.0
    # Strict production mode accepts only frame-indexed clean source contracts.
    # It never fills a shortage with cloned frames or retiming and it never
    # applies the legacy blanket crop without detector provenance.
    production_strict: bool = False
    strict_overlay_min_confidence: float = 0.70
    shot_end_guard_frames: int = 2
    clip_timeout_seconds: float = 900.0
    final_timeout_seconds: float = 3_600.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "RenderConfig":
        if not value:
            return cls()
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise RenderError(f"Unknown render config fields: {', '.join(unknown)}")
        return cls(**dict(value))

    def validate(self) -> None:
        if self.width != 1920 or self.height != 1080:
            raise RenderError("MVP output contract is fixed at 1920x1080")
        if not math.isfinite(float(self.fps)) or float(self.fps) <= 0:
            raise RenderError("fps must be positive")
        if not 0 <= int(self.crf) <= 51:
            raise RenderError("crf must be in [0, 51]")
        if int(self.max_workers) <= 0:
            raise RenderError("max_workers must be positive")
        cleanup_values = {
            "edge_cleanup_side_percent": self.edge_cleanup_side_percent,
            "edge_cleanup_top_percent": self.edge_cleanup_top_percent,
            "edge_cleanup_bottom_percent": self.edge_cleanup_bottom_percent,
        }
        if self.edge_cleanup_percent is not None:
            cleanup_values["edge_cleanup_percent"] = self.edge_cleanup_percent
        normalized_cleanup: dict[str, float] = {}
        for name, raw in cleanup_values.items():
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise RenderError(f"{name} must be in [0, 0.25]") from exc
            if isinstance(raw, bool) or not math.isfinite(value) or not 0.0 <= value <= 0.25:
                raise RenderError(f"{name} must be in [0, 0.25]")
            normalized_cleanup[name] = value
        if self.edge_cleanup_percent is None:
            if 2.0 * normalized_cleanup["edge_cleanup_side_percent"] >= 0.5:
                raise RenderError("edge cleanup must retain more than half the source width")
            if (
                normalized_cleanup["edge_cleanup_top_percent"]
                + normalized_cleanup["edge_cleanup_bottom_percent"]
                >= 0.5
            ):
                raise RenderError("edge cleanup must retain more than half the source height")
        if not isinstance(self.production_strict, bool):
            raise RenderError("production_strict must be boolean")
        try:
            overlay_confidence = float(self.strict_overlay_min_confidence)
        except (TypeError, ValueError) as exc:
            raise RenderError("strict_overlay_min_confidence must be in [0, 1]") from exc
        if (
            isinstance(self.strict_overlay_min_confidence, bool)
            or not math.isfinite(overlay_confidence)
            or not 0.0 <= overlay_confidence <= 1.0
        ):
            raise RenderError("strict_overlay_min_confidence must be in [0, 1]")
        if (
            isinstance(self.shot_end_guard_frames, bool)
            or int(self.shot_end_guard_frames) != self.shot_end_guard_frames
            or not 0 <= int(self.shot_end_guard_frames) <= 5
        ):
            raise RenderError("shot_end_guard_frames must be an integer in [0, 5]")
        if float(self.clip_timeout_seconds) <= 0 or float(self.final_timeout_seconds) <= 0:
            raise RenderError("render timeouts must be positive")


ProgressCallback = Callable[[dict[str, Any]], None]


def _emit(progress: ProgressCallback | None, **event: Any) -> None:
    if progress is not None:
        progress(dict(event))


def _float(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RenderError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise RenderError(f"{name} must be finite")
    return result


def _slot_duration(
    slot: Mapping[str, Any],
    *,
    fps: float | None = None,
) -> float:
    if slot.get("duration") is not None:
        duration = _float(slot["duration"], "slot.duration")
        if slot.get("start") is not None and slot.get("end") is not None:
            by_bounds = _float(slot["end"], "slot.end") - _float(slot["start"], "slot.start")
            if abs(duration - by_bounds) > 0.020:
                raise RenderError(
                    f"slot {slot.get('slot_id', '?')} duration disagrees with start/end"
                )
    elif slot.get("start") is not None and slot.get("end") is not None:
        duration = _float(slot["end"], "slot.end") - _float(slot["start"], "slot.start")
    else:
        raise RenderError("slot requires duration or start/end")
    if fps is None:
        if duration < 4.0 - 1e-6 or duration > 8.0 + 1e-6:
            raise RenderError(
                f"slot {slot.get('slot_id', '?')} duration {duration:.6f}s "
                "is outside 4-8s"
            )
        return duration

    fps_value = _float(fps, "timeline.fps")
    if fps_value <= 0:
        raise RenderError("timeline.fps must be positive")
    start = _float(slot.get("start", 0.0), "slot.start")
    end = _float(slot.get("end", start + duration), "slot.end")
    try:
        _start_frame, _end_frame, render_frames = interval_frame_span(
            start, end, fps_value
        )
    except ValueError as exc:
        raise RenderError(
            f"slot {slot.get('slot_id', '?')} has invalid frame boundaries"
        ) from exc
    minimum_frames = int(math.ceil(4.0 * fps_value - 1e-9))
    maximum_frames = int(math.ceil(8.0 * fps_value - 1e-9))
    if render_frames < minimum_frames or render_frames > maximum_frames:
        raise RenderError(
            f"slot {slot.get('slot_id', '?')} frame span {render_frames} is outside "
            f"{minimum_frames}-{maximum_frames} frames at {fps_value:.9f} fps"
        )
    return duration


def _selected_clip(slot: Mapping[str, Any]) -> Mapping[str, Any]:
    clip = slot.get("selected_clip")
    if clip is None and slot.get("source_path") is not None:
        clip = slot
    if not isinstance(clip, Mapping):
        raise RenderError(f"slot {slot.get('slot_id', '?')} has no selected clip")
    return clip


def _source_bounds(clip: Mapping[str, Any]) -> tuple[str, float, float]:
    source_path = clip.get("source_path") or clip.get("path")
    if not source_path:
        raise RenderError("selected clip requires source_path")
    source_in = clip.get("source_in", clip.get("in", clip.get("start")))
    source_out = clip.get("source_out", clip.get("out", clip.get("end")))
    if source_in is None or source_out is None:
        raise RenderError("selected clip requires source_in and source_out")
    start = _float(source_in, "selected_clip.source_in")
    end = _float(source_out, "selected_clip.source_out")
    if start < 0 or end <= start:
        raise RenderError("selected clip source bounds are invalid")
    return str(Path(str(source_path)).expanduser()), start, end


def _optional_source_end(clip: Mapping[str, Any], name: str) -> float | None:
    value = clip.get(name)
    if value is None:
        return None
    result = _float(value, f"selected_clip.{name}")
    if result < 0:
        raise RenderError(f"selected_clip.{name} must be non-negative")
    return result


_EDGE_CLEANUP_KEYS = ("left", "right", "top", "bottom")


def _edge_cleanup_override(
    slot: Mapping[str, Any],
    clip: Mapping[str, Any],
) -> dict[str, float] | None:
    """Return a validated slot/clip crop override, with slot taking precedence."""

    raw = slot.get("edge_cleanup_margins")
    field_name = "slot.edge_cleanup_margins"
    if raw is None:
        raw = clip.get("edge_cleanup_margins")
        field_name = "selected_clip.edge_cleanup_margins"
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise RenderError(f"{field_name} must be a mapping")

    missing = [key for key in _EDGE_CLEANUP_KEYS if key not in raw]
    unknown = sorted(str(key) for key in raw if key not in _EDGE_CLEANUP_KEYS)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown: {', '.join(unknown)}")
        raise RenderError(
            f"{field_name} requires left/right/top/bottom ({'; '.join(details)})"
        )

    margins: dict[str, float] = {}
    for key in _EDGE_CLEANUP_KEYS:
        raw_value = raw[key]
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise RenderError(f"{field_name}.{key} must be in [0, 0.25]") from exc
        if (
            isinstance(raw_value, bool)
            or not math.isfinite(value)
            or not 0.0 <= value <= 0.25
        ):
            raise RenderError(f"{field_name}.{key} must be in [0, 0.25]")
        margins[key] = value

    if margins["left"] + margins["right"] >= 0.5:
        raise RenderError(f"{field_name} must retain more than half the source width")
    if margins["top"] + margins["bottom"] >= 0.5:
        raise RenderError(f"{field_name} must retain more than half the source height")
    return margins


def _config_edge_cleanup_margins(config: RenderConfig) -> dict[str, float]:
    if config.edge_cleanup_percent is not None:
        uniform = float(config.edge_cleanup_percent)
        return {key: uniform for key in _EDGE_CLEANUP_KEYS}
    return {
        "left": float(config.edge_cleanup_side_percent),
        "right": float(config.edge_cleanup_side_percent),
        "top": float(config.edge_cleanup_top_percent),
        "bottom": float(config.edge_cleanup_bottom_percent),
    }


def _strict_time_base(value: Any) -> Fraction:
    try:
        if isinstance(value, Mapping):
            result = Fraction(
                int(value.get("numerator", value.get("num"))),
                int(value.get("denominator", value.get("den"))),
            )
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) != 2:
                raise ValueError
            result = Fraction(int(value[0]), int(value[1]))
        else:
            result = Fraction(str(value))
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise RenderError("strict clean span requires a positive rational time_base") from exc
    if result <= 0:
        raise RenderError("strict clean span requires a positive rational time_base")
    return result


def _strict_integer(value: Any, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise RenderError(f"{name} must be an integer")
    try:
        result = int(value)
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RenderError(f"{name} must be an integer") from exc
    if not math.isfinite(numeric) or numeric != result:
        raise RenderError(f"{name} must be an integer")
    if minimum is not None and result < minimum:
        raise RenderError(f"{name} must be at least {minimum}")
    return result


def _fraction_document(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def build_exact_selected_pts_render_contract(
    slot: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Authenticate a candidate's persisted half-open exact-PTS selection.

    Legacy candidates without the complete exact-PTS marker set return
    ``None``.  Once all markers are present, every frame, PTS, time-base and
    source-time binding is validated without FPS inference or path access.
    """

    clip = _selected_clip(slot)
    frame_contract = clip.get("frame_contract")
    exact = (
        frame_contract.get("exact_pts_contract")
        if isinstance(frame_contract, Mapping)
        else None
    )
    markers = (
        isinstance(exact, Mapping),
        clip.get("source_frame_start") is not None,
        clip.get("source_frame_end") is not None,
    )
    if not all(markers):
        return None
    assert isinstance(exact, Mapping)
    exact_row = dict(exact)
    if (
        exact_row.get("schema_version") != 1
        or exact_row.get("contract") != _EXACT_PTS_CONTRACT
        or exact_row.get("addressing") != "integer_pts_half_open"
        or exact_row.get("fps_inference_used") is not False
    ):
        raise RenderError("selected exact PTS lineage contract is incompatible")
    exact_hash = _sha256_identity(
        exact_row.get("content_hash"), "frame_contract.exact_pts_contract.content_hash"
    )
    exact_base = {
        key: value
        for key, value in exact_row.items()
        if key not in {"content_hash", "window_id"}
    }
    if canonical_hash(exact_base) != exact_hash:
        raise RenderError("selected exact PTS lineage hash mismatch")
    if exact_row.get("window_id") != f"pts-window-{exact_hash[:24]}":
        raise RenderError("selected exact PTS lineage id mismatch")

    raw_points = exact_row.get("frame_pts")
    if not isinstance(raw_points, Sequence) or isinstance(raw_points, (str, bytes)):
        raise RenderError("selected exact PTS lineage requires frame_pts")
    points = [
        _strict_integer(value, "frame_contract.exact_pts_contract.frame_pts")
        for value in raw_points
    ]
    frame_count = _strict_integer(
        exact_row.get("frame_count"),
        "frame_contract.exact_pts_contract.frame_count",
        minimum=1,
    )
    source_start_pts = _strict_integer(
        exact_row.get("start_pts"), "frame_contract.exact_pts_contract.start_pts"
    )
    source_end_pts = _strict_integer(
        exact_row.get("end_pts"), "frame_contract.exact_pts_contract.end_pts"
    )
    if (
        not points
        or len(points) != frame_count
        or source_start_pts != points[0]
        or source_end_pts <= points[-1]
        or any(right <= left for left, right in zip(points, points[1:]))
    ):
        raise RenderError("selected exact PTS lineage frame vector is invalid")
    tb = _strict_time_base(exact_row.get("time_base"))
    tb_document = {"numerator": tb.numerator, "denominator": tb.denominator}

    source_frame_start = _strict_integer(
        clip.get("source_frame_start"), "selected_clip.source_frame_start", minimum=0
    )
    source_frame_end = _strict_integer(
        clip.get("source_frame_end"), "selected_clip.source_frame_end", minimum=1
    )
    if not 0 <= source_frame_start < source_frame_end <= len(points):
        raise RenderError("selected exact PTS frame bounds escape their lineage")
    start_pts = points[source_frame_start]
    end_pts = (
        points[source_frame_end]
        if source_frame_end < len(points)
        else source_end_pts
    )
    selected_points = points[source_frame_start:source_frame_end]
    source_frames = source_frame_end - source_frame_start
    start_time = Fraction(start_pts) * tb
    end_time = Fraction(end_pts) * tb
    duration = end_time - start_time
    selected_base = {
        "schema_version": 1,
        "contract": _EXACT_PTS_CONTRACT,
        "time_base": tb_document,
        "source_frame_start": source_frame_start,
        "source_frame_end": source_frame_end,
        "frame_count": source_frames,
        "frame_pts": selected_points,
        "start_pts": start_pts,
        "end_pts": end_pts,
        "start_time": _fraction_document(start_time),
        "end_time": _fraction_document(end_time),
        "duration": _fraction_document(duration),
        "addressing": "integer_pts_half_open",
        "fps_inference_used": False,
    }
    selected_exact_pts_hash = canonical_hash(selected_base)
    source_in = _float(
        clip.get("source_in", clip.get("in", clip.get("start"))),
        "selected_clip.source_in",
    )
    source_out = _float(
        clip.get("source_out", clip.get("out", clip.get("end"))),
        "selected_clip.source_out",
    )
    if (
        abs(source_in - float(start_time)) > 1e-9
        or abs(source_out - float(end_time)) > 1e-9
    ):
        raise RenderError("selected source bounds disagree with exact PTS frame bounds")

    gate_range = {
        "source_in": round(source_in, 9),
        "source_out": round(source_out, 9),
        "source_frame_start": source_frame_start,
        "source_frame_end": source_frame_end,
        "source_frames": source_frames,
        "start_pts": start_pts,
        "end_pts": end_pts,
        "time_base": tb_document,
        "exact_pts_hash": selected_exact_pts_hash,
    }
    persisted = clip.get("exact_render_span")
    if persisted is not None:
        if not isinstance(persisted, Mapping):
            raise RenderError("selected_clip.exact_render_span must be a mapping")
        persisted_row = dict(persisted)
        if persisted_row.get("contract") != EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION:
            raise RenderError("selected_clip.exact_render_span contract is stale")
        for key, expected in gate_range.items():
            if persisted_row.get(key) != expected:
                raise RenderError(
                    f"selected_clip.exact_render_span.{key} is tampered or stale"
                )

    return {
        "contract": EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION,
        **gate_range,
        "clean_frame_start": source_frame_start,
        "clean_frame_end": source_frame_end,
        "source_frames_used": source_frames,
        "decode_source_in": float(start_time),
        "decode_source_out": float(end_time),
        "decode_duration": float(duration),
    }


def _frame_range_overlaps(start: int, end: int, zone: Mapping[str, Any]) -> bool:
    try:
        zone_start = int(zone["start_frame"])
        zone_end = int(zone["end_frame"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RenderError("strict transition/overlay zone requires integer frame bounds") from exc
    if zone_start < 0 or zone_end <= zone_start:
        raise RenderError("strict transition/overlay zone has invalid frame bounds")
    return start < zone_end and end > zone_start


def _strict_overlay_policy(
    normalized: Mapping[str, Any],
    *,
    selected_start_frame: int,
    selected_end_frame: int,
    config: RenderConfig,
) -> tuple[dict[str, float], dict[str, Any]]:
    if normalized.get("overlay_blocked"):
        raise RenderError(
            f"slot {normalized['slot_id']} is blocked by overlay analysis: "
            f"{normalized.get('overlay_blocked_reason') or 'contamination'}"
        )
    raw = normalized.get("overlay_analysis")
    if not isinstance(raw, Mapping):
        raise RenderError(
            f"slot {normalized['slot_id']} strict production requires overlay_analysis"
        )
    detector = raw.get("detector")
    if not isinstance(detector, Mapping):
        raise RenderError("strict overlay_analysis requires detector metadata")
    detector_name = str(detector.get("name") or "").strip()
    detector_version = str(detector.get("version") or "").strip()
    if not detector_name or not detector_version:
        raise RenderError("strict overlay detector name/version are required")
    confidence = _float(raw.get("confidence"), "overlay_analysis.confidence")
    if not 0.0 <= confidence <= 1.0:
        raise RenderError("overlay_analysis.confidence must be in [0, 1]")
    if confidence + 1e-9 < float(config.strict_overlay_min_confidence):
        raise RenderError(
            f"slot {normalized['slot_id']} overlay confidence {confidence:.3f} is below "
            f"strict minimum {float(config.strict_overlay_min_confidence):.3f}"
        )
    excluded = raw.get("excluded_ranges") or ()
    if not isinstance(excluded, Sequence) or isinstance(excluded, (str, bytes)):
        raise RenderError("overlay_analysis.excluded_ranges must be a list")
    collisions = [
        dict(zone)
        for zone in excluded
        if isinstance(zone, Mapping)
        and _frame_range_overlaps(selected_start_frame, selected_end_frame, zone)
    ]
    if collisions:
        raise RenderError(
            f"slot {normalized['slot_id']} selected clean frames collide with overlay contamination"
        )
    action = str(raw.get("action") or "").strip()
    crop_actions = {"crop", "crop_and_exclude", "crop_and_inpaint"}
    inpaint_actions = {"inpaint_required", "crop_and_inpaint"}
    if action not in {
        "none",
        "crop",
        "exclude_reselect",
        "crop_and_exclude",
        "inpaint_required",
        "crop_and_inpaint",
    }:
        raise RenderError("strict overlay_analysis has an unsupported action")
    if action in inpaint_actions and not normalized.get("inpaint_applied"):
        raise RenderError(
            f"slot {normalized['slot_id']} requires completed inpaint or reselection"
        )
    if action in crop_actions:
        if raw.get("crop_safe") is not True:
            raise RenderError("strict detector crop must be explicitly marked crop_safe")
        margins = _edge_cleanup_override(
            {"edge_cleanup_margins": raw.get("crop_margins")}, {}
        )
        if margins is None or not any(margins.values()):
            raise RenderError("strict detector crop requires non-zero crop_margins")
    else:
        margins = {key: 0.0 for key in _EDGE_CLEANUP_KEYS}
    metadata = {
        "detector": {**dict(detector), "name": detector_name, "version": detector_version},
        "confidence": confidence,
        "action": action,
        "crop_safe": raw.get("crop_safe") is True,
        "crop_margins": dict(margins),
        "excluded_range_count": len(excluded),
        "inpaint_applied": bool(normalized.get("inpaint_applied")),
    }
    return margins, metadata


def _strict_clean_source_contract(
    normalized: Mapping[str, Any],
    *,
    output_frames: int,
    config: RenderConfig,
) -> dict[str, Any]:
    """Select exactly ``output_frames`` from one clean contiguous span."""

    source_fps = _float(normalized.get("source_fps"), "selected_clip.fps")
    if source_fps + 0.001 < float(config.fps):
        raise RenderError(
            f"slot {normalized['slot_id']} strict source fps {source_fps:.6f} is below "
            f"render fps {float(config.fps):.6f}; duplication is forbidden"
        )
    top_level_tb = normalized.get("time_base")
    raw_spans = normalized.get("clean_spans") or ()
    if not isinstance(raw_spans, Sequence) or isinstance(raw_spans, (str, bytes)):
        raise RenderError("selected_clip.clean_spans must be a list")
    candidates: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_spans):
        if not isinstance(raw, Mapping):
            raise RenderError(f"selected_clip.clean_spans[{index}] must be a mapping")
        try:
            start_frame = int(raw["start_frame"])
            end_frame = int(raw["end_frame"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RenderError("strict clean span requires integer frame bounds") from exc
        if start_frame < 0 or end_frame <= start_frame:
            raise RenderError("strict clean span has invalid frame bounds")
        tb = _strict_time_base(raw.get("time_base", top_level_tb))
        frame_pts_raw = raw.get("frame_pts")
        frame_pts = [int(value) for value in frame_pts_raw] if frame_pts_raw is not None else []
        if frame_pts and (
            len(frame_pts) != end_frame - start_frame
            or any(right <= left for left, right in zip(frame_pts, frame_pts[1:]))
        ):
            raise RenderError("strict clean span frame_pts do not match its frame bounds")
        start_pts = int(raw.get("start_pts", frame_pts[0] if frame_pts else 0))
        end_pts = int(raw.get("end_pts", 0))
        start_time = _float(
            raw.get("start_time", float(start_pts * tb)),
            "clean_span.start_time",
        )
        end_time = _float(
            raw.get("end_time", float(end_pts * tb)),
            "clean_span.end_time",
        )
        if end_time <= start_time:
            raise RenderError("strict clean span has invalid time bounds")
        candidates.append(
            {
                "span_id": str(raw.get("span_id") or f"clean-{start_frame}-{end_frame}"),
                "start_frame": start_frame,
                "end_frame": end_frame,
                "frame_count": end_frame - start_frame,
                "start_pts": start_pts,
                "end_pts": end_pts,
                "frame_pts": frame_pts,
                "start_time": start_time,
                "end_time": end_time,
                "time_base": tb,
            }
        )

    # Canonical selector contract: allow a compact single span without copying
    # the full PTS vector into every timeline slot.
    canonical = (
        normalized.get("clean_frame_start"),
        normalized.get("clean_frame_end"),
        normalized.get("clean_source_in"),
        normalized.get("clean_source_out"),
    )
    if all(value is not None for value in canonical):
        start_frame, end_frame = int(canonical[0]), int(canonical[1])
        tb = _strict_time_base(top_level_tb)
        start_time, end_time = float(canonical[2]), float(canonical[3])
        if end_frame <= start_frame or end_time <= start_time:
            raise RenderError("canonical strict clean span has invalid bounds")
        if not any(
            item["start_frame"] == start_frame and item["end_frame"] == end_frame
            for item in candidates
        ):
            candidates.append(
                {
                    "span_id": f"clean-{start_frame:09d}-{end_frame:09d}",
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "frame_count": end_frame - start_frame,
                    "start_pts": int(round(start_time / float(tb))),
                    "end_pts": int(round(end_time / float(tb))),
                    "frame_pts": [],
                    "start_time": start_time,
                    "end_time": end_time,
                    "time_base": tb,
                }
            )
    if not candidates:
        raise RenderError(
            f"slot {normalized['slot_id']} strict production requires frame-indexed clean spans"
        )

    exact_selection = build_exact_selected_pts_render_contract(normalized)
    if exact_selection is not None:
        selected_start_frame = int(exact_selection["source_frame_start"])
        selected_end_frame = int(exact_selection["source_frame_end"])
        source_frames_used = int(exact_selection["source_frames_used"])
        tb = _strict_time_base(exact_selection["time_base"])
        start_pts = int(exact_selection["start_pts"])
        end_pts = int(exact_selection["end_pts"])
        decode_source_in = float(exact_selection["decode_source_in"])
        decode_source_out = float(exact_selection["decode_source_out"])
        decode_duration = float(exact_selection["decode_duration"])
        containing = [
            item
            for item in candidates
            if item["start_frame"] <= selected_start_frame
            and selected_end_frame <= item["end_frame"]
            and item["time_base"] == tb
        ]
        if not containing:
            raise RenderError(
                f"slot {normalized['slot_id']} exact selected PTS range is not clean"
            )
        selected_clean_span = min(
            containing,
            key=lambda item: (item["frame_count"], item["start_frame"]),
        )
        clean_points = selected_clean_span["frame_pts"]
        if clean_points:
            clean_offset = selected_start_frame - int(
                selected_clean_span["start_frame"]
            )
            clean_end_offset = clean_offset + source_frames_used
            clean_start_pts = clean_points[clean_offset]
            clean_end_pts = (
                clean_points[clean_end_offset]
                if clean_end_offset < len(clean_points)
                else int(selected_clean_span["end_pts"])
            )
            if clean_start_pts != start_pts or clean_end_pts != end_pts:
                raise RenderError(
                    f"slot {normalized['slot_id']} exact selected PTS range "
                    "disagrees with its clean span"
                )
        output_duration = output_frames / float(config.fps)
        if source_frames_used < output_frames or decode_duration + 1e-9 < output_duration:
            raise RenderError(
                f"slot {normalized['slot_id']} exact selected PTS range cannot "
                f"produce {output_frames} output frames"
            )
        for zone in normalized.get("transition_zones") or ():
            if not isinstance(zone, Mapping):
                raise RenderError(
                    "selected_clip.transition_zones must contain mappings"
                )
            if _frame_range_overlaps(
                selected_start_frame, selected_end_frame, zone
            ):
                raise RenderError(
                    f"slot {normalized['slot_id']} clean span overlaps a transition zone"
                )
        margins, overlay_metadata = _strict_overlay_policy(
            normalized,
            selected_start_frame=selected_start_frame,
            selected_end_frame=selected_end_frame,
            config=config,
        )
        return {
            **exact_selection,
            "selection_mode": "persisted_exact_pts",
            "span_id": selected_clean_span["span_id"],
            "available_clean_frames": int(selected_clean_span["frame_count"]),
            "frame_detector": (
                dict(normalized["frame_contract"].get("detector") or {})
                if isinstance(normalized.get("frame_contract"), Mapping)
                else {}
            ),
            "edge_cleanup_margins": margins,
            "overlay_analysis": overlay_metadata,
        }

    tolerance = 0.5 / float(config.fps) + 1e-6
    source_in = float(normalized["source_in"])
    source_out = float(normalized["source_out"])
    output_duration = output_frames / float(config.fps)
    eligible: list[dict[str, Any]] = []
    for item in candidates:
        frame_pts = item["frame_pts"]
        tb = item["time_base"]
        if frame_pts:
            frame_times = [float(point * tb) for point in frame_pts]
            offset = bisect.bisect_left(frame_times, source_in - tolerance)
            if offset >= len(frame_pts):
                continue
            selected_start_time = frame_times[offset]
            target_end_time = selected_start_time + output_duration
            end_offset = bisect.bisect_left(frame_times, target_end_time - 1e-9)
            if end_offset < len(frame_pts):
                selected_end_time = frame_times[end_offset]
            elif target_end_time <= item["end_time"] + 1e-9:
                selected_end_time = float(item["end_pts"] * tb)
            else:
                continue
            source_frames_used = end_offset - offset
        else:
            offset = max(
                0,
                int(math.ceil((source_in - item["start_time"]) * source_fps - 1e-9)),
            )
            selected_start_time = item["start_time"] + offset / source_fps
            source_frames_used = int(math.ceil(output_duration * source_fps - 1e-9))
            end_offset = offset + source_frames_used
            selected_end_time = selected_start_time + source_frames_used / source_fps
        if (
            source_frames_used >= output_frames
            and item["frame_count"] - offset >= source_frames_used
            and selected_start_time >= source_in - tolerance
            and selected_end_time <= source_out + tolerance
        ):
            candidate = dict(item)
            candidate.update(
                {
                    "selection_offset": offset,
                    "selection_end_offset": end_offset,
                    "source_frames_used": source_frames_used,
                    "selected_start_time": selected_start_time,
                    "selected_end_time": selected_end_time,
                }
            )
            eligible.append(candidate)
    if not eligible:
        longest = max((item["frame_count"] for item in candidates), default=0)
        raise RenderError(
            f"slot {normalized['slot_id']} strict production needs {output_frames} clean "
            f"contiguous source frames; longest eligible span has {longest}"
        )
    selected = min(
        eligible,
        key=lambda item: (abs(item["start_time"] - source_in), -item["frame_count"]),
    )
    selection_offset = int(selected["selection_offset"])
    selected_start_frame = int(selected["start_frame"]) + selection_offset
    source_frames_used = int(selected["source_frames_used"])
    selected_end_frame = selected_start_frame + source_frames_used
    tb = selected["time_base"]
    frame_pts = selected["frame_pts"]
    if frame_pts:
        start_pts = frame_pts[selection_offset]
        if selection_offset + source_frames_used < len(frame_pts):
            end_pts = frame_pts[selection_offset + source_frames_used]
        else:
            end_pts = int(selected["end_pts"])
        decode_source_in = float(start_pts * tb)
        decode_source_out = float(end_pts * tb)
    else:
        decode_source_in = float(selected["selected_start_time"])
        decode_source_out = decode_source_in + output_frames / float(config.fps)
        start_pts = int(round(decode_source_in / float(tb)))
        end_pts = int(round(decode_source_out / float(tb)))
    if decode_source_out > float(selected["end_time"]) + tolerance:
        raise RenderError("strict clean span PTS do not contain the required output frames")

    for zone in normalized.get("transition_zones") or ():
        if not isinstance(zone, Mapping):
            raise RenderError("selected_clip.transition_zones must contain mappings")
        if _frame_range_overlaps(selected_start_frame, selected_end_frame, zone):
            raise RenderError(
                f"slot {normalized['slot_id']} clean span overlaps a transition zone"
            )
    margins, overlay_metadata = _strict_overlay_policy(
        normalized,
        selected_start_frame=selected_start_frame,
        selected_end_frame=selected_end_frame,
        config=config,
    )
    return {
        "span_id": selected["span_id"],
        "clean_frame_start": selected_start_frame,
        "clean_frame_end": selected_end_frame,
        "available_clean_frames": int(selected["frame_count"]),
        "source_frames_used": source_frames_used,
        "decode_source_in": decode_source_in,
        "decode_source_out": decode_source_out,
        "decode_duration": decode_source_out - decode_source_in,
        "start_pts": start_pts,
        "end_pts": end_pts,
        "time_base": {"numerator": tb.numerator, "denominator": tb.denominator},
        "frame_detector": (
            dict(normalized["frame_contract"].get("detector") or {})
            if isinstance(normalized.get("frame_contract"), Mapping)
            else {}
        ),
        "edge_cleanup_margins": margins,
        "overlay_analysis": overlay_metadata,
    }


def _transform_lookup(
    timeline: Mapping[str, Any] | None,
    slot: Mapping[str, Any],
    clip: Mapping[str, Any],
) -> dict[str, Any]:
    direct = slot.get("transform") or clip.get("transform")
    if isinstance(direct, Mapping):
        return dict(direct)

    transform_id = clip.get("transform_id") or slot.get("transform_id")
    transforms = (timeline or {}).get("transforms")
    if isinstance(transforms, Mapping) and transform_id in transforms:
        value = transforms[transform_id]
        return dict(value) if isinstance(value, Mapping) else {}
    if isinstance(transforms, Sequence) and not isinstance(transforms, (str, bytes)):
        for candidate in transforms:
            if not isinstance(candidate, Mapping):
                continue
            identifiers = {
                candidate.get("transform_id"),
                candidate.get("id"),
                candidate.get("slot_id"),
            }
            if transform_id in identifiers or slot.get("slot_id") in identifiers:
                return dict(candidate)
    return {}


def _hash_unit(seed: int, label: str) -> float:
    digest = hashlib.sha256(f"{seed}:{label}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float((1 << 64) - 1)


def _sha256_identity(value: object, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise RenderError(f"{field_name} must be a SHA-256 hex digest")
    return normalized


def build_portable_transform_identity(
    slot: Mapping[str, Any],
    *,
    fps: float,
) -> dict[str, Any]:
    """Return the versioned, path-free identity for one production transform."""

    clip = _selected_clip(slot)
    slot_id = str(slot.get("slot_id") or slot.get("beat_id") or "").strip()
    source_asset_id = str(clip.get("source_asset_id") or "").strip()
    source_sha256 = _sha256_identity(
        clip.get("source_sha256") or clip.get("source_id"),
        "selected_clip.source_sha256",
    )
    candidate_range_hash = _sha256_identity(
        clip.get("candidate_range_hash"),
        "selected_clip.candidate_range_hash",
    )
    if not slot_id:
        raise RenderError("portable transform identity requires slot_id")
    if not source_asset_id:
        raise RenderError(
            "portable transform identity requires selected_clip.source_asset_id"
        )

    source_in = clip.get("source_in", clip.get("in", clip.get("start")))
    source_out = clip.get("source_out", clip.get("out", clip.get("end")))
    if source_in is None or source_out is None:
        raise RenderError("portable transform identity requires source_in/source_out")
    try:
        source_in_value = float(source_in)
        source_out_value = float(source_out)
        fps_value = float(fps)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RenderError("portable transform identity has non-numeric timing") from exc
    if (
        isinstance(source_in, bool)
        or isinstance(source_out, bool)
        or isinstance(fps, bool)
        or not math.isfinite(source_in_value)
        or not math.isfinite(source_out_value)
        or source_in_value < 0
        or source_out_value <= source_in_value
        or not math.isfinite(fps_value)
        or fps_value <= 0
    ):
        raise RenderError("portable transform identity has invalid timing")

    render_frames_raw = slot.get("render_frames")
    if render_frames_raw is None:
        render_frames_raw = clip.get("render_frames")
    try:
        render_frames = int(render_frames_raw)
        render_frames_numeric = float(render_frames_raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RenderError(
            "portable transform identity requires integer render_frames"
        ) from exc
    if (
        isinstance(render_frames_raw, bool)
        or not math.isfinite(render_frames_numeric)
        or render_frames_numeric != render_frames
        or render_frames <= 0
    ):
        raise RenderError(
            "portable transform identity requires positive integer render_frames"
        )

    return {
        "policy_version": DETERMINISTIC_TRANSFORM_POLICY_VERSION,
        "slot_id": slot_id,
        "source_asset_id": source_asset_id,
        "source_sha256": source_sha256,
        "candidate_range_hash": candidate_range_hash,
        "source_in": round(source_in_value, 9),
        "source_out": round(source_out_value, 9),
        "render_frames": render_frames,
        "fps": round(fps_value, 9),
    }


def _has_portable_transform_markers(clip: Mapping[str, Any]) -> bool:
    return all(
        bool(str(clip.get(field) or "").strip())
        for field in _PORTABLE_TRANSFORM_MARKERS
    )


def deterministic_transform(
    slot: Mapping[str, Any],
    explicit: Mapping[str, Any] | None = None,
    *,
    fps: float | None = None,
) -> dict[str, Any]:
    """Return a mild deterministic transform; ``hflip`` is opt-in only."""

    explicit = dict(explicit or {})
    clip = _selected_clip(slot)
    portable_identity: dict[str, Any] | None = None
    if "seed" in explicit:
        seed = int(explicit["seed"]) & 0x7FFFFFFF
    elif _has_portable_transform_markers(clip):
        if fps is None:
            raise RenderError("portable transform identity requires render fps")
        portable_identity = build_portable_transform_identity(slot, fps=fps)
        seed = int(canonical_hash(portable_identity)[:8], 16) & 0x7FFFFFFF
    else:
        source_path, source_in, source_out = _source_bounds(clip)
        identity = (
            f"{slot.get('slot_id', '')}|{source_path}|{source_in:.6f}|{source_out:.6f}"
        )
        seed = int.from_bytes(
            hashlib.sha256(identity.encode("utf-8")).digest()[:4], "big"
        ) & 0x7FFFFFFF

    scale = float(explicit.get("scale", 1.020 + 0.025 * _hash_unit(seed, "scale")))
    x_offset = float(explicit.get("x_offset", -0.025 + 0.050 * _hash_unit(seed, "x")))
    y_offset = float(explicit.get("y_offset", -0.020 + 0.040 * _hash_unit(seed, "y")))
    contrast = float(explicit.get("contrast", 0.995 + 0.025 * _hash_unit(seed, "contrast")))
    saturation = float(
        explicit.get("saturation", 0.980 + 0.035 * _hash_unit(seed, "saturation"))
    )
    brightness = float(
        explicit.get("brightness", -0.003 + 0.006 * _hash_unit(seed, "brightness"))
    )
    grain = float(explicit.get("grain", 0.010 + 0.008 * _hash_unit(seed, "grain")))
    hflip = explicit.get("hflip") is True

    if not 1.0 <= scale <= 1.08:
        raise RenderError("transform scale must be in [1.0, 1.08]")
    if not -0.25 <= x_offset <= 0.25 or not -0.25 <= y_offset <= 0.25:
        raise RenderError("transform offsets must be in [-0.25, 0.25]")
    if not 0.90 <= contrast <= 1.10:
        raise RenderError("transform contrast must be in [0.90, 1.10]")
    if not 0.90 <= saturation <= 1.10:
        raise RenderError("transform saturation must be in [0.90, 1.10]")
    if not -0.05 <= brightness <= 0.05:
        raise RenderError("transform brightness must be in [-0.05, 0.05]")
    if not 0.0 <= grain <= 0.05:
        raise RenderError("transform grain must be in [0, 0.05]")

    return {
        "preset": str(explicit.get("preset", "subtle_documentary_v1")),
        "seed": seed,
        "hflip": hflip,
        "scale": scale,
        "x_offset": x_offset,
        "y_offset": y_offset,
        "contrast": contrast,
        "saturation": saturation,
        "brightness": brightness,
        "grain": grain,
        "transform_version": str(
            explicit.get(
                "transform_version",
                (
                    DETERMINISTIC_TRANSFORM_POLICY_VERSION
                    if portable_identity is not None
                    else "1.0"
                ),
            )
        ),
    }


def build_video_filter(
    transform: Mapping[str, Any],
    *,
    width: int = 1920,
    height: int = 1080,
    fps: float = 25.0,
    edge_cleanup_percent: float | None = None,
    edge_cleanup_side_percent: float = 0.108,
    edge_cleanup_top_percent: float = 0.215,
    edge_cleanup_bottom_percent: float = 0.0,
    edge_cleanup_margins: Mapping[str, float] | None = None,
) -> str:
    """Create edge cleanup, fixed 16:9 framing, and subtle normalization."""

    scale = float(transform["scale"])
    if edge_cleanup_margins is not None:
        left_cleanup = float(edge_cleanup_margins["left"])
        right_cleanup = float(edge_cleanup_margins["right"])
        top_cleanup = float(edge_cleanup_margins["top"])
        bottom_cleanup = float(edge_cleanup_margins["bottom"])
    elif edge_cleanup_percent is not None:
        left_cleanup = right_cleanup = top_cleanup = bottom_cleanup = float(
            edge_cleanup_percent
        )
    else:
        left_cleanup = right_cleanup = float(edge_cleanup_side_percent)
        top_cleanup = float(edge_cleanup_top_percent)
        bottom_cleanup = float(edge_cleanup_bottom_percent)
    scaled_width = int(math.ceil(width * scale / 2.0) * 2)
    scaled_height = int(math.ceil(height * scale / 2.0) * 2)
    x_center = 0.5 + float(transform["x_offset"])
    y_center = 0.5 + float(transform["y_offset"])
    filters = []
    if (
        left_cleanup > 0
        or right_cleanup > 0
        or top_cleanup > 0
        or bottom_cleanup > 0
    ):
        # Work in source coordinates and keep dimensions even for yuv420p.
        # Decimal-normalized donor profiles can land infinitesimally below an
        # integer pixel (for example 1567.999998); nearest-even avoids losing
        # an extra two-pixel column/row as truncation would.
        # This must precede scale/crop so later documentary transforms cannot
        # pull an edge badge back into the visible frame.
        keep_width = 1.0 - left_cleanup - right_cleanup
        keep_height = 1.0 - top_cleanup - bottom_cleanup
        filters.append(
            f"crop=round(iw*{keep_width:.6f}/2)*2:"
            f"round(ih*{keep_height:.6f}/2)*2:"
            f"round(iw*{left_cleanup:.6f}/2)*2:"
            f"round(ih*{top_cleanup:.6f}/2)*2"
        )
    filters.extend([
        (
            f"scale={scaled_width}:{scaled_height}:"
            "force_original_aspect_ratio=increase:flags=lanczos"
        ),
        f"crop={width}:{height}:(iw-ow)*{x_center:.6f}:(ih-oh)*{y_center:.6f}",
    ])
    if transform.get("hflip") is True:
        filters.append("hflip")
    filters.append(
        "eq="
        f"contrast={float(transform['contrast']):.6f}:"
        f"saturation={float(transform['saturation']):.6f}:"
        f"brightness={float(transform['brightness']):.6f}"
    )
    grain_strength = int(round(float(transform["grain"]) * 100.0))
    if grain_strength > 0:
        filters.append(
            f"noise=alls={grain_strength}:allf=t+u:all_seed={int(transform['seed'])}"
        )
    filters.extend([f"fps={float(fps):.6f}", "setsar=1", "format=yuv420p"])
    return ",".join(filters)


def _normalize_slot(
    slot: Mapping[str, Any],
    *,
    timeline: Mapping[str, Any] | None = None,
    transform_fps: float | None = None,
) -> dict[str, Any]:
    timeline_fps = None
    if timeline is not None and timeline.get("fps") is not None:
        timeline_fps = _float(timeline.get("fps"), "timeline.fps")
    duration = _slot_duration(slot, fps=timeline_fps)
    clip = _selected_clip(slot)
    source_path, source_in, source_out = _source_bounds(clip)
    render_frames_value = slot.get("render_frames")
    render_frames = None
    if render_frames_value is not None:
        if isinstance(render_frames_value, bool):
            raise RenderError("slot.render_frames must be an integer")
        try:
            render_frames = int(render_frames_value)
            numeric_render_frames = float(render_frames_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RenderError("slot.render_frames must be an integer") from exc
        if not math.isfinite(numeric_render_frames) or numeric_render_frames != render_frames:
            raise RenderError("slot.render_frames must be an integer")
        if render_frames <= 0:
            raise RenderError("slot.render_frames must be positive")
    required_source_duration = duration
    source_duration_tolerance = 1e-6
    if render_frames is not None and timeline is not None:
        if timeline_fps is None:
            timeline_fps = _float(timeline.get("fps"), "timeline.fps")
        if timeline_fps <= 0:
            raise RenderError("timeline.fps must be positive")
        required_source_duration = render_frames / timeline_fps
        # CFR editorial bounds may be fractional while the render contract is
        # an integer number of output frames.  This early guard only rejects a
        # clearly short source range; strict clean-span QA below still requires
        # every actual source frame and forbids padding/retiming.
        source_duration_tolerance = 0.5 / timeline_fps + 1e-9
    if source_out - source_in + source_duration_tolerance < required_source_duration:
        raise RenderError(
            f"slot {slot.get('slot_id', '?')} needs {required_source_duration:.3f}s "
            "of exact rendered frames but selected source "
            f"contains only {source_out - source_in:.3f}s"
        )
    safe_source_in = _optional_source_end(clip, "safe_source_in")
    safe_source_out = _optional_source_end(clip, "safe_source_out")
    shot_end = _optional_source_end(clip, "shot_end")
    detected_source_out = _optional_source_end(clip, "detected_source_out")
    if safe_source_in is not None and safe_source_in < source_in:
        raise RenderError(
            "selected_clip.safe_source_in must be greater than or equal to "
            "selected_clip.source_in"
        )
    for name, value in (
        ("safe_source_out", safe_source_out),
        ("shot_end", shot_end),
        ("detected_source_out", detected_source_out),
    ):
        if value is not None and value <= source_in:
            raise RenderError(
                f"selected_clip.{name} must be greater than selected_clip.source_in"
            )
    transform = deterministic_transform(
        slot,
        _transform_lookup(timeline, slot, clip),
        fps=(timeline_fps if timeline_fps is not None else transform_fps),
    )
    edge_cleanup_margins = _edge_cleanup_override(slot, clip)
    return {
        "slot_id": str(slot.get("slot_id", slot.get("beat_id", "slot"))),
        "start": _float(slot.get("start", 0.0), "slot.start"),
        "end": _float(slot.get("end", duration), "slot.end"),
        "duration": duration,
        "source_path": source_path,
        "source_in": source_in,
        "source_out": source_out,
        "safe_source_in": safe_source_in,
        "safe_source_out": safe_source_out,
        "shot_end": shot_end,
        "detected_source_out": detected_source_out,
        "safe_end_reason": clip.get("safe_end_reason"),
        "edge_cleanup_margins": edge_cleanup_margins,
        "source_fps": clip.get(
            "fps", clip.get("source_fps", (timeline or {}).get("source_fps"))
        ),
        "source_frame_start": clip.get("source_frame_start"),
        "source_frame_end": clip.get("source_frame_end"),
        "time_base": clip.get("time_base"),
        "clean_frame_start": clip.get("clean_frame_start"),
        "clean_frame_end": clip.get("clean_frame_end"),
        "clean_source_in": clip.get("clean_source_in"),
        "clean_source_out": clip.get("clean_source_out"),
        "clean_spans": clip.get("clean_spans") or (),
        "transition_zones": clip.get("transition_zones") or (),
        "frame_contract": clip.get("frame_contract"),
        "exact_render_span": clip.get("exact_render_span"),
        "overlay_analysis": clip.get("overlay_analysis"),
        "overlay_blocked": bool(clip.get("overlay_blocked")),
        "overlay_blocked_reason": clip.get("overlay_blocked_reason"),
        "inpaint_applied": bool(clip.get("inpaint_applied")),
        "transform": transform,
        "render_frames": render_frames,
    }


def validate_timeline(
    timeline: Mapping[str, Any],
    *,
    default_fps: float = 25.0,
) -> dict[str, Any]:
    """Validate coverage/continuity and return a normalized JSON-ready plan."""

    if not isinstance(timeline, Mapping):
        raise RenderError("timeline must be a mapping")
    slots = timeline.get("slots")
    if not isinstance(slots, Sequence) or isinstance(slots, (str, bytes)) or not slots:
        raise RenderError("timeline.slots must be a non-empty list")
    fps = _float(timeline.get("fps", default_fps), "timeline.fps")
    if fps <= 0:
        raise RenderError("timeline.fps must be positive")

    normalized: list[dict[str, Any]] = []
    expected_start = 0.0
    frame_cursor = 0
    continuity_tolerance = max(0.001, 0.5 / fps)
    for raw in slots:
        if not isinstance(raw, Mapping):
            raise RenderError("every timeline slot must be a mapping")
        raw_duration = _slot_duration(raw, fps=fps)
        raw_start = _float(raw.get("start", 0.0), "slot.start")
        raw_end = _float(raw.get("end", raw_duration), "slot.end")
        try:
            start_frame, end_frame, render_frames = interval_frame_span(
                raw_start, raw_end, fps
            )
        except ValueError as exc:
            raise RenderError(
                f"slot {raw.get('slot_id', '?')} has invalid frame boundaries"
            ) from exc
        expected_frame_contract = {
            "render_start_frame": start_frame,
            "render_end_frame": end_frame,
            "render_frames": render_frames,
        }
        for field, expected in expected_frame_contract.items():
            supplied = raw.get(field)
            if supplied is None:
                continue
            if isinstance(supplied, bool):
                raise RenderError(f"slot.{field} must be an integer")
            try:
                supplied_integer = int(supplied)
                supplied_numeric = float(supplied)
            except (TypeError, ValueError, OverflowError) as exc:
                raise RenderError(f"slot.{field} must be an integer") from exc
            if (
                not math.isfinite(supplied_numeric)
                or supplied_numeric != supplied_integer
            ):
                raise RenderError(f"slot.{field} must be an integer")
            if supplied_integer != expected:
                raise RenderError(
                    f"slot {raw.get('slot_id', '?')} {field} "
                    f"expected={expected} got={supplied_integer}"
                )
        slot = _normalize_slot(raw, timeline=timeline)
        if abs(slot["start"] - expected_start) > continuity_tolerance:
            raise RenderError(
                f"timeline gap/overlap before {slot['slot_id']}: expected "
                f"{expected_start:.6f}, got {slot['start']:.6f}"
            )
        if abs((slot["end"] - slot["start"]) - slot["duration"]) > 0.020:
            raise RenderError(f"slot {slot['slot_id']} has inconsistent bounds")
        # Quantise cumulative boundaries, not every duration independently.
        # Independent per-clip rounding loses/gains fractions of a frame 70+
        # times and can shorten a documentary by several hundred milliseconds.
        if start_frame != frame_cursor:
            raise RenderError(
                f"slot {slot['slot_id']} frame boundary expected={frame_cursor} "
                f"got={start_frame}"
            )
        slot["render_start_frame"] = start_frame
        slot["render_end_frame"] = end_frame
        slot["render_frames"] = render_frames
        slot["render_duration"] = render_frames / fps
        normalized.append(slot)
        expected_start = slot["end"]
        frame_cursor = end_frame

    narration_duration = _float(
        timeline.get("narration_duration", timeline.get("duration", expected_start)),
        "timeline.narration_duration",
    )
    if narration_duration <= 0:
        raise RenderError("timeline.narration_duration must be positive")
    if abs(expected_start - narration_duration) > (1.0 / fps + 0.001):
        raise RenderError(
            f"timeline ends at {expected_start:.6f}s, narration is {narration_duration:.6f}s"
        )
    expected_total_frames = int(round(narration_duration * fps))
    if frame_cursor != expected_total_frames:
        raise RenderError(
            f"balanced frame plan ends at {frame_cursor}, expected {expected_total_frames}"
        )
    return {
        "schema_version": int(timeline.get("schema_version", 1)),
        "fps": fps,
        "narration_duration": narration_duration,
        "slots": normalized,
    }


def build_materialize_clip_command(
    slot: Mapping[str, Any],
    output_path: str | Path,
    *,
    timeline: Mapping[str, Any] | None = None,
    ffmpeg: str = "ffmpeg",
    config: RenderConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """Build one strict source-range materialization command."""

    cfg = config if isinstance(config, RenderConfig) else RenderConfig.from_mapping(config)
    cfg.validate()
    if cfg.production_strict and slot.get("render_frames") is None:
        raise RenderError("production_strict requires slot.render_frames")
    normalized = _normalize_slot(slot, timeline=timeline, transform_fps=cfg.fps)
    output = str(Path(output_path).expanduser())
    output_frames = normalized.get("render_frames")
    if output_frames is None:
        output_frames = max(1, int(round(normalized["duration"] * cfg.fps)))
    strict_contract = None
    if cfg.production_strict:
        strict_contract = _strict_clean_source_contract(
            normalized,
            output_frames=output_frames,
            config=cfg,
        )
        edge_cleanup_margins = dict(strict_contract["edge_cleanup_margins"])
    else:
        edge_cleanup_margins = (
            normalized["edge_cleanup_margins"]
            if normalized["edge_cleanup_margins"] is not None
            else _config_edge_cleanup_margins(cfg)
        )
    base_vf = build_video_filter(
        normalized["transform"],
        width=cfg.width,
        height=cfg.height,
        fps=cfg.fps,
        edge_cleanup_percent=cfg.edge_cleanup_percent,
        edge_cleanup_side_percent=cfg.edge_cleanup_side_percent,
        edge_cleanup_top_percent=cfg.edge_cleanup_top_percent,
        edge_cleanup_bottom_percent=cfg.edge_cleanup_bottom_percent,
        edge_cleanup_margins=edge_cleanup_margins,
    )
    gop = max(1, int(round(cfg.fps * 2)))
    safe_source_out = normalized.get("safe_source_out")
    shot_end = normalized.get("shot_end")
    safe_source_in = normalized.get("safe_source_in")
    guard_frames = 0
    exact_pts_selection = False
    if strict_contract is not None:
        decode_source_in = float(strict_contract["decode_source_in"])
        decode_source_out = float(strict_contract["decode_source_out"])
        decode_duration = float(strict_contract["decode_duration"])
        exact_pts_selection = (
            strict_contract.get("selection_mode") == "persisted_exact_pts"
        )
        fill_strategy = "strict_none"
        retime_factor = required_retime_factor = 1.0
        deficit_frames = 0
        # Exact source frames only.  No EOF padding and no speed change are
        # allowed in production; runtime frame-count QA is performed before
        # concat as a second line of defence.
        if exact_pts_selection:
            vf = (
                f"trim=start_pts={int(strict_contract['start_pts'])}:"
                f"end_pts={int(strict_contract['end_pts'])},"
                "setpts=PTS-STARTPTS,"
                f"{base_vf}"
            )
        else:
            vf = (
                "setpts=PTS-STARTPTS,"
                f"trim=start_frame=0:end_frame={int(strict_contract['source_frames_used'])},"
                "setpts=PTS-STARTPTS,"
                f"{base_vf}"
            )
    else:
        requested_source_out = float(normalized["source_out"])
        hard_limits = [requested_source_out]
        if normalized.get("detected_source_out") is not None:
            hard_limits.append(float(normalized["detected_source_out"]))

        # Legacy recovery remains available for old timelines.  Strict mode
        # above never enters this tpad/retime branch.
        if safe_source_out is not None:
            hard_limits.append(float(safe_source_out))
        elif shot_end is not None and requested_source_out >= float(shot_end) - 1.0 / cfg.fps:
            guard_frames = int(cfg.shot_end_guard_frames)
            hard_limits.append(float(shot_end) - guard_frames / cfg.fps)

        decode_source_out = min(hard_limits)
        source_in = float(normalized["source_in"])
        decode_source_in = max(
            source_in,
            float(safe_source_in) if safe_source_in is not None else source_in,
        )
        if safe_source_in is not None and not source_in <= float(safe_source_in) < decode_source_out:
            raise RenderError(
                f"slot {normalized['slot_id']} requires source_in <= safe_source_in "
                "< decode_source_out"
            )
        decode_duration = decode_source_out - decode_source_in
        if decode_duration <= 0:
            raise RenderError(
                f"slot {normalized['slot_id']} has no safe source frames after boundary guard"
            )
        output_duration = output_frames / cfg.fps
        duration_deficit = max(0.0, output_duration - decode_duration)
        deficit_frames = max(
            0,
            int(math.ceil(duration_deficit * cfg.fps - 1e-9)),
        )
        required_retime_factor = (
            output_duration / decode_duration if decode_duration < output_duration else 1.0
        )
        retime_factor = 1.0
        if deficit_frames == 0:
            fill_strategy = "none"
        elif deficit_frames <= 3:
            fill_strategy = "tpad_clone"
        elif required_retime_factor <= 1.25 + 1e-9:
            fill_strategy = "retime"
            retime_factor = required_retime_factor
        else:
            raise RenderError(
                f"slot {normalized['slot_id']} safe source window needs "
                f"{required_retime_factor:.6f}x retime (maximum 1.25x)"
            )
        if fill_strategy == "retime":
            timing_filter = f"setpts=(PTS-STARTPTS)*{retime_factor:.9f}"
            pad_duration = 2.0 / cfg.fps
        else:
            timing_filter = "setpts=PTS-STARTPTS"
            pad_duration = max(1.0 / cfg.fps, duration_deficit + 2.0 / cfg.fps)
        vf = (
            "setpts=PTS-STARTPTS,"
            f"trim=duration={decode_duration:.6f},{timing_filter},"
            f"{base_vf},"
            f"tpad=stop_mode=clone:stop_duration={pad_duration:.6f}"
        )
    if exact_pts_selection:
        input_seek = max(0.0, decode_source_in - 2.0)
        input_options = [
            "-copyts",
            "-ss",
            f"{input_seek:.9f}",
        ]
        input_duration: float | None = None
    else:
        input_seek = decode_source_in
        input_duration = decode_duration
        input_options = [
            "-ss",
            f"{input_seek:.6f}",
            "-t",
            f"{input_duration:.6f}",
        ]
    argv = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
        *input_options,
        "-i",
        normalized["source_path"],
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        vf,
        "-frames:v",
        str(output_frames),
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c:v",
        cfg.video_codec,
        "-preset",
        cfg.preset,
        "-crf",
        str(cfg.crf),
        "-g",
        str(gop),
        "-keyint_min",
        str(gop),
        "-sc_threshold",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        output,
    ]
    proof = assert_donor_audio_excluded(argv)
    facts = {
        "kind": "materialize_video_only_clip",
        "slot_id": normalized["slot_id"],
        "source_path": normalized["source_path"],
        "source_in": normalized["source_in"],
        "source_out": normalized["source_out"],
        "safe_source_in": safe_source_in,
        "safe_source_out": safe_source_out,
        "shot_end": shot_end,
        "detected_source_out": normalized.get("detected_source_out"),
        "safe_end_reason": normalized.get("safe_end_reason"),
        "decode_source_in": decode_source_in,
        "decode_source_out": decode_source_out,
        "decode_duration": decode_duration,
        "input_seek": input_seek,
        "input_duration": input_duration,
        "copyts": exact_pts_selection,
        "fill_strategy": fill_strategy,
        "production_strict": bool(cfg.production_strict),
        "retime_factor": retime_factor,
        "required_retime_factor": required_retime_factor,
        "source_deficit_frames": deficit_frames,
        "shot_end_guard_frames_applied": guard_frames,
        "duration": normalized["duration"],
        "render_frames": output_frames,
        "render_duration": output_frames / cfg.fps,
        "output_path": output,
        "resolution": f"{cfg.width}x{cfg.height}",
        "fps": cfg.fps,
        "edge_cleanup_percent": (
            next(iter(edge_cleanup_margins.values()))
            if len(set(edge_cleanup_margins.values())) == 1
            else None
        ),
        "edge_cleanup_margins": dict(edge_cleanup_margins),
        "clean_frame_contract": (
            {
                key: value
                for key, value in strict_contract.items()
                if key not in {"edge_cleanup_margins", "overlay_analysis"}
            }
            if strict_contract is not None
            else None
        ),
        "overlay_analysis": (
            dict(strict_contract["overlay_analysis"])
            if strict_contract is not None
            else (
                dict(normalized["overlay_analysis"])
                if isinstance(normalized.get("overlay_analysis"), Mapping)
                else None
            )
        ),
        "transform": normalized["transform"],
        "donor_audio_proof": proof,
    }
    return argv, facts


def materialize_clip_command(*args: Any, **kwargs: Any) -> list[str]:
    """Return only argv; use ``build_materialize_clip_command`` for facts too."""

    return build_materialize_clip_command(*args, **kwargs)[0]


def assert_donor_audio_excluded(
    argv: Sequence[str],
    *,
    donor_input_index: int = 0,
) -> dict[str, Any]:
    """Structurally prove that a donor-materialization command cannot map audio."""

    command = [str(item) for item in argv]
    if "-an" not in command:
        raise RenderError("donor command is unsafe: missing -an")
    mappings = [command[i + 1] for i, token in enumerate(command[:-1]) if token == "-map"]
    expected_video = f"{int(donor_input_index)}:v"
    if not any(mapping.lstrip("-").startswith(expected_video) for mapping in mappings):
        raise RenderError("donor command is unsafe: no explicit donor video map")
    if command_maps_input_audio(command, donor_input_index):
        raise RenderError("donor command is unsafe: donor audio is explicitly mapped")
    filter_text = " ".join(
        command[i + 1]
        for i, token in enumerate(command[:-1])
        if token in {"-filter_complex", "-af"}
    )
    if f"[{donor_input_index}:a" in filter_text:
        raise RenderError("donor command is unsafe: filter graph consumes donor audio")
    return {
        "donor_input_index": int(donor_input_index),
        "has_an": True,
        "explicit_maps": mappings,
        "donor_audio_mapping": False,
        "donor_audio_filter_reference": False,
    }


def _concat_quote(path: str | Path) -> str:
    # FFmpeg concat-demuxer quoting follows shell-like single-quote rules even
    # though no shell executes this file.
    absolute = Path(path).expanduser().resolve().as_posix()
    return absolute.replace("'", "'\\''")


def write_concat_file(clip_paths: Sequence[str | Path], destination: str | Path) -> str:
    if not clip_paths:
        raise RenderError("at least one materialized clip is required")
    destination = str(Path(destination).expanduser())
    lines = ["ffconcat version 1.0"]
    lines.extend(f"file '{_concat_quote(path)}'" for path in clip_paths)
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def build_concat_command(
    concat_file: str | Path,
    output_path: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    overwrite: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """Build a stream-copy concat command for already normalized silent clips."""

    concat_file = str(Path(concat_file).expanduser())
    output = str(Path(output_path).expanduser())
    argv = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_file,
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c:v",
        "copy",
        "-movflags",
        "+faststart",
        output,
    ]
    proof = assert_donor_audio_excluded(argv)
    return argv, {
        "kind": "concat_video_only_clips",
        "concat_file": concat_file,
        "output_path": output,
        "video_codec_action": "copy",
        "donor_audio_proof": proof,
    }


def concat_command(*args: Any, **kwargs: Any) -> list[str]:
    return build_concat_command(*args, **kwargs)[0]


def command_sha256(argv: Sequence[str]) -> str:
    payload = json.dumps(list(argv), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _command_record(stage: str, argv: Sequence[str], facts: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "stage": stage,
        "argv": [str(item) for item in argv],
        "sha256": command_sha256(argv),
        "facts": dict(facts),
    }


def run_command(argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run without a shell and retain concise diagnostics on failure."""

    try:
        completed = subprocess.run(
            [str(item) for item in argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=float(timeout),
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandExecutionError(argv, 124, f"timeout after {timeout}s") from exc
    if completed.returncode != 0:
        raise CommandExecutionError(argv, completed.returncode, completed.stderr)
    return completed


def probe_media(path: str | Path, *, ffprobe: str = "ffprobe", timeout: float = 60.0) -> dict[str, Any]:
    argv = [
        str(ffprobe),
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(Path(path).expanduser()),
    ]
    completed = run_command(argv, timeout=timeout)
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RenderError(f"ffprobe returned invalid JSON for {path}") from exc
    if not isinstance(result, dict):
        raise RenderError(f"ffprobe returned an invalid document for {path}")
    return result


def _probe_video_frame_count(
    path: str | Path,
    *,
    ffprobe: str = "ffprobe",
    timeout: float = 120.0,
) -> int:
    argv = [
        str(ffprobe),
        "-v",
        "error",
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_read_frames",
        "-of",
        "json",
        str(Path(path).expanduser()),
    ]
    completed = run_command(argv, timeout=timeout)
    try:
        payload = json.loads(completed.stdout)
        value = payload["streams"][0]["nb_read_frames"]
        frame_count = int(value)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RenderError(f"ffprobe returned no exact frame count for {path}") from exc
    if frame_count <= 0:
        raise RenderError(f"ffprobe returned an invalid frame count for {path}")
    return frame_count


def _probe_duration(probe: Mapping[str, Any]) -> float:
    values: list[float] = []
    fmt = probe.get("format")
    if isinstance(fmt, Mapping) and fmt.get("duration") not in (None, "N/A"):
        try:
            values.append(float(fmt["duration"]))
        except (TypeError, ValueError):
            pass
    streams = probe.get("streams")
    if isinstance(streams, Sequence):
        for stream in streams:
            if not isinstance(stream, Mapping) or stream.get("duration") in (None, "N/A"):
                continue
            try:
                values.append(float(stream["duration"]))
            except (TypeError, ValueError):
                pass
    positive = [value for value in values if math.isfinite(value) and value > 0]
    if not positive:
        raise RenderError("ffprobe did not report a positive duration")
    return max(positive)


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _require_file(path: str | Path, name: str) -> str:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise RenderError(f"{name} does not exist or is not a file: {resolved}")
    return str(resolved)


def _select_background(backgrounds: Any) -> tuple[str, dict[str, Any]]:
    """Select exactly one loopable MVP bed, preserving explicit list order."""

    if isinstance(backgrounds, Mapping):
        candidates: list[Any] = [backgrounds]
    elif isinstance(backgrounds, (str, os.PathLike)):
        candidate_path = Path(backgrounds).expanduser()
        if candidate_path.is_dir():
            supported = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
            candidates = [
                path for path in sorted(candidate_path.iterdir())
                if path.is_file() and path.suffix.lower() in supported
            ]
        else:
            candidates = [candidate_path]
    elif isinstance(backgrounds, Sequence):
        candidates = list(backgrounds)
    else:
        raise RenderError("backgrounds must be a path, directory, mapping, or list")
    if not candidates:
        raise RenderError("no supported background audio was provided")

    selected = next(
        (item for item in candidates if isinstance(item, Mapping) and item.get("selected") is True),
        candidates[0],
    )
    if isinstance(selected, Mapping):
        path = selected.get("path") or selected.get("source_path") or selected.get("asset_path")
        metadata = {str(key): value for key, value in selected.items() if key != "path"}
    else:
        path = selected
        metadata = {}
    if not path:
        raise RenderError("selected background has no path")
    return _require_file(path, "background audio"), metadata


def _ready_local_sound_plan(
    sound_plan: Mapping[str, Any],
    timeline: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and reduce a ready sound plan to local render inputs.

    Rendering is deliberately an offline boundary: provider jobs, preview URLs,
    and ``planned`` assets cannot cross it.  Every file is resolved locally and
    checked against the provenance digest before FFmpeg sees it.
    """

    if not isinstance(sound_plan, Mapping):
        raise RenderError("sound_plan must be a mapping; JSON paths are not render inputs")
    try:
        validate_sound_plan(
            sound_plan,
            timeline,
            require_resolved_assets=True,
        )
    except SoundPlanError as exc:
        raise RenderError(f"invalid sound_plan: {exc}") from exc
    if sound_plan.get("status") != "ready":
        raise RenderError("sound_plan must have status='ready' before rendering")

    provenance: list[dict[str, Any]] = []

    def local_asset(raw: Any, label: str) -> tuple[str, Mapping[str, Any]]:
        if not isinstance(raw, Mapping):
            raise RenderError(f"{label} requires a resolved asset mapping")
        if not raw.get("path"):
            raise RenderError(f"{label} must resolve to a local asset path")
        path = _require_file(raw["path"], f"{label} asset")
        provenance_value = raw.get("provenance")
        if not isinstance(provenance_value, Mapping):
            raise RenderError(f"{label} asset has no provenance")
        expected_digest = str(provenance_value.get("sha256") or "").strip().casefold()
        actual_digest = sha256_file(path)
        if expected_digest != actual_digest:
            raise RenderError(f"{label} asset SHA-256 disagrees with provenance")
        return path, provenance_value

    music_inputs: list[dict[str, Any]] = []
    transition_values: list[float] = []
    for index, segment in enumerate(sound_plan.get("music_segments", [])):
        label = f"music_segments[{index}]"
        path, asset_provenance = local_asset(segment.get("asset"), label)
        transition = segment.get("transition") or {}
        try:
            transition_seconds = _float(
                transition.get("seconds"), f"{label}.transition.seconds"
            )
        except RenderError:
            raise
        transition_values.append(transition_seconds)
        asset = segment["asset"]
        music_inputs.append(
            {
                "path": path,
                "start": float(segment["start"]),
                "end": float(segment["end"]),
                "source_offset": segment.get(
                    "source_offset", asset.get("source_offset", 0.0)
                ),
                "gain": segment.get("gain", asset.get("gain", 1.0)),
                "segment_id": segment["segment_id"],
            }
        )
        provenance.append(
            {
                "lane": "music",
                "entry_id": segment["segment_id"],
                "path": path,
                "provenance": dict(asset_provenance),
            }
        )

    if len(music_inputs) <= 1:
        crossfade_seconds = 0.0
    else:
        if any(abs(value - transition_values[0]) > 0.001 for value in transition_values[1:]):
            raise RenderError(
                "all music segments must declare the same crossfade duration"
            )
        crossfade_seconds = transition_values[0]
        if not 2.0 <= crossfade_seconds <= 5.0:
            raise RenderError(
                "multi-segment sound_plan crossfade must be in [2, 5] seconds"
            )

    sfx_inputs: list[dict[str, Any]] = []
    for index, cue in enumerate(sound_plan.get("sfx_cues", [])):
        label = f"sfx_cues[{index}]"
        path, asset_provenance = local_asset(cue.get("asset"), label)
        asset = cue["asset"]
        gain_db = _float(cue.get("gain_db"), f"{label}.gain_db")
        sfx_inputs.append(
            {
                "path": path,
                "at": float(cue["time"]),
                "max_duration": float(cue["max_duration_seconds"]),
                "declared_asset_duration": asset.get("duration"),
                "source_offset": cue.get(
                    "source_offset", asset.get("source_offset", 0.0)
                ),
                "gain": 10.0 ** (gain_db / 20.0),
                "fade_in": cue.get("fade_in", 0.015),
                "fade_out": cue.get("fade_out", 0.080),
                "cue_id": cue["cue_id"],
            }
        )
        provenance.append(
            {
                "lane": "sfx",
                "entry_id": cue["cue_id"],
                "path": path,
                "provenance": dict(asset_provenance),
            }
        )

    return {
        "schema_version": sound_plan.get("schema_version"),
        "status": sound_plan.get("status"),
        "plan_cache_key": sound_plan.get("plan_cache_key"),
        "timeline_fingerprint": sound_plan.get("timeline_fingerprint"),
        "music_inputs": music_inputs,
        "music_crossfade_seconds": crossfade_seconds,
        "sfx_inputs": sfx_inputs,
        "provenance": provenance,
    }


def _verify_video_only(probe: Mapping[str, Any]) -> dict[str, Any]:
    streams = probe.get("streams") or []
    video_streams = [s for s in streams if isinstance(s, Mapping) and s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if isinstance(s, Mapping) and s.get("codec_type") == "audio"]
    if len(video_streams) != 1:
        raise RenderError(f"video-only concat must have exactly one video stream, got {len(video_streams)}")
    if audio_streams:
        raise RenderError("donor audio invariant failed: concat unexpectedly has an audio stream")
    return {"video_stream_count": 1, "audio_stream_count": 0, "passed": True}


def _verify_final_probe(
    probe: Mapping[str, Any],
    *,
    expected_duration: float,
    fps: float,
    width: int,
    height: int,
) -> dict[str, Any]:
    streams = probe.get("streams") or []
    video = [s for s in streams if isinstance(s, Mapping) and s.get("codec_type") == "video"]
    audio = [s for s in streams if isinstance(s, Mapping) and s.get("codec_type") == "audio"]
    if len(video) != 1 or len(audio) != 1:
        raise RenderError(
            f"final output requires one video and one audio stream; got {len(video)}/{len(audio)}"
        )
    if int(video[0].get("width", 0)) != width or int(video[0].get("height", 0)) != height:
        raise RenderError("final output resolution is not 1920x1080")
    if str(audio[0].get("codec_name", "")).lower() != "aac":
        raise RenderError("final output audio codec is not AAC")
    expected_video_frames = int(round(expected_duration * fps))
    try:
        actual_video_frames = int(video[0].get("nb_frames"))
    except (TypeError, ValueError):
        try:
            actual_video_frames = int(round(float(video[0]["duration"]) * fps))
        except (KeyError, TypeError, ValueError) as exc:
            raise RenderError("final video probe has no usable frame count") from exc
    if actual_video_frames != expected_video_frames:
        raise RenderError(
            "final video frame count differs from timeline: "
            f"{actual_video_frames} != {expected_video_frames}"
        )
    video_duration = float(video[0].get("duration", actual_video_frames / fps))
    if abs(video_duration - expected_video_frames / fps) > (1.0 / fps + 0.001):
        raise RenderError("final video stream duration disagrees with its frame count")
    try:
        audio_duration = float(audio[0].get("duration"))
    except (TypeError, ValueError):
        audio_duration = expected_duration
    if abs(audio_duration - expected_duration) > 0.050:
        raise RenderError("final audio stream duration disagrees with narration")
    duration = _probe_duration(probe)
    delta = abs(duration - expected_duration)
    tolerance = (1.0 / fps) + 0.001
    if delta > tolerance:
        raise RenderError(
            f"final duration differs from narration by {delta:.6f}s (limit {tolerance:.6f}s)"
        )
    return {
        "passed": True,
        "duration": duration,
        "expected_duration": expected_duration,
        "duration_delta": delta,
        "duration_tolerance": tolerance,
        "video_frame_count": actual_video_frames,
        "expected_video_frame_count": expected_video_frames,
        "video_stream_duration": video_duration,
        "audio_stream_duration": audio_duration,
        "resolution": f"{width}x{height}",
        "video_codec": video[0].get("codec_name"),
        "audio_codec": audio[0].get("codec_name"),
        "audio_sample_rate": audio[0].get("sample_rate"),
    }


def _verify_video_only_timeline_probe(
    probe: Mapping[str, Any],
    *,
    expected_duration: float,
    fps: float,
    width: int,
    height: int,
) -> dict[str, Any]:
    facts = _verify_video_only(probe)
    stream = next(
        item
        for item in probe.get("streams", [])
        if isinstance(item, Mapping) and item.get("codec_type") == "video"
    )
    if int(stream.get("width", 0)) != width or int(stream.get("height", 0)) != height:
        raise RenderError(
            f"video-only input resolution must be {width}x{height}"
        )
    duration = _probe_duration(probe)
    delta = abs(duration - expected_duration)
    tolerance = (1.0 / fps) + 0.050
    if delta > tolerance:
        raise RenderError(
            "video-only input duration disagrees with timeline by "
            f"{delta:.6f}s (limit {tolerance:.6f}s)"
        )
    return {
        **facts,
        "duration": duration,
        "expected_duration": expected_duration,
        "duration_delta": delta,
        "duration_tolerance": tolerance,
        "resolution": f"{width}x{height}",
    }


def _verify_pcm_stem_probe(
    probe: Mapping[str, Any],
    *,
    expected_duration: float,
    sample_rate: int,
    name: str,
) -> dict[str, Any]:
    streams = probe.get("streams") or []
    audio = [
        item
        for item in streams
        if isinstance(item, Mapping) and item.get("codec_type") == "audio"
    ]
    video = [
        item
        for item in streams
        if isinstance(item, Mapping) and item.get("codec_type") == "video"
    ]
    if len(audio) != 1 or video:
        raise RenderError(f"{name} must contain exactly one audio stream and no video")
    if str(audio[0].get("codec_name") or "").lower() != "pcm_s24le":
        raise RenderError(f"{name} must use pcm_s24le")
    if int(audio[0].get("sample_rate") or 0) != int(sample_rate):
        raise RenderError(f"{name} sample rate must be {sample_rate}")
    duration = _probe_duration(probe)
    delta = abs(duration - expected_duration)
    tolerance = max(0.002, 2.0 / float(sample_rate))
    if delta > tolerance:
        raise RenderError(
            f"{name} duration disagrees with timeline by {delta:.6f}s "
            f"(limit {tolerance:.6f}s)"
        )
    return {
        "passed": True,
        "codec": audio[0].get("codec_name"),
        "sample_rate": audio[0].get("sample_rate"),
        "duration": duration,
        "expected_duration": expected_duration,
        "duration_delta": delta,
        "duration_tolerance": tolerance,
    }


def render_video_only_timeline(
    timeline: Mapping[str, Any],
    output_path: str | Path,
    work_dir: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    render_config: RenderConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Materialize an exact, donor-audio-free production timeline.

    This is the public hand-off boundary between the visual and audio lanes.
    It intentionally stops before narration, music, or natural sound are
    introduced. Every source clip and the concatenated result are probed at
    runtime; a successful FFmpeg exit alone is not acceptance.

    In strict mode the command builder calls its strategy ``strict_none`` to
    distinguish it from the legacy branch. The persisted editorial fact is
    canonicalized to ``none``: no cloned fill and no retiming.
    """

    if not shutil.which(str(ffmpeg)) and not Path(str(ffmpeg)).is_file():
        raise RenderError(f"ffmpeg executable not found: {ffmpeg}")
    if not shutil.which(str(ffprobe)) and not Path(str(ffprobe)).is_file():
        raise RenderError(f"ffprobe executable not found: {ffprobe}")

    normalized = validate_timeline(timeline)
    cfg_values = (
        dict(render_config or {})
        if not isinstance(render_config, RenderConfig)
        else None
    )
    if cfg_values is not None and "fps" not in cfg_values:
        cfg_values["fps"] = normalized["fps"]
    cfg = (
        render_config
        if isinstance(render_config, RenderConfig)
        else RenderConfig.from_mapping(cfg_values)
    )
    cfg.validate()
    if abs(float(cfg.fps) - float(normalized["fps"])) > 1e-6:
        raise RenderError("render_config.fps must match timeline.fps")

    for slot in normalized["slots"]:
        _require_file(slot["source_path"], f"donor for slot {slot['slot_id']}")

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    working = Path(work_dir).expanduser().resolve()
    clips_dir = working / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    commands: list[dict[str, Any]] = []
    clip_paths: list[str] = []
    clip_jobs: list[tuple[int, list[str], dict[str, Any]]] = []
    for index, slot in enumerate(normalized["slots"]):
        clip_path = clips_dir / f"{index:05d}_{slot['slot_id']}.mp4"
        argv, facts = build_materialize_clip_command(
            slot,
            clip_path,
            timeline=normalized,
            ffmpeg=ffmpeg,
            config=cfg,
            overwrite=overwrite,
        )
        clip_paths.append(str(clip_path))
        clip_jobs.append((index, argv, facts))
        commands.append(_command_record("materialize_clip", argv, facts))

    _emit(progress, stage="materialize_clips", completed=0, total=len(clip_jobs))
    completed_count = 0
    workers = min(int(cfg.max_workers), len(clip_jobs))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="semantic-video-only"
    ) as pool:
        futures = {
            pool.submit(run_command, argv, timeout=cfg.clip_timeout_seconds): index
            for index, argv, _facts in clip_jobs
        }
        for future in as_completed(futures):
            future.result()
            completed_count += 1
            _emit(
                progress,
                stage="materialize_clips",
                completed=completed_count,
                total=len(clip_jobs),
            )

    _emit(progress, stage="clip_runtime_qa", completed=0, total=len(clip_jobs))
    for completed, (index, _argv, facts) in enumerate(clip_jobs, start=1):
        clip_path = clip_paths[index]
        actual_frames = _probe_video_frame_count(
            clip_path,
            ffprobe=ffprobe,
            timeout=min(float(cfg.clip_timeout_seconds), 300.0),
        )
        expected_frames = int(facts["render_frames"])
        if actual_frames != expected_frames:
            raise RenderError(
                f"slot {facts['slot_id']} materialization produced "
                f"{actual_frames} frames, expected {expected_frames}"
            )
        clip_probe = probe_media(clip_path, ffprobe=ffprobe)
        clip_audio_proof = _verify_video_only(clip_probe)
        raw_fill_strategy = str(facts.get("fill_strategy") or "")
        if cfg.production_strict and raw_fill_strategy != "strict_none":
            raise RenderError(
                f"slot {facts['slot_id']} strict render selected "
                f"unexpected fill strategy {raw_fill_strategy!r}"
            )
        canonical_fill_strategy = (
            "none" if raw_fill_strategy == "strict_none" else raw_fill_strategy
        )
        runtime_qa = {
            "passed": True,
            "actual_frames": actual_frames,
            "expected_frames": expected_frames,
            "video_only": True,
            "fill_strategy": canonical_fill_strategy,
            "strict_builder_strategy": raw_fill_strategy,
            "donor_audio_proof": clip_audio_proof,
        }
        facts["fill_strategy"] = canonical_fill_strategy
        facts["strict_builder_strategy"] = raw_fill_strategy
        facts["strict_runtime_qa"] = runtime_qa
        commands[index]["facts"] = dict(facts)
        _emit(
            progress,
            stage="clip_runtime_qa",
            completed=completed,
            total=len(clip_jobs),
        )

    concat_file = write_concat_file(clip_paths, working / "timeline.ffconcat")
    concat_argv, concat_facts = build_concat_command(
        concat_file,
        output,
        ffmpeg=ffmpeg,
        overwrite=overwrite,
    )
    commands.append(_command_record("concat_video", concat_argv, concat_facts))
    _emit(progress, stage="concat_video", completed=0, total=1)
    run_command(concat_argv, timeout=cfg.final_timeout_seconds)
    _emit(progress, stage="concat_video", completed=1, total=1)

    expected_duration = float(normalized["narration_duration"])
    final_probe = probe_media(output, ffprobe=ffprobe)
    final_qa = _verify_video_only_timeline_probe(
        final_probe,
        expected_duration=expected_duration,
        fps=float(cfg.fps),
        width=cfg.width,
        height=cfg.height,
    )
    actual_frames = _probe_video_frame_count(
        output,
        ffprobe=ffprobe,
        timeout=min(float(cfg.final_timeout_seconds), 300.0),
    )
    expected_frames = int(round(expected_duration * float(cfg.fps)))
    if actual_frames != expected_frames:
        raise RenderError(
            "video-only timeline frame count differs from the exact plan: "
            f"{actual_frames} != {expected_frames}"
        )
    final_qa.update(
        actual_frames=actual_frames,
        expected_frames=expected_frames,
        frame_exact=True,
    )
    donor_audio_proof = {
        "all_materialize_commands_have_an": all(
            command["facts"]["donor_audio_proof"]["has_an"]
            for command in commands
            if command["stage"] == "materialize_clip"
        ),
        "materialize_audio_maps": False,
        "concat_probe": {
            "video_stream_count": final_qa["video_stream_count"],
            "audio_stream_count": final_qa["audio_stream_count"],
            "passed": final_qa["passed"],
        },
        "passed": True,
    }
    result = {
        "status": "completed",
        "output": {
            "path": str(output),
            "size_bytes": output.stat().st_size,
            "sha256": sha256_file(output),
        },
        "timeline": normalized,
        "clips": [facts for _index, _argv, facts in clip_jobs],
        "render_config": asdict(cfg),
        "commands": commands,
        "donor_audio_proof": donor_audio_proof,
        "probe": final_probe,
        "qa": final_qa,
    }
    _emit(progress, stage="ffprobe_video_only", completed=1, total=1, status="passed")
    return result


def verify_documentary_master(
    path: str | Path,
    *,
    expected_duration: float,
    fps: float,
    width: int = 1920,
    height: int = 1080,
    ffprobe: str = "ffprobe",
) -> dict[str, Any]:
    """Probe and accept a finished one-video/one-AAC documentary master."""

    probe = probe_media(path, ffprobe=ffprobe)
    qa = _verify_final_probe(
        probe,
        expected_duration=float(expected_duration),
        fps=float(fps),
        width=int(width),
        height=int(height),
    )
    return {"probe": probe, "qa": qa}


def _audio_asset_duration(
    path: str,
    *,
    ffprobe: str,
    cache: dict[str, float],
) -> float:
    if path in cache:
        return cache[path]
    probe = probe_media(path, ffprobe=ffprobe)
    streams = probe.get("streams") or []
    if not any(
        isinstance(item, Mapping) and item.get("codec_type") == "audio"
        for item in streams
    ):
        raise RenderError(f"sound asset has no audio stream: {path}")
    duration = _probe_duration(probe)
    cache[path] = duration
    return duration


def render_audio_plan_on_video_only(
    timeline: Mapping[str, Any],
    video_only_path: str | Path,
    narration_path: str | Path,
    sound_plan: Mapping[str, Any],
    output_path: str | Path,
    work_dir: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    render_config: RenderConfig | Mapping[str, Any] | None = None,
    audio_config: AudioMixConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Apply a ready mood-music/SFX plan to an existing video-only timeline.

    This is the fast remix path used when visual materialization is already
    accepted.  It never re-encodes video and it rejects any video input that
    already contains audio, preserving the donor-audio invariant by structure.
    """

    if not shutil.which(str(ffmpeg)) and not Path(str(ffmpeg)).is_file():
        raise RenderError(f"ffmpeg executable not found: {ffmpeg}")
    if not shutil.which(str(ffprobe)) and not Path(str(ffprobe)).is_file():
        raise RenderError(f"ffprobe executable not found: {ffprobe}")

    normalized = validate_timeline(timeline)
    cfg_values = (
        dict(render_config or {})
        if not isinstance(render_config, RenderConfig)
        else None
    )
    if cfg_values is not None and "fps" not in cfg_values:
        cfg_values["fps"] = normalized["fps"]
    cfg = (
        render_config
        if isinstance(render_config, RenderConfig)
        else RenderConfig.from_mapping(cfg_values)
    )
    cfg.validate()
    if abs(float(cfg.fps) - float(normalized["fps"])) > 1e-6:
        raise RenderError("render_config.fps must match timeline.fps")
    mix_cfg = (
        audio_config
        if isinstance(audio_config, AudioMixConfig)
        else AudioMixConfig.from_mapping(audio_config)
    )
    mix_cfg.validate()

    prepared = _ready_local_sound_plan(sound_plan, timeline)
    video_only = _require_file(video_only_path, "video-only timeline")
    narration = _require_file(narration_path, "narration")
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    working = Path(work_dir).expanduser().resolve()
    working.mkdir(parents=True, exist_ok=True)

    expected_duration = float(normalized["narration_duration"])
    narration_probe = probe_media(narration, ffprobe=ffprobe)
    narration_duration = _probe_duration(narration_probe)
    narration_tolerance = (1.0 / cfg.fps) + 0.050
    if abs(narration_duration - expected_duration) > narration_tolerance:
        raise RenderError(
            f"narration duration {narration_duration:.6f}s does not match timeline "
            f"{expected_duration:.6f}s"
        )
    video_only_probe = probe_media(video_only, ffprobe=ffprobe)
    donor_audio_probe = _verify_video_only_timeline_probe(
        video_only_probe,
        expected_duration=expected_duration,
        fps=float(cfg.fps),
        width=cfg.width,
        height=cfg.height,
    )

    asset_durations: dict[str, float] = {}
    music_inputs: list[dict[str, Any]] = []
    for item in prepared["music_inputs"]:
        source_duration = _audio_asset_duration(
            item["path"], ffprobe=ffprobe, cache=asset_durations
        )
        source_offset = _float(item.get("source_offset", 0.0), "music source_offset")
        if source_offset < 0 or source_offset >= source_duration:
            raise RenderError(
                f"music asset source_offset is outside the file: {item['path']}"
            )
        music_inputs.append(dict(item))

    sfx_inputs: list[dict[str, Any]] = []
    for item in prepared["sfx_inputs"]:
        source_duration = _audio_asset_duration(
            item["path"], ffprobe=ffprobe, cache=asset_durations
        )
        source_offset = _float(item.get("source_offset", 0.0), "SFX source_offset")
        available = source_duration - source_offset
        if source_offset < 0 or available <= 0:
            raise RenderError(
                f"SFX asset source_offset is outside the file: {item['path']}"
            )
        sfx_inputs.append(
            {
                "path": item["path"],
                "at": item["at"],
                "duration": min(float(item["max_duration"]), available),
                "source_offset": source_offset,
                "gain": item["gain"],
                "fade_in": item["fade_in"],
                "fade_out": item["fade_out"],
                "cue_id": item["cue_id"],
            }
        )

    commands: list[dict[str, Any]] = []
    music_wav = working / "music_pcm.wav"
    sfx_wav = working / "sfx_pcm.wav"
    try:
        music_argv, music_facts = build_segmented_music_stem_command(
            music_inputs,
            expected_duration,
            music_wav,
            crossfade_seconds=float(prepared["music_crossfade_seconds"]),
            ffmpeg=ffmpeg,
            config=mix_cfg,
            overwrite=overwrite,
        )
        sfx_argv, sfx_facts = build_sfx_stem_command(
            sfx_inputs,
            expected_duration,
            sfx_wav,
            ffmpeg=ffmpeg,
            config=mix_cfg,
            overwrite=overwrite,
        )
    except AudioPlanError as exc:
        raise RenderError(f"sound plan cannot be materialized: {exc}") from exc

    music_facts["plan_segment_ids"] = [
        item["segment_id"] for item in prepared["music_inputs"]
    ]
    sfx_facts["plan_cue_ids"] = [item["cue_id"] for item in prepared["sfx_inputs"]]

    commands.append(_command_record("music_pcm", music_argv, music_facts))
    _emit(progress, stage="music_pcm", completed=0, total=1)
    run_command(music_argv, timeout=cfg.final_timeout_seconds)
    _emit(progress, stage="music_pcm", completed=1, total=1)

    commands.append(_command_record("sfx_pcm", sfx_argv, sfx_facts))
    _emit(progress, stage="sfx_pcm", completed=0, total=1)
    run_command(sfx_argv, timeout=cfg.final_timeout_seconds)
    _emit(progress, stage="sfx_pcm", completed=1, total=1)

    music_probe = probe_media(music_wav, ffprobe=ffprobe)
    music_qa = _verify_pcm_stem_probe(
        music_probe,
        expected_duration=expected_duration,
        sample_rate=mix_cfg.sample_rate,
        name="music stem",
    )
    sfx_probe = probe_media(sfx_wav, ffprobe=ffprobe)
    sfx_qa = _verify_pcm_stem_probe(
        sfx_probe,
        expected_duration=expected_duration,
        sample_rate=mix_cfg.sample_rate,
        name="SFX stem",
    )

    try:
        final_argv, final_facts = build_layered_final_mix_command(
            video_only,
            narration,
            music_wav,
            sfx_wav,
            output,
            expected_duration,
            ffmpeg=ffmpeg,
            config=mix_cfg,
            overwrite=overwrite,
        )
    except AudioPlanError as exc:
        raise RenderError(f"layered final mix is invalid: {exc}") from exc
    if command_maps_input_audio(final_argv, 0):
        raise RenderError("unsafe final command: video-only input audio was mapped")
    commands.append(_command_record("final_layered_mix", final_argv, final_facts))
    _emit(progress, stage="final_layered_mix", completed=0, total=1)
    run_command(final_argv, timeout=cfg.final_timeout_seconds)
    _emit(progress, stage="final_layered_mix", completed=1, total=1)

    final_probe = probe_media(output, ffprobe=ffprobe)
    final_qa = _verify_final_probe(
        final_probe,
        expected_duration=expected_duration,
        fps=float(cfg.fps),
        width=cfg.width,
        height=cfg.height,
    )
    result = {
        "status": "completed",
        "output": {
            "path": str(output),
            "size_bytes": output.stat().st_size,
            "sha256": sha256_file(output),
        },
        "timeline": normalized,
        "audio": {
            "mode": "mood_music_and_sfx",
            "narration_path": narration,
            "sound_plan": {
                key: prepared[key]
                for key in (
                    "schema_version",
                    "status",
                    "plan_cache_key",
                    "timeline_fingerprint",
                )
            },
            "music": music_facts,
            "sfx": sfx_facts,
            "final_mix": final_facts,
            "provenance": prepared["provenance"],
        },
        "render_config": asdict(cfg),
        "commands": commands,
        "donor_audio_proof": {
            "video_only_input_probe": donor_audio_probe,
            "final_video_input_audio_map": False,
            "passed": True,
        },
        "probe": final_probe,
        "qa": {
            **final_qa,
            "music_stem": music_qa,
            "sfx_stem": sfx_qa,
        },
    }
    _emit(progress, stage="ffprobe_final", completed=1, total=1, status="passed")
    return result


def render_timeline(
    timeline: Mapping[str, Any],
    narration_path: str | Path,
    background_paths: Any = None,
    output_path: str | Path | None = None,
    work_dir: str | Path | None = None,
    *,
    sound_plan: Mapping[str, Any] | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    render_config: RenderConfig | Mapping[str, Any] | None = None,
    audio_config: AudioMixConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Execute the complete local render lane and return manifest-ready facts.

    The selected clips are encoded in parallel, but command/result ordering in
    the returned manifest follows timeline order.  No API or network access is
    used.  A final ffprobe is mandatory; a superficially successful FFmpeg exit
    is not treated as completion.
    """

    if not shutil.which(str(ffmpeg)) and not Path(str(ffmpeg)).is_file():
        raise RenderError(f"ffmpeg executable not found: {ffmpeg}")
    if not shutil.which(str(ffprobe)) and not Path(str(ffprobe)).is_file():
        raise RenderError(f"ffprobe executable not found: {ffprobe}")

    normalized = validate_timeline(timeline)
    cfg_values = dict(render_config or {}) if not isinstance(render_config, RenderConfig) else None
    if cfg_values is not None and "fps" not in cfg_values:
        cfg_values["fps"] = normalized["fps"]
    cfg = render_config if isinstance(render_config, RenderConfig) else RenderConfig.from_mapping(cfg_values)
    cfg.validate()
    if abs(float(cfg.fps) - float(normalized["fps"])) > 1e-6:
        raise RenderError("render_config.fps must match timeline.fps")
    mix_cfg = audio_config if isinstance(audio_config, AudioMixConfig) else AudioMixConfig.from_mapping(audio_config)
    mix_cfg.validate()

    if output_path is None:
        raise RenderError("output_path is required")
    if work_dir is None:
        raise RenderError("work_dir is required")

    narration = _require_file(narration_path, "narration")
    if sound_plan is None:
        background, background_metadata = _select_background(background_paths)
    else:
        # Fail before expensive clip materialization when a remote, stale, or
        # merely planned asset accidentally reaches the render boundary.
        _ready_local_sound_plan(sound_plan, timeline)
        background = None
        background_metadata = {}
    for slot in normalized["slots"]:
        _require_file(slot["source_path"], f"donor for slot {slot['slot_id']}")

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    working = Path(work_dir).expanduser().resolve()
    clips_dir = working / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    narration_probe = probe_media(narration, ffprobe=ffprobe)
    narration_duration = _probe_duration(narration_probe)
    expected_duration = float(normalized["narration_duration"])
    if abs(narration_duration - expected_duration) > (1.0 / cfg.fps + 0.050):
        raise RenderError(
            f"narration duration {narration_duration:.6f}s does not match timeline "
            f"{expected_duration:.6f}s"
        )
    if background is not None:
        background_probe = probe_media(background, ffprobe=ffprobe)
        background_duration = _probe_duration(background_probe)
    else:
        background_duration = None

    commands: list[dict[str, Any]] = []
    clip_paths: list[str] = []
    clip_jobs: list[tuple[int, list[str], dict[str, Any]]] = []
    for index, slot in enumerate(normalized["slots"]):
        clip_path = clips_dir / f"{index:05d}_{slot['slot_id']}.mp4"
        argv, facts = build_materialize_clip_command(
            slot,
            clip_path,
            timeline=normalized,
            ffmpeg=ffmpeg,
            config=cfg,
            overwrite=overwrite,
        )
        clip_paths.append(str(clip_path))
        clip_jobs.append((index, argv, facts))
        commands.append(_command_record("materialize_clip", argv, facts))

    _emit(progress, stage="materialize_clips", completed=0, total=len(clip_jobs))
    completed_count = 0
    workers = min(int(cfg.max_workers), len(clip_jobs))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="semantic-clip") as pool:
        futures = {
            pool.submit(run_command, argv, timeout=cfg.clip_timeout_seconds): index
            for index, argv, _facts in clip_jobs
        }
        for future in as_completed(futures):
            future.result()
            completed_count += 1
            _emit(
                progress,
                stage="materialize_clips",
                completed=completed_count,
                total=len(clip_jobs),
            )

    if cfg.production_strict:
        _emit(progress, stage="strict_clip_qa", completed=0, total=len(clip_jobs))
        for completed, (index, _argv, facts) in enumerate(clip_jobs, start=1):
            clip_path = clip_paths[index]
            actual_frames = _probe_video_frame_count(
                clip_path,
                ffprobe=ffprobe,
                timeout=min(float(cfg.clip_timeout_seconds), 300.0),
            )
            expected_frames = int(facts["render_frames"])
            if actual_frames != expected_frames:
                raise RenderError(
                    f"slot {facts['slot_id']} strict materialization produced "
                    f"{actual_frames} frames, expected {expected_frames}"
                )
            clip_probe = probe_media(clip_path, ffprobe=ffprobe)
            stream_types = [
                item.get("codec_type")
                for item in clip_probe.get("streams") or ()
                if isinstance(item, Mapping)
            ]
            if stream_types.count("video") != 1 or "audio" in stream_types:
                raise RenderError(
                    f"slot {facts['slot_id']} strict materialization is not video-only"
                )
            strict_qa = {
                "passed": True,
                "actual_frames": actual_frames,
                "expected_frames": expected_frames,
                "video_only": True,
                "fill_strategy": "strict_none",
            }
            facts["strict_runtime_qa"] = strict_qa
            commands[index]["facts"]["strict_runtime_qa"] = dict(strict_qa)
            _emit(
                progress,
                stage="strict_clip_qa",
                completed=completed,
                total=len(clip_jobs),
            )

    concat_file = write_concat_file(clip_paths, working / "timeline.ffconcat")
    video_only_path = working / "timeline_video_only.mp4"
    concat_argv, concat_facts = build_concat_command(
        concat_file, video_only_path, ffmpeg=ffmpeg, overwrite=overwrite
    )
    commands.append(_command_record("concat_video", concat_argv, concat_facts))
    _emit(progress, stage="concat_video", completed=0, total=1)
    run_command(concat_argv, timeout=cfg.final_timeout_seconds)
    _emit(progress, stage="concat_video", completed=1, total=1)

    video_only_probe = probe_media(video_only_path, ffprobe=ffprobe)
    donor_audio_probe = _verify_video_only(video_only_probe)

    if sound_plan is not None:
        layered = render_audio_plan_on_video_only(
            timeline,
            video_only_path,
            narration,
            sound_plan,
            output,
            working / "sound",
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            render_config=cfg,
            audio_config=mix_cfg,
            overwrite=overwrite,
            progress=progress,
        )
        layered["timeline"] = normalized
        layered["clips"] = [facts for _index, _argv, facts in clip_jobs]
        layered["commands"] = commands + list(layered["commands"])
        layered["donor_audio_proof"] = {
            "all_materialize_commands_have_an": all(
                command["facts"]["donor_audio_proof"]["has_an"]
                for command in commands
                if command["stage"] == "materialize_clip"
            ),
            "materialize_audio_maps": False,
            "concat_probe": donor_audio_probe,
            "final_video_input_audio_map": False,
            "passed": True,
        }
        return layered

    background_wav = working / "background_pcm.wav"
    assert background is not None and background_duration is not None
    background_argv, background_facts = build_looped_background_command(
        background,
        background_duration,
        expected_duration,
        background_wav,
        ffmpeg=ffmpeg,
        config=mix_cfg,
        overwrite=overwrite,
    )
    background_facts["selection_metadata"] = background_metadata
    commands.append(_command_record("background_pcm", background_argv, background_facts))
    _emit(progress, stage="background_pcm", completed=0, total=1)
    run_command(background_argv, timeout=cfg.final_timeout_seconds)
    _emit(progress, stage="background_pcm", completed=1, total=1)

    final_argv, final_facts = build_final_mix_command(
        video_only_path,
        narration,
        background_wav,
        output,
        expected_duration,
        ffmpeg=ffmpeg,
        config=mix_cfg,
        overwrite=overwrite,
    )
    if command_maps_input_audio(final_argv, 0):
        raise RenderError("unsafe final command: video-only input audio was mapped")
    commands.append(_command_record("final_mix", final_argv, final_facts))
    _emit(progress, stage="final_mix", completed=0, total=1)
    run_command(final_argv, timeout=cfg.final_timeout_seconds)
    _emit(progress, stage="final_mix", completed=1, total=1)

    # Mandatory runtime acceptance, not merely command inspection.
    final_probe = probe_media(output, ffprobe=ffprobe)
    final_qa = _verify_final_probe(
        final_probe,
        expected_duration=expected_duration,
        fps=float(cfg.fps),
        width=cfg.width,
        height=cfg.height,
    )
    output_stat = output.stat()
    result = {
        "status": "completed",
        "output": {
            "path": str(output),
            "size_bytes": output_stat.st_size,
            "sha256": sha256_file(output),
        },
        "timeline": normalized,
        "clips": [facts for _index, _argv, facts in clip_jobs],
        "audio": {
            "narration_path": narration,
            "background": background_facts,
            "final_mix": final_facts,
        },
        "render_config": asdict(cfg),
        "commands": commands,
        "donor_audio_proof": {
            "all_materialize_commands_have_an": all(
                command["facts"]["donor_audio_proof"]["has_an"]
                for command in commands
                if command["stage"] == "materialize_clip"
            ),
            "materialize_audio_maps": False,
            "concat_probe": donor_audio_probe,
            "final_video_input_audio_map": False,
            "passed": True,
        },
        "probe": final_probe,
        "qa": final_qa,
    }
    _emit(progress, stage="ffprobe_final", completed=1, total=1, status="passed")
    return result


__all__ = [
    "CommandExecutionError",
    "DETERMINISTIC_TRANSFORM_POLICY_VERSION",
    "EXACT_SELECTED_PTS_RENDER_CONTRACT_VERSION",
    "RenderConfig",
    "RenderError",
    "assert_donor_audio_excluded",
    "build_concat_command",
    "build_materialize_clip_command",
    "build_exact_selected_pts_render_contract",
    "build_portable_transform_identity",
    "build_video_filter",
    "command_sha256",
    "concat_command",
    "deterministic_transform",
    "materialize_clip_command",
    "probe_media",
    "render_audio_plan_on_video_only",
    "render_timeline",
    "render_video_only_timeline",
    "run_command",
    "sha256_file",
    "validate_timeline",
    "verify_documentary_master",
    "write_concat_file",
]
