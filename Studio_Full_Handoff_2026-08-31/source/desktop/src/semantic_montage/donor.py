"""Local donor-video inspection and virtual clip planning.

The module never downloads media and never extracts donor audio.  It records
whether an audio stream exists only so the renderer can later prove that the
stream was excluded from the final render graph.
"""

from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import statistics
import subprocess
from typing import Any, Iterable, Mapping, Sequence


MIN_CLIP_SECONDS = 4.0
MAX_CLIP_SECONDS = 8.0
SAMPLE_FRACTIONS = (0.10, 0.35, 0.65, 0.90)
_EPSILON = 1e-6
FRAME_CONTRACT_SCHEMA_VERSION = 2
TRANSITION_DETECTOR_VERSION = "semantic-transition-v2.0"


class DonorError(ValueError):
    """The local donor cannot be safely indexed."""


@dataclass(frozen=True)
class DonorMedia:
    path: str
    sha256: str
    size_bytes: int
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    video_stream_index: int
    has_audio: bool
    audio_stream_count: int
    format_name: str

    @property
    def source_id(self) -> str:
        return self.sha256

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShotBoundary:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, float]:
        return {
            "start": self.start,
            "end": self.end,
            "duration": round(self.duration, 6),
        }


@dataclass(frozen=True)
class VirtualInterval:
    start: float
    end: float
    sample_timestamps: tuple[float, float, float, float]

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "duration": round(self.duration, 6),
            "sample_timestamps": list(self.sample_timestamps),
        }


@dataclass(frozen=True)
class DonorClip:
    clip_id: str
    source_id: str
    source_path: str
    source_in: float
    source_out: float
    duration: float
    sample_times: tuple[float, float, float, float]
    # Absolute hard scene cuts contained inside this 4--8 second virtual
    # interval.  Virtual intervals sometimes merge short detected shots; the
    # selector needs the cuts to avoid ending on a sub-second fragment.
    shot_cuts: tuple[float, ...] = ()
    source_start_frame: int | None = None
    source_end_frame: int | None = None
    clean_spans: tuple[dict[str, Any], ...] = ()
    transition_zones: tuple[dict[str, Any], ...] = ()
    frame_contract: dict[str, Any] | None = None
    fps: float | None = None
    time_base: dict[str, int] | None = None
    clean_frame_start: int | None = None
    clean_frame_end: int | None = None
    clean_source_in: float | None = None
    clean_source_out: float | None = None
    overlay_analysis: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "clip_id": self.clip_id,
            "source_id": self.source_id,
            "source_path": self.source_path,
            "source_in": self.source_in,
            "source_out": self.source_out,
            "duration": self.duration,
            "sample_times": list(self.sample_times),
            "shot_cuts": list(self.shot_cuts),
        }
        if self.source_start_frame is not None:
            result["source_start_frame"] = self.source_start_frame
        if self.source_end_frame is not None:
            result["source_end_frame"] = self.source_end_frame
        if self.clean_spans:
            result["clean_spans"] = [dict(item) for item in self.clean_spans]
        if self.transition_zones or self.frame_contract is not None:
            result["transition_zones"] = [dict(item) for item in self.transition_zones]
        if self.frame_contract is not None:
            result["frame_contract"] = dict(self.frame_contract)
        for name in (
            "fps",
            "clean_frame_start",
            "clean_frame_end",
            "clean_source_in",
            "clean_source_out",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        if self.time_base is not None:
            result["time_base"] = dict(self.time_base)
        if self.overlay_analysis is not None:
            result["overlay_analysis"] = dict(self.overlay_analysis)
        return result


@dataclass(frozen=True)
class DonorIndex:
    media: DonorMedia
    shots: tuple[ShotBoundary, ...]
    clips: tuple[DonorClip, ...]
    range_start: float
    range_end: float
    detector: str = "PySceneDetect.ContentDetector"
    frame_contract: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": 2 if self.frame_contract is not None else 1,
            "detector": self.detector,
            "media": self.media.to_dict(),
            "range_start": self.range_start,
            "range_end": self.range_end,
            "shots": [shot.to_dict() for shot in self.shots],
            "clips": [clip.to_dict() for clip in self.clips],
        }
        if self.frame_contract is not None:
            result["frame_contract"] = dict(self.frame_contract)
        return result


def sha256_file(path: str | Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    """Return a full content hash without loading the donor into memory."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_media_path(path: str | Path) -> Path:
    raw = str(path)
    if "://" in raw:
        raise DonorError("only local donor files are allowed")
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DonorError(f"donor file is unavailable: {path}") from exc
    if not resolved.is_file():
        raise DonorError(f"donor path is not a file: {resolved}")
    return resolved


def _positive_number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DonorError(f"ffprobe returned invalid {field}") from exc
    if not math.isfinite(number) or number <= 0:
        raise DonorError(f"ffprobe returned invalid {field}")
    return number


def _frame_rate(stream: dict[str, Any]) -> float:
    raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    try:
        rate = float(Fraction(str(raw)))
    except (ValueError, ZeroDivisionError) as exc:
        raise DonorError("ffprobe returned invalid frame rate") from exc
    if not math.isfinite(rate) or rate <= 0:
        raise DonorError("ffprobe returned invalid frame rate")
    return rate


def inspect_donor(
    path: str | Path,
    *,
    ffprobe_bin: str = "ffprobe",
    timeout: float = 90.0,
) -> DonorMedia:
    """Validate a local video with ffprobe and bind it to a full SHA-256 hash."""
    source = _local_media_path(path)
    before = source.stat()
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(source),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DonorError(f"ffprobe failed for {source.name}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "unknown ffprobe error").strip()[-500:]
        raise DonorError(f"ffprobe rejected {source.name}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DonorError("ffprobe returned malformed JSON") from exc

    streams = payload.get("streams") or []
    video_streams = [item for item in streams if item.get("codec_type") == "video"]
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    if not video_streams:
        raise DonorError("donor has no video stream")
    video = video_streams[0]
    container = payload.get("format") or {}
    duration = _positive_number(
        video.get("duration") or container.get("duration"), "duration"
    )
    if duration + _EPSILON < MIN_CLIP_SECONDS:
        raise DonorError(
            f"donor is shorter than the minimum virtual clip ({MIN_CLIP_SECONDS:g}s)"
        )
    width = int(_positive_number(video.get("width"), "width"))
    height = int(_positive_number(video.get("height"), "height"))
    stream_index = int(video.get("index", 0))
    digest = sha256_file(source)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise DonorError(f"donor changed while it was being indexed: {source.name}")

    return DonorMedia(
        path=str(source),
        sha256=digest,
        size_bytes=after.st_size,
        duration=round(duration, 6),
        width=width,
        height=height,
        fps=round(_frame_rate(video), 6),
        video_codec=str(video.get("codec_name") or "unknown"),
        video_stream_index=stream_index,
        has_audio=bool(audio_streams),
        audio_stream_count=len(audio_streams),
        format_name=str(container.get("format_name") or "unknown"),
    )


def normalize_time_base(value: Any) -> Fraction:
    """Normalize an FFmpeg time base without losing integer PTS precision."""

    try:
        if isinstance(value, Mapping):
            numerator = int(value.get("numerator", value.get("num")))
            denominator = int(value.get("denominator", value.get("den")))
            result = Fraction(numerator, denominator)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) != 2:
                raise ValueError
            result = Fraction(int(value[0]), int(value[1]))
        else:
            result = Fraction(str(value))
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise DonorError("frame time_base must be a positive rational") from exc
    if result <= 0:
        raise DonorError("frame time_base must be a positive rational")
    return result


def probe_frame_timeline(
    path: str | Path,
    *,
    ffprobe_bin: str = "ffprobe",
    timeout: float = 3_600.0,
) -> dict[str, Any]:
    """Read every video frame's integer PTS and stream time base.

    The result is intentionally compact: only integer PTS values are retained.
    That is enough to create exact half-open clean spans even for VFR sources;
    conversion to seconds happens only at the FFmpeg boundary.
    """

    source = _local_media_path(path)
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=time_base:frame=best_effort_timestamp,pkt_duration",
        "-of",
        "json",
        str(source),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DonorError(f"frame timeline probe failed for {source.name}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "unknown ffprobe error").strip()[-500:]
        raise DonorError(f"frame timeline probe rejected {source.name}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DonorError("frame timeline probe returned malformed JSON") from exc
    streams = payload.get("streams") or []
    if not streams or not isinstance(streams[0], Mapping):
        raise DonorError("frame timeline probe returned no video stream")
    time_base = normalize_time_base(streams[0].get("time_base"))
    pts: list[int] = []
    durations: list[int] = []
    for index, frame in enumerate(payload.get("frames") or []):
        if not isinstance(frame, Mapping) or frame.get("best_effort_timestamp") is None:
            raise DonorError(f"video frame {index} has no integer PTS")
        try:
            point = int(frame["best_effort_timestamp"])
            duration = int(frame.get("pkt_duration") or 0)
        except (TypeError, ValueError) as exc:
            raise DonorError(f"video frame {index} has invalid PTS metadata") from exc
        if pts and point <= pts[-1]:
            raise DonorError("video frame PTS values must be strictly increasing")
        pts.append(point)
        if duration > 0:
            durations.append(duration)
    if not pts:
        raise DonorError("frame timeline probe returned no frames")
    inferred_step = int(round(statistics.median(durations or [
        right - left for left, right in zip(pts, pts[1:])
    ] or [1])))
    if inferred_step <= 0:
        raise DonorError("frame timeline has no positive terminal duration")
    end_pts = pts[-1] + inferred_step
    return {
        "schema_version": FRAME_CONTRACT_SCHEMA_VERSION,
        "frame_count": len(pts),
        "frame_pts": pts,
        "end_pts": end_pts,
        "time_base": {
            "numerator": time_base.numerator,
            "denominator": time_base.denominator,
        },
        "start_time": float(pts[0] * time_base),
        "end_time": float(end_pts * time_base),
        "source_path": str(source),
    }


def _metric_series(values: Sequence[float] | None, frame_count: int, name: str) -> list[float]:
    if values is None:
        return [0.0] * frame_count
    parsed = [float(value) for value in values]
    if len(parsed) == frame_count - 1:
        parsed.insert(0, 0.0)
    if len(parsed) != frame_count or any(not math.isfinite(value) for value in parsed):
        raise DonorError(f"{name} must contain frame_count or frame_count-1 finite values")
    return parsed


def _runs(indices: Sequence[int]) -> list[tuple[int, int]]:
    """Convert sorted transition indices to inclusive-index runs."""

    if not indices:
        return []
    output: list[tuple[int, int]] = []
    start = previous = int(indices[0])
    for value in indices[1:]:
        value = int(value)
        if value != previous + 1:
            output.append((start, previous))
            start = value
        previous = value
    output.append((start, previous))
    return output


def detect_transition_zones(
    frame_luma: Sequence[float],
    *,
    frame_mae: Sequence[float] | None = None,
    histogram_distance: Sequence[float] | None = None,
    guard_frames: int = 2,
) -> tuple[dict[str, Any], ...]:
    """Detect hard cuts and multi-frame dissolve/fade zones from frame metrics.

    Metric index ``i`` describes the transition from frame ``i-1`` to frame
    ``i``.  Returned ranges are half-open frame intervals and deliberately
    include a small guard on both sides; no transition frame can therefore be
    selected as the final clean frame of a strict slot.
    """

    luma = [float(value) for value in frame_luma]
    if len(luma) < 2 or any(not math.isfinite(value) for value in luma):
        raise DonorError("frame_luma must contain at least two finite values")
    if isinstance(guard_frames, bool) or int(guard_frames) != guard_frames or guard_frames < 0:
        raise DonorError("guard_frames must be a non-negative integer")
    frame_count = len(luma)
    mae = _metric_series(frame_mae, frame_count, "frame_mae")
    hist = _metric_series(histogram_distance, frame_count, "histogram_distance")
    zones: list[dict[str, Any]] = []

    hard_indices = [
        index
        for index in range(1, frame_count)
        if mae[index] >= 15.0 and hist[index] >= 0.15
    ]
    for index in hard_indices:
        zones.append(
            {
                "kind": "hard_cut",
                "start_frame": max(0, index - int(guard_frames)),
                "end_frame": min(frame_count, index + int(guard_frames) + 1),
                "confidence": round(min(1.0, 0.5 * mae[index] / 15.0 + 0.5 * hist[index] / 0.15), 6),
                "boundary_frame": index,
                "detector_version": TRANSITION_DETECTOR_VERSION,
            }
        )

    # Fade: at least four consecutive, directionally consistent luminance
    # steps with a material total change.  Hard-cut positions split the run.
    fade_candidates: list[int] = []
    prior_sign = 0
    for index in range(1, frame_count):
        delta = luma[index] - luma[index - 1]
        sign = 1 if delta >= 1.0 else -1 if delta <= -1.0 else 0
        if index in hard_indices or sign == 0:
            if fade_candidates and fade_candidates[-1] != -1:
                fade_candidates.append(-1)
            prior_sign = 0
            continue
        if prior_sign and sign != prior_sign:
            fade_candidates.append(-1)  # sentinel splits directional runs
        fade_candidates.append(index)
        prior_sign = sign
    directional_runs: list[list[int]] = []
    current: list[int] = []
    for item in fade_candidates + [-1]:
        if item == -1:
            if current:
                directional_runs.append(current)
            current = []
        else:
            current.append(item)
    fade_member_indices: set[int] = set()
    for run in directional_runs:
        total_change = abs(luma[run[-1]] - luma[run[0] - 1])
        if len(run) >= 4 and total_change >= 18.0:
            fade_member_indices.update(run)
            zones.append(
                {
                    "kind": "fade",
                    "start_frame": max(0, run[0] - 1 - int(guard_frames)),
                    "end_frame": min(frame_count, run[-1] + int(guard_frames) + 1),
                    "confidence": round(min(1.0, 0.55 + total_change / 100.0), 6),
                    "detector_version": TRANSITION_DETECTOR_VERSION,
                }
            )

    # Dissolve: several consecutive moderate content/histogram changes that
    # are neither a single hard cut nor a monotonic fade.
    dissolve_indices = [
        index
        for index in range(1, frame_count)
        if index not in hard_indices
        and index not in fade_member_indices
        and 3.0 <= mae[index] < 15.0
        and hist[index] >= 0.025
    ]
    for start, end in _runs(dissolve_indices):
        count = end - start + 1
        hist_total = sum(hist[start : end + 1])
        # A dissolve is a bounded transition, not sustained camera/subject
        # motion.  Long runs were the source of false exclusions on waterfalls,
        # herds and aerial pans.
        if count < 3 or count > 18 or hist_total < 0.12:
            continue
        zones.append(
            {
                "kind": "dissolve",
                "start_frame": max(0, start - 1 - int(guard_frames)),
                "end_frame": min(frame_count, end + int(guard_frames) + 1),
                "confidence": round(min(1.0, 0.5 + hist_total), 6),
                "detector_version": TRANSITION_DETECTOR_VERSION,
            }
        )
    return tuple(sorted(zones, key=lambda item: (item["start_frame"], item["end_frame"])))


def _normalize_frame_zone(raw: Mapping[str, Any], frame_count: int, index: int) -> dict[str, Any]:
    try:
        start = int(raw["start_frame"])
        end = int(raw["end_frame"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DonorError(f"frame zone {index} requires integer start_frame/end_frame") from exc
    if isinstance(raw.get("start_frame"), bool) or isinstance(raw.get("end_frame"), bool):
        raise DonorError(f"frame zone {index} requires integer start_frame/end_frame")
    if not 0 <= start < end <= frame_count:
        raise DonorError(f"frame zone {index} is outside the frame timeline")
    confidence = float(raw.get("confidence", 1.0))
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise DonorError(f"frame zone {index} confidence must be in [0, 1]")
    result = dict(raw)
    result.update(
        {
            "kind": str(raw.get("kind") or "contamination"),
            "start_frame": start,
            "end_frame": end,
            "frame_count": end - start,
            "confidence": confidence,
        }
    )
    return result


def build_frame_clean_spans(
    frame_pts: Sequence[int],
    time_base: Any,
    *,
    transition_zones: Sequence[Mapping[str, Any]] = (),
    contamination_zones: Sequence[Mapping[str, Any]] = (),
    end_pts: int | None = None,
    detector_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build frame-exact clean spans as the complement of unsafe zones."""

    try:
        points = [int(value) for value in frame_pts]
    except (TypeError, ValueError) as exc:
        raise DonorError("frame_pts must contain integers") from exc
    if not points or any(right <= left for left, right in zip(points, points[1:])):
        raise DonorError("frame_pts must be a non-empty strictly increasing sequence")
    tb = normalize_time_base(time_base)
    steps = [right - left for left, right in zip(points, points[1:])]
    terminal = int(end_pts) if end_pts is not None else points[-1] + int(round(statistics.median(steps or [1])))
    if terminal <= points[-1]:
        raise DonorError("end_pts must be greater than the final frame PTS")
    frame_count = len(points)
    transitions = [
        _normalize_frame_zone(item, frame_count, index)
        for index, item in enumerate(transition_zones)
    ]
    contaminations = [
        _normalize_frame_zone(item, frame_count, index)
        for index, item in enumerate(contamination_zones)
    ]
    unsafe = sorted(
        (item["start_frame"], item["end_frame"])
        for item in transitions + contaminations
    )
    merged: list[list[int]] = []
    for start, end in unsafe:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)

    safe_ranges: list[tuple[int, int]] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            safe_ranges.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < frame_count:
        safe_ranges.append((cursor, frame_count))

    def pts_at_boundary(frame_index: int) -> int:
        return points[frame_index] if frame_index < frame_count else terminal

    def timed_zone(item: Mapping[str, Any]) -> dict[str, Any]:
        start = int(item["start_frame"])
        end = int(item["end_frame"])
        result = dict(item)
        result.update(
            {
                "start_pts": pts_at_boundary(start),
                "end_pts": pts_at_boundary(end),
                "start_time": float(pts_at_boundary(start) * tb),
                "end_time": float(pts_at_boundary(end) * tb),
                "time_base": {"numerator": tb.numerator, "denominator": tb.denominator},
            }
        )
        return result

    spans: list[dict[str, Any]] = []
    for start, end in safe_ranges:
        spans.append(
            {
                "span_id": f"clean-{start:09d}-{end:09d}",
                "start_frame": start,
                "end_frame": end,
                "frame_count": end - start,
                "start_pts": pts_at_boundary(start),
                "end_pts": pts_at_boundary(end),
                "frame_pts": points[start:end],
                "start_time": float(pts_at_boundary(start) * tb),
                "end_time": float(pts_at_boundary(end) * tb),
                "time_base": {"numerator": tb.numerator, "denominator": tb.denominator},
                "status": "clean",
            }
        )
    metadata = dict(detector_metadata or {})
    metadata.setdefault("transition_detector_version", TRANSITION_DETECTOR_VERSION)
    return {
        "schema_version": FRAME_CONTRACT_SCHEMA_VERSION,
        "frame_count": frame_count,
        "frame_pts": points,
        "end_pts": terminal,
        "time_base": {"numerator": tb.numerator, "denominator": tb.denominator},
        "start_time": float(points[0] * tb),
        "end_time": float(terminal * tb),
        "clean_spans": spans,
        "transition_zones": [timed_zone(item) for item in transitions],
        "contamination_zones": [timed_zone(item) for item in contaminations],
        "detector": metadata,
    }


def analyze_production_frames(
    frames: Sequence[Any],
    frame_pts: Sequence[int],
    time_base: Any,
    *,
    fps: float,
    end_pts: int | None = None,
) -> dict[str, Any]:
    """Create overlay analysis and a strict clean-frame contract in one pass.

    This helper is intended for bounded donor chunks/virtual intervals.  It is
    fully local and deterministic; callers processing a long documentary can
    invoke it chunk-by-chunk while retaining absolute frame indices/PTS.
    """

    if len(frames) != len(frame_pts):
        raise DonorError("frames and frame_pts must have equal length")
    try:
        from .overlay_detector import (
            analyze_temporal_overlays,
            compute_transition_metrics,
        )
    except ImportError as exc:  # pragma: no cover
        raise DonorError("overlay detector dependencies are unavailable") from exc
    frame_width = int(getattr(frames[0], "shape", (0, 0))[1])
    overlay_analysis = analyze_temporal_overlays(
        frames,
        fps=fps,
        # Keep physical safety padding roughly resolution-independent. The
        # public detector default remains conservative for direct callers.
        padding_pixels=max(4, int(round(frame_width * 0.0083333333))),
        edge_anchor_percent=0.10,
    )
    metrics = compute_transition_metrics(frames)
    transition_zones = detect_transition_zones(
        metrics["frame_luma"],
        frame_mae=metrics["frame_mae"],
        histogram_distance=metrics["histogram_distance"],
    )
    frame_contract = build_frame_clean_spans(
        frame_pts,
        time_base,
        transition_zones=transition_zones,
        contamination_zones=overlay_analysis.get("excluded_ranges") or (),
        end_pts=end_pts,
        detector_metadata={
            "transition_detector_version": TRANSITION_DETECTOR_VERSION,
            "overlay_detector": dict(overlay_analysis["detector"]),
            "overlay_confidence": overlay_analysis["confidence"],
        },
    )
    return {
        "frame_contract": frame_contract,
        "overlay_analysis": overlay_analysis,
        "metrics": metrics,
    }


def slice_frame_contract(
    contract: Mapping[str, Any],
    source_in: float,
    source_out: float,
) -> dict[str, Any]:
    """Return the frame-exact portion of a donor contract for one clip."""

    if not isinstance(contract, Mapping):
        raise DonorError("frame contract must be a mapping")
    try:
        points = [int(value) for value in contract["frame_pts"]]
        terminal = int(contract["end_pts"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DonorError("frame contract requires frame_pts and end_pts") from exc
    tb = normalize_time_base(contract.get("time_base"))
    if source_in < 0 or source_out <= source_in:
        raise DonorError("clip source range is invalid")
    times = [float(point * tb) for point in points]
    start_frame = bisect.bisect_left(times, float(source_in) - _EPSILON)
    end_frame = bisect.bisect_left(times, float(source_out) - _EPSILON)
    end_frame = min(len(points), max(start_frame, end_frame))

    def boundary_pts(index: int) -> int:
        return points[index] if index < len(points) else terminal

    clean_spans: list[dict[str, Any]] = []
    for raw in contract.get("clean_spans") or ():
        if not isinstance(raw, Mapping):
            raise DonorError("frame contract clean_spans must contain mappings")
        left = max(start_frame, int(raw["start_frame"]))
        right = min(end_frame, int(raw["end_frame"]))
        if left >= right:
            continue
        clean_spans.append(
            {
                "span_id": f"clean-{left:09d}-{right:09d}",
                "start_frame": left,
                "end_frame": right,
                "frame_count": right - left,
                "start_pts": boundary_pts(left),
                "end_pts": boundary_pts(right),
                "frame_pts": points[left:right],
                "start_time": float(boundary_pts(left) * tb),
                "end_time": float(boundary_pts(right) * tb),
                "time_base": {"numerator": tb.numerator, "denominator": tb.denominator},
                "status": "clean",
            }
        )

    transition_zones = [
        dict(item)
        for item in contract.get("transition_zones") or ()
        if isinstance(item, Mapping)
        and int(item["start_frame"]) < end_frame
        and int(item["end_frame"]) > start_frame
    ]
    contamination_zones = [
        dict(item)
        for item in contract.get("contamination_zones") or ()
        if isinstance(item, Mapping)
        and int(item["start_frame"]) < end_frame
        and int(item["end_frame"]) > start_frame
    ]
    return {
        "schema_version": int(contract.get("schema_version", FRAME_CONTRACT_SCHEMA_VERSION)),
        "source_start_frame": start_frame,
        "source_end_frame": end_frame,
        "clean_spans": clean_spans,
        "transition_zones": transition_zones,
        "contamination_zones": contamination_zones,
        "frame_contract": {
            "schema_version": int(contract.get("schema_version", FRAME_CONTRACT_SCHEMA_VERSION)),
            "time_base": {"numerator": tb.numerator, "denominator": tb.denominator},
            "detector": dict(contract.get("detector") or {}),
        },
    }


def normalize_shots(
    duration: float,
    shots: Iterable[ShotBoundary | tuple[float, float]],
) -> tuple[ShotBoundary, ...]:
    """Turn detector boundaries into exact, gap-free coverage of the donor."""
    duration = _positive_number(duration, "duration")
    cuts = {0.0, duration}
    for shot in shots:
        start, end = (shot.start, shot.end) if isinstance(shot, ShotBoundary) else shot
        for value in (start, end):
            try:
                point = float(value)
            except (TypeError, ValueError) as exc:
                raise DonorError("shot boundary is not numeric") from exc
            if not math.isfinite(point):
                raise DonorError("shot boundary is not finite")
            cuts.add(min(duration, max(0.0, point)))
    ordered = sorted(cuts)
    normalized = []
    for start, end in zip(ordered, ordered[1:]):
        if end - start > _EPSILON:
            normalized.append(ShotBoundary(round(start, 6), round(end, 6)))
    return tuple(normalized)


def detect_shots(
    path: str | Path,
    duration: float,
    *,
    threshold: float = 22.0,
    min_scene_len_frames: int = 15,
    start_seconds: float = 0.0,
    max_seconds: float | None = None,
) -> tuple[ShotBoundary, ...]:
    """Detect scene cuts locally with PySceneDetect's ContentDetector."""
    source = _local_media_path(path)
    duration = _positive_number(duration, "duration")
    start_seconds = float(start_seconds)
    if not math.isfinite(start_seconds) or start_seconds < 0:
        raise ValueError("start_seconds must be a finite non-negative number")
    if start_seconds >= duration:
        raise DonorError("detection range starts after the donor ends")
    if max_seconds is not None:
        max_seconds = _positive_number(max_seconds, "max_seconds")
    range_end = min(duration, start_seconds + max_seconds) if max_seconds else duration
    if range_end - start_seconds < MIN_CLIP_SECONDS - _EPSILON:
        raise DonorError("detection range is shorter than one virtual clip")
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    if min_scene_len_frames <= 0:
        raise ValueError("min_scene_len_frames must be positive")
    try:
        from scenedetect import ContentDetector, detect
    except ImportError as exc:
        raise DonorError("PySceneDetect is required for donor indexing") from exc
    try:
        detected = detect(
            str(source),
            ContentDetector(
                threshold=float(threshold),
                min_scene_len=int(min_scene_len_frames),
            ),
            show_progress=False,
            start_in_scene=True,
            start_time=start_seconds if start_seconds > 0 else None,
            end_time=range_end if range_end < duration else None,
        )
    except Exception as exc:  # PySceneDetect wraps backend-specific failures.
        raise DonorError(f"scene detection failed for {source.name}: {exc}") from exc
    raw_relative = [
        (
            float(start.get_seconds()) - start_seconds,
            float(end.get_seconds()) - start_seconds,
        )
        for start, end in detected
    ]
    relative = normalize_shots(range_end - start_seconds, raw_relative)
    return tuple(
        ShotBoundary(
            round(shot.start + start_seconds, 6),
            round(shot.end + start_seconds, 6),
        )
        for shot in relative
    )


def _partition_count(
    duration: float,
    min_len: float,
    max_len: float,
) -> int | None:
    smallest = max(1, math.ceil((duration - _EPSILON) / max_len))
    largest = math.floor((duration + _EPSILON) / min_len)
    if smallest > largest:
        return None
    # Prefer the fewest valid virtual clips: scene cuts still win when present,
    # while synthetic subdivision remains cheap enough for a fast canary.
    return smallest


def _remainder_is_feasible(duration: float, min_len: float, max_len: float) -> bool:
    if abs(duration) <= _EPSILON:
        return True
    if duration < min_len - _EPSILON:
        return False
    return _partition_count(duration, min_len, max_len) is not None


def sample_timestamps(
    start: float,
    end: float,
    fractions: Sequence[float] = SAMPLE_FRACTIONS,
) -> tuple[float, float, float, float]:
    """Absolute donor timestamps at 10/35/65/90 percent of an interval."""
    if end <= start:
        raise ValueError("sample interval must have positive duration")
    if len(fractions) != 4 or any(value <= 0 or value >= 1 for value in fractions):
        raise ValueError("exactly four sample fractions inside (0, 1) are required")
    length = end - start
    return tuple(round(start + length * value, 6) for value in fractions)  # type: ignore[return-value]


def plan_virtual_intervals(
    duration: float,
    shots: Iterable[ShotBoundary | tuple[float, float]],
    *,
    min_len: float = MIN_CLIP_SECONDS,
    max_len: float = MAX_CLIP_SECONDS,
) -> tuple[VirtualInterval, ...]:
    """Create gap-free 4–8 second source ranges using scene cuts as soft edges.

    Short shots are merged.  A long shot is split into balanced virtual ranges;
    critically, its remainder is rebalanced instead of discarded.  Every frame
    of a donor lasting at least ``min_len`` is therefore represented exactly once.
    """
    duration = _positive_number(duration, "duration")
    min_len = _positive_number(min_len, "min_len")
    max_len = _positive_number(max_len, "max_len")
    if max_len < min_len:
        raise ValueError("max_len must be greater than or equal to min_len")
    target_len = (min_len + max_len) / 2
    if _partition_count(duration, min_len, max_len) is None:
        raise DonorError(
            f"duration {duration:.3f}s cannot be fully covered by "
            f"{min_len:g}–{max_len:g}s intervals"
        )

    normalized = normalize_shots(duration, shots)
    preferred_cuts = [shot.end for shot in normalized[:-1]]
    starts_and_ends: list[tuple[float, float]] = []
    cursor = 0.0
    while duration - cursor > _EPSILON:
        remaining = duration - cursor
        if remaining <= max_len + _EPSILON and remaining >= min_len - _EPSILON:
            endpoint = duration
        else:
            candidates = []
            for cut in preferred_cuts:
                length = cut - cursor
                if length < min_len - _EPSILON or length > max_len + _EPSILON:
                    continue
                if _remainder_is_feasible(duration - cut, min_len, max_len):
                    candidates.append(cut)
            if candidates:
                endpoint = min(
                    candidates,
                    key=lambda cut: (abs((cut - cursor) - target_len), -(cut - cursor)),
                )
            else:
                count = _partition_count(remaining, min_len, max_len)
                if count is None:  # guarded above; defensive against float drift.
                    raise DonorError("failed to rebalance virtual interval tail")
                endpoint = cursor + remaining / count
        if duration - endpoint <= _EPSILON:
            endpoint = duration
        starts_and_ends.append((cursor, endpoint))
        cursor = endpoint

    intervals: list[VirtualInterval] = []
    for index, (start, end) in enumerate(starts_and_ends):
        rounded_start = 0.0 if index == 0 else intervals[-1].end
        rounded_end = round(duration if index == len(starts_and_ends) - 1 else end, 6)
        intervals.append(
            VirtualInterval(
                start=rounded_start,
                end=rounded_end,
                sample_timestamps=sample_timestamps(rounded_start, rounded_end),
            )
        )

    for index, interval in enumerate(intervals):
        if interval.duration < min_len - 2 * _EPSILON:
            raise DonorError("planned interval is shorter than minimum")
        if interval.duration > max_len + 2 * _EPSILON:
            raise DonorError("planned interval is longer than maximum")
        if index and abs(interval.start - intervals[index - 1].end) > _EPSILON:
            raise DonorError("planned intervals contain a gap or overlap")
    if abs(intervals[-1].end - duration) > _EPSILON:
        raise DonorError("planned intervals do not cover the usable tail")
    return tuple(intervals)


def index_donor(
    path: str | Path,
    *,
    ffprobe_bin: str = "ffprobe",
    probe_timeout: float = 90.0,
    detector_threshold: float = 22.0,
    min_scene_len_frames: int = 15,
    start_seconds: float = 0.0,
    max_seconds: float | None = None,
    frame_contract: Mapping[str, Any] | None = None,
    overlay_analysis: Mapping[str, Any] | None = None,
) -> DonorIndex:
    """Run the complete local-only donor indexing foundation."""
    media = inspect_donor(path, ffprobe_bin=ffprobe_bin, timeout=probe_timeout)
    shots = detect_shots(
        media.path,
        media.duration,
        threshold=detector_threshold,
        min_scene_len_frames=min_scene_len_frames,
        start_seconds=start_seconds,
        max_seconds=max_seconds,
    )
    range_start = round(float(start_seconds), 6)
    range_end = round(
        min(media.duration, range_start + max_seconds)
        if max_seconds is not None
        else media.duration,
        6,
    )
    relative_shots = [
        (shot.start - range_start, shot.end - range_start) for shot in shots
    ]
    intervals = plan_virtual_intervals(range_end - range_start, relative_shots)
    clips = []
    detected_cuts = tuple(shot.end for shot in shots[:-1])
    for interval in intervals:
        source_in = round(range_start + interval.start, 6)
        source_out = round(range_start + interval.end, 6)
        samples = sample_timestamps(source_in, source_out)
        internal_cuts = tuple(
            round(cut, 6)
            for cut in detected_cuts
            if source_in + _EPSILON < cut < source_out - _EPSILON
        )
        sliced = (
            slice_frame_contract(frame_contract, source_in, source_out)
            if frame_contract is not None
            else None
        )
        clean_spans = tuple(sliced["clean_spans"]) if sliced is not None else ()
        longest_clean = max(
            clean_spans,
            key=lambda item: (int(item["frame_count"]), -int(item["start_frame"])),
            default=None,
        )
        time_base = (
            dict(sliced["frame_contract"]["time_base"])
            if sliced is not None
            else None
        )
        clips.append(
            DonorClip(
                clip_id=(
                    f"{media.source_id[:16]}_"
                    f"{round(source_in * 1000):010d}_{round(source_out * 1000):010d}"
                ),
                source_id=media.source_id,
                source_path=media.path,
                source_in=source_in,
                source_out=source_out,
                duration=round(source_out - source_in, 6),
                sample_times=samples,
                shot_cuts=internal_cuts,
                source_start_frame=(
                    int(sliced["source_start_frame"]) if sliced is not None else None
                ),
                source_end_frame=(
                    int(sliced["source_end_frame"]) if sliced is not None else None
                ),
                clean_spans=clean_spans,
                transition_zones=(
                    tuple(sliced["transition_zones"]) if sliced is not None else ()
                ),
                frame_contract=(
                    dict(sliced["frame_contract"]) if sliced is not None else None
                ),
                fps=media.fps if sliced is not None else None,
                time_base=time_base,
                clean_frame_start=(
                    int(longest_clean["start_frame"])
                    if longest_clean is not None
                    else None
                ),
                clean_frame_end=(
                    int(longest_clean["end_frame"])
                    if longest_clean is not None
                    else None
                ),
                clean_source_in=(
                    float(longest_clean["start_time"])
                    if longest_clean is not None
                    else None
                ),
                clean_source_out=(
                    float(longest_clean["end_time"])
                    if longest_clean is not None
                    else None
                ),
                overlay_analysis=(
                    dict(overlay_analysis) if overlay_analysis is not None else None
                ),
            )
        )
    return DonorIndex(
        media=media,
        shots=shots,
        clips=tuple(clips),
        range_start=range_start,
        range_end=range_end,
        frame_contract=(dict(frame_contract) if frame_contract is not None else None),
    )
