"""Bounded, exact-PTS production analysis for shortlisted donor ranges.

The public :func:`analyze_strict_candidate` wrapper remains compatible with
the legacy caller, but its execution contract is deliberately stricter:

* frame addresses come from integer ``ffprobe`` PTS, never average FPS;
* decode is aspect-preserving and bounded by frame/byte/window limits;
* overlays are evaluated against a donor profile sampled in separated
  contexts, so a candidate-local natural edge cannot become an automatic crop;
* both accepted and rejected results can be cached content-addressably; and
* no audio, hold, padding, inpaint, manual crop, or retime path exists here.
"""
from __future__ import annotations

import bisect
from fractions import Fraction
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence

from .broad_lazy import (
    BroadLazyError,
    EXACT_PTS_CONTRACT,
    build_donor_overlay_profile,
    build_exact_pts_window_contract,
    resolve_strict_detector_policy,
    strict_window_cache_key,
    validate_donor_overlay_profile,
)
from .common import atomic_write_json, canonical_hash
from .donor import (
    DonorError,
    build_frame_clean_spans,
    detect_transition_zones,
    inspect_donor,
    normalize_time_base,
)
from .overlay_detector import (
    OverlayDetectorError,
    analyze_candidate_overlay_with_profile,
    build_separated_context_overlay_profile,
    compute_transition_metrics,
)


STRICT_ANALYZER_VERSION = "targeted-exact-pts-v3.2-ffmpeg44-passthrough"
STRICT_RESULT_CACHE_CONTRACT = "strict_candidate_result_cache_v2"
STRICT_VARIANT_CONTRACT = "strict_clean_span_variant_v1"
STRICT_REMATERIALIZATION_CONTRACT = "strict_exact_pts_rematerialization_v1"
# Must remain equal to broad_lazy_runtime.MAX_STRICT_VARIANTS_PER_TASK.  This
# producer cannot import the runtime without introducing a dependency cycle.
MAX_STRICT_VARIANTS = 16
DEFAULT_MAX_WINDOW_SECONDS = 30.0
DEFAULT_MAX_DECODE_FRAMES = 750
DEFAULT_MAX_DECODE_BYTES = 64 * 1024 * 1024
_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class StrictCandidateError(ValueError):
    """A source range cannot satisfy the exact production contract."""


@lru_cache(maxsize=32)
def _inspect_cached(path: str, ffprobe: str, size: int, mtime_ns: int):
    """Avoid re-hashing a multi-gigabyte donor for every shortlisted range."""

    del size, mtime_ns  # They intentionally participate in cache identity.
    return inspect_donor(path, ffprobe_bin=ffprobe)


def _finite(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise StrictCandidateError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise StrictCandidateError(f"{name} must be finite")
    return result


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise StrictCandidateError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise StrictCandidateError(f"{name} must be a positive integer") from exc
    if result <= 0 or result != value:
        raise StrictCandidateError(f"{name} must be a positive integer")
    return result


def _portable_identifier(value: Any, fallback: str) -> str:
    raw = str(value or "").strip()
    if _PORTABLE_ID.fullmatch(raw):
        return raw
    cleaned = re.sub(r"[^A-Za-z0-9._:-]+", "-", raw).strip("-._:")[:240]
    return cleaned if cleaned and _PORTABLE_ID.fullmatch(cleaned) else fallback


def _time_base_document(value: Any) -> tuple[Fraction, dict[str, int]]:
    try:
        fraction = normalize_time_base(value)
    except DonorError as exc:
        raise StrictCandidateError(str(exc)) from exc
    return fraction, {
        "numerator": fraction.numerator,
        "denominator": fraction.denominator,
    }


def _run_json(command: Sequence[str], *, timeout: float, label: str) -> Mapping[str, Any]:
    try:
        result = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=float(timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StrictCandidateError(f"{label} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "unknown process error").strip()[-1000:]
        raise StrictCandidateError(f"{label} rejected: {detail}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise StrictCandidateError(f"{label} returned malformed JSON") from exc
    if not isinstance(payload, Mapping):
        raise StrictCandidateError(f"{label} returned a non-object JSON document")
    return payload


def probe_exact_frame_window(
    source_path: str | Path,
    *,
    start: float,
    end: float,
    ffprobe: str = "ffprobe",
    timeout: float = 180.0,
    guard_seconds: float = 2.0,
    max_window_seconds: float = DEFAULT_MAX_WINDOW_SECONDS,
    max_frames: int = DEFAULT_MAX_DECODE_FRAMES,
) -> dict[str, Any]:
    """Probe a bounded window and snap it to observed integer PTS boundaries."""

    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise StrictCandidateError(f"candidate source is missing: {source}")
    start = _finite(start, "start")
    end = _finite(end, "end")
    guard_seconds = _finite(guard_seconds, "guard_seconds")
    max_window_seconds = _finite(max_window_seconds, "max_window_seconds")
    max_frames = _positive_integer(max_frames, "max_frames")
    if start < 0 or end <= start:
        raise StrictCandidateError("analysis window must satisfy 0 <= start < end")
    if guard_seconds < 0 or max_window_seconds <= 0:
        raise StrictCandidateError("probe bounds must be positive")
    if end - start > max_window_seconds + 1e-9:
        raise StrictCandidateError("analysis window exceeds the configured duration bound")
    probe_start = max(0.0, start - guard_seconds)
    probe_end = end + guard_seconds
    payload = _run_json(
        [
            str(ffprobe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-read_intervals",
            f"{probe_start:.9f}%{probe_end:.9f}",
            "-show_entries",
            "stream=time_base,width,height:frame=best_effort_timestamp,pkt_duration",
            "-of",
            "json",
            str(source),
        ],
        timeout=timeout,
        label="exact frame window probe",
    )
    streams = payload.get("streams") or ()
    if not streams or not isinstance(streams[0], Mapping):
        raise StrictCandidateError("exact frame window probe returned no video stream")
    stream = streams[0]
    try:
        width, height = int(stream["width"]), int(stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StrictCandidateError("exact frame window probe returned invalid dimensions") from exc
    if width <= 0 or height <= 0:
        raise StrictCandidateError("exact frame window probe returned invalid dimensions")
    time_base, time_base_doc = _time_base_document(stream.get("time_base"))
    rows: list[tuple[int, int | None]] = []
    for index, raw in enumerate(payload.get("frames") or ()):
        if not isinstance(raw, Mapping) or raw.get("best_effort_timestamp") is None:
            raise StrictCandidateError(f"probed video frame {index} has no integer PTS")
        try:
            point = int(raw["best_effort_timestamp"])
            duration = int(raw["pkt_duration"]) if raw.get("pkt_duration") is not None else None
        except (TypeError, ValueError) as exc:
            raise StrictCandidateError(f"probed video frame {index} has invalid PTS") from exc
        if rows and point <= rows[-1][0]:
            raise StrictCandidateError("probed frame PTS values are not strictly increasing")
        rows.append((point, duration if duration and duration > 0 else None))
    if not rows:
        raise StrictCandidateError("exact frame window probe returned no frames")
    points = [row[0] for row in rows]
    frame_times = [Fraction(point) * time_base for point in points]
    requested_start = Fraction(str(start))
    requested_end = Fraction(str(end))
    start_index = bisect.bisect_left(frame_times, requested_start)
    if start_index >= len(points):
        raise StrictCandidateError("analysis start has no observed frame boundary")
    end_index = bisect.bisect_left(frame_times, requested_end, lo=start_index + 1)
    source_end_pts: int | None = None
    if end_index < len(points):
        end_pts = points[end_index]
    else:
        last_duration = rows[-1][1]
        if last_duration is not None:
            source_end_pts = points[-1] + last_duration
        else:
            # Some MP4 decoders omit the final decoded frame's pkt_duration.
            # Packet PTS+duration is still an exact coded-stream boundary and
            # avoids the unsafe legacy median/average-cadence fallback.
            packet_payload = _run_json(
                [
                    str(ffprobe),
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-read_intervals",
                    f"{probe_start:.9f}%{probe_end:.9f}",
                    "-show_entries",
                    "packet=pts,duration",
                    "-show_packets",
                    "-of",
                    "json",
                    str(source),
                ],
                timeout=timeout,
                label="exact packet terminal probe",
            )
            packet_ends: list[int] = []
            for packet in packet_payload.get("packets") or ():
                if not isinstance(packet, Mapping):
                    continue
                try:
                    packet_pts = int(packet["pts"])
                    packet_duration = int(packet["duration"])
                except (KeyError, TypeError, ValueError):
                    continue
                if packet_duration > 0:
                    packet_ends.append(packet_pts + packet_duration)
            source_end_pts = max(packet_ends, default=0)
            if source_end_pts <= points[-1]:
                raise StrictCandidateError(
                    "analysis tail has no exact frame or packet terminal boundary"
                )
        if Fraction(source_end_pts) * time_base + Fraction(1, 1_000_000) < requested_end:
            raise StrictCandidateError("probed terminal PTS does not cover the analysis window")
        end_pts = source_end_pts
    try:
        exact = build_exact_pts_window_contract(
            points,
            time_base_doc,
            start_pts=points[start_index],
            end_pts=end_pts,
            source_end_pts=source_end_pts,
        )
    except BroadLazyError as exc:
        raise StrictCandidateError(str(exc)) from exc
    if int(exact["frame_count"]) > max_frames:
        raise StrictCandidateError(
            f"analysis window has {exact['frame_count']} frames; configured maximum is {max_frames}"
        )
    return {
        "schema_version": 1,
        "contract": "strict_exact_probe_v1",
        "source_dimensions": {"width": width, "height": height},
        "requested_window": {"start": start, "end": end},
        "probe_window": {"start": probe_start, "end": probe_end},
        "exact_pts": exact,
    }


def _bounded_dimensions(
    source_width: int,
    source_height: int,
    *,
    max_width: int,
    max_height: int,
    frame_count: int,
    max_decode_bytes: int,
) -> tuple[int, int, int]:
    max_width = _positive_integer(max_width, "width")
    max_height = _positive_integer(max_height, "height")
    max_decode_bytes = _positive_integer(max_decode_bytes, "max_decode_bytes")
    scale = min(1.0, max_width / source_width, max_height / source_height)
    target_width = max(2, int(math.floor(source_width * scale / 2.0) * 2))
    target_height = max(2, int(math.floor(source_height * scale / 2.0) * 2))
    required = frame_count * target_width * target_height * 3
    if required > max_decode_bytes:
        scale *= math.sqrt(max_decode_bytes / required)
        target_width = max(2, int(math.floor(source_width * scale / 2.0) * 2))
        target_height = max(2, int(math.floor(source_height * scale / 2.0) * 2))
        required = frame_count * target_width * target_height * 3
    if target_width < 32 or target_height < 32 or required > max_decode_bytes:
        raise StrictCandidateError("analysis frames cannot fit the configured decode byte bound")
    source_ratio = source_width / source_height
    output_ratio = target_width / target_height
    if abs(output_ratio / source_ratio - 1.0) > 0.02:
        raise StrictCandidateError("bounded decode dimensions do not preserve source aspect ratio")
    return target_width, target_height, required


def decode_exact_analysis_window(
    source_path: str | Path,
    probe: Mapping[str, Any],
    *,
    width: int = 480,
    height: int = 270,
    ffmpeg: str = "ffmpeg",
    timeout: float = 180.0,
    max_decode_bytes: int = DEFAULT_MAX_DECODE_BYTES,
) -> dict[str, Any]:
    """Decode exactly the probed PTS window into bounded BGR frames."""

    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - explicit dependency gate.
        raise StrictCandidateError("numpy is required for strict candidate analysis") from exc
    source = Path(source_path).expanduser().resolve()
    exact = probe.get("exact_pts") if isinstance(probe, Mapping) else None
    dimensions = probe.get("source_dimensions") if isinstance(probe, Mapping) else None
    if not isinstance(exact, Mapping) or not isinstance(dimensions, Mapping):
        raise StrictCandidateError("exact probe contract is malformed")
    frame_count = _positive_integer(exact.get("frame_count"), "probe.frame_count")
    try:
        source_width = int(dimensions["width"])
        source_height = int(dimensions["height"])
        start_pts = int(exact["start_pts"])
        end_pts = int(exact["end_pts"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StrictCandidateError("exact probe contract is malformed") from exc
    target_width, target_height, expected_bytes = _bounded_dimensions(
        source_width,
        source_height,
        max_width=width,
        max_height=height,
        frame_count=frame_count,
        max_decode_bytes=max_decode_bytes,
    )
    start_fraction = exact.get("start_time") or {}
    try:
        start_seconds = float(
            Fraction(int(start_fraction["numerator"]), int(start_fraction["denominator"]))
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise StrictCandidateError("exact probe start time is malformed") from exc
    seek_seconds = max(0.0, start_seconds - 2.0)
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-copyts",
        "-ss",
        f"{seek_seconds:.9f}",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        (
            f"trim=start_pts={start_pts}:end_pts={end_pts},"
            f"setpts=PTS-STARTPTS,scale={target_width}:{target_height}:flags=area"
        ),
        # Numeric vsync=0 is passthrough on FFmpeg 4.4+; unlike the newer
        # -fps_mode spelling it also works on the pinned Ubuntu 22.04 runtime.
        "-vsync",
        "0",
        "-pix_fmt",
        "bgr24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=float(timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StrictCandidateError(f"candidate decode failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or b"unknown ffmpeg error")[-1000:].decode(
            "utf-8", errors="replace"
        )
        raise StrictCandidateError(f"candidate decode rejected: {detail}")
    if len(result.stdout) != expected_bytes:
        raise StrictCandidateError(
            "candidate decode frame count disagrees with the exact PTS probe "
            f"({len(result.stdout)} bytes != {expected_bytes})"
        )
    array = np.frombuffer(result.stdout, dtype=np.uint8).reshape(
        (frame_count, target_height, target_width, 3)
    )
    if len(array) < 3:
        raise StrictCandidateError("candidate analysis window has fewer than three frames")
    return {
        "frames": [array[index] for index in range(len(array))],
        "dimensions": {"width": target_width, "height": target_height},
        "decoded_bytes": expected_bytes,
        "exact_pts": dict(exact),
    }


def decode_analysis_window(
    source_path: str | Path,
    *,
    start: float,
    end: float,
    width: int = 480,
    height: int = 270,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    timeout: float = 180.0,
    max_decode_bytes: int = DEFAULT_MAX_DECODE_BYTES,
) -> list[Any]:
    """Compatibility wrapper returning bounded, exact-PTS BGR frames."""

    probe = probe_exact_frame_window(
        source_path,
        start=start,
        end=end,
        ffprobe=ffprobe,
        timeout=timeout,
    )
    decoded = decode_exact_analysis_window(
        source_path,
        probe,
        width=width,
        height=height,
        ffmpeg=ffmpeg,
        timeout=timeout,
        max_decode_bytes=max_decode_bytes,
    )
    return list(decoded["frames"])


def _eligible_exact_span_selections(
    spans: Sequence[Mapping[str, Any]],
    frame_pts: Sequence[int],
    *,
    end_pts: int,
    time_base: Fraction,
    render_frames: int,
    output_fps: float,
    preferred_time: float,
) -> tuple[list[tuple[tuple[float, float, int], dict[str, Any]]], int]:
    """Return the best exact selection inside every eligible clean span."""

    points = [int(value) for value in frame_pts]
    boundaries = [Fraction(point) * time_base for point in points]
    boundaries.append(Fraction(int(end_pts)) * time_base)
    required_duration = Fraction(render_frames, 1) / Fraction(str(output_fps))
    eligible: list[tuple[tuple[float, float, int], dict[str, Any]]] = []
    longest = 0
    for span_index, raw_span in enumerate(spans):
        start_frame = int(raw_span.get("start_frame", 0))
        end_frame = int(raw_span.get("end_frame", 0))
        longest = max(longest, end_frame - start_frame)
        if not 0 <= start_frame < end_frame <= len(points):
            continue
        span_selections: list[tuple[tuple[float, float, int], dict[str, Any]]] = []
        for selected_start in range(start_frame, end_frame):
            target = boundaries[selected_start] + required_duration
            selected_end = bisect.bisect_left(
                boundaries,
                target,
                lo=selected_start + 1,
                hi=end_frame + 1,
            )
            if selected_end > end_frame:
                continue
            source_frames = selected_end - selected_start
            if source_frames < render_frames:
                continue
            source_in = float(boundaries[selected_start])
            source_out = float(boundaries[selected_end])
            selection = {
                "span": dict(raw_span),
                "span_index": span_index,
                "source_frame_start": selected_start,
                "source_frame_end": selected_end,
                "source_frames_used": source_frames,
                "source_in": source_in,
                "source_out": source_out,
            }
            score = (
                abs(source_in - preferred_time),
                source_out - source_in - float(required_duration),
                -int(raw_span.get("frame_count", end_frame - start_frame)),
            )
            span_selections.append((score, selection))
        if span_selections:
            eligible.append(
                min(
                    span_selections,
                    key=lambda item: (
                        item[0],
                        item[1]["source_frame_start"],
                        item[1]["source_frame_end"],
                    ),
                )
            )
    return eligible, longest


def _select_exact_span(
    spans: Sequence[Mapping[str, Any]],
    frame_pts: Sequence[int],
    *,
    end_pts: int,
    time_base: Fraction,
    render_frames: int,
    output_fps: float,
    preferred_time: float,
) -> dict[str, Any]:
    eligible, longest = _eligible_exact_span_selections(
        spans,
        frame_pts,
        end_pts=end_pts,
        time_base=time_base,
        render_frames=render_frames,
        output_fps=output_fps,
        preferred_time=preferred_time,
    )
    if not eligible:
        raise StrictCandidateError(
            "no clean exact-PTS span can provide "
            f"{render_frames} output frames; longest_source_span={longest} frames"
        )
    return min(eligible, key=lambda item: item[0])[1]


def _strict_variant_identity(candidate: Mapping[str, Any]) -> str:
    """Mirror the canonical exact-range identity consumed by broad lazy runtime."""

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


def _validate_exact_pts_lineage(
    frame_contract: Mapping[str, Any],
) -> tuple[list[int], Fraction, dict[str, int], int, list[dict[str, Any]]]:
    """Validate cached frame/clean-span evidence before deriving alternatives."""

    exact = frame_contract.get("exact_pts_contract")
    if not isinstance(exact, Mapping):
        raise StrictCandidateError("strict result has no exact PTS lineage contract")
    exact_row = dict(exact)
    if (
        exact_row.get("schema_version") != 1
        or exact_row.get("contract") != EXACT_PTS_CONTRACT
        or exact_row.get("addressing") != "integer_pts_half_open"
        or exact_row.get("fps_inference_used") is not False
    ):
        raise StrictCandidateError("strict exact PTS lineage contract is incompatible")
    supplied_hash = str(exact_row.get("content_hash") or "")
    supplied_id = str(exact_row.get("window_id") or "")
    exact_base = {
        key: value
        for key, value in exact_row.items()
        if key not in {"content_hash", "window_id"}
    }
    actual_hash = canonical_hash(exact_base)
    if supplied_hash != actual_hash or supplied_id != f"pts-window-{actual_hash[:24]}":
        raise StrictCandidateError("strict exact PTS lineage hash/id mismatch")

    raw_points = exact_row.get("frame_pts")
    if not isinstance(raw_points, Sequence) or isinstance(
        raw_points, (str, bytes, bytearray)
    ):
        raise StrictCandidateError("strict exact PTS lineage has no frame vector")
    try:
        points = [int(value) for value in raw_points]
        start_pts = int(exact_row["start_pts"])
        end_pts = int(exact_row["end_pts"])
        source_frame_start = int(exact_row["source_frame_start"])
        source_frame_end = int(exact_row["source_frame_end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StrictCandidateError("strict exact PTS lineage is malformed") from exc
    if (
        not points
        or any(right <= left for left, right in zip(points, points[1:]))
        or end_pts <= points[-1]
        or int(exact_row.get("frame_count") or 0) != len(points)
        or start_pts != points[0]
        or source_frame_start < 0
        or source_frame_end - source_frame_start != len(points)
    ):
        raise StrictCandidateError("strict exact PTS lineage frame bounds are invalid")
    time_base, time_base_doc = _time_base_document(exact_row.get("time_base"))
    if dict(exact_row.get("time_base") or {}) != time_base_doc:
        raise StrictCandidateError("strict exact PTS lineage time base is non-canonical")

    for label, supplied, expected in (
        ("start_time", exact_row.get("start_time"), Fraction(points[0]) * time_base),
        ("end_time", exact_row.get("end_time"), Fraction(end_pts) * time_base),
        ("duration", exact_row.get("duration"), Fraction(end_pts - points[0]) * time_base),
    ):
        if not isinstance(supplied, Mapping):
            raise StrictCandidateError(f"strict exact PTS {label} is malformed")
        try:
            numerator = int(supplied["numerator"])
            denominator = int(supplied["denominator"])
            value = Fraction(numerator, denominator)
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise StrictCandidateError(f"strict exact PTS {label} is malformed") from exc
        if value != expected or dict(supplied) != {
            "numerator": expected.numerator,
            "denominator": expected.denominator,
        }:
            raise StrictCandidateError(f"strict exact PTS {label} disagrees with frame bounds")

    try:
        if (
            int(frame_contract.get("frame_count") or 0) != len(points)
            or [int(value) for value in frame_contract.get("frame_pts") or ()] != points
            or int(frame_contract.get("end_pts")) != end_pts
            or dict(frame_contract.get("time_base") or {}) != time_base_doc
            or abs(
                float(frame_contract.get("start_time"))
                - float(Fraction(points[0]) * time_base)
            )
            > 1e-9
            or abs(
                float(frame_contract.get("end_time"))
                - float(Fraction(end_pts) * time_base)
            )
            > 1e-9
        ):
            raise StrictCandidateError("frame contract disagrees with exact PTS lineage")
    except (TypeError, ValueError) as exc:
        raise StrictCandidateError("frame contract exact bounds are malformed") from exc

    raw_spans = frame_contract.get("clean_spans")
    raw_transitions = frame_contract.get("transition_zones") or ()
    raw_contaminations = frame_contract.get("contamination_zones") or ()
    if (
        not isinstance(raw_spans, Sequence)
        or isinstance(raw_spans, (str, bytes, bytearray))
        or not isinstance(raw_transitions, Sequence)
        or isinstance(raw_transitions, (str, bytes, bytearray))
        or not isinstance(raw_contaminations, Sequence)
        or isinstance(raw_contaminations, (str, bytes, bytearray))
    ):
        raise StrictCandidateError("frame contract clean/unsafe spans are malformed")
    if any(
        not isinstance(row, Mapping)
        for row in (*raw_spans, *raw_transitions, *raw_contaminations)
    ):
        raise StrictCandidateError("frame contract clean/unsafe span row is malformed")
    try:
        rebuilt = build_frame_clean_spans(
            points,
            time_base_doc,
            transition_zones=raw_transitions,
            contamination_zones=raw_contaminations,
            end_pts=end_pts,
            detector_metadata=(
                frame_contract.get("detector")
                if isinstance(frame_contract.get("detector"), Mapping)
                else None
            ),
        )
    except DonorError as exc:
        raise StrictCandidateError(str(exc)) from exc
    for field in (
        "schema_version",
        "frame_count",
        "frame_pts",
        "end_pts",
        "time_base",
        "start_time",
        "end_time",
        "clean_spans",
        "transition_zones",
        "contamination_zones",
        "detector",
    ):
        if frame_contract.get(field) != rebuilt[field]:
            raise StrictCandidateError(f"frame contract {field} failed canonical validation")
    return (
        points,
        time_base,
        time_base_doc,
        end_pts,
        [dict(row) for row in rebuilt["clean_spans"]],
    )


def expand_strict_candidate_variants(
    analyzed_candidate: Mapping[str, Any],
    *,
    output_fps: float,
    render_frames: int | None = None,
) -> list[dict[str, Any]]:
    """Derive one exact variant per eligible clean span without decoding again.

    ``analyzed_candidate`` must be a successful result from
    :func:`analyze_strict_candidate`. Its cached exact-PTS and clean-span
    evidence is fully revalidated before any alternative is exposed.
    """

    if not isinstance(analyzed_candidate, Mapping):
        raise StrictCandidateError("analyzed_candidate must be a mapping")
    output_fps = _finite(output_fps, "output_fps")
    if output_fps <= 0:
        raise StrictCandidateError("output_fps must be positive")
    declared_render_frames = _positive_integer(
        analyzed_candidate.get("render_frames"), "analyzed_candidate.render_frames"
    )
    if render_frames is None:
        render_frames_value = declared_render_frames
    else:
        render_frames_value = _positive_integer(render_frames, "render_frames")
        if render_frames_value != declared_render_frames:
            raise StrictCandidateError("variant render_frames disagrees with strict result")
    if str(analyzed_candidate.get("fill_strategy") or "") != "none":
        raise StrictCandidateError("strict result uses a fill strategy")
    try:
        if abs(float(analyzed_candidate.get("retime_factor")) - 1.0) > 1e-9:
            raise StrictCandidateError("strict result uses retiming")
    except (TypeError, ValueError) as exc:
        raise StrictCandidateError("strict result retime factor is malformed") from exc
    if not str(analyzed_candidate.get("selection_contract") or "").startswith("frame_exact"):
        raise StrictCandidateError("strict result is not frame exact")
    if any(
        bool(analyzed_candidate.get(field))
        for field in (
            "allow_hold",
            "allow_tpad",
            "manual_selection",
            "manual_crop",
            "held_tail_seconds",
            "tpad",
            "hold_seconds",
        )
    ):
        raise StrictCandidateError("strict result enables manual/fill behavior")
    overlay = analyzed_candidate.get("overlay_analysis")
    if (
        not isinstance(overlay, Mapping)
        or overlay.get("status") != "resolved"
        or overlay.get("action") not in {"none", "crop"}
        or bool(overlay.get("inpaint_markers"))
    ):
        raise StrictCandidateError("strict result has unresolved overlay evidence")
    frame_contract = analyzed_candidate.get("frame_contract")
    if not isinstance(frame_contract, Mapping):
        raise StrictCandidateError("strict result has no frame contract")
    points, time_base, time_base_doc, end_pts, spans = _validate_exact_pts_lineage(
        frame_contract
    )
    if analyzed_candidate.get("clean_spans") != spans:
        raise StrictCandidateError("strict result clean spans disagree with frame contract")
    if analyzed_candidate.get("transition_zones") != frame_contract.get("transition_zones"):
        raise StrictCandidateError("strict result transition zones disagree with frame contract")
    analysis = analyzed_candidate.get("strict_analysis")
    if not isinstance(analysis, Mapping):
        raise StrictCandidateError("strict result has no strict analysis lineage")
    if str(analysis.get("pts_contract_hash") or "") != str(
        frame_contract["exact_pts_contract"].get("content_hash") or ""
    ):
        raise StrictCandidateError("strict result PTS hash disagrees with frame contract")
    max_decode_frames = _positive_integer(
        analysis.get("max_decode_frames"), "strict_analysis.max_decode_frames"
    )
    if len(points) > max_decode_frames:
        raise StrictCandidateError("strict result exceeds its decode-frame bound")
    try:
        source_fps = _finite(analyzed_candidate.get("fps"), "analyzed_candidate.fps")
    except StrictCandidateError:
        raise
    if source_fps + 1e-6 < output_fps:
        raise StrictCandidateError("strict result source fps is below output fps")
    if dict(analyzed_candidate.get("time_base") or {}) != time_base_doc:
        raise StrictCandidateError("strict result time base disagrees with frame contract")
    analysis_window = frame_contract.get("analysis_window")
    if not isinstance(analysis_window, Mapping):
        raise StrictCandidateError("strict result has no bounded analysis window")
    try:
        actual_window_start = _finite(analysis_window.get("start"), "analysis_window.start")
        actual_window_end = _finite(analysis_window.get("end"), "analysis_window.end")
        requested_window_start = _finite(
            analysis_window.get("requested_start"), "analysis_window.requested_start"
        )
        requested_window_end = _finite(
            analysis_window.get("requested_end"), "analysis_window.requested_end"
        )
        actual_window_frames = _positive_integer(
            analysis_window.get("frame_count"), "analysis_window.frame_count"
        )
    except StrictCandidateError:
        raise
    maximum_frame_duration = float(
        max(
            [right - left for left, right in zip(points, points[1:])]
            + [end_pts - points[-1]]
        )
        * time_base
    )
    if (
        actual_window_frames != len(points)
        or abs(actual_window_start - float(Fraction(points[0]) * time_base)) > 1e-9
        or abs(actual_window_end - float(Fraction(end_pts) * time_base)) > 1e-9
        or not 0 <= requested_window_start <= actual_window_start
        or actual_window_start - requested_window_start
        > maximum_frame_duration + 1e-9
        or not actual_window_start < requested_window_end <= actual_window_end
        or actual_window_end - requested_window_end
        > maximum_frame_duration + 1e-9
        or requested_window_end - requested_window_start
        > DEFAULT_MAX_WINDOW_SECONDS + 1e-9
    ):
        raise StrictCandidateError("strict bounded analysis window disagrees with exact PTS")
    preferred_time = _finite(
        analysis.get("preferred_source_in"), "strict_analysis.preferred_source_in"
    )
    selections, longest = _eligible_exact_span_selections(
        spans,
        points,
        end_pts=end_pts,
        time_base=time_base,
        render_frames=render_frames_value,
        output_fps=output_fps,
        preferred_time=preferred_time,
    )
    if not selections:
        raise StrictCandidateError(
            "no clean exact-PTS span can provide "
            f"{render_frames_value} output frames; longest_source_span={longest} frames"
        )
    if len(selections) > MAX_STRICT_VARIANTS:
        raise StrictCandidateError(
            "eligible clean-span variants exceed the bounded callback limit "
            f"({len(selections)} > {MAX_STRICT_VARIANTS})"
        )
    if analyzed_candidate.get("strict_variant_count") is not None:
        try:
            if int(analyzed_candidate["strict_variant_count"]) != len(selections):
                raise StrictCandidateError(
                    "declared strict variant count disagrees with clean spans"
                )
            if int(analyzed_candidate.get("strict_variant_index", 0)) != 0:
                raise StrictCandidateError("top-level strict variant must be the primary")
        except (TypeError, ValueError) as exc:
            raise StrictCandidateError("declared strict variant metadata is malformed") from exc

    try:
        primary_signature = (
            int(analyzed_candidate.get("source_frame_start")),
            int(analyzed_candidate.get("source_frame_end")),
            int(analyzed_candidate.get("clean_frame_start")),
            int(analyzed_candidate.get("clean_frame_end")),
        )
    except (TypeError, ValueError) as exc:
        raise StrictCandidateError("primary strict selection frame bounds are malformed") from exc
    declared_primary_key = str(analyzed_candidate.get("strict_variant_key") or "")
    if declared_primary_key and declared_primary_key != _strict_variant_identity(
        analyzed_candidate
    ):
        raise StrictCandidateError(
            "declared strict variant key disagrees with the primary exact range"
        )
    generated: list[dict[str, Any]] = []
    primary_key = ""
    for _score, selection in selections:
        span = selection["span"]
        variant = {
            key: value
            for key, value in analyzed_candidate.items()
            if key != "strict_variants"
        }
        variant.update(
            {
                "source_in": selection["source_in"],
                "source_out": selection["source_out"],
                "detected_source_out": selection["source_out"],
                "time_base": time_base_doc,
                "source_frame_start": selection["source_frame_start"],
                "source_frame_end": selection["source_frame_end"],
                "clean_frame_start": int(span["start_frame"]),
                "clean_frame_end": int(span["end_frame"]),
                "clean_source_in": float(span["start_time"]),
                "clean_source_out": float(span["end_time"]),
                "selected_clean_span": dict(span),
                "clean_span_id": str(span["span_id"]),
                "render_frames": render_frames_value,
                "fill_strategy": "none",
                "retime_factor": 1.0,
                "selection_contract": "frame_exact_v2",
                "strict_analysis": {
                    **dict(analysis),
                    "source_frames_used": int(selection["source_frames_used"]),
                    "strict_variant_contract": STRICT_VARIANT_CONTRACT,
                    "strict_variant_span_id": str(span["span_id"]),
                },
            }
        )
        key = _strict_variant_identity(variant)
        variant["strict_variant_key"] = key
        signature = (
            int(variant["source_frame_start"]),
            int(variant["source_frame_end"]),
            int(variant["clean_frame_start"]),
            int(variant["clean_frame_end"]),
        )
        if signature == primary_signature:
            try:
                primary_source_in = float(analyzed_candidate.get("source_in"))
                primary_source_out = float(analyzed_candidate.get("source_out"))
            except (TypeError, ValueError) as exc:
                raise StrictCandidateError(
                    "primary strict selection PTS bounds are malformed"
                ) from exc
            if (
                abs(float(variant["source_in"]) - primary_source_in) > 1e-9
                or abs(float(variant["source_out"]) - primary_source_out) > 1e-9
            ):
                raise StrictCandidateError("primary strict selection has inconsistent PTS bounds")
            try:
                primary_clean_source_in = float(
                    analyzed_candidate.get("clean_source_in")
                )
                primary_clean_source_out = float(
                    analyzed_candidate.get("clean_source_out")
                )
                primary_detected_source_out = float(
                    analyzed_candidate.get("detected_source_out")
                )
            except (TypeError, ValueError) as exc:
                raise StrictCandidateError(
                    "primary strict clean/source bounds are malformed"
                ) from exc
            if (
                abs(primary_clean_source_in - float(span["start_time"])) > 1e-9
                or abs(primary_clean_source_out - float(span["end_time"])) > 1e-9
                or abs(primary_detected_source_out - primary_source_out) > 1e-9
            ):
                raise StrictCandidateError(
                    "primary strict clean/source bounds disagree with exact PTS"
                )
            if analyzed_candidate.get("selected_clean_span") is not None and (
                analyzed_candidate.get("selected_clean_span") != span
                or str(analyzed_candidate.get("clean_span_id") or "")
                != str(span["span_id"])
            ):
                raise StrictCandidateError(
                    "primary selected clean-span metadata is inconsistent"
                )
            primary_key = key
        generated.append(variant)
    if not primary_key:
        raise StrictCandidateError(
            "primary strict selection is not an eligible clean-span variant"
        )
    keys = [str(row["strict_variant_key"]) for row in generated]
    if len(set(keys)) != len(keys):
        raise StrictCandidateError("strict clean-span variants have duplicate range identities")
    generated.sort(
        key=lambda row: (
            0 if row["strict_variant_key"] == primary_key else 1,
            int(row["source_frame_start"]),
            int(row["source_frame_end"]),
            str(row["strict_variant_key"]),
        )
    )
    for index, variant in enumerate(generated):
        variant["strict_variant_index"] = index
        variant["strict_variant_count"] = len(generated)
        variant["strict_analysis"]["eligible_clean_span_count"] = len(generated)
    return generated


def rematerialize_strict_candidate_variants(
    analyzed_candidate: Mapping[str, Any],
    *,
    output_fps: float,
    render_frames: int,
) -> list[dict[str, Any]]:
    """Derive new exact ranges for another output-frame count without decoding.

    Unlike :func:`expand_strict_candidate_variants`, this repair API requires a
    *different* ``render_frames`` value. It revalidates the immutable cached
    frame/PTS/clean-span evidence first, then anchors one deterministic variant
    per eligible clean span as close as possible to the currently judged
    ``source_in``. Runtime paths are neither required nor introduced.
    """

    if not isinstance(analyzed_candidate, Mapping):
        raise StrictCandidateError("analyzed_candidate must be a mapping")
    output_fps_value = _finite(output_fps, "output_fps")
    if output_fps_value <= 0:
        raise StrictCandidateError("output_fps must be positive")
    declared_render_frames = _positive_integer(
        analyzed_candidate.get("render_frames"), "analyzed_candidate.render_frames"
    )
    requested_render_frames = _positive_integer(render_frames, "render_frames")
    if requested_render_frames == declared_render_frames:
        raise StrictCandidateError(
            "rematerialized render_frames must differ from the strict result"
        )

    # A downstream exact retrieval may select any clean-span variant. Remove
    # only its list-order annotations so the existing strict validator can
    # validate that selected range as the local primary without weakening the
    # old public expand contract.
    validation_candidate = {
        key: value
        for key, value in analyzed_candidate.items()
        if key not in {"strict_variant_index", "strict_variant_count"}
    }
    expand_strict_candidate_variants(
        validation_candidate,
        output_fps=output_fps_value,
    )

    frame_contract = analyzed_candidate.get("frame_contract")
    if not isinstance(frame_contract, Mapping):
        raise StrictCandidateError("strict result has no frame contract")
    points, time_base, time_base_doc, end_pts, spans = _validate_exact_pts_lineage(
        frame_contract
    )
    analysis = analyzed_candidate.get("strict_analysis")
    if not isinstance(analysis, Mapping):
        raise StrictCandidateError("strict result has no strict analysis lineage")
    preferred_source_in = _finite(
        analyzed_candidate.get("source_in"), "analyzed_candidate.source_in"
    )
    selections, longest = _eligible_exact_span_selections(
        spans,
        points,
        end_pts=end_pts,
        time_base=time_base,
        render_frames=requested_render_frames,
        output_fps=output_fps_value,
        preferred_time=preferred_source_in,
    )
    if not selections:
        raise StrictCandidateError(
            "no clean exact-PTS span can provide "
            f"{requested_render_frames} output frames; "
            f"longest_source_span={longest} frames"
        )
    if len(selections) > MAX_STRICT_VARIANTS:
        raise StrictCandidateError(
            "eligible clean-span variants exceed the bounded callback limit "
            f"({len(selections)} > {MAX_STRICT_VARIANTS})"
        )

    source_variant_key = str(analyzed_candidate.get("strict_variant_key") or "")
    if not source_variant_key:
        source_variant_key = _strict_variant_identity(analyzed_candidate)
    generated: list[tuple[tuple[float, float, int], dict[str, Any]]] = []
    for score, selection in selections:
        span = selection["span"]
        variant = {
            key: value
            for key, value in analyzed_candidate.items()
            if key
            not in {
                "strict_variants",
                "strict_variant_index",
                "strict_variant_count",
                "candidate_range_hash",
                "contact_sheet_artifact_id",
                "exact_sheet_contract",
                "max_render_frames",
                "max_render_seconds",
            }
        }
        variant.update(
            {
                "source_in": selection["source_in"],
                "source_out": selection["source_out"],
                "detected_source_out": selection["source_out"],
                "time_base": time_base_doc,
                "source_frame_start": selection["source_frame_start"],
                "source_frame_end": selection["source_frame_end"],
                "clean_frame_start": int(span["start_frame"]),
                "clean_frame_end": int(span["end_frame"]),
                "clean_source_in": float(span["start_time"]),
                "clean_source_out": float(span["end_time"]),
                "selected_clean_span": dict(span),
                "clean_span_id": str(span["span_id"]),
                "render_frames": requested_render_frames,
                "fill_strategy": "none",
                "retime_factor": 1.0,
                "selection_contract": "frame_exact_v2",
                "strict_analysis": {
                    **dict(analysis),
                    "preferred_source_in": preferred_source_in,
                    "source_frames_used": int(selection["source_frames_used"]),
                    "strict_variant_contract": STRICT_VARIANT_CONTRACT,
                    "strict_variant_span_id": str(span["span_id"]),
                    "rematerialization_contract": STRICT_REMATERIALIZATION_CONTRACT,
                    "rematerialized_from_render_frames": declared_render_frames,
                    "rematerialized_to_render_frames": requested_render_frames,
                    "rematerialized_from_variant_key": source_variant_key,
                },
            }
        )
        variant["strict_variant_key"] = _strict_variant_identity(variant)
        generated.append((score, variant))

    generated.sort(
        key=lambda item: (
            item[0],
            int(item[1]["source_frame_start"]),
            int(item[1]["source_frame_end"]),
            str(item[1]["strict_variant_key"]),
        )
    )
    variants = [variant for _score, variant in generated]
    keys = [str(row["strict_variant_key"]) for row in variants]
    if len(set(keys)) != len(keys) or source_variant_key in keys:
        raise StrictCandidateError(
            "rematerialized variants do not have unique new exact-range identities"
        )
    for index, variant in enumerate(variants):
        variant["strict_variant_index"] = index
        variant["strict_variant_count"] = len(variants)
        variant["strict_analysis"]["eligible_clean_span_count"] = len(variants)
    return variants


def _portable_cache_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).casefold() in {
                "path",
                "source_path",
                "absolute_path",
                "original_location",
                "original_locations",
            }:
                continue
            output[str(key)] = _portable_cache_value(item)
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_portable_cache_value(item) for item in value]
    return value


def _cache_document(cache_key: str, *, status: str, payload: Any) -> dict[str, Any]:
    base = {
        "schema_version": 1,
        "contract": STRICT_RESULT_CACHE_CONTRACT,
        "cache_key": cache_key,
        "status": status,
        "payload": _portable_cache_value(payload),
    }
    return {**base, "content_hash": canonical_hash(base)}


def _cache_path(directory: str | Path, cache_key: str) -> Path:
    return Path(directory).expanduser().resolve() / f"{cache_key}.json"


def _load_cached_result(path: Path, cache_key: str, source_path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StrictCandidateError("strict result cache is unreadable or malformed") from exc
    if not isinstance(document, Mapping):
        raise StrictCandidateError("strict result cache must contain an object")
    base = {key: value for key, value in document.items() if key != "content_hash"}
    if (
        document.get("schema_version") != 1
        or document.get("contract") != STRICT_RESULT_CACHE_CONTRACT
        or document.get("cache_key") != cache_key
        or document.get("content_hash") != canonical_hash(base)
        or document.get("status") not in {"success", "rejected"}
    ):
        raise StrictCandidateError("strict result cache failed content validation")
    payload = document.get("payload")
    if document["status"] == "rejected":
        reason = payload.get("reason") if isinstance(payload, Mapping) else "cached rejection"
        raise StrictCandidateError(f"cached strict rejection: {reason}")
    if not isinstance(payload, Mapping):
        raise StrictCandidateError("strict success cache has no result object")
    result = dict(payload)
    result["source_path"] = str(source_path)
    result.setdefault("strict_analysis", {})["cache_hit"] = True
    return result


def _write_cache(path: Path, cache_key: str, *, status: str, payload: Any) -> None:
    atomic_write_json(path, _cache_document(cache_key, status=status, payload=payload))


def analyze_strict_candidate(
    candidate: Mapping[str, Any],
    *,
    render_frames: int,
    output_fps: float,
    search_seconds: float = 4.0,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    donor_overlay_profile: Mapping[str, Any] | None = None,
    donor_overlay_contexts: Sequence[Mapping[str, Any]] | None = None,
    detector_policy: Mapping[str, Any] | None = None,
    model_contract_hash: str | None = None,
    cache_directory: str | Path | None = None,
    max_decode_bytes: int = DEFAULT_MAX_DECODE_BYTES,
    max_decode_frames: int = DEFAULT_MAX_DECODE_FRAMES,
) -> dict[str, Any]:
    """Upgrade one broad candidate into an exact, immutable render contract."""

    if not isinstance(candidate, Mapping):
        raise StrictCandidateError("candidate must be a mapping")
    render_frames = _positive_integer(render_frames, "render_frames")
    output_fps = _finite(output_fps, "output_fps")
    search_seconds = _finite(search_seconds, "search_seconds")
    if output_fps <= 0 or search_seconds < 0:
        raise StrictCandidateError("output_fps must be positive and search_seconds non-negative")
    source = Path(str(candidate.get("source_path") or "")).expanduser().resolve()
    source_in = _finite(candidate.get("source_in"), "candidate.source_in")
    source_out = _finite(candidate.get("source_out"), "candidate.source_out")
    if source_in < 0 or source_out <= source_in:
        raise StrictCandidateError("candidate source range is invalid")
    try:
        stat = source.stat()
        media = _inspect_cached(str(source), str(ffprobe), stat.st_size, stat.st_mtime_ns)
    except (OSError, DonorError) as exc:
        raise StrictCandidateError(str(exc)) from exc
    source_fps = float(media.fps)
    if source_fps + 1e-6 < output_fps:
        raise StrictCandidateError(
            f"source fps {source_fps:.6f} is below output fps {output_fps:.6f}"
        )
    window_start = max(0.0, source_in - search_seconds)
    window_end = min(float(media.duration), source_out + search_seconds)
    if window_end - window_start > DEFAULT_MAX_WINDOW_SECONDS + 1e-9:
        raise StrictCandidateError("candidate search window exceeds the strict duration bound")

    clip_id = _portable_identifier(
        candidate.get("clip_id"), f"clip-{canonical_hash([source_in, source_out])[:16]}"
    )
    source_asset_id = _portable_identifier(
        candidate.get("source_asset_id") or candidate.get("source_id"),
        f"source-{media.sha256[:16]}",
    )
    if detector_policy is not None and not isinstance(detector_policy, Mapping):
        raise StrictCandidateError("detector_policy must be a mapping")
    try:
        if donor_overlay_profile is not None:
            profile = validate_donor_overlay_profile(donor_overlay_profile)
        elif donor_overlay_contexts is not None:
            profile = build_separated_context_overlay_profile(
                donor_overlay_contexts,
                source_asset_id=source_asset_id,
                source_sha256=media.sha256,
                detector_policy=detector_policy,
            )
        else:
            profile = build_donor_overlay_profile(
                source_asset_id,
                media.sha256,
                [],
                detector_policy=detector_policy,
            )
    except (BroadLazyError, OverlayDetectorError) as exc:
        raise StrictCandidateError(str(exc)) from exc
    if profile["source_sha256"] != media.sha256:
        raise StrictCandidateError("donor overlay profile SHA-256 does not match the source")
    if profile["source_asset_id"] != source_asset_id:
        raise StrictCandidateError("donor overlay profile asset id does not match the candidate")
    try:
        effective_detector_policy = resolve_strict_detector_policy(
            profile, detector_policy
        )
    except BroadLazyError as exc:
        raise StrictCandidateError(str(exc)) from exc
    model_hash = str(model_contract_hash or canonical_hash({"strict_analyzer": STRICT_ANALYZER_VERSION}))
    try:
        window_key = strict_window_cache_key(
            donor_sha256=media.sha256,
            clip_id=clip_id,
            source_in=source_in,
            source_out=source_out,
            window_start=window_start,
            window_end=window_end,
            model_contract_hash=model_hash,
            detector_policy=effective_detector_policy,
            overlay_profile_hash=profile["content_hash"],
        )
    except BroadLazyError as exc:
        raise StrictCandidateError(str(exc)) from exc
    result_cache_key = "strict-result-v2-" + canonical_hash(
        {
            "strict_window_cache_key": window_key,
            "render_frames": render_frames,
            "output_fps": round(output_fps, 12),
            "strict_analyzer_version": STRICT_ANALYZER_VERSION,
            "max_decode_bytes": int(max_decode_bytes),
            "max_decode_frames": int(max_decode_frames),
        }
    )
    cache_path = _cache_path(cache_directory, result_cache_key) if cache_directory else None
    if cache_path is not None:
        cached = _load_cached_result(cache_path, result_cache_key, source)
        if cached is not None:
            return cached

    try:
        probe = probe_exact_frame_window(
            source,
            start=window_start,
            end=window_end,
            ffprobe=ffprobe,
            max_frames=max_decode_frames,
        )
        decoded = decode_exact_analysis_window(
            source,
            probe,
            ffmpeg=ffmpeg,
            max_decode_bytes=max_decode_bytes,
        )
        frames = decoded["frames"]
        exact = decoded["exact_pts"]
        time_base, time_base_doc = _time_base_document(exact["time_base"])
        frame_pts = [int(value) for value in exact["frame_pts"]]
        end_pts = int(exact["end_pts"])
        metrics = compute_transition_metrics(frames)
        transition_zones = detect_transition_zones(
            metrics["frame_luma"],
            frame_mae=metrics["frame_mae"],
            histogram_distance=metrics["histogram_distance"],
        )
        overlay = analyze_candidate_overlay_with_profile(
            frames,
            profile,
            candidate_id=clip_id,
            fps=source_fps,
        )
        if overlay["status"] != "resolved" or overlay["action"] == "reject":
            reasons = overlay.get("candidate_profile", {}).get("rejection_reasons") or ()
            raise StrictCandidateError(
                "candidate overlay is unresolved: " + ", ".join(str(item) for item in reasons)
            )
        frame_contract = build_frame_clean_spans(
            frame_pts,
            time_base_doc,
            transition_zones=transition_zones,
            contamination_zones=(),
            end_pts=end_pts,
            detector_metadata={
                "strict_analyzer_version": STRICT_ANALYZER_VERSION,
                "overlay_profile_hash": profile["content_hash"],
            },
        )
        selection = _select_exact_span(
            frame_contract["clean_spans"],
            frame_pts,
            end_pts=end_pts,
            time_base=time_base,
            render_frames=render_frames,
            output_fps=output_fps,
            preferred_time=source_in,
        )
        span = selection["span"]
        upgraded = dict(candidate)
        upgraded.update(
            {
                "clip_id": clip_id,
                "source_asset_id": source_asset_id,
                "source_path": str(source),
                "source_in": selection["source_in"],
                "source_out": selection["source_out"],
                "detected_source_out": selection["source_out"],
                "fps": source_fps,
                "time_base": time_base_doc,
                "source_frame_start": selection["source_frame_start"],
                "source_frame_end": selection["source_frame_end"],
                "clean_frame_start": int(span["start_frame"]),
                "clean_frame_end": int(span["end_frame"]),
                "clean_source_in": float(span["start_time"]),
                "clean_source_out": float(span["end_time"]),
                "clean_spans": [dict(row) for row in frame_contract["clean_spans"]],
                "transition_zones": [dict(row) for row in frame_contract["transition_zones"]],
                "frame_contract": {
                    **frame_contract,
                    "exact_pts_contract": dict(exact),
                    "analysis_window": {
                        "requested_start": window_start,
                        "requested_end": window_end,
                        "start": float(Fraction(frame_pts[0]) * time_base),
                        "end": float(Fraction(end_pts) * time_base),
                        "frame_count": len(frame_pts),
                    },
                },
                "overlay_analysis": overlay,
                "render_frames": render_frames,
                "fill_strategy": "none",
                "retime_factor": 1.0,
                "selection_contract": "frame_exact_v2",
                "strict_analysis": {
                    "version": STRICT_ANALYZER_VERSION,
                    "search_seconds": search_seconds,
                    "preferred_source_in": source_in,
                    "source_frames_used": selection["source_frames_used"],
                    "strict_window_cache_key": window_key,
                    "result_cache_key": result_cache_key,
                    "cache_hit": False,
                    "pts_contract_hash": exact["content_hash"],
                    "decode_dimensions": dict(decoded["dimensions"]),
                    "decoded_bytes": int(decoded["decoded_bytes"]),
                    "max_decode_bytes": int(max_decode_bytes),
                    "max_decode_frames": int(max_decode_frames),
                    "fps_inference_used": False,
                },
            }
        )
        for key in (
            "safe_source_in",
            "safe_source_out",
            "safe_end_reason",
            "held_tail_seconds",
            "shot_end",
            "edge_cleanup_margins",
            "overlay_blocked",
            "overlay_blocked_reason",
            "manual_crop",
            "tpad",
            "hold_seconds",
        ):
            upgraded.pop(key, None)
    except (BroadLazyError, DonorError, OverlayDetectorError, StrictCandidateError) as exc:
        if cache_path is not None:
            reason = str(exc).replace(str(source), "<donor>")
            _write_cache(cache_path, result_cache_key, status="rejected", payload={"reason": reason})
        if isinstance(exc, StrictCandidateError):
            raise
        raise StrictCandidateError(str(exc)) from exc

    if cache_path is not None:
        _write_cache(cache_path, result_cache_key, status="success", payload=upgraded)
    return upgraded


__all__ = [
    "DEFAULT_MAX_DECODE_BYTES",
    "DEFAULT_MAX_DECODE_FRAMES",
    "MAX_STRICT_VARIANTS",
    "STRICT_ANALYZER_VERSION",
    "STRICT_REMATERIALIZATION_CONTRACT",
    "STRICT_VARIANT_CONTRACT",
    "StrictCandidateError",
    "analyze_strict_candidate",
    "decode_analysis_window",
    "decode_exact_analysis_window",
    "expand_strict_candidate_variants",
    "probe_exact_frame_window",
    "rematerialize_strict_candidate_variants",
]
