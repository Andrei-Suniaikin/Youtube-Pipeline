"""Single-pass, hash-bound visual QA for production montage masters."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from .common import interval_frame_span, sha256_file


class VisualQAError(RuntimeError):
    pass


@dataclass(frozen=True)
class VisualQAConfig:
    sample_fractions: tuple[float, ...] = (0.10, 0.30, 0.50, 0.70, 0.90)
    proof_width: int = 240
    proof_height: int = 135
    hard_cut_mae: float = 15.0
    hard_cut_hsv: float = 0.15
    subtle_mae: float = 8.0
    subtle_ratio: float = 4.0


SemanticJudge = Callable[[Mapping[str, Any], Path], Mapping[str, Any]]
TransitionJudge = Callable[[Mapping[str, Any], Path], Mapping[str, Any]]
OverlayAnalyzer = Callable[[Mapping[str, Any], list[np.ndarray]], Mapping[str, Any]]


def _supplied_frame(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise VisualQAError(f"{name} must be an integer")
    try:
        result = int(value)
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise VisualQAError(f"{name} must be an integer") from exc
    if not math.isfinite(numeric) or numeric != result:
        raise VisualQAError(f"{name} must be an integer")
    return result


def _sheet(cv2, frames: list[np.ndarray], width: int, height: int) -> np.ndarray:
    cells = [cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA) for frame in frames]
    return np.hstack(cells)


def _frame_metrics(cv2, previous: np.ndarray, current: np.ndarray) -> tuple[float, float]:
    prev = cv2.resize(previous, (240, 135), interpolation=cv2.INTER_AREA)
    curr = cv2.resize(current, (240, 135), interpolation=cv2.INTER_AREA)
    gray_prev = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gray_curr = cv2.cvtColor(curr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    mae = float(np.mean(np.abs(gray_curr - gray_prev)))
    hsv_prev = cv2.cvtColor(prev, cv2.COLOR_BGR2HSV)
    hsv_curr = cv2.cvtColor(curr, cv2.COLOR_BGR2HSV)
    hist_prev = cv2.calcHist([hsv_prev], [0, 1], None, [24, 16], [0, 180, 0, 256])
    hist_curr = cv2.calcHist([hsv_curr], [0, 1], None, [24, 16], [0, 180, 0, 256])
    cv2.normalize(hist_prev, hist_prev, norm_type=cv2.NORM_L2)
    cv2.normalize(hist_curr, hist_curr, norm_type=cv2.NORM_L2)
    hsv_distance = float(cv2.compareHist(hist_prev, hist_curr, cv2.HISTCMP_BHATTACHARYYA))
    return mae, hsv_distance


def run_visual_qa(
    *,
    master_path: str | os.PathLike[str],
    timeline_path: str | os.PathLike[str],
    timeline: Mapping[str, Any],
    proof_root: str | os.PathLike[str],
    semantic_judge: SemanticJudge,
    transition_judge: TransitionJudge,
    overlay_analyzer: OverlayAnalyzer,
    config: VisualQAConfig = VisualQAConfig(),
) -> dict[str, Any]:
    """Decode the master once and validate every slot plus all internal transitions."""

    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - dependency preflight
        raise VisualQAError("opencv-python is required for visual QA") from exc
    master = Path(master_path).expanduser().resolve()
    timeline_source = Path(timeline_path).expanduser().resolve()
    proofs = Path(proof_root).expanduser().resolve()
    proofs.mkdir(parents=True, exist_ok=True)
    slots = list(timeline.get("slots") or [])
    if not slots:
        raise VisualQAError("timeline has no slots")
    fps = float(timeline.get("fps") or 0.0)
    if fps <= 0:
        raise VisualQAError("timeline fps must be positive")
    slot_frames: list[int] = []
    for row in slots:
        if not isinstance(row, Mapping):
            raise VisualQAError("every timeline slot must be a mapping")
        slot_id = str(row.get("slot_id") or "?")
        try:
            start_frame, end_frame, render_frames = interval_frame_span(
                row["start"], row["end"], fps
            )
        except (KeyError, ValueError) as exc:
            raise VisualQAError(f"slot {slot_id} has invalid frame boundaries") from exc
        expected_contract = {
            "render_start_frame": start_frame,
            "render_end_frame": end_frame,
            "render_frames": render_frames,
        }
        for field, expected in expected_contract.items():
            if row.get(field) is None:
                continue
            supplied = _supplied_frame(row[field], f"slot.{field}")
            if supplied != expected:
                raise VisualQAError(
                    f"slot {slot_id} {field} expected={expected} got={supplied}"
                )
        slot_frames.append(render_frames)
    expected_frames = sum(slot_frames)
    if timeline.get("narration_duration") is None:
        raise VisualQAError("timeline.narration_duration is required")
    try:
        narration_duration = float(timeline["narration_duration"])
    except (TypeError, ValueError) as exc:
        raise VisualQAError("timeline.narration_duration must be positive") from exc
    if not math.isfinite(narration_duration) or narration_duration <= 0:
        raise VisualQAError("timeline.narration_duration must be positive")
    narration_frames = int(round(narration_duration * fps))
    if expected_frames != narration_frames:
        raise VisualQAError(
            "timeline frame total disagrees with narration: "
            f"slots={expected_frames} narration={narration_frames}"
        )
    capture = cv2.VideoCapture(str(master))
    if not capture.isOpened():
        raise VisualQAError(f"cannot open master: {master}")

    slot_payloads: list[dict[str, Any]] = []
    current_slot = 0
    local_index = 0
    decoded = 0
    previous: np.ndarray | None = None
    frame_store: list[np.ndarray] = []
    transitions: list[dict[str, Any]] = []
    sample_targets = {
        max(0, min(slot_frames[0] - 1, int(round((slot_frames[0] - 1) * fraction))))
        for fraction in config.sample_fractions
    }
    samples: list[np.ndarray] = []

    def finish_slot() -> None:
        nonlocal samples, transitions, frame_store
        slot = slots[len(slot_payloads)]
        count = slot_frames[len(slot_payloads)]
        if not samples:
            raise VisualQAError(f"slot {slot.get('slot_id')} has no decoded samples")
        while len(samples) < len(config.sample_fractions):
            samples.append(samples[-1])
        sheet = _sheet(cv2, samples[:len(config.sample_fractions)],
                       config.proof_width, config.proof_height)
        filename = f"{slot.get('slot_id')}_samples.jpg"
        sheet_path = proofs / filename
        if not cv2.imwrite(str(sheet_path), sheet):
            raise VisualQAError(f"cannot write proof {filename}")
        semantic = dict(semantic_judge(slot, sheet_path))
        overlay = dict(overlay_analyzer(slot, frame_store))
        maes = np.asarray([row["mae"] for row in transitions], dtype=float)
        baseline = float(np.median(maes)) if maes.size else 0.0
        cut_candidates = []
        for row in transitions:
            ratio = float(row["mae"]) / max(baseline, 1e-6)
            hard = row["mae"] >= config.hard_cut_mae and row["hsv_distance"] >= config.hard_cut_hsv
            subtle = row["mae"] >= config.subtle_mae and ratio >= config.subtle_ratio
            if hard or subtle:
                cut_candidates.append({**row, "mae_ratio": ratio})
        confirmed = []
        if cut_candidates:
            transition_frames: list[np.ndarray] = []
            for candidate in cut_candidates:
                center = int(candidate["incoming_local_frame"])
                transition_frames.extend(frame_store[max(0, center - 2):min(count, center + 3)])
            edge_sheet = _sheet(
                cv2,
                transition_frames[: max(1, min(len(transition_frames), 10))],
                config.proof_width,
                config.proof_height,
            )
            edge_name = f"{slot.get('slot_id')}_transitions.jpg"
            edge_path = proofs / edge_name
            cv2.imwrite(str(edge_path), edge_sheet)
            for candidate in cut_candidates:
                decision = dict(transition_judge({**slot, "candidate": candidate}, edge_path))
                if decision.get("verdict") != "continuous_motion":
                    confirmed.append({**candidate, "judge": decision})
        slot_payloads.append(
            {
                "slot_id": str(slot.get("slot_id")),
                "proof": filename,
                "semantic": semantic,
                "overlay": overlay,
                "unplanned_transitions": confirmed,
                "decoded_frames": count,
            }
        )
        samples = []
        transitions = []
        frame_store = []

    extra = False
    try:
        while current_slot < len(slots):
            ok, frame = capture.read()
            if not ok:
                break
            decoded += 1
            small_frame = cv2.resize(frame, (480, 270), interpolation=cv2.INTER_AREA)
            frame_store.append(small_frame)
            if local_index in sample_targets:
                samples.append(small_frame)
            if previous is not None and local_index > 0:
                mae, hsv_distance = _frame_metrics(cv2, previous, frame)
                transitions.append(
                    {
                        "incoming_local_frame": local_index,
                        "incoming_master_frame": decoded - 1,
                        "mae": mae,
                        "hsv_distance": hsv_distance,
                    }
                )
            previous = frame.copy()
            local_index += 1
            if local_index == slot_frames[current_slot]:
                finish_slot()
                current_slot += 1
                local_index = 0
                previous = None
                if current_slot < len(slots):
                    count = slot_frames[current_slot]
                    sample_targets = {
                        max(0, min(count - 1, int(round((count - 1) * fraction))))
                        for fraction in config.sample_fractions
                    }
        # Detect extra frames without a second full decode.
        extra, _ = capture.read()
    finally:
        capture.release()
    errors = []
    if decoded != expected_frames or extra:
        errors.append(f"frame_count expected={expected_frames} decoded={decoded} extra={bool(extra)}")
    if len(slot_payloads) != len(slots):
        errors.append(f"slot_decode expected={len(slots)} got={len(slot_payloads)}")
    for row in slot_payloads:
        semantic = row["semantic"]
        if semantic.get("verdict") not in {"exact", "relevant"}:
            errors.append(f"semantic {row['slot_id']}={semantic.get('verdict')}")
        if not bool(row["overlay"].get("passed")):
            errors.append(f"overlay residual in {row['slot_id']}")
        if row["unplanned_transitions"]:
            errors.append(f"unplanned transition in {row['slot_id']}")
    return {
        "schema_version": 2,
        "passed": not errors,
        "errors": errors,
        "input_hashes": {
            "master": sha256_file(master),
            "timeline": sha256_file(timeline_source),
        },
        "expected_frames": expected_frames,
        "decoded_frames": decoded,
        "slot_count": len(slot_payloads),
        "proof_root": ".",
        "slots": slot_payloads,
    }


__all__ = ["VisualQAConfig", "VisualQAError", "run_visual_qa"]
