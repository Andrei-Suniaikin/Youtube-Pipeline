"""Deterministic canary QA; semantic quality remains a human/VLM judgement."""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path

import numpy as np

from .common import probe_media
from .retrieval import ranges_overlap


def _tail_transition_verdict(
    *,
    mae: float,
    hsv_distance: float,
    local_median_mae: float,
    tail_frames: int,
) -> tuple[bool, str | None, float]:
    """Classify a visual transition close to a planned clip boundary."""

    ratio = float(mae) / max(float(local_median_mae), 1e-6)
    if float(mae) >= 15.0 and float(hsv_distance) >= 0.15:
        return True, "hard_cut", ratio
    if int(tail_frames) <= 2 and float(mae) >= 8.0 and ratio >= 4.0:
        return True, "subtle_end_transition", ratio
    return False, None, ratio


def analyze_tail_cuts(
    path: str | Path,
    *,
    window_seconds: float = 1.0,
    baseline_seconds: float = 2.0,
    resize: tuple[int, int] = (240, 135),
) -> dict:
    """Detect accidental next-shot fragments in the final second of a clip.

    The primary detector combines a grayscale frame delta with an HSV
    histogram distance.  A stricter ratio gate catches one/two-frame dissolve
    starts without flagging ordinary camera or animal motion.
    """

    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - dependency preflight.
        raise RuntimeError("opencv-python is required for tail-cut QA") from exc
    source = Path(path).expanduser().resolve()
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise ValueError(f"cannot open clip for tail QA: {source}")
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        total_frames = int(round(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0))
        if not math.isfinite(fps) or fps <= 0 or total_frames < 2:
            raise ValueError(f"invalid video timing for tail QA: {source}")
        baseline_frames = max(2, int(math.ceil(float(baseline_seconds) * fps)))
        start_frame = max(0, total_frames - baseline_frames - 1)
        capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        frames: list[tuple[int, np.ndarray, np.ndarray]] = []
        frame_index = start_frame
        while frame_index < total_frames:
            ok, frame = capture.read()
            if not ok:
                break
            small = cv2.resize(frame, resize, interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            histogram = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
            cv2.normalize(histogram, histogram, norm_type=cv2.NORM_L2)
            frames.append((frame_index, gray, histogram))
            frame_index += 1
    finally:
        capture.release()

    if len(frames) < 2:
        raise ValueError(f"tail QA could not decode enough frames: {source}")
    transitions = []
    for previous, current in zip(frames, frames[1:]):
        incoming_index = current[0]
        mae = float(np.mean(np.abs(current[1] - previous[1])))
        hsv_distance = float(
            cv2.compareHist(previous[2], current[2], cv2.HISTCMP_BHATTACHARYYA)
        )
        transitions.append(
            {
                "incoming_frame": incoming_index,
                "tail_frames": total_frames - incoming_index,
                "mae": mae,
                "hsv_distance": hsv_distance,
            }
        )

    max_tail_frames = int(math.ceil(float(window_seconds) * fps))
    flags = []
    for candidate_index, candidate in enumerate(transitions):
        tail_frames = int(candidate["tail_frames"])
        if not 1 <= tail_frames < max_tail_frames:
            continue
        baseline = [
            float(row["mae"])
            for index, row in enumerate(transitions)
            if abs(index - candidate_index) > 2
        ]
        local_median = float(np.median(baseline)) if baseline else 0.0
        accepted, kind, ratio = _tail_transition_verdict(
            mae=float(candidate["mae"]),
            hsv_distance=float(candidate["hsv_distance"]),
            local_median_mae=local_median,
            tail_frames=tail_frames,
        )
        if accepted:
            flags.append(
                {
                    **candidate,
                    "kind": kind,
                    "mae_ratio": ratio,
                    "local_median_mae": local_median,
                    "tail_seconds": tail_frames / fps,
                    "transition_seconds": int(candidate["incoming_frame"]) / fps,
                }
            )
    return {
        "path": str(source),
        "fps": fps,
        "frame_count": total_frames,
        "window_seconds": float(window_seconds),
        "passed": not flags,
        "flags": flags,
    }


def validate_timeline(timeline: dict, narration_duration: float, fps: float = 30.0) -> dict:
    slots = list(timeline.get("slots") or [])
    errors: list[str] = []
    warnings: list[str] = []
    frame = 1.0 / fps
    previous_end = 0.0
    selected: list[dict] = []
    for index, slot in enumerate(slots):
        start = float(slot.get("start") or 0.0)
        end = float(slot.get("end") or 0.0)
        duration = end - start
        if abs(start - previous_end) > frame + 1e-6:
            errors.append(f"slot {index} gap/overlap: {start - previous_end:+.3f}s")
        if duration < 4.0 - frame or duration > 8.0 + frame:
            errors.append(f"slot {index} duration {duration:.3f}s outside 4–8s")
        clip = slot.get("selected_clip")
        if not isinstance(clip, dict):
            errors.append(f"slot {index} has no selected clip")
        else:
            if float(clip.get("source_out") or 0.0) <= float(clip.get("source_in") or 0.0):
                errors.append(f"slot {index} invalid source range")
            if any(ranges_overlap(clip, old) for old in selected):
                errors.append(f"slot {index} reuses an overlapping source range")
            selected.append(clip)
        if slot.get("status") not in {"resolved", "manual"}:
            warnings.append(f"slot {index} status={slot.get('status')}")
        previous_end = end
    if not slots:
        errors.append("timeline has no slots")
    elif abs(previous_end - float(narration_duration)) > frame + 1e-6:
        errors.append(
            f"timeline ends at {previous_end:.3f}s, narration at {narration_duration:.3f}s"
        )
    return {
        "timeline_pass": not errors,
        "slot_count": len(slots),
        "errors": errors,
        "warnings": warnings,
    }


def probe_final(path: str | Path, expected_duration: float, fps: float = 30.0) -> dict:
    payload = probe_media(path)
    streams = payload.get("streams") or []
    types = [row.get("codec_type") for row in streams]
    duration = float((payload.get("format") or {}).get("duration") or 0.0)
    errors = []
    if types.count("video") != 1:
        errors.append(f"expected one video stream, got {types.count('video')}")
    if types.count("audio") != 1:
        errors.append(f"expected one final master audio stream, got {types.count('audio')}")
    if abs(duration - expected_duration) > max(1.0 / fps, 0.08):
        errors.append(f"final duration drift {duration - expected_duration:+.3f}s")
    return {"media_pass": not errors, "duration": duration, "stream_types": types,
            "errors": errors, "probe": payload}


def analyze_loudness(path: str | Path) -> dict:
    process = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-vn", "-af", "loudnorm=I=-16:TP=-1:LRA=11:print_format=json",
         "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    match = re.search(r"\{\s*\"input_i\".*?\}", process.stderr or "", re.S)
    if not match:
        return {"pass": False, "error": "loudnorm measurement missing"}
    row = json.loads(match.group(0))
    try:
        integrated = float(row["input_i"])
        peak = float(row["input_tp"])
    except (KeyError, TypeError, ValueError):
        return {"pass": False, "error": "invalid loudnorm measurement", "raw": row}
    return {
        "pass": -17.5 <= integrated <= -14.5 and peak <= -0.5,
        "integrated_lufs": integrated,
        "true_peak_dbtp": peak,
        "raw": row,
    }
